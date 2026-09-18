"""Every network call the dashboard makes, wrapped in a Streamlit cache.

Rules of thumb used here:
  * one function per upstream resource, so a single broken feed cannot take
    down the others;
  * cache TTLs match how often the source actually changes (quotes: seconds,
    FRED: hours, JGB history: hours);
  * loaders return plain pandas objects so nothing Streamlit-specific leaks
    into the analytics.
"""
from __future__ import annotations

import io
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import pandas as pd
import requests
import streamlit as st
import yfinance as yf

from config import (
    CENTRAL_BANK_FEEDS, FRED_CSV, HEADERS, LABEL, MOF_CUR, MOF_HIST, SGT, TICKERS,
)

_SESSION = requests.Session()
_SESSION.headers.update(HEADERS)


def get(url: str, timeout: int = 25, **kw) -> requests.Response:
    """One place to do HTTP, so headers and error handling stay consistent."""
    r = _SESSION.get(url, timeout=timeout, **kw)
    r.raise_for_status()
    return r


def now_sgt() -> pd.Timestamp:
    return pd.Timestamp.now(tz=SGT)


def safe(fn, *args, label: str = "", quiet: bool = False):
    """Run a loader; surface the real error instead of crashing the page."""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001 - a dashboard should degrade, not die
        if not quiet:
            st.warning(f"{label or fn.__name__} unavailable: {type(e).__name__}: {e}")
        return None


# ===================================================================== prices
def _closes(df: pd.DataFrame) -> pd.DataFrame:
    close = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]]
    return close.rename(columns=LABEL).sort_index()


@st.cache_data(ttl=30 * 60, show_spinner="Pulling daily price history")
def load_daily() -> pd.DataFrame:
    """Full daily history for every instrument. Slow, so cached for 30 minutes."""
    raw = yf.download(list(TICKERS), period="max", interval="1d",
                      progress=False, auto_adjust=False, threads=True)
    df = _closes(raw)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df.ffill(limit=3)


@st.cache_data(ttl=45, show_spinner=False)
def load_intraday() -> pd.DataFrame:
    """Recent 5-minute bars, used for the live 'Last' price and the intraday sparkline.

    Two days is enough for a last price and an intraday shape, and downloads in a
    fraction of the time the old five-day pull took.
    """
    raw = yf.download(list(TICKERS), period="2d", interval="5m",
                      progress=False, auto_adjust=False, threads=True)
    df = _closes(raw)
    idx = df.index.tz_localize("UTC") if df.index.tz is None else df.index
    df.index = idx.tz_convert(SGT)
    return df


# ====================================================================== FRED
@st.cache_data(ttl=3 * 3600, show_spinner=False)
def load_fred(series_id: str) -> pd.Series:
    """One FRED series as a dated float series. FRED rejects some custom user agents,
    so this deliberately uses a bare request rather than the shared session."""
    raw = requests.get(FRED_CSV.format(series_id), timeout=25).text
    if not raw.startswith("observation_date"):
        raise ValueError(f"unexpected FRED response for {series_id}: {raw[:80]!r}")
    df = pd.read_csv(io.StringIO(raw), na_values=".")
    df.columns = ["Date", series_id]
    return df.assign(Date=pd.to_datetime(df["Date"])).set_index("Date")[series_id].dropna()


@st.cache_data(ttl=3 * 3600, show_spinner="Pulling FRED series")
def load_fred_many(series: tuple[tuple[str, str], ...]) -> pd.DataFrame:
    """Several FRED series at once, as columns named by their label.

    Missing series are skipped rather than failing the whole frame, so one retired
    series id cannot blank out the tab.
    """
    cols = {}
    for label, sid in series:
        try:
            cols[label] = load_fred(sid)
        except Exception:  # noqa: BLE001
            continue
    if not cols:
        raise RuntimeError("no FRED series could be loaded")
    return pd.concat(cols, axis=1, sort=True).sort_index()


