"""Pure pandas / numpy. No Streamlit, no network, so it is easy to test."""
from __future__ import annotations

from itertools import combinations

import numpy as np
import pandas as pd
import streamlit as st

from config import BOARD_ROWS, ET, IN_PERCENT


# ================================================================ price board
def _pct_or_bp(name: str, last: float, ref: float) -> float:
    """Yields and the VIX are already in percent, so quote them in basis points
    of change rather than a percent change of a percent."""
    if not np.isfinite(ref) or ref == 0:
        return np.nan
    return (last - ref) * 100 if name in IN_PERCENT else (last / ref - 1) * 100


def build_board(daily: pd.DataFrame, intra: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for name, group in BOARD_ROWS.items():
        if name not in daily or daily[name].dropna().shape[0] < 260:
            continue
        s = daily[name].dropna()
        live = intra[name].dropna() if name in intra else pd.Series(dtype=float)
        last = float(live.iloc[-1]) if len(live) else float(s.iloc[-1])
        updated = live.index[-1].strftime("%d %b %H:%M") if len(live) else "daily close"
        # Yahoo's daily series may already include today's in-progress bar, which
        # would make the 1-day change zero. Drop it when the live tick is same-day.
        same_day = bool(len(live)) and live.index[-1].tz_convert(ET).date() <= s.index[-1].date()
        base = s.iloc[:-1] if same_day else s
        if len(base) < 260:
            continue
        rets = base.pct_change().dropna().iloc[-252:]
        r3m = pd.concat([base.iloc[-62:], pd.Series([last])])
        yr = base.iloc[-252:]
        ytd_base = base[base.index.year == base.index[-1].year]
        rows.append({
            "Group": group,
            "Instrument": name,
            "Last": last,
            "Updated (SGT)": updated,
            "1D": _pct_or_bp(name, last, base.iloc[-1]),
            "1W": _pct_or_bp(name, last, base.iloc[-5]),
            "1M": _pct_or_bp(name, last, base.iloc[-21]),
            "3M": _pct_or_bp(name, last, base.iloc[-63]),
            "YTD": _pct_or_bp(name, last, ytd_base.iloc[0]) if len(ytd_base) else np.nan,
            "1D move (z)": ((last / base.iloc[-1] - 1) - rets.mean()) / rets.std(),
            "3M range %ile": (last - r3m.min()) / (r3m.max() - r3m.min()) * 100,
            "52w range %ile": (last - yr.min()) / (yr.max() - yr.min()) * 100,
            "20D vol %": rets.iloc[-20:].std() * np.sqrt(252) * 100,
            "1Y vol %": rets.std() * np.sqrt(252) * 100,
            "Last 3M": r3m.round(6).tolist(),
        })
    return pd.DataFrame(rows)


# ========================================================= mean reversion math
def adf_tstat(x: np.ndarray) -> float:
    """Dickey-Fuller t-stat (constant, no lags). More negative = stronger evidence
    of mean reversion."""
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    if len(x) < 30:
        return np.nan
    y, lag = np.diff(x), x[:-1]
    X = np.column_stack([np.ones_like(lag), lag])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    sigma2 = resid @ resid / (len(y) - 2)
    se = np.sqrt(sigma2 * np.linalg.inv(X.T @ X)[1, 1])
    return coef[1] / se


def half_life(x: np.ndarray) -> float:
    """Days for a deviation to halve, from an AR(1) fit. NaN if not mean reverting."""
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    if len(x) < 30:
        return np.nan
    beta = np.polyfit(x[:-1], np.diff(x), 1)[0]
    return -np.log(2) / beta if beta < 0 else np.nan


def spearman(a: pd.Series, b: pd.Series) -> float:
    """Rank correlation without scipy: Pearson correlation of the ranks."""
    return a.rank().corr(b.rank())


def pair_series(prices: pd.DataFrame, a: str, b: str, how: str) -> pd.Series:
    df = prices[[a, b]].dropna()
    return np.log(df[a] / df[b]) if how == "ratio" else df[a] - df[b]


@st.cache_data(ttl=30 * 60, show_spinner="Scanning every pair")
def scan_pairs(prices: pd.DataFrame, z_window: int) -> pd.DataFrame:
    """Rank every pair by how stretched its log price ratio is right now."""
    rets = pd.concat({c: np.log(prices[c].dropna()).diff() for c in prices}, axis=1, sort=True)
    rows = []
    for a, b in combinations(prices.columns, 2):
        lr = pair_series(prices, a, b, "ratio")
        if len(lr) < max(z_window * 2, 120):
            continue
        mean, sd = lr.rolling(z_window).mean(), lr.rolling(z_window).std()
        sd_now = sd.iloc[-1]
        if not np.isfinite(sd_now) or sd_now == 0:
            continue
        z = (lr.iloc[-1] - mean.iloc[-1]) / sd_now
        rr = rets[[a, b]].dropna().iloc[-252:]
        r = rr[a].corr(rr[b])
        rows.append({
            "A": a, "B": b, "Z-score": z,
            "Signal": "A rich vs B" if z >= 2 else ("A cheap vs B" if z <= -2 else ""),
            "ADF t": adf_tstat(lr.values), "Half-life (days)": half_life(lr.values),
            "r (1Y returns)": r, "R²": r * r, "Spearman ρ": spearman(rr[a], rr[b]),
            "Days of data": len(lr),
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.reindex(out["Z-score"].abs().sort_values(ascending=False).index).reset_index(drop=True)


# ================================================================ macro board
def frequency(s: pd.Series) -> str:
    """'D', 'W', 'M' or 'Q', inferred from the median gap between observations."""
    if len(s.index) < 3:
        return "M"
    gap = pd.Series(s.index).diff().dt.days.median()
    if not gap or np.isnan(gap):
        return "M"
    if gap <= 4:
        return "D"
    if gap <= 10:
        return "W"
    return "Q" if gap > 45 else "M"


def transform(series: pd.Series, how: str, scale: float = 1.0):
    """Return (latest, prior, as-of date, transformed history, frequency).

    `how` is one of:
      yoy     : percent change against 12 months / 4 quarters ago
      mom_chg : change against the previous observation, in the series' units
      level   : the level itself
    `scale` rescales a level into readable units (e.g. FRED's $m into $bn); it is
    ignored for yoy, where a constant factor cancels out.
    """
    s = series.dropna()
    freq = frequency(s)
    if s.empty:
        return np.nan, np.nan, pd.NaT, s, freq
    if how == "yoy":
        t = (s / s.shift(4 if freq == "Q" else 12) - 1) * 100
    elif how == "mom_chg":
        t = s.diff() * scale
    else:
        t = s * scale
    t = t.dropna()
    if t.empty:
        return np.nan, np.nan, pd.NaT, t, freq
    prior = float(t.iloc[-2]) if len(t) > 1 else np.nan
    return float(t.iloc[-1]), prior, t.index[-1], t, freq


def as_of_label(ts: pd.Timestamp, freq: str) -> str:
    """Date the reading refers to, written the way the release is quoted."""
    if pd.isna(ts):
        return "unavailable"
    if freq == "Q":
        return f"Q{ts.quarter} {ts.year}"
    if freq in ("D", "W"):
        return f"{ts:%d %b %Y}"
    return f"{ts:%b %Y}"
