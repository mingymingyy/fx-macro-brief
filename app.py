"""
FX & Macro Daily Brief
======================
Streamlit dashboard for G10 FX, commodities, rates and macro, in Singapore time.

Tabs
  Markets board : FX, commodities and risk proxies with live prices and move stats
  Mean reversion: scan every pair for stretched ratios, then inspect one in detail
  Rates         : the US curve, breakevens, real yields, JGBs and cross-market spreads
  Macro data    : the latest hard prints -- CPI, payrolls, GDP, liquidity -- from FRED
  Calendar      : a rolling two-week economic calendar with actual vs forecast
  News          : themed headlines plus central bank releases

Run locally
  python -m pip install -r requirements.txt
  python -m streamlit run app.py

Data: Yahoo Finance via yfinance (unofficial), FRED, Japan Ministry of Finance,
Forex Factory weekly JSON (unofficial), Nasdaq economic events (unofficial),
Google News RSS, central bank RSS feeds, plus any TradingView CSV you upload.
For education and research only.
"""
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import econ_calendar as ec
from analytics import (
    adf_tstat, as_of_label, build_board, half_life, scan_pairs, transform,
)
from config import (
    ADF_5PCT, BOARD_TITLE, CURVE, GROUPS, LOOKBACKS, MACRO_SCALE, MACRO_SERIES,
    NEWS_THEMES, PRESETS, RATE_EXTRAS, Z_WINDOWS,
)
from sources import (
    cb_feeds, load_cb, load_daily, load_fred, load_fred_many, load_intraday,
    load_jgb10, load_live_yields, load_news, load_spot_quotes, load_yahoo_yields,
    now_sgt, parse_tradingview_csv, safe,
)

st.set_page_config(page_title="FX & Macro Brief", page_icon="💱", layout="wide")

IMPACT_ICON = {"High": "🔴 High", "Medium": "🟠 Medium", "Low": "⚪ Low", "Holiday": "🏖 Holiday"}
MAJORS = ["USD", "EUR", "JPY", "GBP", "AUD", "CAD", "CHF", "NZD", "CNY", "SGD"]


def run_tab(render, *args):
    """One tab failing must never stop the tabs after it from loading."""
    try:
        render(*args)
    except Exception as e:  # noqa: BLE001
        st.error(f"This tab hit an error: {type(e).__name__}: {e}")


# =====================================================================================
# HEADER AND SIDEBAR
# =====================================================================================
st.title("FX & macro brief")
st.caption(f"{now_sgt():%A %d %B %Y, %H:%M} SGT")

with st.sidebar:
    refresh_label = st.selectbox(
        "Auto-refresh prices", ["Off", "30 seconds", "1 minute", "2 minutes", "5 minutes"], index=2)
    REFRESH = {"Off": None, "30 seconds": 30, "1 minute": 60,
               "2 minutes": 120, "5 minutes": 300}[refresh_label]
    if st.button("Refresh all data now", width="stretch"):
        st.cache_data.clear()
        st.rerun()
    st.divider()
    st.subheader("Add TradingView data")
    st.caption("On TradingView, open a daily chart, choose Export chart data, and upload the "
               "CSV here. It becomes available in the Mean reversion tab.")
    uploads = st.file_uploader("TradingView CSV exports", type="csv", accept_multiple_files=True)
    st.divider()
    st.caption("Yahoo quotes can be delayed. Check the Updated column on the board. "
               "FRED publishes the Treasury curve at a New York close, so the Rates tab "
               "also shows a live Yahoo print.")

tab_board, tab_mr, tab_rates, tab_macro, tab_cal, tab_news = st.tabs(
    [BOARD_TITLE, "Mean reversion", "Rates", "Macro data", "Calendar", "News"])


