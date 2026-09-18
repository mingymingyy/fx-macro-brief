"""
FX & Macro Daily Brief
======================
Streamlit dashboard for G10 FX, commodities and rates, in Singapore time.

Tabs
  Markets board : G10 FX, selected Asia FX, metals and energy, auto-refreshing prices
  Mean reversion: scan every pair for stretched ratios, then inspect one pair in detail
  Rates         : UST 2Y/10Y, JGB 10Y, 2s10s, UST-JGB spread
  Calendar      : this week's economic calendar
  News          : themed headlines plus central bank releases

Run locally
  python -m pip install streamlit pandas numpy requests yfinance plotly
  python -m streamlit run app.py

Data: Yahoo Finance via yfinance (unofficial), FRED, Japan Ministry of Finance,
Forex Factory weekly JSON (unofficial), Google News RSS, central bank RSS feeds,
plus any TradingView CSV exports you upload. For education and research only.
"""
import io
from email.utils import parsedate_to_datetime
from itertools import combinations
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
import yfinance as yf

# =====================================================================================
# CONFIG: edit these to change what the dashboard shows
# =====================================================================================
BOARD_TITLE = "Markets board"
SGT = "Asia/Singapore"
HEADERS = {"User-Agent": "Mozilla/5.0 (fx-brief personal dashboard)"}

# label: (Yahoo ticker, group)
INSTRUMENTS = {
    "EUR/USD": ("EURUSD=X", "G10 FX"), "GBP/USD": ("GBPUSD=X", "G10 FX"), "USD/JPY": ("JPY=X", "G10 FX"),
    "AUD/USD": ("AUDUSD=X", "G10 FX"), "NZD/USD": ("NZDUSD=X", "G10 FX"), "USD/CAD": ("CAD=X", "G10 FX"),
    "USD/CHF": ("CHF=X", "G10 FX"), "USD/NOK": ("NOK=X", "G10 FX"), "USD/SEK": ("SEK=X", "G10 FX"),
    "EUR/GBP": ("EURGBP=X", "G10 FX"), "EUR/JPY": ("EURJPY=X", "G10 FX"), "DXY": ("DX-Y.NYB", "G10 FX"),
    "USD/SGD": ("SGD=X", "Asia FX"), "USD/CNY": ("CNY=X", "Asia FX"), "USD/KRW": ("KRW=X", "Asia FX"),
    "Gold": ("GC=F", "Commodities"), "Silver": ("SI=F", "Commodities"), "Copper": ("HG=F", "Commodities"),
    "WTI crude": ("CL=F", "Commodities"), "Brent crude": ("BZ=F", "Commodities"),
}
TICKERS = tuple(t for t, _ in INSTRUMENTS.values())
LABEL = {t: name for name, (t, _) in INSTRUMENTS.items()}

PRESETS = {  # quick picks for the pair inspector
    "Gold vs Silver": ("Gold", "Silver"),
    "Gold vs WTI": ("Gold", "WTI crude"),
    "Brent vs WTI": ("Brent crude", "WTI crude"),
    "AUD vs NZD": ("AUD/USD", "NZD/USD"),
    "EUR vs GBP": ("EUR/USD", "GBP/USD"),
    "Copper vs Gold": ("Copper", "Gold"),
}

NEWS_THEMES = {  # label: Google News query (quoted phrases keep results on topic)
    "USD & Fed": '"dollar index" OR "Federal Reserve" OR "Treasury yields"',
    "EUR & ECB": '"EUR/USD" OR "European Central Bank" OR "euro zone" inflation',
    "JPY & BOJ": '"USD/JPY" OR "Bank of Japan" OR "JGB yields"',
    "GBP & BoE": '"GBP/USD" OR "Bank of England" OR "sterling"',
    "Commodity FX": '"Australian dollar" OR "New Zealand dollar" OR "Canadian dollar" OR "Norwegian crown"',
    "Gold & oil": '"gold prices" OR "oil prices" OR OPEC',
    "SGD & Asia": '"Singapore dollar" OR "MAS monetary policy" OR "offshore yuan"',
}
CENTRAL_BANK_FEEDS = {
    "Fed": "https://www.federalreserve.gov/feeds/press_all.xml",
    "ECB": "https://www.ecb.europa.eu/rss/press.html",
    "BOJ": "https://www.boj.or.jp/en/rss/whatsnew.xml",
    "BoE": "https://www.bankofengland.co.uk/rss/news",
    "RBA": "https://www.rba.gov.au/rss/rss-cb-media-releases.xml",
    "BoC": "https://www.bankofcanada.ca/content_type/press-releases/feed/",
}
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"
MOF_HIST = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/historical/jgbcme_all.csv"
MOF_CUR = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/jgbcme.csv"