@st.cache_data(ttl=10 * 60, show_spinner=False)
def load_live_yields() -> pd.Series:
    """Today's Treasury yields from Yahoo, in percent.

    FRED's daily curve is a New York close and lands a day late, so these fill the
    gap between the last FRED print and right now.

    Deliberately no ^IRX: the 13-week index is quoted on a discount basis, so
    differencing it against FRED's bond-equivalent DGS3MO shows a ~15bp gap that
    is pure convention rather than a move.
    """
    names = {"^FVX": "UST 5Y", "^TNX": "UST 10Y", "^TYX": "UST 30Y"}
    raw = yf.download(list(names), period="5d", interval="1d",
                      progress=False, auto_adjust=False, threads=True)["Close"]
    last = raw.dropna(how="all").iloc[-1].rename(names)
    return last.reindex([n for n in names.values() if n in set(last.index)])


@st.cache_data(ttl=6 * 3600, show_spinner="Pulling Treasury yields from Yahoo")
def load_yahoo_yields() -> pd.DataFrame:
    """Fallback history when FRED is unreachable: 5Y, 10Y, 30Y, in percent."""
    df = yf.download(["^FVX", "^TNX", "^TYX"], period="5y",
                     progress=False, auto_adjust=False, threads=True)["Close"]
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df.rename(columns={"^FVX": "UST5Y", "^TNX": "UST10Y", "^TYX": "UST30Y"})


# ======================================================================= JGB
def _read_mof(url: str) -> pd.Series:
    raw = get(url).content.decode("shift_jis", errors="ignore")
    df = pd.read_csv(io.StringIO(raw), skiprows=1, na_values="-")
    df["Date"] = pd.to_datetime(df["Date"], format="%Y/%m/%d", errors="coerce")
    return df.dropna(subset=["Date"]).set_index("Date")["10Y"].astype(float)


@st.cache_data(ttl=6 * 3600, show_spinner="Pulling JGB yields")
def load_jgb10() -> pd.Series:
    s = pd.concat([_read_mof(MOF_HIST), _read_mof(MOF_CUR)])
    return s[~s.index.duplicated(keep="last")].sort_index().dropna().rename("JGB10Y")


# ====================================================================== news
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


@st.cache_data(ttl=10 * 60, show_spinner=False)
def load_news(theme: str, query: str) -> pd.DataFrame:
    from urllib.parse import quote_plus
    url = (f"https://news.google.com/rss/search?q={quote_plus(query)}+when:3d"
           "&hl=en-SG&gl=SG&ceid=SG:en")
    df = _parse_feed(get(url).content, "Google News")
    return df.assign(theme=theme) if not df.empty else df


@st.cache_data(ttl=20 * 60, show_spinner=False)
def load_cb(name: str, url: str) -> pd.DataFrame:
    return _parse_feed(get(url).content, name)


def cb_feeds() -> list[tuple[str, str]]:
    return list(CENTRAL_BANK_FEEDS.items())


# ============================================================ user CSV upload
def parse_tradingview_csv(file, name: str) -> pd.Series:
    """TradingView 'Export chart data' CSV: a time column (UNIX seconds or ISO)
    and a close column."""
    df = pd.read_csv(file)
    cols = {c.lower().strip(): c for c in df.columns}
    if "time" not in cols or "close" not in cols:
        raise ValueError(f"expected 'time' and 'close' columns, found {list(df.columns)}")
    t = df[cols["time"]]
    utc = pd.DatetimeIndex(pd.to_datetime(t, unit="s", utc=True)
                           if pd.api.types.is_numeric_dtype(t)
                           else pd.to_datetime(t, utc=True))
    # Daily bars can be stamped at the session open (e.g. 22:00 UTC the previous day
    # for CME futures). Shifting 12 hours before taking the date lands every session
    # on its trading date.
    dates = (utc + pd.Timedelta(hours=12)).tz_convert(None).normalize()
    s = pd.Series(df[cols["close"]].astype(float).values, index=dates, name=name)
    return s[~s.index.duplicated(keep="last")].sort_index()