# =====================================================================================
# MARKETS BOARD
# =====================================================================================
@st.fragment(run_every=REFRESH)
def render_board():
    daily = safe(load_daily, label="Daily prices")
    if daily is None:
        return
    # Yahoo's 5-minute bars cover the FX, futures and index rows; the metals get
    # their live level from a spot quote instead. Both are timestamped in SGT, so
    # the board can read the last tick out of either without caring which is which.
    live = [safe(load_intraday, label="Intraday prices", quiet=True),
            safe(load_spot_quotes, label="Spot metal quotes", quiet=True)]
    live = [f for f in live if f is not None and not f.empty]
    board = build_board(daily, pd.concat(live).sort_index() if live else pd.DataFrame())
    if board.empty:
        st.warning("No instrument had enough history to build the board.")
        return

    groups = st.pills("Show", GROUPS, selection_mode="multi", default=GROUPS, key="board_groups")
    view = board[board["Group"].isin(groups or GROUPS)].reset_index(drop=True)

    num = st.column_config.NumberColumn
    st.dataframe(
        view, hide_index=True, width="stretch", height=680,
        column_config={
            "Last": num(format="%.4f"),
            "1D": num("1D", format="%+.2f", help="Percent, or basis points for yields and the VIX"),
            "1W": num("1W", format="%+.2f"),
            "1M": num("1M", format="%+.2f"),
            "3M": num("3M", format="%+.2f"),
            "YTD": num("YTD", format="%+.2f"),
            "1D move (z)": num(format="%+.2f",
                help="Today's return divided by the standard deviation of the past year's daily returns"),
            "3M range %ile": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f"),
            "52w range %ile": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f"),
            "20D vol %": num(format="%.1f", help="Annualised realised volatility, last 20 days"),
            "1Y vol %": num(format="%.1f", help="Annualised realised volatility, last year"),
            "Last 3M": st.column_config.LineChartColumn(width="medium"),
        })

    movers = view.dropna(subset=["1D move (z)"]).reindex(
        view["1D move (z)"].abs().sort_values(ascending=False).index).head(3)
    if not movers.empty:
        cols = st.columns(len(movers))
        for c, (_, row) in zip(cols, movers.iterrows()):
            c.metric(row["Instrument"], f"{row['Last']:,.4f}".rstrip("0").rstrip("."),
                     f"{row['1D']:+.2f} ({row['1D move (z)']:+.1f}σ)")
    big = view.loc[view["1D move (z)"].abs() >= 2, "Instrument"].tolist()
    if big:
        st.info(f"Unusual moves today (|z| of at least 2): {', '.join(big)}. "
                "Check the print, then find the headline.")
    st.caption(
        f"Refreshed {now_sgt():%H:%M:%S} SGT. Gold, silver, platinum and palladium are **spot** "
        "(XAU/USD and friends): the daily history is LBMA's afternoon auction and the live level is "
        "a spot quote, so the 1D change is measured against yesterday's London fix rather than a "
        "5pm New York close. Copper and energy are futures, which jump when the contract rolls. "
        "Yield and VIX moves are shown in basis points.")


with tab_board:
    run_tab(render_board)


