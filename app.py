"""
FX & Macro Daily Brief
======================
A single-file Streamlit dashboard for daily FX, commodity and rates work, in
Singapore time.

Tabs
  Markets board : FX, metals, energy and rates futures, auto-refreshing
  Mean reversion: scan every pair for stretched ratios, then inspect one in detail
  Rates         : live rates futures plus daily cash curves (UST, JGB, SGS)
  Calendar      : this week's economic calendar
  News          : themed headlines plus central bank releases

Everything is in this one file on purpose. A subdirectory is the one thing that can
reach GitHub half-committed, and on Windows it can also be committed under a
different capitalisation, which imports fine locally and fails on Streamlit Cloud's
Linux filesystem.

Contents
  1. Instrument registry      what the dashboard covers, Yahoo tickers and IB specs
  2. Price sources            Quote, IBSource, YahooSource, get_source()
  3. Analytics                Dickey-Fuller, half-life, pair scan, board building
  4. Bond and macro loaders   FRED, Yahoo, Japan MOF, MAS
  5. Dashboard                the five tabs
  6. IB feed daemon           runs locally, writes the SQLite store
  7. CLI and dispatch         feed / contracts / selftest, else render the dashboard

Deploying
  Upload this file and a requirements.txt containing:
      streamlit>=1.49
      pandas>=2.2
      numpy>=1.26
      requests>=2.32
      plotly>=5.22
      yfinance>=0.2.54
  ib_async is deliberately not in there. Only the local feed needs it, and the
  cloud never imports it.

Running locally
  python -m streamlit run app.py          the dashboard, Yahoo prices
  python app.py selftest                  check the statistics, no network needed
  python app.py contracts --port 4002     see what IB thinks each contract is
  python app.py feed --market-data-type 3 live IB quotes (needs ib_async + Gateway)

For education and research only. Not investment advice.
"""
from __future__ import annotations

import io
import math
import os
import sqlite3
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from itertools import combinations
from pathlib import Path
from typing import Protocol
from urllib.parse import quote_plus

import numpy as np
import pandas as pd
import requests

SGT = "Asia/Singapore"
HEADERS = {"User-Agent": "Mozilla/5.0 (fx-macro-brief personal dashboard)"}
TIMEOUT = 30
DB_PATH = Path(os.environ.get("FXBRIEF_DB", "data/quotes.db"))


def now_sgt() -> pd.Timestamp:
    return pd.Timestamp.now(tz=SGT)


# =====================================================================================
# 1. INSTRUMENT REGISTRY
# =====================================================================================
# Notes on the IB specs:
#   - FX is secType CASH on IDEALPRO. IB quotes offshore CNH, not onshore CNY.
#   - KRW has no IB spec on purpose: the won is non-deliverable and IBKR offers no
#     spot contract, so that row always falls back to Yahoo.
#   - Futures specs omit the expiry. The front month is resolved at runtime, because
#     a hardcoded month is wrong within a quarter.
#   - Specs marked VERIFY need checking against your own account entitlements:
#     run `python app.py contracts`.

@dataclass(frozen=True)
class Instrument:
    label: str
    group: str
    yahoo: str | None = None
    ib: dict | None = None
    note: str = ""


FX, ASIA, CMDTY, RATES = "G10 FX", "Asia FX", "Commodities", "Rates futures"


def _cash(sym: str, ccy: str) -> dict:
    return {"secType": "CASH", "symbol": sym, "currency": ccy, "exchange": "IDEALPRO"}


def _fut(sym: str, exch: str, ccy: str = "USD") -> dict:
    return {"secType": "FUT", "symbol": sym, "exchange": exch, "currency": ccy}


INSTRUMENTS: tuple[Instrument, ...] = (
    Instrument("EUR/USD", FX, "EURUSD=X", _cash("EUR", "USD")),
    Instrument("GBP/USD", FX, "GBPUSD=X", _cash("GBP", "USD")),
    Instrument("USD/JPY", FX, "JPY=X", _cash("USD", "JPY")),
    Instrument("AUD/USD", FX, "AUDUSD=X", _cash("AUD", "USD")),
    Instrument("NZD/USD", FX, "NZDUSD=X", _cash("NZD", "USD")),
    Instrument("USD/CAD", FX, "CAD=X", _cash("USD", "CAD")),
    Instrument("USD/CHF", FX, "CHF=X", _cash("USD", "CHF")),
    Instrument("USD/NOK", FX, "NOK=X", _cash("USD", "NOK")),
    Instrument("USD/SEK", FX, "SEK=X", _cash("USD", "SEK")),
    Instrument("EUR/GBP", FX, "EURGBP=X", _cash("EUR", "GBP")),
    Instrument("EUR/JPY", FX, "EURJPY=X", _cash("EUR", "JPY")),
    Instrument("DXY", FX, "DX-Y.NYB", _fut("DX", "NYBOT"),
               "Yahoo gives the ICE index, IB gives the DX future. VERIFY exchange code."),
    Instrument("USD/SGD", ASIA, "SGD=X", _cash("USD", "SGD")),
    Instrument("USD/CNH", ASIA, "CNY=X", _cash("USD", "CNH"),
               "Yahoo CNY=X is onshore CNY, IB quotes offshore CNH. Different markets."),
    Instrument("USD/KRW", ASIA, "KRW=X", None,
               "NDF only. No IB spot contract, so this row stays on Yahoo."),
    Instrument("Gold", CMDTY, "GC=F", _fut("GC", "COMEX")),
    Instrument("Silver", CMDTY, "SI=F", _fut("SI", "COMEX")),
    Instrument("Copper", CMDTY, "HG=F", _fut("HG", "COMEX")),
    Instrument("WTI crude", CMDTY, "CL=F", _fut("CL", "NYMEX")),
    Instrument("Brent crude", CMDTY, "BZ=F", _fut("BZ", "NYMEX"),
               "NYMEX BZ is the financially settled Brent look-alike, not ICE Brent "
               "(ICE is symbol COIL on exchange IPE). VERIFY which you want."),
    Instrument("UST 2Y fut (ZT)", RATES, "ZT=F", _fut("ZT", "CBOT")),
    Instrument("UST 10Y fut (ZN)", RATES, "ZN=F", _fut("ZN", "CBOT")),
    Instrument("UST 30Y fut (ZB)", RATES, "ZB=F", _fut("ZB", "CBOT")),
    Instrument("JGB 10Y fut", RATES, None, _fut("JGB", "OSE.JPN", "JPY"),
               "VERIFY symbol and multiplier: IB lists both the standard and mini JGB "
               "contract on OSE.JPN under different symbols. No Yahoo equivalent, so "
               "this row is IB-only for quotes and history."),
    Instrument("Bund fut (FGBL)", RATES, None, _fut("GBL", "EUREX", "EUR"),
               "VERIFY. Eurex Euro-Bund, commonly symbol GBL on IB."),
)

BY_LABEL: dict[str, Instrument] = {i.label: i for i in INSTRUMENTS}
GROUPS = (FX, ASIA, CMDTY, RATES)
BOARD_DEFAULT_GROUPS = (FX, ASIA, CMDTY)
YAHOO_TICKERS = tuple(i.yahoo for i in INSTRUMENTS if i.yahoo)
LABEL_FOR_YAHOO = {i.yahoo: i.label for i in INSTRUMENTS if i.yahoo}
IB_INSTRUMENTS = tuple(i for i in INSTRUMENTS if i.ib)

PRESETS: dict[str, tuple[str, str]] = {
    "Gold vs Silver": ("Gold", "Silver"),
    "Gold vs WTI": ("Gold", "WTI crude"),
    "Brent vs WTI": ("Brent crude", "WTI crude"),
    "AUD vs NZD": ("AUD/USD", "NZD/USD"),
    "EUR vs GBP": ("EUR/USD", "GBP/USD"),
    "JGB vs UST 10Y fut": ("JGB 10Y fut", "UST 10Y fut (ZN)"),
}

