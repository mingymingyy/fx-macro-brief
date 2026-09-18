"""Everything you are likely to want to edit: instruments, feeds, series IDs, windows.

Keeping these out of app.py means you can add a ticker or a macro series without
touching any plotting or Streamlit code.
"""

SGT = "Asia/Singapore"
ET = "America/New_York"
BOARD_TITLE = "Markets board"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; fx-macro-brief/2.0; personal research dashboard)",
    "Accept": "application/json, text/csv, */*",
}

# ---------------------------------------------------------------- instruments
# label: (Yahoo ticker, group)
INSTRUMENTS = {
    # G10 FX
    "EUR/USD": ("EURUSD=X", "G10 FX"),
    "GBP/USD": ("GBPUSD=X", "G10 FX"),
    "USD/JPY": ("JPY=X", "G10 FX"),
    "AUD/USD": ("AUDUSD=X", "G10 FX"),
    "NZD/USD": ("NZDUSD=X", "G10 FX"),
    "USD/CAD": ("CAD=X", "G10 FX"),
    "USD/CHF": ("CHF=X", "G10 FX"),
    "USD/NOK": ("NOK=X", "G10 FX"),
    "USD/SEK": ("SEK=X", "G10 FX"),
    "EUR/GBP": ("EURGBP=X", "G10 FX"),
    "EUR/JPY": ("EURJPY=X", "G10 FX"),
    "GBP/JPY": ("GBPJPY=X", "G10 FX"),
    "AUD/JPY": ("AUDJPY=X", "G10 FX"),
    "EUR/CHF": ("EURCHF=X", "G10 FX"),
    "DXY": ("DX-Y.NYB", "G10 FX"),
    # Asia & EM FX
    "USD/SGD": ("SGD=X", "Asia & EM FX"),
    "USD/CNH": ("CNH=X", "Asia & EM FX"),
    "USD/KRW": ("KRW=X", "Asia & EM FX"),
    "USD/TWD": ("TWD=X", "Asia & EM FX"),
    "USD/INR": ("INR=X", "Asia & EM FX"),
    "USD/IDR": ("IDR=X", "Asia & EM FX"),
    "USD/THB": ("THB=X", "Asia & EM FX"),
    "USD/MXN": ("MXN=X", "Asia & EM FX"),
    "USD/BRL": ("BRL=X", "Asia & EM FX"),
    "USD/ZAR": ("ZAR=X", "Asia & EM FX"),
    # Commodities
    "Gold": ("GC=F", "Commodities"),
    "Silver": ("SI=F", "Commodities"),
    "Platinum": ("PL=F", "Commodities"),
    "Copper": ("HG=F", "Commodities"),
    "WTI crude": ("CL=F", "Commodities"),
    "Brent crude": ("BZ=F", "Commodities"),
    "Nat gas": ("NG=F", "Commodities"),
    "Iron ore proxy (BHP)": ("BHP", "Commodities"),
    # Risk & rates proxies: useful context for an FX board
    "S&P 500": ("^GSPC", "Risk & rates"),
    "Nasdaq 100": ("^NDX", "Risk & rates"),
    "Euro Stoxx 50": ("^STOXX50E", "Risk & rates"),
    "Nikkei 225": ("^N225", "Risk & rates"),
    "VIX": ("^VIX", "Risk & rates"),
    "UST 3M yield": ("^IRX", "Risk & rates"),  # 13-week bill, the short end
    "UST 10Y yield": ("^TNX", "Risk & rates"),
    "UST 30Y yield": ("^TYX", "Risk & rates"),
    "Bitcoin": ("BTC-USD", "Risk & rates"),
}
GROUPS = ["G10 FX", "Asia & EM FX", "Commodities", "Risk & rates"]

# Instruments quoted as a yield/index in percent, so "%" moves are misleading.
IN_PERCENT = {"VIX", "UST 3M yield", "UST 10Y yield", "UST 30Y yield"}

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

# ---------------------------------------------------------------- news feeds
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
    "SNB": "https://www.snb.ch/public/en/rss/press-releases",
    "RBNZ": "https://www.rbnz.govt.nz/rss/news",
}