# =====================================================================================
# MEAN REVERSION
# =====================================================================================
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

    st.markdown("**How to use this tab:** the scanner ranks every pair by how stretched its "
                "price ratio is. Click a row, or a preset, and the inspector below explains "
                "that pair in plain English.")

    c1, c2 = st.columns(2)
    lb_label = c1.segmented_control("History used", list(LOOKBACKS), default="5Y", key="lookback") or "5Y"
    zw_label = c2.segmented_control("Z-score measured against the average of the last",
                                    list(Z_WINDOWS), default="3 months", key="zwin") or "3 months"
    n, z_window = LOOKBACKS[lb_label], Z_WINDOWS[zw_label]
    hist = prices if n is None else prices.iloc[-n:]

    # ---- Scanner
    st.subheader("1. Scanner: which pairs look stretched?")
    f1, f2, f3 = st.columns(3)
    min_z = f1.slider("Show |z| of at least", 0.0, 3.0, 1.5, 0.25)
    only_stationary = f2.toggle("Only pairs that pass the mean reversion test (ADF)", value=False)
    max_hl = f3.slider("Half-life no longer than (days)", 5, 250, 120, 5)
    scan = scan_pairs(hist, z_window)
    if scan.empty:
        st.warning("Not enough overlapping history to scan pairs over this window.")
        return
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
               "The z-score uses the log of A ÷ B, so a positive z means A has outperformed B.")

    # ---- Inspector
    st.subheader("2. Inspector: is this pair a mean reversion candidate?")
    p = st.columns(len(PRESETS))
    for col, (label_, (a_, b_)) in zip(p, PRESETS.items()):
        col.button(label_, on_click=set_pair, args=(a_, b_), width="stretch")
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

    if z_now >= 2:
        stance = f"**{a} looks expensive versus {b}.** A mean reversion trade would sell {a} and buy {b}."
    elif z_now <= -2:
        stance = f"**{a} looks cheap versus {b}.** A mean reversion trade would buy {a} and sell {b}."
    else:
        stance = f"**No signal.** The {label} is within 2 standard deviations of its {zw_label} average."
    evidence = (f"passes the mean reversion test (ADF t = {adf:.2f}, below {ADF_5PCT})" if adf <= ADF_5PCT
                else f"does **not** pass the mean reversion test (ADF t = {adf:.2f}, needs to be below "
                     f"{ADF_5PCT}), so the gap could keep widening")
    hl_text = (f"a typical deviation halves in about {hl:.0f} trading days" if np.isfinite(hl)
               else "no measurable half-life")
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

    rebased = df / df.iloc[0] * 100
    fig1 = go.Figure()
    fig1.add_trace(go.Scatter(x=rebased.index, y=rebased[a], name=a, line=dict(width=1.6)))
    fig1.add_trace(go.Scatter(x=rebased.index, y=rebased[b], name=b, line=dict(width=1.6)))
    fig1.update_layout(title=f"{a} and {b}, both rebased to 100 on {df.index[0]:%d %b %Y}",
                       height=340, hovermode="x unified", legend=dict(orientation="h", y=1.12),
                       margin=dict(t=60, b=10), yaxis_title="Rebased (start = 100)")
    st.plotly_chart(fig1, width="stretch")

    fig2 = go.Figure()
    fig2.add_trace(go.Scatter(x=mean.index, y=mean + 2 * sd, line=dict(width=0),
                              showlegend=False, hoverinfo="skip"))
    fig2.add_trace(go.Scatter(x=mean.index, y=mean - 2 * sd, fill="tonexty", line=dict(width=0),
                              fillcolor="rgba(120,140,170,0.18)", name="Average ± 2σ"))
    fig2.add_trace(go.Scatter(x=mean.index, y=mean, name=f"{zw_label} average",
                              line=dict(dash="dot", width=1.2)))
    fig2.add_trace(go.Scatter(x=level.index, y=level, name=label, line=dict(width=1.8, color="#1F4E79")))
    rich, cheap = level[z >= 2], level[z <= -2]
    fig2.add_trace(go.Scatter(x=rich.index, y=rich, mode="markers", name=f"{a} rich (z ≥ 2)",
                              marker=dict(size=5, color="#C0392B")))
    fig2.add_trace(go.Scatter(x=cheap.index, y=cheap, mode="markers", name=f"{a} cheap (z ≤ −2)",
                              marker=dict(size=5, color="#1E8449")))
    fig2.update_layout(title=f"{label}: outside the shaded band = stretched", height=380,
                       hovermode="x unified", legend=dict(orientation="h", y=1.12), margin=dict(t=60, b=10))
    st.plotly_chart(fig2, width="stretch")

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
        st.caption("A mean reversion trade assumes the link between A and B holds. If this line has "
                   "collapsed toward zero recently, the old average may no longer apply.")

    with st.expander("Before trading any of this"):
        st.markdown(
            "- **The ADF test here uses the whole history you selected.** A pair can pass over 10 years and fail over 1.\n"
            "- **Scanning many pairs finds false positives.** With hundreds of pairs, some will look stretched by chance.\n"
            "- **A ratio of A ÷ B is not a hedge ratio.** A proper pairs trade sizes each leg by volatility or regression beta.\n"
            "- **Carry and costs are ignored, and the futures rows roll.** Copper and energy jump at contract rolls; the metals are spot, so they do not.\n"
            "- **Z-scores do not tell you when it reverts.** Structural changes can keep a ratio stretched for years.")