NEWS_THEMES = {
    "USD & Fed": '"dollar index" OR "Federal Reserve" OR "Treasury yields"',
    "EUR & ECB": '"EUR/USD" OR "European Central Bank" OR "euro zone" inflation',
    "JPY & BOJ": '"USD/JPY" OR "Bank of Japan" OR "JGB yields"',
    "GBP & BoE": '"GBP/USD" OR "Bank of England" OR "sterling"',
    "Commodity FX": ('"Australian dollar" OR "New Zealand dollar" OR '
                     '"Canadian dollar" OR "Norwegian crown"'),
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
# MAS publishes no RSS feed (every /rss path returns the HTML page), so it is linked
# rather than parsed. All six feeds above were checked and parse as RSS.
MAS_NEWS_URL = "https://www.mas.gov.sg/news"
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
LOOKBACKS = {"1Y": 252, "2Y": 504, "5Y": 1260, "10Y": 2520, "Max": None}
Z_WINDOWS = {"1 month": 21, "3 months": 63, "6 months": 126, "1 year": 252}


# =====================================================================================
# 2. PRICE SOURCES
# =====================================================================================
# One interface, two backends. Nothing in the dashboard knows which it got.
#
# IBSource never opens a socket. The feed daemon (section 6) holds the single IB
# connection and writes data/quotes.db; IBSource reads it. Streamlit reruns its
# script on every interaction, so a socket opened in the script would be torn down
# and reopened constantly, and IB rejects two connections sharing a clientId.
# SQLite in WAL mode supports one writer plus many readers, which is this shape.

MARKET_DATA_TYPES = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed frozen"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    label        TEXT PRIMARY KEY,
    local_symbol TEXT,
    bid  REAL, ask REAL, last REAL,
    ts   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bars (
    label TEXT NOT NULL,
    date  TEXT NOT NULL,
    close REAL NOT NULL,
    PRIMARY KEY (label, date)
);
CREATE TABLE IF NOT EXISTS feed_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    heartbeat TEXT NOT NULL,
    connected INTEGER NOT NULL,
    market_data_type INTEGER,
    message TEXT
);
"""


def db_connect(path: Path = DB_PATH, create: bool = False) -> sqlite3.Connection:
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), check_same_thread=False, timeout=5)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=3000")
    if create:
        con.executescript(SCHEMA)
        con.commit()
    return con


def _clean(x) -> float | None:
    """IB uses NaN, -1 and 0 for 'no value'. Normalise all of them to None."""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or f <= 0) else f


@dataclass(frozen=True)
class Quote:
    label: str
    ts: datetime                  # timezone-aware, UTC
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    source: str = ""

    def __post_init__(self):
        for f in ("bid", "ask", "last"):
            object.__setattr__(self, f, _clean(getattr(self, f)))

    @property
    def mid(self) -> float | None:
        return (self.bid + self.ask) / 2 if (self.bid and self.ask) else None

    @property
    def price(self) -> float | None:
        """What to print: mid when there are two sides, else last."""
        return self.mid or self.last

    @property
    def spread(self) -> float | None:
        return (self.ask - self.bid) if (self.bid and self.ask) else None

    @property
    def spread_bp(self) -> float | None:
        mid, sp = self.mid, self.spread
        return (sp / mid) * 10_000 if (mid and sp is not None) else None

    @property
    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.ts).total_seconds()


class QuoteSource(Protocol):
    name: str

    def quotes(self, labels: Iterable[str]) -> dict[str, Quote]:
        ...


class IBSource:
    name = "IB"

    def __init__(self, path: Path = DB_PATH, max_age_seconds: int = 120):
        self.path, self.max_age_seconds = path, max_age_seconds

    def quotes(self, labels: Iterable[str]) -> dict[str, Quote]:
        if not self.path.exists():
            return {}
        wanted = set(labels)
        con = db_connect(self.path)
        try:
            rows = con.execute("SELECT label, bid, ask, last, ts FROM quotes").fetchall()
        except sqlite3.OperationalError:
            return {}
        finally:
            con.close()
        out: dict[str, Quote] = {}
        for label, bid, ask, last, ts in rows:
            if label not in wanted:
                continue
            q = Quote(label, pd.Timestamp(ts).tz_convert("UTC").to_pydatetime(),
                      bid, ask, last, self.name)
            if q.age_seconds <= self.max_age_seconds:
                out[label] = q
        return out

    def daily_bars(self) -> pd.DataFrame:
        """Daily closes the feed pulled from IB.

        This is how instruments Yahoo does not carry (JGB and Bund futures) get into
        the mean-reversion scanner.
        """
        if not self.path.exists():
            return pd.DataFrame()
        con = db_connect(self.path)
        try:
            df = pd.read_sql("SELECT label, date, close FROM bars", con,
                             parse_dates=["date"])
        except Exception:
            return pd.DataFrame()
        finally:
            con.close()
        if df.empty:
            return df
        return df.pivot(index="date", columns="label", values="close").sort_index()

    def status(self) -> dict:
        if not self.path.exists():
            return {"available": False, "reason": f"no database at {self.path}"}
        con = db_connect(self.path)
        try:
            row = con.execute("SELECT heartbeat, connected, market_data_type, message "
                              "FROM feed_status WHERE id = 1").fetchone()
            n = con.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
        except sqlite3.OperationalError as e:
            return {"available": False, "reason": str(e)}
        finally:
            con.close()
        if not row:
            return {"available": False, "reason": "the feed has never written a heartbeat"}
        heartbeat, connected, mdt, message = row
        age = (datetime.now(timezone.utc)
               - pd.Timestamp(heartbeat).tz_convert("UTC").to_pydatetime()).total_seconds()
        return {"available": bool(connected) and age < 30, "heartbeat_age": age,
                "connected": bool(connected), "market_data_type": mdt,
                "message": message or "", "instruments": n}


class YahooSource:
    """Delayed, one-sided (no bid/ask) and unofficial. What the public deploy uses."""

    name = "Yahoo (delayed)"

    def quotes(self, labels: Iterable[str]) -> dict[str, Quote]:
        df = _yahoo_intraday()
        out: dict[str, Quote] = {}
        for label in labels:
            if label not in df:
                continue
            s = df[label].dropna()
            if s.empty:
                continue
            out[label] = Quote(label, s.index[-1].to_pydatetime(),
                               last=float(s.iloc[-1]), source=self.name)
        return out


class CompositeSource:
    name = "IB + Yahoo"

    def __init__(self, primary: QuoteSource, fallback: QuoteSource):
        self.primary, self.fallback = primary, fallback

    def quotes(self, labels: Iterable[str]) -> dict[str, Quote]:
        labels = list(labels)
        out = dict(self.fallback.quotes(labels))
        out.update(self.primary.quotes(labels))  # primary wins where it has a price
        return out


def get_source(mode: str = "auto", max_age_seconds: int = 120):
    if mode == "yahoo":
        return YahooSource()
    ib = IBSource(max_age_seconds=max_age_seconds)
    return ib if mode == "ib" else CompositeSource(ib, YahooSource())


# =====================================================================================
# 3. ANALYTICS
# =====================================================================================
# MacKinnon asymptotic 5% critical value, Dickey-Fuller with constant, no trend.
DF_5PCT = -2.86


def df_tstat(x) -> float:
    """Dickey-Fuller t-stat on the level, constant, zero lags.

    More negative means stronger evidence of mean reversion. This is the plain DF
    test, not augmented: with serially correlated residuals the statistic is biased,
    usually toward rejecting the unit root. Treat it as a screen, not a verdict.
    """
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
    return float(coef[1] / se)


def half_life(x) -> float:
    """Trading days for a deviation to halve, from an AR(1) fit.

    NaN when the fitted coefficient is not negative, meaning the series is not
    pulling back toward anything.
    """
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    if len(x) < 30:
        return np.nan
    beta = np.polyfit(x[:-1], np.diff(x), 1)[0]
    return float(-np.log(2) / beta) if beta < 0 else np.nan


def spearman(a: pd.Series, b: pd.Series) -> float:
    """Rank correlation without scipy: Pearson correlation of average ranks."""
    return float(a.rank().corr(b.rank()))


def pair_series(prices: pd.DataFrame, a: str, b: str, how: str) -> pd.Series:
    df = prices[[a, b]].dropna()
    return np.log(df[a] / df[b]) if how == "ratio" else df[a] - df[b]


def scan_pairs(prices: pd.DataFrame, z_window: int) -> pd.DataFrame:
    """Rank every pair by how stretched its log price ratio is."""
    rets = pd.concat({c: np.log(prices[c].dropna()).diff() for c in prices},
                     axis=1, sort=True)
    rows = []
    for a, b in combinations(prices.columns, 2):
        lr = pair_series(prices, a, b, "ratio")
        if len(lr) < max(z_window * 2, 120):
            continue
        mean, sd = lr.rolling(z_window).mean(), lr.rolling(z_window).std()
        if not np.isfinite(sd.iloc[-1]) or sd.iloc[-1] == 0:
            continue
        z = (lr.iloc[-1] - mean.iloc[-1]) / sd.iloc[-1]
        rr = rets[[a, b]].dropna().iloc[-252:]
        r = rr[a].corr(rr[b])
        rows.append({"A": a, "B": b, "Z-score": z,
                     "Signal": "A rich vs B" if z >= 2 else ("A cheap vs B" if z <= -2 else ""),
                     "DF t": df_tstat(lr.values), "Half-life (days)": half_life(lr.values),
                     "r (1Y returns)": r, "R²": r * r,
                     "Spearman ρ": spearman(rr[a], rr[b]), "Days of data": len(lr)})
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.reindex(out["Z-score"].abs().sort_values(ascending=False).index
                       ).reset_index(drop=True)


def build_board(daily: pd.DataFrame, quotes: dict[str, Quote],
                min_history: int = 260) -> pd.DataFrame:
    """One row per instrument: live price plus daily-history statistics."""
    rows = []
    for label, inst in BY_LABEL.items():
        if label not in daily:
            continue
        s = daily[label].dropna()
        if len(s) < min_history:
            continue
        q = quotes.get(label)
        last = (q.price if q and q.price else None) or float(s.iloc[-1])

        # Yahoo's daily frame may already carry today's in-progress bar, which would
        # make the 1D change zero. Drop it when the live stamp is not newer. The
        # comparison is done in New York because Yahoo's daily index is stamped on
        # the US session date; using the server's local timezone would behave
        # differently on a laptop and in the cloud.
        base = s
        if q is not None:
            live_date = pd.Timestamp(q.ts).tz_convert("America/New_York").date()
            if live_date <= s.index[-1].date():
                base = s.iloc[:-1]
        if len(base) < min_history - 5:
            continue

        rets = base.pct_change().dropna().iloc[-252:]
        r3m = pd.concat([base.iloc[-62:], pd.Series([last], index=[base.index[-1]])])
        rng = r3m.max() - r3m.min()
        rows.append({
            "Group": inst.group, "Instrument": label, "Last": last,
            "Bid": q.bid if q else None, "Ask": q.ask if q else None,
            "Spread (bp)": q.spread_bp if q else None,
            "Source": q.source if q else "daily close",
            "Age (s)": round(q.age_seconds) if q else None,
            "1D %": (last / base.iloc[-1] - 1) * 100,
            "1W %": (last / base.iloc[-5] - 1) * 100,
            "1M %": (last / base.iloc[-21] - 1) * 100,
            "1D move (z)": ((last / base.iloc[-1] - 1) - rets.mean()) / rets.std(),
            "3M range %ile": (last - r3m.min()) / rng * 100 if rng else np.nan,
            "20D vol %": rets.iloc[-20:].std() * np.sqrt(252) * 100,
            "Last 3M": r3m.round(6).tolist(),
        })
    return pd.DataFrame(rows)


def zscore(s: pd.Series, window: int) -> pd.Series:
    return (s - s.rolling(window).mean()) / s.rolling(window).std()


# =====================================================================================
# 4. BOND AND MACRO LOADERS
# =====================================================================================
# Latency, stated plainly:
#   UST 2Y/10Y   FRED DGS series. Daily, after the New York close.
#   UST fallback Yahoo ^FVX ^TNX ^TYX (CBOE yield indices). Delayed, US session only.
#   JGB 10Y      Japan Ministry of Finance CSV. Daily, Tokyo close.
#   SGS          MAS API. Daily.
#   Intraday     Futures only, via IB: ZT/ZN/ZB on CBOT, JGB on OSE.JPN, FGBL on
#                Eurex. There is no free intraday cash-bond feed.
# Do not mix frequencies silently. A daily cash series and an intraday futures print
# are not one series.

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"
MOF_HIST = ("https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/"
            "historical/jgbcme_all.csv")
MOF_CUR = ("https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/"
           "jgbcme.csv")
MAS_API = "https://eservices.mas.gov.sg/api/action/datastore/search.json"
# VERIFY: this resource id came from published MAS API examples, not from a live
# call (eservices.mas.gov.sg was serving its failover page when this was written).
# Confirm it and the field names at https://eservices.mas.gov.sg/apimg-portal/api-catalog
MAS_RATES_RESOURCE = "9a0bf149-308c-4bd2-832d-76c8e6cb47ed"


def fetch_fred(series_id: str) -> pd.Series:
    raw = requests.get(FRED_CSV.format(series_id), timeout=TIMEOUT).text
    if not raw.startswith("observation_date"):
        raise ValueError(f"unexpected FRED response: {raw[:80]!r}")
    df = pd.read_csv(io.StringIO(raw), na_values=".")
    df.columns = ["Date", series_id]
    return df.assign(Date=pd.to_datetime(df["Date"])).set_index("Date")[series_id].dropna()


def fetch_yahoo_yields() -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(["^FVX", "^TNX", "^TYX"], period="5y", progress=False,
                     auto_adjust=False)["Close"]
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df.rename(columns={"^FVX": "UST5Y", "^TNX": "UST10Y", "^TYX": "UST30Y"})


def _read_mof(url: str) -> pd.Series:
    text = requests.get(url, timeout=TIMEOUT, headers=HEADERS).content.decode(
        "shift_jis", errors="ignore")
    df = pd.read_csv(io.StringIO(text), skiprows=1, na_values="-")
    df["Date"] = pd.to_datetime(df["Date"], format="%Y/%m/%d", errors="coerce")
    return df.dropna(subset=["Date"]).set_index("Date")["10Y"].astype(float)


def fetch_jgb10() -> pd.Series:
    s = pd.concat([_read_mof(MOF_HIST), _read_mof(MOF_CUR)])
    return s[~s.index.duplicated(keep="last")].sort_index().dropna().rename("JGB10Y")


def fetch_mas_rates(limit: int = 3000) -> pd.DataFrame:
    """MAS daily domestic interest rates, including SGS benchmark yields.

    Field names are not hardcoded: the frame is returned as published so the UI can
    let you pick columns. That avoids silently charting the wrong field if MAS
    renames one.
    """
    resp = requests.get(MAS_API, timeout=TIMEOUT, headers=HEADERS,
                        params={"resource_id": MAS_RATES_RESOURCE, "limit": limit,
                                "sort": "end_of_day desc"})
    resp.raise_for_status()
    if not resp.text.lstrip().startswith("{"):
        raise ValueError("MAS returned HTML, not JSON. The eservices site is likely "
                         "in maintenance, or the resource id is wrong.")
    records = resp.json().get("result", {}).get("records", [])
    if not records:
        raise ValueError("MAS returned no records. Check the resource id.")
    df = pd.DataFrame(records)
    date_col = "end_of_day" if "end_of_day" in df else df.columns[0]
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=[date_col]).set_index(date_col).sort_index()
    return df.apply(pd.to_numeric, errors="coerce").dropna(axis=1, how="all")


def _parse_rss(xml_bytes: bytes, source: str) -> pd.DataFrame:
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


def parse_tradingview_csv(file, name: str) -> pd.Series:
    """TradingView 'Export chart data' CSV: a time column and a close column."""
    df = pd.read_csv(file)
    cols = {c.lower().strip(): c for c in df.columns}
    if "time" not in cols or "close" not in cols:
        raise ValueError(f"expected 'time' and 'close' columns, found {list(df.columns)}")
    t = df[cols["time"]]
    utc = pd.DatetimeIndex(
        pd.to_datetime(t, unit="s", utc=True) if pd.api.types.is_numeric_dtype(t)
        else pd.to_datetime(t, utc=True))
    # Daily bars can be stamped at the session open (22:00 UTC the previous day for
    # CME futures). Shifting 12 hours before taking the date lands every session on
    # its trading date.
    dates = (utc + pd.Timedelta(hours=12)).tz_convert(None).normalize()
    s = pd.Series(df[cols["close"]].astype(float).values, index=dates, name=name)
    return s[~s.index.duplicated(keep="last")].sort_index()


# =====================================================================================
# 5. DASHBOARD
# =====================================================================================
# Streamlit is imported lazily so the CLI modes in section 7 work without it and,
# more importantly, so `python app.py selftest` exercises the statistics with no
# Streamlit runtime at all.

def _st():
    import streamlit as st
    return st


try:  # cached loaders need the decorator at import time
    import streamlit as _stmod

    @_stmod.cache_data(ttl=60, show_spinner=False)
    def _yahoo_intraday() -> pd.DataFrame:
        import yfinance as yf
        raw = yf.download(list(YAHOO_TICKERS), period="5d", interval="5m",
                          progress=False, auto_adjust=False)
        close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]
        df = close.rename(columns=LABEL_FOR_YAHOO).sort_index()
        idx = df.index.tz_localize("UTC") if df.index.tz is None else df.index
        df.index = idx.tz_convert("UTC")
        return df

    @_stmod.cache_data(ttl=30 * 60, show_spinner="Pulling daily price history")
    def load_daily_yahoo() -> pd.DataFrame:
        import yfinance as yf
        raw = yf.download(list(YAHOO_TICKERS), period="max", interval="1d",
                          progress=False, auto_adjust=False)
        close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]
        df = close.rename(columns=LABEL_FOR_YAHOO).sort_index()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df

    @_stmod.cache_data(ttl=10 * 60, show_spinner=False)
    def load_daily_ib() -> pd.DataFrame:
        df = IBSource().daily_bars()
        if df.empty:
            return df
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df

    @_stmod.cache_data(ttl=30 * 60, show_spinner="Scanning every pair")
    def cached_scan(prices: pd.DataFrame, z_window: int) -> pd.DataFrame:
        return scan_pairs(prices, z_window)

    @_stmod.cache_data(ttl=6 * 3600, show_spinner="Pulling FRED")
    def load_fred(series_id: str) -> pd.Series:
        return fetch_fred(series_id)

    @_stmod.cache_data(ttl=6 * 3600, show_spinner="Pulling Treasury yields from Yahoo")
    def load_yahoo_yields() -> pd.DataFrame:
        return fetch_yahoo_yields()

    @_stmod.cache_data(ttl=6 * 3600, show_spinner="Pulling JGB yields")
    def load_jgb10() -> pd.Series:
        return fetch_jgb10()

    @_stmod.cache_data(ttl=6 * 3600, show_spinner="Pulling SGS yields from MAS")
    def load_mas_rates() -> pd.DataFrame:
        return fetch_mas_rates()

    @_stmod.cache_data(ttl=60 * 60, show_spinner="Pulling calendar")
    def load_calendar() -> pd.DataFrame:
        resp = requests.get(CALENDAR_URL, timeout=TIMEOUT, headers=HEADERS)
        resp.raise_for_status()
        df = pd.DataFrame(resp.json())
        df["SGT"] = pd.to_datetime(df["date"], utc=True).dt.tz_convert(SGT)
        return df.drop(columns="date").sort_values("SGT")

    @_stmod.cache_data(ttl=15 * 60, show_spinner="Pulling news")
    def load_news(theme: str, query: str) -> pd.DataFrame:
        url = (f"https://news.google.com/rss/search?q={quote_plus(query)}"
               "+when:3d&hl=en-SG&gl=SG&ceid=SG:en")
        df = _parse_rss(requests.get(url, timeout=TIMEOUT, headers=HEADERS).content,
                        "Google News")
        return df.assign(theme=theme) if not df.empty else df

    @_stmod.cache_data(ttl=30 * 60, show_spinner="Pulling central bank releases")
    def load_cb(name: str, url: str) -> pd.DataFrame:
        return _parse_rss(requests.get(url, timeout=TIMEOUT, headers=HEADERS).content,
                          name)
except ImportError:  # CLI modes do not need Streamlit
    pass


def safe(fn, *args, label="", **kwargs):
    """Run a loader; show the real error instead of crashing the page."""
    st = _st()
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        st.warning(f"{label or getattr(fn, '__name__', 'loader')} unavailable: "
                   f"{type(e).__name__}: {e}")
        return None


def load_prices(use_ib_history: bool) -> pd.DataFrame:
    """Yahoo history, with IB columns added for anything Yahoo does not carry."""
    daily = load_daily_yahoo()
    if not use_ib_history:
        return daily
    ib = load_daily_ib()
    if ib is None or ib.empty:
        return daily
    extra = [c for c in ib.columns if c not in daily.columns]
    return daily.join(ib[extra], how="outer") if extra else daily


@dataclass
class Ctx:
    """Sidebar choices, passed to each tab so no renderer reads globals."""
    mode: str
    max_age: int
    refresh: int | None
    uploads: list


# ----------------------------------------------------------------- Markets board
def render_board(ctx: Ctx):
    st = _st()
    daily = safe(load_prices, ctx.mode != "yahoo", label="Daily prices")
    if daily is None:
        return
    source = get_source(ctx.mode, max_age_seconds=ctx.max_age)
    quotes = safe(source.quotes, list(BY_LABEL), label="Live quotes") or {}
    board = build_board(daily, quotes)
    if board.empty:
        st.warning("No instrument had enough history to build a row.")
        return

    groups = st.pills("Show", list(GROUPS), selection_mode="multi",
                      default=list(BOARD_DEFAULT_GROUPS), key="board_groups")
    view = board[board["Group"].isin(groups or [])]
    st.dataframe(view, hide_index=True, width="stretch", height=740, column_config={
        "Last": st.column_config.NumberColumn(format="%.4f"),
        "Bid": st.column_config.NumberColumn(format="%.4f"),
        "Ask": st.column_config.NumberColumn(format="%.4f"),
        "Spread (bp)": st.column_config.NumberColumn(
            format="%.2f", help="Bid-ask spread in bp of mid. Blank on one-sided sources."),
        "Age (s)": st.column_config.NumberColumn(
            format="%d", help="Seconds since the feed wrote this quote"),
        "1D %": st.column_config.NumberColumn(format="%+.2f"),
        "1W %": st.column_config.NumberColumn(format="%+.2f"),
        "1M %": st.column_config.NumberColumn(format="%+.2f"),
        "1D move (z)": st.column_config.NumberColumn(
            format="%+.2f", help="Today's return divided by the standard deviation "
                                 "of the past year's daily returns"),
        "3M range %ile": st.column_config.ProgressColumn(min_value=0, max_value=100,
                                                         format="%.0f"),
        "20D vol %": st.column_config.NumberColumn(
            format="%.1f", help="Annualised realised volatility"),
        "Last 3M": st.column_config.LineChartColumn(width="medium"),
    })
    big = view.loc[view["1D move (z)"].abs() >= 2, "Instrument"].tolist()
    if big:
        st.info(f"Unusual moves today (|z| of at least 2): {', '.join(big)}. "
                "Check the print, then find the headline.")
    live_n = int(view["Source"].str.startswith("IB").sum())
    st.caption(f"Refreshed {now_sgt():%H:%M:%S} SGT. {live_n} of {len(view)} rows on IB. "
               "Metals and energy are front-month futures, which jump at the roll.")


# ---------------------------------------------------------------- Mean reversion
def _set_pair(a: str, b: str):
    st = _st()
    st.session_state["pair_a"], st.session_state["pair_b"] = a, b


def _on_scan_select():
    st = _st()
    rows = st.session_state["scan_table"].selection.rows
    table = st.session_state.get("scan_view")
    if rows and table is not None:
        _set_pair(table.iloc[rows[0]]["A"], table.iloc[rows[0]]["B"])


def render_mean_reversion(ctx: Ctx):
    st = _st()
    daily = safe(load_prices, ctx.mode != "yahoo", label="Daily prices")
    if daily is None:
        return
    prices = daily.copy()
    for f in ctx.uploads or []:
        name = "TV: " + f.name.rsplit(".", 1)[0][:30]
        s = safe(parse_tradingview_csv, f, name, label=f"Upload {f.name}")
        if s is not None:
            prices = prices.join(s, how="outer")
    names = list(prices.columns)
    st.session_state.setdefault("pair_a", "Gold")
    st.session_state.setdefault("pair_b", "Silver")

    st.markdown("**How to use this tab:** the scanner ranks every pair by how "
                "stretched its price ratio is. Click a row, or a preset, and the "
                "inspector below explains that pair in plain English.")
    c1, c2 = st.columns(2)
    lb_label = c1.segmented_control("History used", list(LOOKBACKS), default="5Y",
                                    key="lookback") or "5Y"
    zw_label = c2.segmented_control("Z-score measured against the average of the last",
                                    list(Z_WINDOWS), default="3 months",
                                    key="zwin") or "3 months"
    n, z_window = LOOKBACKS[lb_label], Z_WINDOWS[zw_label]
    hist = prices if n is None else prices.iloc[-n:]

    st.subheader("1. Scanner: which pairs look stretched?")
    f1, f2, f3 = st.columns(3)
    min_z = f1.slider("Show |z| of at least", 0.0, 3.0, 1.5, 0.25)
    only_stationary = f2.toggle("Only pairs that pass the mean reversion test",
                                value=False)
    max_hl = f3.slider("Half-life no longer than (days)", 5, 250, 120, 5)
    scan = cached_scan(hist, z_window)
    if scan.empty:
        st.warning("Not enough overlapping history to scan. Choose a longer lookback.")
        return
    view = scan[(scan["Z-score"].abs() >= min_z) & (scan["Half-life (days)"] <= max_hl)]
    if only_stationary:
        view = view[view["DF t"] <= DF_5PCT]
    view = view.reset_index(drop=True)
    st.session_state["scan_view"] = view
    st.dataframe(view, hide_index=True, width="stretch", height=320, key="scan_table",
                 on_select=_on_scan_select, selection_mode="single-row", column_config={
                     "Z-score": st.column_config.NumberColumn(format="%+.2f"),
                     "DF t": st.column_config.NumberColumn(
                         format="%.2f", help=f"Dickey-Fuller t-stat, zero lags. Below "
                                             f"{DF_5PCT} clears the 5% critical value."),
                     "Half-life (days)": st.column_config.NumberColumn(format="%.0f"),
                     "r (1Y returns)": st.column_config.NumberColumn(format="%+.2f"),
                     "R²": st.column_config.ProgressColumn(min_value=0, max_value=1,
                                                           format="%.2f"),
                     "Spearman ρ": st.column_config.NumberColumn(format="%+.2f"),
                 })
    st.caption(f"{len(view)} of {len(scan)} pairs shown. Click a column header to "
               "sort. The z-score uses the log of A ÷ B, so a positive z means A has "
               "outperformed B recently.")

    st.subheader("2. Inspector: is this pair a mean reversion candidate?")
    cols = st.columns(len(PRESETS))
    for col, (label, (a_, b_)) in zip(cols, PRESETS.items()):
        col.button(label, on_click=_set_pair, args=(a_, b_), width="stretch",
                   disabled=not (a_ in names and b_ in names))
    s1, s2, s3 = st.columns([2, 2, 2])
    a = s1.selectbox("Instrument A", names, key="pair_a")
    b = s2.selectbox("Instrument B", names, key="pair_b")
    how = s3.radio("Measure", ["Ratio A ÷ B", "Spread A − B"], horizontal=True,
                   help="Use a spread only when both legs are in the same units, e.g. "
                        "Brent − WTI in USD/bbl. A UST future in USD points against a "
                        "JGB future in JPY points is not a spread.")
    if a == b:
        st.warning("Pick two different instruments.")
        return

    df = hist[[a, b]].dropna()
    if len(df) < z_window + 30:
        st.warning(f"Only {len(df)} overlapping days of data for {a} and {b}. "
                   "Choose a longer history.")
        return
    is_ratio = how.startswith("Ratio")
    level = df[a] / df[b] if is_ratio else df[a] - df[b]
    test_series = np.log(level) if is_ratio else level
    mean, sd = level.rolling(z_window).mean(), level.rolling(z_window).std()
    z = (level - mean) / sd
    z_now = z.iloc[-1]
    adf, hl = df_tstat(test_series.values), half_life(test_series.values)
    ra, rb = np.log(df[a]).diff(), np.log(df[b]).diff()
    r1y = ra.iloc[-252:].corr(rb.iloc[-252:])
    pct_rank = (level < level.iloc[-1]).mean() * 100
    label = f"{a} ÷ {b}" if is_ratio else f"{a} − {b}"

    if z_now >= 2:
        stance = (f"**{a} looks expensive versus {b}.** A mean reversion trade would "
                  f"sell {a} and buy {b}.")
    elif z_now <= -2:
        stance = (f"**{a} looks cheap versus {b}.** A mean reversion trade would buy "
                  f"{a} and sell {b}.")
    else:
        stance = (f"**No signal.** The {label} is within 2 standard deviations of its "
                  f"{zw_label} average.")
    evidence = (f"passes the mean reversion test (DF t = {adf:.2f}, below {DF_5PCT})"
                if adf <= DF_5PCT else
                f"does **not** pass the mean reversion test (DF t = {adf:.2f}, needs "
                f"to be below {DF_5PCT}), so the gap could keep widening")
    hl_text = (f"a typical deviation halves in about {hl:.0f} trading days"
               if np.isfinite(hl) else "no measurable half-life")
    box = (st.success if (abs(z_now) >= 2 and adf <= DF_5PCT)
           else (st.warning if abs(z_now) >= 2 else st.info))
    box(f"{stance}\n\nOver the last {lb_label}, this series {evidence}; {hl_text}. "
        f"Today's level is higher than {pct_rank:.0f}% of days in that history.")

    m = st.columns(6)
    m[0].metric(label, f"{level.iloc[-1]:,.4f}")
    m[1].metric("Z-score", f"{z_now:+.2f}")
    m[2].metric("DF t-stat", f"{adf:.2f}")
    m[3].metric("Half-life (days)", f"{hl:.0f}" if np.isfinite(hl) else "n/a")
    m[4].metric("r (1Y returns)", f"{r1y:+.2f}")
    m[5].metric("R²", f"{r1y * r1y:.2f}")

    import plotly.graph_objects as go
    rebased = df / df.iloc[0] * 100
    fig1 = go.Figure()
    fig1.add_trace(go.Scatter(x=rebased.index, y=rebased[a], name=a, line=dict(width=1.6)))
    fig1.add_trace(go.Scatter(x=rebased.index, y=rebased[b], name=b, line=dict(width=1.6)))
    fig1.update_layout(title=f"{a} and {b}, both rebased to 100 on {df.index[0]:%d %b %Y}",
                       height=340, hovermode="x unified",
                       legend=dict(orientation="h", y=1.12), margin=dict(t=60, b=10),
                       yaxis_title="Rebased (start = 100)")
    st.plotly_chart(fig1, width="stretch")

    fig2 = go.Figure()
    fig2.add_trace(go.Scatter(x=mean.index, y=mean + 2 * sd, line=dict(width=0),
                              showlegend=False, hoverinfo="skip"))
    fig2.add_trace(go.Scatter(x=mean.index, y=mean - 2 * sd, fill="tonexty",
                              line=dict(width=0), fillcolor="rgba(120,140,170,0.18)",
                              name="Average ± 2σ"))
    fig2.add_trace(go.Scatter(x=mean.index, y=mean, name=f"{zw_label} average",
                              line=dict(dash="dot", width=1.2)))
    fig2.add_trace(go.Scatter(x=level.index, y=level, name=label,
                              line=dict(width=1.8, color="#1F4E79")))
    rich, cheap = level[z >= 2], level[z <= -2]
    fig2.add_trace(go.Scatter(x=rich.index, y=rich, mode="markers",
                              name=f"{a} rich (z ≥ 2)",
                              marker=dict(size=5, color="#C0392B")))
    fig2.add_trace(go.Scatter(x=cheap.index, y=cheap, mode="markers",
                              name=f"{a} cheap (z ≤ −2)",
                              marker=dict(size=5, color="#1E8449")))
    fig2.update_layout(title=f"{label}: outside the shaded band = stretched",
                       height=380, hovermode="x unified",
                       legend=dict(orientation="h", y=1.12), margin=dict(t=60, b=10))
    st.plotly_chart(fig2, width="stretch")

    fig3 = go.Figure()
    fig3.add_hrect(y0=2, y1=max(4, z.max()), fillcolor="rgba(192,57,43,0.10)", line_width=0)
    fig3.add_hrect(y0=min(-4, z.min()), y1=-2, fillcolor="rgba(30,132,73,0.10)", line_width=0)
    fig3.add_trace(go.Scatter(x=z.index, y=z, name="Z-score",
                              line=dict(width=1.4, color="#1F4E79")))
    fig3.add_hline(y=0, line_width=1)
    fig3.update_layout(title="Z-score: red zone = A rich, green zone = A cheap",
                       height=280, margin=dict(t=50, b=10), yaxis_title="z")
    st.plotly_chart(fig3, width="stretch")

    with st.expander("Is the relationship stable? Rolling 6-month return correlation"):
        roll = ra.rolling(126).corr(rb).dropna()
        fig4 = go.Figure(go.Scatter(x=roll.index, y=roll, line=dict(color="#1F4E79")))
        fig4.add_hline(y=0, line_width=1)
        fig4.update_layout(height=260, yaxis=dict(range=[-1, 1], title="r"),
                           margin=dict(t=20))
        st.plotly_chart(fig4, width="stretch")
        st.caption("A mean reversion trade assumes the link between A and B holds. If "
                   "this line has collapsed toward zero recently, the old average may "
                   "no longer apply.")

    with st.expander("Before trading any of this"):
        st.markdown(
            "- **The test uses the whole history you selected.** A pair can pass over "
            "10 years and fail over 1.\n"
            "- **It is a plain Dickey-Fuller test, zero lags.** With autocorrelated "
            "residuals the t-stat is biased toward rejecting the unit root.\n"
            "- **Scanning many pairs finds false positives.** With hundreds of pairs, "
            "some will look stretched and pass tests by chance.\n"
            "- **A ratio of A ÷ B is not a hedge ratio.** A proper pairs trade sizes "
            "each leg by volatility, DV01 or regression beta.\n"
            "- **Roll gaps, carry and costs are ignored.** Front-month series jump at "
            "the roll, and a JGB leg and a UST leg sit in different currencies.\n"
            "- **Z-scores do not tell you when it reverts.** A regime change can keep "
            "a ratio stretched for years.")


# ------------------------------------------------------------------------- Rates
def render_live_rates(ctx: Ctx):
    """Intraday rates come from futures: there is no free cash-bond feed."""
    st = _st()
    source = get_source(ctx.mode, max_age_seconds=ctx.max_age)
    labels = [lbl for lbl, inst in BY_LABEL.items() if inst.group == RATES]
    quotes = safe(source.quotes, labels, label="Live rates quotes") or {}
    if not quotes:
        st.info("No live rates futures. Start the feed, or pick Yahoo only in the "
                "sidebar for delayed ZT/ZN/ZB (Yahoo has no JGB or Bund future).")
        return
    hist = safe(load_prices, ctx.mode != "yahoo", label="Daily prices")
    cols = st.columns(len(quotes))
    for col, (lbl, q) in zip(cols, quotes.items()):
        delta = None
        if hist is not None and lbl in hist:
            s = hist[lbl].dropna()
            if len(s) > 1 and q.price:
                prev = s.iloc[-2] if s.index[-1].date() >= q.ts.date() else s.iloc[-1]
                delta = f"{(q.price / prev - 1) * 100:+.2f}%"
        col.metric(lbl, f"{q.price:,.3f}" if q.price else "n/a", delta)
        col.caption(f"{q.source}, {q.age_seconds:.0f}s old")
    st.caption(f"Futures prices, not yields. Refreshed {now_sgt():%H:%M:%S} SGT. "
               "Converting a futures price to a yield needs the cheapest-to-deliver "
               "bond and its conversion factor, so z-score the prices, or use the "
               "exchange's published implied yield.")


def render_rates(ctx: Ctx):
    st = _st()
    st.subheader("Live: rates futures")
    fragment = st.fragment(run_every=ctx.refresh)(lambda: render_live_rates(ctx))
    fragment()

    st.subheader("Daily: cash curves")
    ust2 = safe(load_fred, "DGS2", label="UST 2Y (FRED)")
    ust10 = safe(load_fred, "DGS10", label="UST 10Y (FRED)")
    if ust2 is not None and ust10 is not None:
        r = pd.concat([ust2.rename("UST2Y"), ust10.rename("UST10Y")], axis=1,
                      sort=True).dropna()
        r["2s10s (bp)"] = (r["UST10Y"] - r["UST2Y"]) * 100
        src = "FRED (New York close)"
    else:
        y = safe(load_yahoo_yields, label="Treasury yields (Yahoo fallback)")
        if y is None:
            return
        r = y.dropna()
        r["5s30s (bp)"] = (r["UST30Y"] - r["UST5Y"]) * 100
        src = "Yahoo fallback (FRED unreachable, so 2Y is unavailable)"

    jgb = safe(load_jgb10, label="JGB 10Y (Japan MOF)")
    if jgb is not None:
        r = r.join(jgb, how="left")
        r["JGB10Y"] = r["JGB10Y"].ffill()
        r["UST10-JGB10 (bp)"] = (r["UST10Y"] - r["JGB10Y"]) * 100
    r = r[r.index >= r.index.max() - pd.DateOffset(years=2)].dropna()
    if len(r) < 10:
        st.warning("Too few overlapping observations to show the curve.")
        return

    last, prev = r.iloc[-1], r.iloc[-6]
    cols = st.columns(len(r.columns))
    for c, name in zip(cols, r.columns):
        is_bp = "bp" in name
        delta = (last[name] - prev[name]) * (1 if is_bp else 100)
        c.metric(name, f"{last[name]:.1f}" if is_bp else f"{last[name]:.2f}%",
                 f"{delta:+.1f}bp 1W")
    st.caption(f"Source: {src}; JGB from Japan MOF (Tokyo close). "
               f"Last date {r.index[-1]:%d %b %Y}.")
    left, right = st.columns(2)
    left.line_chart(r[[c for c in r.columns if "bp" not in c]], height=320)
    right.line_chart(r[[c for c in r.columns if "bp" in c]], height=320)

    if "UST10-JGB10 (bp)" in r:
        st.metric("UST10-JGB10 z-score (63d)",
                  f"{zscore(r['UST10-JGB10 (bp)'], 63).iloc[-1]:+.2f}",
                  help="Daily cash spread only. The intraday version of this trade "
                       "lives in the futures block above.")

    with st.expander("Singapore: SGS benchmark yields (MAS)"):
        mas = safe(load_mas_rates, label="MAS domestic interest rates")
        if mas is None or mas.empty:
            st.caption("MAS API unavailable. The same data is published at "
                       "eservices.mas.gov.sg under SGS Prices and Yields.")
        else:
            fields = st.multiselect("Fields", list(mas.columns),
                                    default=list(mas.columns[:4]),
                                    help="Field names come straight from the MAS API, "
                                         "so nothing is silently mislabelled.")
            if fields:
                st.line_chart(mas[fields].tail(750), height=300)
                st.dataframe(mas[fields].tail(5).sort_index(ascending=False),
                             width="stretch")
            st.caption("MAS publishes SGS benchmark yields daily. SGS is the natural "
                       "local leg for a Singapore-based rates or FX-hedged idea.")


# ---------------------------------------------------------------------- Calendar
def render_calendar(ctx: Ctx):
    st = _st()
    cal = safe(load_calendar, label="Economic calendar (Forex Factory)")
    if cal is None or cal.empty:
        st.markdown("Open the calendar directly: "
                    "[Forex Factory](https://www.forexfactory.com/calendar) or "
                    "[Investing.com](https://www.investing.com/economic-calendar/).")
        return
    c1, c2, c3 = st.columns([2, 2, 1])
    countries = sorted(cal["country"].unique())
    default = [c for c in ["USD", "EUR", "JPY", "GBP", "AUD", "CAD", "CHF", "NZD", "SGD"]
               if c in countries]
    ccys = c1.multiselect("Currencies", countries, default=default)
    impacts = c2.multiselect("Impact", ["High", "Medium", "Low", "Holiday"],
                             default=["High", "Medium"])
    upcoming = c3.toggle("Upcoming only", value=True)
    view = cal[cal["country"].isin(ccys) & cal["impact"].isin(impacts)]
    if upcoming:
        view = view[view["SGT"] >= now_sgt() - pd.Timedelta(hours=1)]
    view = view.assign(Day=view["SGT"].dt.strftime("%a %d %b"),
                       Time=view["SGT"].dt.strftime("%H:%M"))
    st.dataframe(view[["Day", "Time", "country", "impact", "title", "forecast",
                       "previous"]], hide_index=True, width="stretch", height=560)
    st.caption("Times in SGT. Source: Forex Factory weekly JSON (unofficial, current "
               "week only).")


# -------------------------------------------------------------------------- News
def render_news(ctx: Ctx):
    st = _st()
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
            st.markdown(f"**{row['time']:%d %b %H:%M}** `{row['theme']}` "
                        f"[{row['title']}]({row['link']})")

    st.subheader("Central bank releases")
    items = list(CENTRAL_BANK_FEEDS.items())
    for chunk in (items[:3], items[3:]):
        cols = st.columns(3)
        for col, (name, url) in zip(cols, chunk):
            with col:
                st.markdown(f"**{name}**")
                cb = safe(load_cb, name, url, label=name)
                if cb is not None and not cb.empty:
                    for _, row in cb.sort_values("time", ascending=False,
                                                 na_position="last").head(5).iterrows():
                        when = f"{row['time']:%d %b} " if pd.notna(row["time"]) else ""
                        st.markdown(f"{when}[{row['title']}]({row['link']})")
    st.caption(f"MAS publishes no RSS feed, so its releases are not listed here: "
               f"[MAS news]({MAS_NEWS_URL}). The SGD & Asia theme above does cover "
               "MAS policy headlines.")


# ------------------------------------------------------------------------- shell
def render_dashboard():
    st = _st()
    st.set_page_config(page_title="FX & Macro Brief", page_icon="💱", layout="wide")
    st.title("FX & macro brief")
    st.caption(f"{now_sgt():%A %d %B %Y, %H:%M} SGT")

    with st.sidebar:
        st.subheader("Price source")
        ib_status = IBSource().status()
        mode_label = st.radio(
            "Live quotes from", ["Auto (IB, Yahoo fallback)", "IB only", "Yahoo only"],
            index=0 if ib_status.get("available") else 2,
            help="IB needs the feed running against an open IB Gateway or TWS. "
                 "Streamlit Cloud cannot reach a local gateway, so the public "
                 "deployment always runs on Yahoo.")
        mode = {"Auto (IB, Yahoo fallback)": "auto", "IB only": "ib",
                "Yahoo only": "yahoo"}[mode_label]
        max_age = st.slider("Treat an IB quote as stale after (seconds)", 15, 600, 120, 15)
        if ib_status.get("available"):
            mdt = MARKET_DATA_TYPES.get(ib_status.get("market_data_type"), "unknown")
            st.success(f"IB feed up: {ib_status['instruments']} instruments, {mdt} "
                       f"data, heartbeat {ib_status['heartbeat_age']:.0f}s ago")
        else:
            reason = ib_status.get("reason") or ib_status.get("message") or "no heartbeat"
            st.info(f"IB feed down ({reason}). Start it locally with: "
                    "python app.py feed --market-data-type 3")

        st.divider()
        refresh_label = st.selectbox("Auto-refresh prices",
                                     ["Off", "5 seconds", "15 seconds", "30 seconds",
                                      "1 minute", "5 minutes"], index=3)
        refresh = {"Off": None, "5 seconds": 5, "15 seconds": 15, "30 seconds": 30,
                   "1 minute": 60, "5 minutes": 300}[refresh_label]
        if st.button("Refresh all data now"):
            st.cache_data.clear()

        st.divider()
        st.subheader("Add TradingView data")
        st.caption("On TradingView, open a daily chart, choose Export chart data, and "
                   "upload the CSV here. It appears in the Mean reversion tab.")
        uploads = st.file_uploader("TradingView CSV exports", type="csv",
                                   accept_multiple_files=True)
        st.divider()
        st.caption("Yahoo quotes are delayed and one-sided. IB quotes carry bid and "
                   "ask. Check the Source and Age columns on the board.")

    ctx = Ctx(mode=mode, max_age=max_age, refresh=refresh, uploads=uploads or [])
    tabs = st.tabs(["Markets board", "Mean reversion", "Rates", "Calendar", "News"])

    def run(tab, render, fragment=False):
        """One tab failing must never stop the tabs after it from loading."""
        with tab:
            try:
                if fragment:
                    st.fragment(run_every=ctx.refresh)(lambda: render(ctx))()
                else:
                    render(ctx)
            except Exception as e:
                st.error(f"This tab hit an error: {type(e).__name__}: {e}")

    run(tabs[0], render_board, fragment=True)
    run(tabs[1], render_mean_reversion)
    run(tabs[2], render_rates)
    run(tabs[3], render_calendar)
    run(tabs[4], render_news)

    st.divider()
    st.caption("Built with Python and Streamlit. Live quotes from Interactive Brokers "
               "or Yahoo Finance. Rates from FRED, Japan MOF and MAS. Calendar from "
               "Forex Factory, headlines from Google News and central bank feeds. "
               "For education and research only; not investment advice.")


# =====================================================================================
# 6. IB FEED DAEMON  (local only: python app.py feed)
# =====================================================================================
# ib_async is imported inside these functions, never at module level, so the cloud
# deployment does not need it in requirements.txt.
#
# ib_insync was archived by its author on 14 March 2024 and is read-only. ib_async is
# the maintained successor and keeps the same API surface.

QUOTE_UPSERT = """
INSERT INTO quotes (label, local_symbol, bid, ask, last, ts)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(label) DO UPDATE SET
    local_symbol = excluded.local_symbol, bid = excluded.bid,
    ask = excluded.ask, last = excluded.last, ts = excluded.ts
