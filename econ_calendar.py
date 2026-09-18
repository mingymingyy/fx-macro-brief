"""The economic calendar, rebuilt around two feeds instead of one.

Why two:

  Forex Factory's weekly JSON is the only free feed that rates each event
  High / Medium / Low, and its event names are the ones traders use. But it
  covers *this calendar week only* and never carries the released number, so
  by Friday afternoon the tab was empty and it could never show you what a
  print actually came in at.

  Nasdaq's economic-events endpoint is per-date, so it can be walked forward
  as far as you like, and it carries `actual` alongside consensus and
  previous. What it lacks is an importance rating.

So: Nasdaq is the spine (dates, actuals, forward coverage), Forex Factory is
laid over the current week for its impact ratings and better event names, and
anything Forex Factory cannot rate falls back to a keyword classifier. If
either feed dies the other still renders a usable calendar.
"""
from __future__ import annotations

import datetime as dt
import re
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import streamlit as st

from config import ET, FF_CALENDAR, NASDAQ_CALENDAR, SGT
from sources import get

IMPACTS = ["High", "Medium", "Low", "Holiday"]

# Nasdaq reports countries; an FX dashboard wants the currency. Euro-area members
# all map to EUR but keep their own name in the "Region" column.
COUNTRY_CCY = {
    "United States": "USD", "Euro Zone": "EUR", "Germany": "EUR", "France": "EUR",
    "Italy": "EUR", "Spain": "EUR", "Netherlands": "EUR", "Ireland": "EUR",
    "Portugal": "EUR", "Greece": "EUR", "Belgium": "EUR", "Austria": "EUR",
    "Finland": "EUR", "Slovakia": "EUR", "Slovenia": "EUR", "Luxembourg": "EUR",
    "Cyprus": "EUR", "Estonia": "EUR", "Latvia": "EUR", "Lithuania": "EUR",
    "Malta": "EUR", "Croatia": "EUR",
    "United Kingdom": "GBP", "Japan": "JPY", "Australia": "AUD",
    "New Zealand": "NZD", "Canada": "CAD", "Switzerland": "CHF",
    "China": "CNY", "Hong Kong": "HKD", "Singapore": "SGD", "South Korea": "KRW",
    "Taiwan": "TWD", "India": "INR", "Indonesia": "IDR", "Thailand": "THB",
    "Malaysia": "MYR", "Philippines": "PHP", "Vietnam": "VND",
    "Norway": "NOK", "Sweden": "SEK", "Denmark": "DKK", "Iceland": "ISK",
    "Poland": "PLN", "Czech Republic": "CZK", "Hungary": "HUF", "Romania": "RON",
    "Russia": "RUB", "Turkey": "TRY", "Israel": "ILS", "Saudi Arabia": "SAR",
    "South Africa": "ZAR", "Egypt": "EGP", "Nigeria": "NGN",
    "Brazil": "BRL", "Mexico": "MXN", "Chile": "CLP", "Colombia": "COP",
    "Argentina": "ARS", "Peru": "PEN",
}
# Forex Factory uses currency codes already, but tags global events "All".
FF_REGION = {v: k for k, v in COUNTRY_CCY.items() if v not in {"EUR"}}
FF_REGION["EUR"] = "Euro Zone"
FF_REGION["All"] = "Global"

# Keyword classifier, used for any event Forex Factory does not rate.
HIGH_WORDS = (
    "interest rate decision", "rate decision", "rate statement", "policy rate",
    "cash rate", "bank rate", "fomc", "federal funds", "monetary policy",
    "powell", "lagarde", "ueda", "bailey", "macklem", "bullock",
    "non-farm", "nonfarm", "payroll", "employment change", "unemployment rate",
    "employment report", "cpi", "consumer price", "core inflation", "inflation rate",
    "pce price", "gdp", "retail sales", "ism ", "tankan",
)
MEDIUM_WORDS = (
    "pmi", "ppi", "producer price", "trade balance", "industrial production",
    "durable goods", "factory orders", "confidence", "sentiment", "ifo", "zew",
    "jobless claims", "current account", "wage", "earnings", "housing starts",
    "building permits", "home sales", "capacity utilization", "money supply",
    "business climate", "crude oil inventories", "budget balance", "speaks",
    "press conference", "minutes", "auction",
)
# Phrases that contain a higher-tier keyword but are not themselves that tier.
# Checked before the tiers above, most specific first.
FORCE_LOW = ("bill auction", "note auction", "bond auction", "btf auction",
             "bubill auction", "index-linked", "car registration")