# ---------------------------------------------------------------- endpoints
FF_CALENDAR = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NASDAQ_CALENDAR = "https://api.nasdaq.com/api/calendar/economicevents?date={date}"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"
MOF_HIST = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/historical/jgbcme_all.csv"
MOF_CUR = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/jgbcme.csv"

# ---------------------------------------------------------------- rates board
# label: FRED series id. All are daily and in percent.
CURVE = {
    "UST 3M": "DGS3MO",
    "UST 2Y": "DGS2",
    "UST 5Y": "DGS5",
    "UST 10Y": "DGS10",
    "UST 30Y": "DGS30",
}
RATE_EXTRAS = {
    "10Y breakeven": "T10YIE",
    "5Y breakeven": "T5YIE",
    "10Y TIPS real": "DFII10",
    "Fed funds (EFFR)": "EFFR",
    "SOFR": "SOFR",
    "ECB depo rate": "ECBDFR",
}

# ---------------------------------------------------------------- macro board
# label: (FRED id, transform, unit, note)
# transform: "yoy" = % change vs 12 months ago, "mom_chg" = change vs prior month
#            in the series' own units, "level" = print the level as-is.
MACRO_SERIES = {
    "Inflation": [
        ("US CPI", "CPIAUCSL", "yoy", "% y/y", "Headline consumer prices"),
        ("US core CPI", "CPILFESL", "yoy", "% y/y", "Ex food and energy"),
        ("US PCE", "PCEPI", "yoy", "% y/y", "The Fed's target index"),
        ("US core PCE", "PCEPILFE", "yoy", "% y/y", "The Fed's preferred core measure"),
        ("Euro area HICP", "CP0000EZ19M086NEST", "yoy", "% y/y", "Euro area harmonised CPI"),
    ],
    "Labour & activity": [
        ("US non-farm payrolls", "PAYEMS", "mom_chg", "k jobs m/m", "Monthly change in jobs"),
        ("US unemployment", "UNRATE", "level", "%", "U-3 rate"),
        ("US average hourly earnings", "AHETPI", "yoy", "% y/y", "Production workers"),
        ("US initial claims", "ICSA", "level", "claims", "Weekly, seasonally adjusted"),
        ("US industrial production", "INDPRO", "yoy", "% y/y", ""),
        ("US retail sales", "RSAFS", "yoy", "% y/y", "Advance retail and food services"),
        ("US real GDP", "GDPC1", "yoy", "% y/y", "Quarterly, chained 2017 USD"),
        ("US housing starts", "HOUST", "level", "k SAAR", ""),
        ("US durable goods orders", "DGORDER", "yoy", "% y/y", ""),
        ("US consumer sentiment", "UMCSENT", "level", "index", "University of Michigan"),
    ],
    "Money & liquidity": [
        ("Fed balance sheet", "WALCL", "level", "$tn", "Total assets, weekly"),
        ("Reverse repo", "RRPONTSYD", "level", "$bn", "Overnight RRP take-up"),
        ("US M2", "M2SL", "yoy", "% y/y", ""),
        ("Broad dollar index", "DTWEXBGS", "level", "index", "Fed nominal broad USD index"),
        ("VIX", "VIXCLS", "level", "index", ""),
    ],
}

# FRED publishes some of these in awkward units. Rescale levels for readability;
# year-on-year rates are unaffected, so only "level" and "mom_chg" series need it.
MACRO_SCALE = {
    "WALCL": 1e-6,   # $ millions -> $ trillions
    "PAYEMS": 1.0,   # already thousands of jobs
}

# ---------------------------------------------------------------- analytics
LOOKBACKS = {"1Y": 252, "2Y": 504, "5Y": 1260, "10Y": 2520, "Max": None}
Z_WINDOWS = {"1 month": 21, "3 months": 63, "6 months": 126, "1 year": 252}
ADF_5PCT = -2.86  # MacKinnon asymptotic 5% critical value, ADF with constant, no trend