with tab_mr:
    run_tab(render_mean_reversion)


# =====================================================================================
# RATES
# =====================================================================================
def render_rates():
    curve = safe(load_fred_many, tuple(CURVE.items()), label="US curve (FRED)", quiet=True)
    live = safe(load_live_yields, label="Live yields (Yahoo)", quiet=True)

    if curve is None:
        st.warning("FRED is unreachable, falling back to Yahoo Finance history.")
        y = safe(load_yahoo_yields, label="Treasury yields (Yahoo fallback)")
        if y is None:
            return
        curve = y.rename(columns={"UST5Y": "UST 5Y", "UST10Y": "UST 10Y", "UST30Y": "UST 30Y"})
        source = "Yahoo Finance (FRED unreachable)"
    else:
        source = "FRED, New York close"

    curve = curve.dropna(how="all").ffill()
    last_date = curve.dropna(how="all").index[-1]

    # ---- Live vs last official close
    st.subheader("Where the curve is now")
    if live is not None and not live.empty:
        cols = st.columns(len(live))
        for c, (name, val) in zip(cols, live.items()):
            prev = curve[name].dropna().iloc[-1] if name in curve else np.nan
            delta = (val - prev) * 100 if np.isfinite(prev) else None
            c.metric(name, f"{val:.2f}%", f"{delta:+.1f}bp vs {last_date:%d %b}" if delta is not None else None)
        st.caption("Live prints are Yahoo's CBOE yield indices, which update through the US session. "
                   "The delta compares them with the last official FRED close.")
    else:
        cols = st.columns(len(curve.columns))
        for c, name in zip(cols, curve.columns):
            s = curve[name].dropna()
            c.metric(name, f"{s.iloc[-1]:.2f}%", f"{(s.iloc[-1] - s.iloc[-6]) * 100:+.1f}bp 1W")

    # ---- Curve shape today vs a month and a year ago
    snap = curve.dropna(how="all")
    tenors = [c for c in CURVE if c in snap.columns]
    if len(tenors) >= 3:
        fig = go.Figure()
        for lbl, offset in [("Today", 0), ("1 month ago", 21), ("1 year ago", 252)]:
            if len(snap) > offset:
                row = snap.iloc[-1 - offset]
                fig.add_trace(go.Scatter(x=tenors, y=[row[t] for t in tenors], name=lbl,
                                         mode="lines+markers"))
        fig.update_layout(title="US Treasury curve", height=320, yaxis_title="%",
                          hovermode="x unified", margin=dict(t=50, b=10),
                          legend=dict(orientation="h", y=1.14))
        st.plotly_chart(fig, width="stretch")

    # ---- Spreads and cross-market
    st.subheader("Spreads")
    spreads = pd.DataFrame(index=curve.index)
    if {"UST 2Y", "UST 10Y"} <= set(curve.columns):
        spreads["2s10s (bp)"] = (curve["UST 10Y"] - curve["UST 2Y"]) * 100
    if {"UST 3M", "UST 10Y"} <= set(curve.columns):
        spreads["3M10Y (bp)"] = (curve["UST 10Y"] - curve["UST 3M"]) * 100
    if {"UST 5Y", "UST 30Y"} <= set(curve.columns):
        spreads["5s30s (bp)"] = (curve["UST 30Y"] - curve["UST 5Y"]) * 100

    jgb = safe(load_jgb10, label="JGB 10Y (Japan MOF)", quiet=True)
    if jgb is not None and "UST 10Y" in curve:
        aligned = jgb.reindex(curve.index).ffill()
        spreads["UST10−JGB10 (bp)"] = (curve["UST 10Y"] - aligned) * 100

    extras = safe(load_fred_many, tuple(RATE_EXTRAS.items()), label="Breakevens and policy rates", quiet=True)
    if extras is not None:
        extras = extras.ffill()

    spreads = spreads.dropna(how="all")
    if not spreads.empty:
        cols = st.columns(len(spreads.columns))
        for c, name in zip(cols, spreads.columns):
            s = spreads[name].dropna()
            wk = s.iloc[-6] if len(s) > 6 else s.iloc[0]
            c.metric(name.replace(" (bp)", ""), f"{s.iloc[-1]:.0f}bp", f"{s.iloc[-1] - wk:+.0f}bp 1W")

    if extras is not None and not extras.empty:
        st.subheader("Inflation expectations and policy rates")
        cols = st.columns(len(extras.columns))
        for c, name in zip(cols, extras.columns):
            s = extras[name].dropna()
            if s.empty:
                continue
            mo = s.iloc[-22] if len(s) > 22 else s.iloc[0]
            c.metric(name, f"{s.iloc[-1]:.2f}%", f"{(s.iloc[-1] - mo) * 100:+.0f}bp 1M")

    # ---- Charts
    window = st.segmented_control("History", ["1Y", "2Y", "5Y", "10Y"], default="2Y", key="rates_window") or "2Y"
    years = int(window.rstrip("Y"))
    cut = curve.index.max() - pd.DateOffset(years=years)
    left, right = st.columns(2)
    left.markdown("**Yield levels (%)**")
    left.line_chart(curve[curve.index >= cut].dropna(how="all"), height=320)
    if not spreads.empty:
        right.markdown("**Spreads (bp)**")
        right.line_chart(spreads[spreads.index >= cut], height=320)
    if extras is not None and not extras.empty:
        st.markdown("**Breakevens, real yields and policy rates (%)**")
        st.line_chart(extras[extras.index >= cut].dropna(how="all"), height=300)

    st.caption(f"Source: {source}; JGB 10Y from Japan MOF (Tokyo close); breakevens and policy "
               f"rates from FRED. Last official curve date {last_date:%d %b %Y}.")