FORCE_MEDIUM = ("retail sales ytd", "card retail sales", "cpi expectations",
                "gdp expectations", "gdp deflator", "core cpi ytd",
                "corporate goods price")
HOLIDAY_WORDS = ("holiday", "bank holiday", "market closed")

_STOP = {
    "mm", "yy", "qq", "m", "y", "q", "flash", "prelim", "preliminary", "final",
    "revised", "revision", "sa", "nsa", "annualized", "annualised", "index",
    "rate", "the", "of", "and", "s", "p", "adv", "advance", "yoy", "mom", "qoq",
}


def _tokens(title: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    return frozenset(w for w in words if w not in _STOP) or frozenset(words)


def _similar(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def classify(title: str) -> str:
    t = (title or "").lower()
    if any(w in t for w in HOLIDAY_WORDS):
        return "Holiday"
    if any(w in t for w in FORCE_LOW):
        return "Low"
    if any(w in t for w in FORCE_MEDIUM):
        return "Medium"
    if any(w in t for w in HIGH_WORDS):
        return "High"
    if any(w in t for w in MEDIUM_WORDS):
        return "Medium"
    return "Low"


def _clean(v) -> str:
    """Feed values arrive as '', ' ', '&nbsp;' or a real figure like '-0.2%'."""
    if v is None:
        return ""
    s = str(v).replace("&nbsp;", " ").replace("\xa0", " ").strip()
    return "" if s in {"", "-", "--", "n/a", "N/A"} else s


_SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}


def to_number(v: str) -> float | None:
    """'-0.2%' -> -0.2, '3.786B' -> 3.786e9, '196K' -> 196000. None if not numeric."""
    s = _clean(v).replace(",", "").replace("%", "").replace("$", "").replace("€", "")
    s = s.replace("£", "").replace("¥", "").strip()
    if not s:
        return None
    mult = 1.0
    if s[-1].lower() in _SUFFIX:
        mult, s = _SUFFIX[s[-1].lower()], s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def _surprise(actual: str, forecast: str) -> str:
    a, f = to_number(actual), to_number(forecast)
    if a is None or f is None:
        return ""
    if abs(a - f) < 1e-12:
        return "= in line"
    return "▲ above" if a > f else "▼ below"


# ============================================================== Forex Factory
@st.cache_data(ttl=30 * 60, show_spinner=False)
def load_ff_week() -> pd.DataFrame:
    """This calendar week from Forex Factory: impact ratings and trader-facing names.

    Forex Factory rate-limits this file aggressively (HTTP 429 from shared cloud
    IPs is routine). Returning an empty frame rather than raising means the empty
    result gets cached too, so a throttled response costs one request per half
    hour instead of one per rerun -- and the calendar still renders from Nasdaq.
    """
    try:
        rows = get(FF_CALENDAR).json()
    except Exception:  # noqa: BLE001
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if df.empty or "date" not in df:
        return pd.DataFrame()
    when = pd.to_datetime(df["date"], utc=True, format="ISO8601")
    out = pd.DataFrame({
        "When": when.dt.tz_convert(SGT),
        "ET date": when.dt.tz_convert(ET).dt.date,
        "Ccy": df["country"].astype(str),
        "Event": df["title"].astype(str).str.strip(),
        "Impact": df["impact"].astype(str).str.title(),
        "Forecast": df["forecast"].map(_clean),
        "Previous": df["previous"].map(_clean),
    })
    out["Region"] = out["Ccy"].map(FF_REGION).fillna(out["Ccy"])
    return out.sort_values("When").reset_index(drop=True)


# ==================================================================== Nasdaq
@st.cache_data(ttl=15 * 60, show_spinner=False)
def _nasdaq_day(day: str) -> list[dict]:
    """One calendar date from Nasdaq. Cached per date so a two-week window costs
    at most one request per day, not one per rerun."""
    payload = get(NASDAQ_CALENDAR.format(date=day), timeout=20).json()
    return ((payload or {}).get("data") or {}).get("rows") or []


# Nasdaq's endpoint is "as of" a date: ?date=D returns the events of the New York
# day BEFORE D, timed on a New York clock. Checked against releases whose dates are
# not in doubt -- the September 2026 FOMC decision (actual 4.00%) comes back under
# ?date=2026-09-17 but the FOMC announced on Wednesday the 16th, and the jobless
# claims print for the week ending 12 September comes back under ?date=2026-09-18
# having been released on Thursday the 17th. Taking the label at face value pushed
# the whole calendar a day late, which is what filled Saturdays with Friday's data.
NASDAQ_DATE_OFFSET = dt.timedelta(days=-1)


def _nasdaq_rows(day: dt.date) -> pd.DataFrame:
    try:
        rows = _nasdaq_day(day.isoformat())
    except Exception:  # noqa: BLE001 - one bad date must not sink the window
        return pd.DataFrame()
    et_day = day + NASDAQ_DATE_OFFSET
    recs = []
    for r in rows:
        country = _clean(r.get("country"))
        gmt = _clean(r.get("gmt"))
        all_day = not re.fullmatch(r"\d{1,2}:\d{2}", gmt)
        if all_day:
            # No release time to convert. Anchor it to the end of that day in
            # Singapore so it keeps the right date and sorts after the timed events.
            when = pd.Timestamp(f"{et_day} 23:59", tz=SGT)
        else:
            try:
                when = pd.Timestamp(f"{et_day} {gmt}", tz=ET).tz_convert(SGT)
            except Exception:  # noqa: BLE001 - DST-ambiguous or malformed time
                when = pd.Timestamp(f"{et_day} 23:59", tz=SGT)
                all_day = True
        recs.append({
            "When": when, "ET date": when.tz_convert(ET).date(), "All day": all_day,
            "Ccy": COUNTRY_CCY.get(country, country[:3].upper() if country else "?"),
            "Region": country or "Global",
            "Event": _clean(r.get("eventName")),
            "Actual": _clean(r.get("actual")),
            "Forecast": _clean(r.get("consensus")),
            "Previous": _clean(r.get("previous")),
        })
    return pd.DataFrame(recs)


@st.cache_data(ttl=15 * 60, show_spinner="Pulling economic calendar")
def load_nasdaq_window(start: dt.date, end: dt.date) -> pd.DataFrame:
    """Every event between `start` and `end` inclusive, both real New York dates.

    The query dates are shifted the other way by NASDAQ_DATE_OFFSET so that the
    window we ask for is the window we get back.
    """
    first, last = start - NASDAQ_DATE_OFFSET, end - NASDAQ_DATE_OFFSET
    days = [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        frames = [f for f in pool.map(_nasdaq_rows, days) if not f.empty]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values("When").reset_index(drop=True)


# ===================================================================== merge
def _attach_ff(nas: pd.DataFrame, ff: pd.DataFrame) -> pd.DataFrame:
    """Give each Nasdaq row Forex Factory's impact rating and name where we can
    confidently match the two.

    Matching is on currency first, then event-name overlap, with the `previous`
    figure breaking ties -- that is what separates a 'Retail Sales m/m' from a
    'Retail Sales y/y' printed at the same minute under the same name. The day
    window is deliberately +/- 1: the two feeds occasionally disagree by a
    calendar day on the same release, and a strict same-day join turns that
    disagreement into a duplicate row.
    """
    nas = nas.copy()
    nas["Impact"] = ""
    if ff.empty:
        nas["Impact"] = nas["Event"].map(classify)
        return nas

    ff = ff.copy()
    ff["_tok"] = ff["Event"].map(_tokens)
    ff["_used"] = False
    ff_by_ccy: dict[str, list[int]] = {}
    for i, row in ff.iterrows():
        ff_by_ccy.setdefault(row["Ccy"], []).append(i)

    nas["_tok"] = nas["Event"].map(_tokens)
    nas_by_ccy: dict[str, list[int]] = {}
    for j, row in nas.iterrows():
        nas_by_ccy.setdefault(row["Ccy"], []).append(j)

    def near(d1, d2) -> bool:
        return abs((d1 - d2).days) <= 1

    for j, row in nas.iterrows():
        tok = row["_tok"]
        best, best_score = None, 0.0
        for i in ff_by_ccy.get(row["Ccy"], []):
            if ff.at[i, "_used"] or not near(row["ET date"], ff.at[i, "ET date"]):
                continue
            score = _similar(tok, ff.at[i, "_tok"])
            if score < 0.45:
                continue
            if row["ET date"] == ff.at[i, "ET date"]:
                score += 0.3
            if row["Previous"] and ff.at[i, "Previous"] == row["Previous"]:
                score += 0.5  # same prior figure: almost certainly the same release
            if score > best_score:
                best, best_score = i, score
        if best is not None:
            ff.at[best, "_used"] = True
            nas.at[j, "Impact"] = ff.at[best, "Impact"]
            nas.at[j, "Event"] = ff.at[best, "Event"]  # Forex Factory names read better
            if not nas.at[j, "Forecast"]:
                nas.at[j, "Forecast"] = ff.at[best, "Forecast"]

    nas["Impact"] = nas["Impact"].where(nas["Impact"] != "", nas["Event"].map(classify))

    # Anything Forex Factory rates High or Medium that Nasdaq does not list at all
    # is worth keeping. Anything that merely failed the one-to-one match above but
    # clearly describes an event already on the board is dropped, so the same
    # release never appears twice.
    keep = []
    for i, row in ff[~ff["_used"]].iterrows():
        if row["Impact"] not in ("High", "Medium", "Holiday"):
            continue
        dupe = any(
            near(row["ET date"], nas.at[j, "ET date"]) and _similar(row["_tok"], nas.at[j, "_tok"]) >= 0.6
            for j in nas_by_ccy.get(row["Ccy"], [])
        )
        if not dupe:
            keep.append(i)
    if keep:
        extra = ff.loc[keep].drop(columns=["_used"]).assign(Actual="", **{"All day": False})
        nas = pd.concat([nas, extra], ignore_index=True)
    return nas.drop(columns=["_tok"])


@st.cache_data(ttl=15 * 60, show_spinner=False)
def load_calendar(days_back: int = 3, days_forward: int = 14) -> pd.DataFrame:
    """The merged calendar: one row per event, with actual, forecast, previous,
    an impact rating and a surprise flag, in Singapore time."""
    today = pd.Timestamp.now(tz=ET).date()
    start, end = today - dt.timedelta(days=days_back), today + dt.timedelta(days=days_forward)

    try:
        nas = load_nasdaq_window(start, end)
    except Exception:  # noqa: BLE001
        nas = pd.DataFrame()
    try:
        ff = load_ff_week()
    except Exception:  # noqa: BLE001
        ff = pd.DataFrame()

    if nas.empty and ff.empty:
        return pd.DataFrame()

    if nas.empty:  # Nasdaq down: fall back to Forex Factory alone
        cal = ff.assign(Actual="", **{"All day": False})
    else:
        cal = _attach_ff(nas, ff)

    cal = cal[cal["Event"].astype(str).str.len() > 0].copy()
    for col in ("Actual", "Forecast", "Previous"):
        cal[col] = cal[col].fillna("") if col in cal else ""
    cal["Surprise"] = [_surprise(a, f) for a, f in zip(cal["Actual"], cal["Forecast"])]
    cal["Day"] = cal["When"].dt.strftime("%a %d %b")
    cal["Time"] = cal.apply(lambda r: "all day" if r.get("All day") else f"{r['When']:%H:%M}", axis=1)
    cal["Date"] = cal["When"].dt.date
    cal["Impact"] = pd.Categorical(cal["Impact"], categories=IMPACTS, ordered=True)

    cal = cal.drop_duplicates(subset=["Date", "Ccy", "Event", "Previous"])
    order = ["When", "Date", "Day", "Time", "Ccy", "Region", "Impact", "Event",
             "Actual", "Forecast", "Previous", "Surprise"]
    return cal[order].sort_values(["When", "Impact"]).reset_index(drop=True)


def next_high_impact(cal: pd.DataFrame, now: pd.Timestamp, ccys: list[str] | None = None) -> pd.Series | None:
    """The next High-impact event still ahead of us, for the countdown banner."""
    if cal.empty:
        return None
    fwd = cal[(cal["When"] >= now) & (cal["Impact"] == "High")]
    if ccys:
        fwd = fwd[fwd["Ccy"].isin(ccys)]
    return None if fwd.empty else fwd.iloc[0]