"""
BAR_UPSERT = ("INSERT INTO bars (label, date, close) VALUES (?, ?, ?) "
              "ON CONFLICT(label, date) DO UPDATE SET close = excluded.close")


def resolve_contract(ib, inst: Instrument, log=print):
    """Turn a spec into one tradable contract.

    Futures specs carry no expiry, so the front month is resolved at runtime; a
    hardcoded month is wrong within a quarter. Continuous futures (ContFuture) are
    deliberately not used: IB supports them for historical data, not for streaming.
    """
    from ib_async import Contract
    details = ib.reqContractDetails(Contract(**inst.ib))
    if not details:
        log(f"  {inst.label}: no contract details. Check the symbol, the exchange "
            f"code and your market data entitlements.")
        return None
    if inst.ib["secType"] != "FUT":
        return details[0].contract
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    months = sorted((d.contract for d in details),
                    key=lambda c: (c.lastTradeDateOrContractMonth or "99999999").ljust(8, "0"))
    for c in months:
        if (c.lastTradeDateOrContractMonth or "").ljust(8, "0") >= today:
            log(f"  {inst.label}: front month {c.lastTradeDateOrContractMonth} "
                f"(conId {c.conId}, multiplier {c.multiplier})")
            return c
    return months[-1] if months else None


def what_to_show(inst: Instrument) -> str:
    """Spot FX has no consolidated tape, so TRADES returns nothing. Use MIDPOINT."""
    return "MIDPOINT" if inst.ib.get("secType") == "CASH" else "TRADES"


def run_feed(host: str, port: int, client_id: int, mdt: int, bar_refresh: int,
             bar_duration: str) -> int:
    import signal
    import time

    from ib_async import IB

    con = db_connect(DB_PATH, create=True)
    ib = IB()
    state = {"running": True, "contracts": {}, "by_conid": {}, "last_bars": 0.0}

    def heartbeat(message=""):
        con.execute(
            "INSERT INTO feed_status (id, heartbeat, connected, market_data_type, message) "
            "VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "heartbeat = excluded.heartbeat, connected = excluded.connected, "
            "market_data_type = excluded.market_data_type, message = excluded.message",
            (datetime.now(timezone.utc).isoformat(), int(ib.isConnected()), mdt, message))
        con.commit()

    def on_ticks(tickers):
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for t in tickers:
            label = state["by_conid"].get(t.contract.conId)
            if label:
                rows.append((label, t.contract.localSymbol, _clean(t.bid),
                             _clean(t.ask), _clean(t.last) or _clean(t.close), now))
        if rows:
            con.executemany(QUOTE_UPSERT, rows)
            con.commit()

    def refresh_bars():
        """Daily closes, so the scanner can use instruments Yahoo does not carry."""
        for inst in IB_INSTRUMENTS:
            c = state["contracts"].get(inst.label)
            if c is None:
                continue
            try:
                bars = ib.reqHistoricalData(c, endDateTime="", durationStr=bar_duration,
                                            barSizeSetting="1 day",
                                            whatToShow=what_to_show(inst),
                                            useRTH=True, formatDate=1)
            except Exception as e:
                print(f"  {inst.label}: history failed: {type(e).__name__}: {e}")
                continue
            rows = [(inst.label, str(b.date), float(b.close))
                    for b in (bars or []) if b.close and b.close > 0]
            if rows:
                con.executemany(BAR_UPSERT, rows)
                con.commit()
                print(f"  {inst.label}: {len(rows)} daily bars")
            ib.sleep(0.2)  # stay under IB's historical data pacing limits
        state["last_bars"] = time.time()

    def connect():
        ib.connect(host, port, clientId=client_id, timeout=15)
        ib.reqMarketDataType(mdt)
        ib.pendingTickersEvent += on_ticks
        state["contracts"], state["by_conid"] = {}, {}
        for inst in IB_INSTRUMENTS:
            try:
                c = resolve_contract(ib, inst)
            except Exception as e:  # one bad symbol must not kill the feed
                print(f"  {inst.label}: resolve failed: {type(e).__name__}: {e}")
                continue
            if c is None:
                continue
            state["contracts"][inst.label] = c
            state["by_conid"][c.conId] = inst.label
            ib.reqMktData(c, "", False, False)
        print(f"subscribed to {len(state['contracts'])} of {len(IB_INSTRUMENTS)} "
              f"instruments, {MARKET_DATA_TYPES.get(mdt, mdt)} data")
        refresh_bars()
        heartbeat("connected")

    def stop(*_):
        state["running"] = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    connect()
    print(f"feed running, writing to {DB_PATH}. Ctrl+C to stop.")
    while state["running"]:
        ib.sleep(1)  # pumps the asyncio loop, so ticks arrive
        if not ib.isConnected():
            heartbeat("disconnected, retrying")
            print("lost connection, reconnecting in 5s")
            ib.sleep(5)
            try:
                connect()
            except Exception as e:
                print(f"reconnect failed: {type(e).__name__}: {e}")
            continue
        if time.time() - state["last_bars"] > bar_refresh:
            refresh_bars()
        heartbeat("ok")
    heartbeat("stopped")
    ib.disconnect()
    con.close()
    print("stopped cleanly")
    return 0


def run_contracts(host: str, port: int, client_id: int) -> int:
    """Print what IB thinks each spec resolves to. Run after every futures roll."""
    from ib_async import IB, Contract
    ib = IB()
    ib.connect(host, port, clientId=client_id, timeout=15)
    print(f"{'label':22} {'conId':>10} {'localSymbol':14} {'expiry':10} "
          f"{'mult':>6} {'exch':10} ccy")
    print("-" * 86)
    for inst in IB_INSTRUMENTS:
        try:
            details = ib.reqContractDetails(Contract(**inst.ib))
        except Exception as e:
            print(f"{inst.label:22} ERROR {type(e).__name__}: {e}")
            continue
        if not details:
            print(f"{inst.label:22} (nothing returned) spec={inst.ib}")
            continue
        for d in details[:6]:
            c = d.contract
            print(f"{inst.label:22} {c.conId:>10} {str(c.localSymbol):14} "
                  f"{str(c.lastTradeDateOrContractMonth):10} "
                  f"{str(c.multiplier):>6} {c.exchange:10} {c.currency}")
        if len(details) > 6:
            print(f"{'':22} ... {len(details) - 6} more months")
        print(f"{'':22} hours: {details[0].tradingHours[:60]}")
        ib.sleep(0.1)
    ib.disconnect()
    return 0


# =====================================================================================
# 7. CLI AND DISPATCH
# =====================================================================================
def run_selftest() -> int:
    """Check the statistics and the quote plumbing. No network, no Streamlit."""
    fails = []

    def check(name, cond):
        print(f"  {'ok  ' if cond else 'FAIL'}  {name}")
        if not cond:
            fails.append(name)

    rng = np.random.default_rng(0)
    x, ar1 = 0.0, []
    for _ in range(1000):
        x = 0.85 * x + rng.normal()
        ar1.append(x)
    ar1 = np.array(ar1)
    walk = np.cumsum(np.random.default_rng(1).normal(size=1000))

    print("statistics")
    check("DF test rejects the unit root for a mean-reverting AR(1)",
          df_tstat(ar1) < DF_5PCT)
    check("DF test does not reject for a random walk", df_tstat(walk) > DF_5PCT)
    check("DF test returns NaN on too little data", np.isnan(df_tstat(np.arange(10.0))))
    # phi = 0.85 implies ln(2)/-ln(0.85) = 4.265 days
    check("half-life matches AR(1) theory", abs(half_life(ar1) - 4.265) < 1.0)
    hl_walk = half_life(walk)
    check("half-life is NaN or very long for a random walk",
          np.isnan(hl_walk) or hl_walk > 100)
    check("Spearman is 1 for a monotone relationship",
          abs(spearman(pd.Series([1, 2, 3, 4, 5]), pd.Series([10, 20, 30, 40, 50])) - 1)
          < 1e-9)

    print("pair scan")
    idx = pd.bdate_range("2022-01-03", periods=600)
    r2 = np.random.default_rng(7)
    base = 100 * np.exp(np.cumsum(r2.normal(0, 0.01, 600)))
    noise = np.exp(np.cumsum(r2.normal(0, 0.003, 600)) * 0.2)
    prices = pd.DataFrame({"A": base, "B": base / noise, "C": base * 1.5}, index=idx)
    scan = scan_pairs(prices, 63)
    check("scan returns the expected columns",
          set(scan.columns) >= {"A", "B", "Z-score", "DF t", "Half-life (days)", "R²"})
    check("scan is sorted by |z| descending",
          bool((np.diff(scan["Z-score"].abs().to_numpy()) <= 1e-9).all()))
    check("R² stays within [0, 1]",
          bool((scan["R²"] >= 0).all() and (scan["R²"] <= 1).all()))
    check("scan is empty when history is too short",
          scan_pairs(prices.iloc[:40], 63).empty)

    print("quotes and board")
    q = Quote("EUR/USD", pd.Timestamp.now("UTC").to_pydatetime(), 1.08, 1.0801,
              float("nan"), "IB")
    check("NaN last from IB becomes None", q.last is None)
    check("price falls back to mid", abs(q.price - 1.08005) < 1e-9)
    check("spread in bp is computed off mid",
          abs(q.spread_bp - (0.0001 / 1.08005 * 10_000)) < 1e-6)
    daily = pd.DataFrame({"Gold": np.linspace(1800, 2000, 400)},
                         index=pd.bdate_range("2020-01-01", periods=400))
    gq = Quote("Gold", pd.Timestamp.now("UTC").to_pydatetime(), 2500.0, 2500.4,
               None, "IB")
    row = build_board(daily, {"Gold": gq}, min_history=100).set_index("Instrument").loc["Gold"]
    check("board prefers the live mid over the daily close",
          abs(row["Last"] - 2500.2) < 1e-9)
    check("board reports the source", row["Source"] == "IB")
    row2 = build_board(daily, {}, min_history=100).set_index("Instrument").loc["Gold"]
    check("board falls back to the close with no quotes",
          abs(row2["Last"] - 2000.0) < 1e-9 and row2["Source"] == "daily close")

    print("registry")
    check("every instrument has a Yahoo ticker or an IB spec",
          all(i.yahoo or i.ib for i in INSTRUMENTS))
    check("labels are unique", len(BY_LABEL) == len(INSTRUMENTS))
    check("every preset names real instruments",
          all(a in BY_LABEL and b in BY_LABEL for a, b in PRESETS.values()))
    check("every group is declared", {i.group for i in INSTRUMENTS} <= set(GROUPS))
    check("every central bank feed is an https URL",
          all(u.startswith("https://") for u in CENTRAL_BANK_FEEDS.values()))
    check("no feed points at a known-HTML MAS path",
          not any("mas.gov.sg/rss" in u for u in CENTRAL_BANK_FEEDS.values()))

    print(f"\n{len(fails)} failed" if fails else "\nSELFTEST PASSED")
    return 1 if fails else 0


USAGE = """fx-macro-brief, single file.

  python -m streamlit run app.py             the dashboard
  python app.py selftest                     check the statistics, no network
  python app.py contracts [--port 4002]      what IB thinks each contract is
  python app.py feed [options]               live IB quotes into data/quotes.db