LOOKBACKS = {"1Y": 252, "2Y": 504, "5Y": 1260, "10Y": 2520, "Max": None}
Z_WINDOWS = {"1 month": 21, "3 months": 63, "6 months": 126, "1 year": 252}
ADF_5PCT = -2.86  # MacKinnon asymptotic 5% critical value, ADF with constant, no trend

st.set_page_config(page_title="FX & Macro Brief", page_icon="💱", layout="wide")


def now_sgt() -> pd.Timestamp:
    return pd.Timestamp.now(tz=SGT)


# =====================================================================================
# DATA LOADERS (each is cached so the app does not re-download on every click)
# =====================================================================================
def _closes(df: pd.DataFrame) -> pd.DataFrame:
    close = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]]
    return close.rename(columns=LABEL).sort_index()


@st.cache_data(ttl=30 * 60, show_spinner="Pulling full daily price history")
def load_daily() -> pd.DataFrame:
    df = _closes(yf.download(list(TICKERS), period="max", interval="1d", progress=False, auto_adjust=False))
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df


@st.cache_data(ttl=60, show_spinner=False)
def load_intraday() -> pd.DataFrame:
    df = _closes(yf.download(list(TICKERS), period="5d", interval="5m", progress=False, auto_adjust=False))
    idx = df.index.tz_localize("UTC") if df.index.tz is None else df.index
    df.index = idx.tz_convert(SGT)
    return df


@st.cache_data(ttl=6 * 3600, show_spinner="Pulling FRED")
def load_fred(series_id: str) -> pd.Series:
    raw = requests.get(FRED.format(series_id), timeout=30).text  # FRED rejects some custom user agents
    if not raw.startswith("observation_date"):
        raise ValueError(f"unexpected FRED response: {raw[:80]!r}")
    df = pd.read_csv(io.StringIO(raw), na_values=".")
    df.columns = ["Date", series_id]
    return df.assign(Date=pd.to_datetime(df["Date"])).set_index("Date")[series_id].dropna()


@st.cache_data(ttl=6 * 3600, show_spinner="Pulling Treasury yields from Yahoo")
def load_yahoo_yields() -> pd.DataFrame:
    """Fallback when FRED is unreachable: 5Y (^FVX), 10Y (^TNX), 30Y (^TYX), in percent."""
    df = yf.download(["^FVX", "^TNX", "^TYX"], period="5y", progress=False, auto_adjust=False)["Close"]
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df.rename(columns={"^FVX": "UST5Y", "^TNX": "UST10Y", "^TYX": "UST30Y"})


def _read_mof(url: str) -> pd.Series:
    raw = requests.get(url, timeout=30, headers=HEADERS).content.decode("shift_jis", errors="ignore")
    df = pd.read_csv(io.StringIO(raw), skiprows=1, na_values="-")
    df["Date"] = pd.to_datetime(df["Date"], format="%Y/%m/%d", errors="coerce")
    return df.dropna(subset=["Date"]).set_index("Date")["10Y"].astype(float)


@st.cache_data(ttl=6 * 3600, show_spinner="Pulling JGB yields")
def load_jgb10() -> pd.Series:
    s = pd.concat([_read_mof(MOF_HIST), _read_mof(MOF_CUR)])
    return s[~s.index.duplicated(keep="last")].sort_index().dropna().rename("JGB10Y")


@st.cache_data(ttl=60 * 60, show_spinner="Pulling calendar")
def load_calendar() -> pd.DataFrame:
    resp = requests.get(CALENDAR_URL, timeout=30, headers=HEADERS)
    resp.raise_for_status()
    df = pd.DataFrame(resp.json())
    df["SGT"] = pd.to_datetime(df["date"], utc=True).dt.tz_convert(SGT)
    return df.drop(columns="date").sort_values("SGT")