with tab_rates:
    run_tab(render_rates)


# =====================================================================================
# MACRO DATA
# =====================================================================================
def render_macro():
    st.markdown("**The latest hard prints.** Every number below is the released figure from FRED, "
                "not a forecast. `Latest` is the most recent observation, `Prior` the one before it, "
                "and `As of` the period the reading refers to.")

    for section, items in MACRO_SERIES.items():
        st.subheader(section)
        rows, spark = [], {}
        for label, sid, how, unit, note in items:
            s = safe(load_fred, sid, label=label, quiet=True)
            if s is None or s.empty:
                rows.append({"Series": label, "Latest": np.nan, "Prior": np.nan,
                             "Change": np.nan, "Unit": unit, "As of": "unavailable",
                             "Trend": [], "Note": note})
                continue
            latest, prior, asof, hist, freq = transform(s, how, MACRO_SCALE.get(sid, 1.0))
            rows.append({
                "Series": label,
                "Latest": latest,
                "Prior": prior,
                "Change": latest - prior if np.isfinite(prior) else np.nan,
                "Unit": unit,
                "As of": as_of_label(asof, freq),
                "Trend": hist.iloc[-36:].round(4).tolist(),
                "Note": note,
            })
            spark[label] = hist
        df = pd.DataFrame(rows)
        st.dataframe(df, hide_index=True, width="stretch", column_config={
            "Series": st.column_config.TextColumn(width="medium"),
            "Latest": st.column_config.NumberColumn(format="localized"),
            "Prior": st.column_config.NumberColumn(format="localized"),
            "Change": st.column_config.NumberColumn(format="%+.2f"),
            "Unit": st.column_config.TextColumn(width="small"),
            "As of": st.column_config.TextColumn(width="small"),
            "Trend": st.column_config.LineChartColumn("Recent trend", width="medium"),
            "Note": st.column_config.TextColumn(width="medium"),
        })

        chart_for = st.selectbox("Chart one of these", ["(none)"] + list(spark),
                                 key=f"macro_chart_{section}")
        if chart_for != "(none)":
            hist = spark[chart_for]
            fig = go.Figure(go.Scatter(x=hist.index, y=hist.values, line=dict(width=1.6, color="#1F4E79")))
            fig.add_hline(y=0, line_width=1)
            fig.update_layout(title=chart_for, height=300, margin=dict(t=50, b=10), hovermode="x")
            st.plotly_chart(fig, width="stretch")

    st.caption("Source: Federal Reserve Bank of St. Louis (FRED). Year-on-year figures are computed "
               "from the index level, so they can differ in the last decimal from the headline print "
               "published by the statistics agency.")