feed and contracts options
  --host HOST               default 127.0.0.1
  --port PORT               4002 Gateway paper, 4001 Gateway live,
                            7497 TWS paper, 7496 TWS live (default 4002)
  --client-id N             must be unused by any other program (default 11)
  --market-data-type N      1 live, 2 frozen, 3 delayed (free), 4 delayed frozen
                            (default 3)
  --bar-refresh SECONDS     daily-bar refresh interval (default 3600)
  --bar-duration STR        history to request, IB syntax (default "5 Y")

feed and contracts need `pip install ib_async` and a running IB Gateway or TWS with
ActiveX and Socket Clients enabled and 127.0.0.1 in Trusted IPs.
"""


def main(argv: list[str]) -> int:
    import argparse
    cmd = argv[0]
    p = argparse.ArgumentParser(prog=f"app.py {cmd}", add_help=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4002)
    p.add_argument("--client-id", type=int, default=11 if cmd == "feed" else 99)
    p.add_argument("--market-data-type", type=int, default=3, choices=[1, 2, 3, 4])
    p.add_argument("--bar-refresh", type=int, default=3600)
    p.add_argument("--bar-duration", default="5 Y")
    a = p.parse_args(argv[1:])

    if cmd == "selftest":
        return run_selftest()
    if cmd == "contracts":
        return run_contracts(a.host, a.port, a.client_id)
    if cmd == "feed":
        return run_feed(a.host, a.port, a.client_id, a.market_data_type,
                        a.bar_refresh, a.bar_duration)
    print(USAGE)
    return 2


def _in_streamlit() -> bool:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        return get_script_run_ctx() is not None
    except Exception:
        return True  # if it cannot be determined, render rather than print usage


_COMMANDS = {"feed", "contracts", "selftest", "help", "--help", "-h"}

if len(sys.argv) > 1 and sys.argv[1] in _COMMANDS:
    sys.exit(main(sys.argv[1:]))
elif _in_streamlit():
    render_dashboard()
else:
    print(USAGE)