def _parse_feed(xml_bytes: bytes, source: str) -> pd.DataFrame:
    """Handles RSS 2.0 and RSS 1.0 (RDF) feeds."""
    rows = []
    for it in ET.fromstring(xml_bytes).iter():
        if not it.tag.endswith("item"):
            continue
        title = (it.findtext("{*}title") or it.findtext("title") or "").strip()
        link = (it.findtext("{*}link") or it.findtext("link") or "").strip()
        raw_date = it.findtext("pubDate") or it.findtext("{*}date")
        ts = pd.NaT
        if raw_date:
            try:
                ts = pd.Timestamp(parsedate_to_datetime(raw_date))
            except (TypeError, ValueError):
                ts = pd.to_datetime(raw_date, utc=True, errors="coerce")
            if pd.notna(ts):
                ts = (ts.tz_localize("UTC") if ts.tzinfo is None else ts).tz_convert(SGT)
        src = it.find("source")
        rows.append({"time": ts, "title": title, "link": link,
                     "source": src.text if src is not None else source})
    return pd.DataFrame(rows)


@st.cache_data(ttl=15 * 60, show_spinner="Pulling news")
def load_news(theme: str, query: str) -> pd.DataFrame:
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}+when:3d&hl=en-SG&gl=SG&ceid=SG:en"
    df = _parse_feed(requests.get(url, timeout=30, headers=HEADERS).content, "Google News")
    return df.assign(theme=theme) if not df.empty else df


@st.cache_data(ttl=30 * 60, show_spinner="Pulling central bank releases")
def load_cb(name: str, url: str) -> pd.DataFrame:
    return _parse_feed(requests.get(url, timeout=30, headers=HEADERS).content, name)


def parse_tradingview_csv(file, name: str) -> pd.Series:
    """TradingView 'Export chart data' CSV: a time column (UNIX seconds or ISO) and a close column."""
    df = pd.read_csv(file)
    cols = {c.lower().strip(): c for c in df.columns}
    if "time" not in cols or "close" not in cols:
        raise ValueError(f"expected 'time' and 'close' columns, found {list(df.columns)}")
    t = df[cols["time"]]
    utc = pd.DatetimeIndex(pd.to_datetime(t, unit="s", utc=True) if pd.api.types.is_numeric_dtype(t)
                           else pd.to_datetime(t, utc=True))
    # Daily bars can be stamped at the session open (e.g. 22:00 UTC the previous day for CME futures).
    # Shifting 12 hours before taking the date lands every session on its trading date.
    dates = (utc + pd.Timedelta(hours=12)).tz_convert(None).normalize()
    s = pd.Series(df[cols["close"]].astype(float).values, index=dates, name=name)
    return s[~s.index.duplicated(keep="last")].sort_index()


def safe(fn, *args, label=""):
    """Run a loader; show the real error instead of crashing the page."""
    try:
        return fn(*args)
    except Exception as e:
        st.warning(f"{label or fn.__name__} unavailable: {type(e).__name__}: {e}")
        return None


