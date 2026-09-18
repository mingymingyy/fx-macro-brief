# FX & Macro Brief

A single-page Streamlit dashboard for G10 FX, commodities, rates and macro, in
Singapore time.

**Live app:** https://mingymacrobrief.streamlit.app/

```bash
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

## Tabs

| Tab | What it shows |
| --- | --- |
| **Markets board** | FX, commodities and risk proxies with a live price, 1D/1W/1M/3M/YTD moves, a z-score of today's move, 3M and 52-week range position, realised vol and a 3-month sparkline. Flags anything that moved more than 2σ. |
| **Mean reversion** | Ranks every pair of instruments by how stretched its log price ratio is, with an ADF test and a half-life. Click a row to open the inspector: a plain-English verdict, the ratio against its ±2σ band, the z-score history and a rolling correlation. |
| **Rates** | The US Treasury curve now versus a month and a year ago, live yields against the last official close, 2s10s / 3M10Y / 5s30s, the UST–JGB spread, breakevens, 10-year real yields and policy rates. |
| **Macro data** | The latest released prints — CPI, core PCE, payrolls, unemployment, retail sales, GDP, the Fed balance sheet — with the prior reading, the change and a trend sparkline. |
| **Calendar** | A rolling economic calendar, roughly three days back to two weeks ahead, grouped by day, with actual against forecast and previous, an impact rating, and a countdown to the next high-impact release. |
| **News** | Themed Google News headlines plus the latest releases from eight central banks. |

## Where the numbers come from

| Source | Used for | Notes |
| --- | --- | --- |
| Yahoo Finance (`yfinance`) | Prices, live yields | Unofficial and sometimes delayed; the board shows a per-instrument timestamp. |
| FRED | Treasury curve, breakevens, policy rates, all macro series | Official, but published at a New York close, so the Rates tab also shows a live Yahoo print. |
| Japan MOF | JGB 10-year | Tokyo close. |
| Nasdaq economic events | Calendar dates, actual / consensus / previous | Unofficial. Per-date, which is what gives the calendar its forward coverage and its released figures. |
| Forex Factory weekly JSON | Calendar impact ratings, event names | Unofficial, current week only, and rate-limited from shared IPs. Treated as optional enrichment. |
| Google News RSS, central bank RSS | Headlines | |

Every loader is wrapped so that a feed going down degrades that one panel rather
than taking down the page.

## How the calendar works

The old version read a single feed, Forex Factory's weekly JSON. That feed covers
the current calendar week only and carries no released figure, so the tab emptied
out by Friday afternoon and could never tell you what a print actually came in at.
It also returns HTTP 429 often from shared cloud IPs.

The calendar now merges two feeds:

- **Nasdaq** is the spine. It is queried one date at a time, so the window can run
  from a few days back to two weeks ahead, and it carries `actual` alongside
  consensus and previous.
- **Forex Factory** is laid over the current week purely for its High / Medium /
  Low ratings and its event names.

Matching the two is the interesting part. Rows are matched on currency, then on
event-name token overlap, with the `previous` figure breaking ties — that is what
separates a "Retail Sales m/m" from a "Retail Sales y/y" printed at the same
minute under the same name. The day window is ±1 because the feeds occasionally
disagree by a calendar day on the same release.

Anything Forex Factory cannot rate falls back to a keyword classifier in
`econ_calendar.py` (`HIGH_WORDS`, `MEDIUM_WORDS`, `FORCE_LOW`, `FORCE_MEDIUM`) —
worth tuning to taste. If either feed is unreachable the other still renders a
usable calendar.

## Layout

```
app.py             page shell, sidebar and the six tab renderers
config.py          instruments, feeds, FRED series ids, windows -- edit this first
sources.py         every network call, each behind a Streamlit cache
analytics.py       pure pandas/numpy: the board, ADF, half-life, pair scan
econ_calendar.py   the two-feed economic calendar and its merge logic
```

To add an instrument, add one line to `INSTRUMENTS` in `config.py`. To add a
macro series, add a row to `MACRO_SERIES` with its FRED id and a transform
(`yoy`, `mom_chg` or `level`). Nothing else needs to change.

## Caching

TTLs are set to how often each source actually moves: 45 seconds for intraday
quotes, 15 minutes for the calendar and news, 30 minutes for daily price history,
three hours for FRED, six hours for JGB history. The sidebar's **Refresh all data
now** clears every cache.

## Adding your own data

The Mean reversion tab accepts TradingView chart exports. On TradingView open a
daily chart, choose *Export chart data*, and upload the CSV in the sidebar; it
needs a `time` column (UNIX seconds or ISO) and a `close` column. Uploaded series
join the scanner and the inspector alongside the built-in instruments.

## Caveats

- Futures series (gold, silver, copper, WTI, Brent, gas) are front-month and jump
  at contract rolls.
- The pair scanner tests hundreds of combinations, so some will look stretched and
  pass an ADF test by chance. A ratio is not a hedge ratio.
- Calendar impact ratings outside the current week are keyword-derived — a hint,
  not gospel.
- Both calendar feeds are unofficial and can lag a release by a few minutes.

For education and research only. Not investment advice.