with tab_macro:
    run_tab(render_macro)


# =====================================================================================
# CALENDAR
# =====================================================================================
HORIZONS = {"Today": 0, "Next 3 days": 3, "Next 7 days": 7, "Next 14 days": 14}


def render_calendar():
    cal = safe(ec.load_calendar, 3, 14, label="Economic calendar")
    if cal is None or cal.empty:
        st.warning("Both calendar feeds are unreachable right now.")
        st.markdown("Open one directly: [Forex Factory](https://www.forexfactory.com/calendar) · "
                    "[Investing.com](https://www.investing.com/economic-calendar/) · "
                    "[Nasdaq](https://www.nasdaq.com/market-activity/economic-calendar)")
        return

    now = now_sgt()
    today = now.date()

    c1, c2 = st.columns([3, 2])
    available = [c for c in MAJORS if c in set(cal["Ccy"])]
    others = sorted(set(cal["Ccy"]) - set(MAJORS))
    ccys = c1.multiselect("Currencies", available + others, default=available)
    impacts = c2.multiselect("Impact", ec.IMPACTS, default=["High", "Medium"])
    c3, c4 = st.columns([3, 2])
    horizon = c3.segmented_control("Looking ahead", list(HORIZONS), default="Next 7 days",
                                   key="cal_horizon") or "Next 7 days"
    with c4:
        st.write("")
        show_past = st.toggle("Include the last 3 days, to see what already printed", value=True)

    days = HORIZONS[horizon]
    end = today + pd.Timedelta(days=days)
    start = (today - pd.Timedelta(days=3)) if show_past else today
    view = cal[(cal["Date"] >= start) & (cal["Date"] <= end)]
    if ccys:
        view = view[view["Ccy"].isin(ccys)]
    if impacts:
        view = view[view["Impact"].isin(impacts)]

    # ---- Next high-impact event
    nxt = ec.next_high_impact(cal, now, ccys)
    if nxt is not None:
        delta = nxt["When"] - now
        hours, minutes = divmod(int(delta.total_seconds() // 60), 60)
        when = f"in {hours}h {minutes:02d}m" if hours < 48 else f"{nxt['When']:%a %d %b}"
        st.success(f"**Next high-impact event:** {nxt['Ccy']} · {nxt['Event']} — "
                   f"{nxt['When']:%a %d %b %H:%M} SGT ({when})"
                   + (f" · forecast {nxt['Forecast']}" if nxt["Forecast"] else "")
                   + (f", previous {nxt['Previous']}" if nxt["Previous"] else ""))

    if view.empty:
        st.info("Nothing matches those filters. Widen the horizon or add currencies.")
        return

    # ---- Today's releases, with what actually printed
    printed = view[(view["Date"] == today) & (view["Actual"] != "")]
    if not printed.empty:
        st.markdown("#### Already out today")
        shown = printed.tail(4)
        cols = st.columns(len(shown))
        for c, (_, r) in zip(cols, shown.iterrows()):
            c.metric(f"{r['Ccy']} · {r['Event'][:30]}", r["Actual"],
                     f"{r['Surprise']} {r['Forecast']}" if r["Forecast"] else None,
                     delta_color="off", help="Released figure, and how it compared with consensus")

    # ---- Day by day
    st.markdown("#### Schedule")
    display = view.assign(Impact=view["Impact"].astype(str).map(IMPACT_ICON))
    for date, block in display.groupby("Date", sort=True):
        tag = " · today" if date == today else (" · tomorrow" if date == today + pd.Timedelta(days=1) else "")
        past = " · already printed" if date < today else ""
        n = len(block)
        st.markdown(f"**{block['Day'].iloc[0]}{tag}{past}** — {n} event{'' if n == 1 else 's'}")
        st.dataframe(
            block[["Time", "Ccy", "Region", "Impact", "Event", "Actual", "Forecast",
                   "Previous", "Surprise"]],
            hide_index=True, width="stretch",
            height=min(420, 38 + 35 * len(block)),
            column_config={
                "Time": st.column_config.TextColumn(width="small"),
                "Ccy": st.column_config.TextColumn(width="small"),
                "Region": st.column_config.TextColumn(width="small"),
                "Impact": st.column_config.TextColumn(width="small"),
                "Event": st.column_config.TextColumn(width="large"),
                "Surprise": st.column_config.TextColumn("vs forecast", width="small"),
            })

    st.download_button("Download this view as CSV",
                       view.drop(columns=["When"]).to_csv(index=False).encode(),
                       file_name=f"econ-calendar-{today}.csv", mime="text/csv")
    st.caption("Times in SGT. Impact ratings come from Forex Factory where the event falls in the "
               "current week, otherwise from a keyword classifier — treat those as a hint, not gospel. "
               "Actual, forecast and previous come from Nasdaq's economic-events feed. "
               "Both feeds are unofficial and can lag a release by a few minutes.")


with tab_cal:
    run_tab(render_calendar)


# =====================================================================================
# NEWS
# =====================================================================================
def render_news():
    c1, c2 = st.columns([3, 1])
    themes = c1.multiselect("Themes", list(NEWS_THEMES), default=list(NEWS_THEMES))
    hours = c2.slider("Last N hours", 6, 72, 24, step=6)
    keyword = st.text_input("Filter headlines containing (optional)")
    frames = [safe(load_news, t, NEWS_THEMES[t], label=f"News: {t}", quiet=True) for t in themes]
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
    else:
        st.info("No headlines came back. Google News rate-limits bursts; try again in a minute.")

    st.subheader("Central bank releases")
    items = cb_feeds()
    for chunk in (items[:4], items[4:]):
        if not chunk:
            continue
        cols = st.columns(4)
        for col, (name, url) in zip(cols, chunk):
            with col:
                st.markdown(f"**{name}**")
                cb = safe(load_cb, name, url, label=name, quiet=True)
                if cb is None or cb.empty:
                    st.caption("feed unavailable")
                    continue
                for _, row in cb.sort_values("time", ascending=False, na_position="last").head(5).iterrows():
                    when = f"{row['time']:%d %b} " if pd.notna(row["time"]) else ""
                    st.markdown(f"{when}[{row['title']}]({row['link']})")


with tab_news:
    run_tab(render_news)

st.divider()
st.caption("Built with Python and Streamlit. Data from Yahoo Finance, LBMA, FRED, Japan MOF, "
           "Forex Factory, Nasdaq, Google News and central bank feeds. "
           "For education and research only; not investment advice.")