# =====================================================================================
# ANALYTICS
# =====================================================================================
def build_board(daily: pd.DataFrame, intra: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for name, (_, group) in INSTRUMENTS.items():
        if name not in daily or daily[name].dropna().shape[0] < 260:
            continue
        s = daily[name].dropna()
        live = intra[name].dropna() if name in intra else pd.Series(dtype=float)
        last = live.iloc[-1] if len(live) else s.iloc[-1]
        updated = live.index[-1].strftime("%d %b %H:%M") if len(live) else "daily close"
        # Yahoo's daily series may already include today's in-progress bar
        same_day = len(live) and live.index[-1].tz_convert("America/New_York").date() <= s.index[-1].date()
        base = s.iloc[:-1] if same_day else s
        rets = base.pct_change().dropna().iloc[-252:]
        r3m = pd.concat([base.iloc[-62:], pd.Series([last])])
        rows.append({
            "Group": group, "Instrument": name, "Last": last, "Updated (SGT)": updated,
            "1D %": (last / base.iloc[-1] - 1) * 100,
            "1W %": (last / base.iloc[-5] - 1) * 100,
            "1M %": (last / base.iloc[-21] - 1) * 100,
            "1D move (z)": ((last / base.iloc[-1] - 1) - rets.mean()) / rets.std(),
            "3M range %ile": (last - r3m.min()) / (r3m.max() - r3m.min()) * 100,
            "20D vol %": rets.iloc[-20:].std() * np.sqrt(252) * 100,
            "Last 3M": r3m.round(6).tolist(),
        })
    return pd.DataFrame(rows)


def adf_tstat(x: np.ndarray) -> float:
    """Dickey-Fuller t-stat (constant, no lags). More negative = stronger evidence of mean reversion."""
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
    """Days for a deviation to halve, from an AR(1) fit. NaN if the series is not mean reverting."""
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
    rets = pd.concat({c: np.log(prices[c].dropna()).diff() for c in prices}, axis=1, sort=True)
    rows = []
    for a, b in combinations(prices.columns, 2):
        lr = pair_series(prices, a, b, "ratio")
        if len(lr) < max(z_window * 2, 120):
            continue
        mean, sd = lr.rolling(z_window).mean(), lr.rolling(z_window).std()
        z = (lr.iloc[-1] - mean.iloc[-1]) / sd.iloc[-1]
        rr = rets[[a, b]].dropna().iloc[-252:]
        r = rr[a].corr(rr[b])
        rows.append({
            "A": a, "B": b, "Z-score": z,
            "Signal": "A rich vs B" if z >= 2 else ("A cheap vs B" if z <= -2 else ""),
            "ADF t": adf_tstat(lr.values), "Half-life (days)": half_life(lr.values),
            "r (1Y returns)": r, "R²": r * r, "Spearman ρ": spearman(rr[a], rr[b]), "Days of data": len(lr),
        })
    out = pd.DataFrame(rows)
    return out.reindex(out["Z-score"].abs().sort_values(ascending=False).index).reset_index(drop=True)


# =====================================================================================
# PAGE
# =====================================================================================
st.title("FX & macro brief")
st.caption(f"{now_sgt():%A %d %B %Y, %H:%M} SGT")

with st.sidebar:
    refresh_label = st.selectbox("Auto-refresh prices", ["Off", "30 seconds", "1 minute", "2 minutes", "5 minutes"], index=2)
    REFRESH = {"Off": None, "30 seconds": 30, "1 minute": 60, "2 minutes": 120, "5 minutes": 300}[refresh_label]
    if st.button("Refresh all data now"):
        st.cache_data.clear()
    st.divider()
    st.subheader("Add TradingView data")
    st.caption("On TradingView, open a daily chart, choose Export chart data, and upload the CSV here. "
               "It becomes available in the Mean reversion tab.")
    uploads = st.file_uploader("TradingView CSV exports", type="csv", accept_multiple_files=True)
    st.divider()
    st.caption("Yahoo quotes can be delayed. Check the Updated column on the board.")

tab_board, tab_mr, tab_rates, tab_cal, tab_news = st.tabs(
    [BOARD_TITLE, "Mean reversion", "Rates", "Calendar", "News"])


def run_tab(render):
    """One tab failing must never stop the tabs after it from loading."""
    try:
        render()
    except Exception as e:
        st.error(f"This tab hit an error: {type(e).__name__}: {e}")


# ------------------------------------------------------------------------ Markets board
@st.fragment(run_every=REFRESH)
def render_board():
    daily = safe(load_daily, label="Daily prices")
    if daily is None:
        return
    intra = safe(load_intraday, label="Intraday prices")
    board = build_board(daily, intra if intra is not None else pd.DataFrame())
    groups = st.pills("Show", ["G10 FX", "Asia FX", "Commodities"], selection_mode="multi",
                      default=["G10 FX", "Asia FX", "Commodities"], key="board_groups")
    view = board[board["Group"].isin(groups or [])]
    st.dataframe(view, hide_index=True, width="stretch", height=740, column_config={
        "Last": st.column_config.NumberColumn(format="%.4f"),
        "1D %": st.column_config.NumberColumn(format="%+.2f"),
        "1W %": st.column_config.NumberColumn(format="%+.2f"),
        "1M %": st.column_config.NumberColumn(format="%+.2f"),
        "1D move (z)": st.column_config.NumberColumn(format="%+.2f",
            help="Today's return divided by the standard deviation of the past year's daily returns"),
        "3M range %ile": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f"),
        "20D vol %": st.column_config.NumberColumn(format="%.1f", help="Annualised realised volatility"),
        "Last 3M": st.column_config.LineChartColumn(width="medium"),
    })
    big = view.loc[view["1D move (z)"].abs() >= 2, "Instrument"].tolist()
    if big:
        st.info(f"Unusual moves today (|z| of at least 2): {', '.join(big)}. Check the print, then find the headline.")
    st.caption(f"Refreshed {now_sgt():%H:%M:%S} SGT. Gold, silver and copper are COMEX front-month futures; "
               "WTI and Brent are front-month futures, which jump when the contract rolls.")


with tab_board:
    run_tab(render_board)


# ------------------------------------------------------------------------ Mean reversion
def set_pair(a: str, b: str):
    st.session_state["pair_a"], st.session_state["pair_b"] = a, b


def on_scan_select():
    rows = st.session_state["scan_table"].selection.rows
    table = st.session_state.get("scan_view")
    if rows and table is not None:
        set_pair(table.iloc[rows[0]]["A"], table.iloc[rows[0]]["B"])


def render_mean_reversion():
    daily = safe(load_daily, label="Daily prices")
    if daily is None:
        return
    prices = daily.copy()
    for f in uploads or []:
        name = "TV: " + f.name.rsplit(".", 1)[0][:30]
        s = safe(parse_tradingview_csv, f, name, label=f"Upload {f.name}")
        if s is not None:
            prices = prices.join(s, how="outer")
    names = list(prices.columns)
    st.session_state.setdefault("pair_a", "Gold")
    st.session_state.setdefault("pair_b", "Silver")

    st.markdown("**How to use this tab:** the scanner ranks every pair by how stretched its price ratio is. "
                "Click a row, or a preset, and the inspector below explains that pair in plain English.")

    c1, c2 = st.columns(2)
    lb_label = c1.segmented_control("History used", list(LOOKBACKS), default="5Y", key="lookback") or "5Y"
    zw_label = c2.segmented_control("Z-score measured against the average of the last", list(Z_WINDOWS),
                                    default="3 months", key="zwin") or "3 months"
    n = LOOKBACKS[lb_label]
    z_window = Z_WINDOWS[zw_label]
    hist = prices if n is None else prices.iloc[-n:]

    # ---- Scanner
    st.subheader("1. Scanner: which pairs look stretched?")
    f1, f2, f3 = st.columns(3)
    min_z = f1.slider("Show |z| of at least", 0.0, 3.0, 1.5, 0.25)
    only_stationary = f2.toggle("Only pairs that pass the mean reversion test (ADF)", value=False)
    max_hl = f3.slider("Half-life no longer than (days)", 5, 250, 120, 5)
    scan = scan_pairs(hist, z_window)
    view = scan[(scan["Z-score"].abs() >= min_z) & (scan["Half-life (days)"] <= max_hl)]
    if only_stationary:
        view = view[view["ADF t"] <= ADF_5PCT]
    view = view.reset_index(drop=True)
    st.session_state["scan_view"] = view
    st.dataframe(view, hide_index=True, width="stretch", height=320, key="scan_table",
                 on_select=on_scan_select, selection_mode="single-row", column_config={
                     "Z-score": st.column_config.NumberColumn(format="%+.2f"),
                     "ADF t": st.column_config.NumberColumn(format="%.2f",
                         help=f"Below {ADF_5PCT} means the log ratio passed a 5% Dickey-Fuller test"),
                     "Half-life (days)": st.column_config.NumberColumn(format="%.0f"),
                     "r (1Y returns)": st.column_config.NumberColumn(format="%+.2f"),
                     "R²": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f"),
                     "Spearman ρ": st.column_config.NumberColumn(format="%+.2f"),
                 })
    st.caption(f"{len(view)} of {len(scan)} pairs shown. Click a column header to sort. "
               "The z-score uses the log of A ÷ B, so a positive z means A has outperformed B recently.")

    # ---- Inspector
    st.subheader("2. Inspector: is this pair a mean reversion candidate?")
    p = st.columns(len(PRESETS))
    for col, (label, (a_, b_)) in zip(p, PRESETS.items()):
        col.button(label, on_click=set_pair, args=(a_, b_), width="stretch")
    s1, s2, s3 = st.columns([2, 2, 2])
    a = s1.selectbox("Instrument A", names, key="pair_a")
    b = s2.selectbox("Instrument B", names, key="pair_b")
    how = s3.radio("Measure", ["Ratio A ÷ B", "Spread A − B"], horizontal=True,
                   help="Use a spread only when both are in the same units, e.g. Brent − WTI in USD/bbl.")
    if a == b:
        st.warning("Pick two different instruments.")
        return

    df = hist[[a, b]].dropna()
    if len(df) < z_window + 30:
        st.warning(f"Only {len(df)} overlapping days of data for {a} and {b}. Choose a longer history.")
        return
    is_ratio = how.startswith("Ratio")
    level = df[a] / df[b] if is_ratio else df[a] - df[b]
    test_series = np.log(level) if is_ratio else level
    mean, sd = level.rolling(z_window).mean(), level.rolling(z_window).std()
    z = (level - mean) / sd
    z_now, adf, hl = z.iloc[-1], adf_tstat(test_series.values), half_life(test_series.values)
    ra, rb = np.log(df[a]).diff(), np.log(df[b]).diff()
    r1y = ra.iloc[-252:].corr(rb.iloc[-252:])
    pct_rank = (level < level.iloc[-1]).mean() * 100
    label = f"{a} ÷ {b}" if is_ratio else f"{a} − {b}"

    # Plain-English verdict
    if z_now >= 2:
        stance = f"**{a} looks expensive versus {b}.** A mean reversion trade would sell {a} and buy {b}."
    elif z_now <= -2:
        stance = f"**{a} looks cheap versus {b}.** A mean reversion trade would buy {a} and sell {b}."
    else:
        stance = f"**No signal.** The {label} is within 2 standard deviations of its {zw_label} average."
    evidence = (f"passes the mean reversion test (ADF t = {adf:.2f}, below {ADF_5PCT})" if adf <= ADF_5PCT
                else f"does **not** pass the mean reversion test (ADF t = {adf:.2f}, needs to be below {ADF_5PCT}), "
                     "so the gap could keep widening")
    hl_text = f"a typical deviation halves in about {hl:.0f} trading days" if np.isfinite(hl) else "no measurable half-life"
    box = st.success if (abs(z_now) >= 2 and adf <= ADF_5PCT) else (st.warning if abs(z_now) >= 2 else st.info)
    box(f"{stance}\n\nOver the last {lb_label}, this series {evidence}; {hl_text}. "
        f"Today's level is higher than {pct_rank:.0f}% of days in that history.")

    m = st.columns(6)
    m[0].metric(label, f"{level.iloc[-1]:,.4f}")
    m[1].metric("Z-score", f"{z_now:+.2f}")
    m[2].metric("ADF t-stat", f"{adf:.2f}")
    m[3].metric("Half-life (days)", f"{hl:.0f}" if np.isfinite(hl) else "n/a")
    m[4].metric("r (1Y returns)", f"{r1y:+.2f}")
    m[5].metric("R²", f"{r1y * r1y:.2f}")

    # Chart 1: both instruments
    rebased = df / df.iloc[0] * 100
    fig1 = go.Figure()
    fig1.add_trace(go.Scatter(x=rebased.index, y=rebased[a], name=a, line=dict(width=1.6)))
    fig1.add_trace(go.Scatter(x=rebased.index, y=rebased[b], name=b, line=dict(width=1.6)))
    fig1.update_layout(title=f"{a} and {b}, both rebased to 100 on {df.index[0]:%d %b %Y}",
                       height=340, hovermode="x unified", legend=dict(orientation="h", y=1.12),
                       margin=dict(t=60, b=10), yaxis_title="Rebased (start = 100)")
    st.plotly_chart(fig1, width="stretch")

    # Chart 2: the ratio with its moving average and bands
    fig2 = go.Figure()
    fig2.add_trace(go.Scatter(x=mean.index, y=mean + 2 * sd, line=dict(width=0), showlegend=False, hoverinfo="skip"))
    fig2.add_trace(go.Scatter(x=mean.index, y=mean - 2 * sd, fill="tonexty", line=dict(width=0),
                              fillcolor="rgba(120,140,170,0.18)", name="Average ± 2σ"))
    fig2.add_trace(go.Scatter(x=mean.index, y=mean, name=f"{zw_label} average", line=dict(dash="dot", width=1.2)))
    fig2.add_trace(go.Scatter(x=level.index, y=level, name=label, line=dict(width=1.8, color="#1F4E79")))
    rich, cheap = level[z >= 2], level[z <= -2]
    fig2.add_trace(go.Scatter(x=rich.index, y=rich, mode="markers", name=f"{a} rich (z ≥ 2)",
                              marker=dict(size=5, color="#C0392B")))
    fig2.add_trace(go.Scatter(x=cheap.index, y=cheap, mode="markers", name=f"{a} cheap (z ≤ −2)",
                              marker=dict(size=5, color="#1E8449")))
    fig2.update_layout(title=f"{label}: outside the shaded band = stretched", height=380, hovermode="x unified",
                       legend=dict(orientation="h", y=1.12), margin=dict(t=60, b=10))
    st.plotly_chart(fig2, width="stretch")

    # Chart 3: z-score
    fig3 = go.Figure()
    fig3.add_hrect(y0=2, y1=max(4, z.max()), fillcolor="rgba(192,57,43,0.10)", line_width=0)
    fig3.add_hrect(y0=min(-4, z.min()), y1=-2, fillcolor="rgba(30,132,73,0.10)", line_width=0)
    fig3.add_trace(go.Scatter(x=z.index, y=z, name="Z-score", line=dict(width=1.4, color="#1F4E79")))
    fig3.add_hline(y=0, line_width=1)
    fig3.update_layout(title="Z-score: red zone = A rich, green zone = A cheap", height=280,
                       margin=dict(t=50, b=10), yaxis_title="z")
    st.plotly_chart(fig3, width="stretch")

    with st.expander("Is the relationship stable? Rolling 6-month correlation of daily returns"):
        roll = ra.rolling(126).corr(rb).dropna()
        fig4 = go.Figure(go.Scatter(x=roll.index, y=roll, line=dict(color="#1F4E79")))
        fig4.add_hline(y=0, line_width=1)
        fig4.update_layout(height=260, yaxis=dict(range=[-1, 1], title="r"), margin=dict(t=20))
        st.plotly_chart(fig4, width="stretch")
        st.caption("A mean reversion trade assumes the link between A and B holds. If this line has collapsed "
                   "toward zero recently, the old average may no longer apply.")

    with st.expander("Before trading any of this"):
        st.markdown(
            "- **The ADF test here uses the whole history you selected.** A pair can pass over 10 years and fail over 1.\n"
            "- **Scanning many pairs finds false positives.** With 190 pairs, some will look stretched and pass tests by chance.\n"
            "- **A ratio of A ÷ B is not a hedge ratio.** A proper pairs trade sizes each leg by volatility or regression beta.\n"
            "- **Futures roll gaps, carry and costs are ignored.** Gold, silver and oil series jump at contract rolls.\n"
            "- **Z-scores do not tell you when it reverts.** Structural changes (a new OPEC policy, a central bank regime) "
            "can keep a ratio stretched for years.")


with tab_mr:
    run_tab(render_mean_reversion)


# ------------------------------------------------------------------------ Rates
def render_rates():
    ust2, ust10 = safe(load_fred, "DGS2", label="UST 2Y (FRED)"), safe(load_fred, "DGS10", label="UST 10Y (FRED)")
    if ust2 is not None and ust10 is not None:
        r = pd.concat([ust2.rename("UST2Y"), ust10.rename("UST10Y")], axis=1, sort=True).dropna()
        r["2s10s (bp)"] = (r["UST10Y"] - r["UST2Y"]) * 100
        source = "FRED (New York close)"
    else:
        y = safe(load_yahoo_yields, label="Treasury yields (Yahoo fallback)")
        if y is None:
            return
        r = y.dropna()
        r["5s30s (bp)"] = (r["UST30Y"] - r["UST5Y"]) * 100
        source = "Yahoo Finance fallback (FRED unreachable, so 2Y is unavailable)"
    jgb = safe(load_jgb10, label="JGB 10Y (Japan MOF)")
    if jgb is not None:
        r = r.join(jgb, how="left")
        r["JGB10Y"] = r["JGB10Y"].ffill()
        r["UST10-JGB10 (bp)"] = (r["UST10Y"] - r["JGB10Y"]) * 100
    r = r[r.index >= r.index.max() - pd.DateOffset(years=2)].dropna()
    last, prev = r.iloc[-1], r.iloc[-6]
    cols = st.columns(len(r.columns))
    for c, name in zip(cols, r.columns):
        is_bp = "bp" in name
        delta = (last[name] - prev[name]) * (1 if is_bp else 100)
        c.metric(name, f"{last[name]:.1f}" if is_bp else f"{last[name]:.2f}%", f"{delta:+.1f}bp 1W")
    st.caption(f"Source: {source}; JGB from Japan MOF (Tokyo close). Last date {r.index[-1]:%d %b %Y}.")
    left, right = st.columns(2)
    left.line_chart(r[[c for c in r.columns if "bp" not in c]], height=320)
    right.line_chart(r[[c for c in r.columns if "bp" in c]], height=320)


with tab_rates:
    run_tab(render_rates)


# ------------------------------------------------------------------------ Calendar
def render_calendar():
    cal = safe(load_calendar, label="Economic calendar (Forex Factory)")
    if cal is None or cal.empty:
        st.markdown("Open the calendar directly: [Forex Factory](https://www.forexfactory.com/calendar) or "
                    "[Investing.com](https://www.investing.com/economic-calendar/).")
        return
    c1, c2, c3 = st.columns([2, 2, 1])
    countries = sorted(cal["country"].unique())
    ccys = c1.multiselect("Currencies", countries,
                          default=[c for c in ["USD", "EUR", "JPY", "GBP", "AUD", "CAD", "CHF", "NZD"] if c in countries])
    impacts = c2.multiselect("Impact", ["High", "Medium", "Low", "Holiday"], default=["High", "Medium"])
    upcoming = c3.toggle("Upcoming only", value=True)
    view = cal[cal["country"].isin(ccys) & cal["impact"].isin(impacts)]
    if upcoming:
        view = view[view["SGT"] >= now_sgt() - pd.Timedelta(hours=1)]
    view = view.assign(Day=view["SGT"].dt.strftime("%a %d %b"), Time=view["SGT"].dt.strftime("%H:%M"))
    st.dataframe(view[["Day", "Time", "country", "impact", "title", "forecast", "previous"]],
                 hide_index=True, width="stretch", height=560)
    st.caption("Times in SGT. Source: Forex Factory weekly JSON (unofficial, current week only).")


with tab_cal:
    run_tab(render_calendar)


# ------------------------------------------------------------------------ News
def render_news():
    c1, c2 = st.columns([3, 1])
    themes = c1.multiselect("Themes", list(NEWS_THEMES), default=list(NEWS_THEMES))
    hours = c2.slider("Last N hours", 6, 72, 24, step=6)
    keyword = st.text_input("Filter headlines containing (optional)")
    frames = [safe(load_news, t, NEWS_THEMES[t], label=f"News: {t}") for t in themes]
    frames = [f for f in frames if f is not None and not f.empty]
    if frames:
        news = pd.concat(frames).dropna(subset=["time"])
        news = news[news["time"] >= now_sgt() - pd.Timedelta(hours=hours)]
        news = news.drop_duplicates(subset="title").sort_values("time", ascending=False)
        if keyword:
            news = news[news["title"].str.contains(keyword, case=False, regex=False)]
        st.write(f"{len(news)} headlines")
        for _, row in news.head(80).iterrows():
            st.markdown(f"**{row['time']:%d %b %H:%M}** `{row['theme']}` [{row['title']}]({row['link']})")

    st.subheader("Central bank releases")
    items = list(CENTRAL_BANK_FEEDS.items())
    for chunk in (items[:3], items[3:]):
        cols = st.columns(3)
        for col, (name, url) in zip(cols, chunk):
            with col:
                st.markdown(f"**{name}**")
                cb = safe(load_cb, name, url, label=name)
                if cb is not None and not cb.empty:
                    for _, row in cb.sort_values("time", ascending=False, na_position="last").head(5).iterrows():
                        when = f"{row['time']:%d %b} " if pd.notna(row["time"]) else ""
                        st.markdown(f"{when}[{row['title']}]({row['link']})")


with tab_news:
    run_tab(render_news)

st.divider()
st.caption("Built with Python and Streamlit. Data from Yahoo Finance, FRED, Japan MOF, Forex Factory, Google News "
           "and central bank feeds. For education and research only; not investment advice.")
