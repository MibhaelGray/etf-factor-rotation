"""Instrument universe: the ETFs we track, the factor pairs we trade, and the
largest constituents behind them.

Constituent lists are a seed. Refresh them from issuer holdings files
periodically; weights drift and index reconstitutions change the top names.
"""

from dataclasses import dataclass

BROAD = ["SPY", "QQQ", "IWM", "DIA", "RSP", "MDY"]

SECTORS = [
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLY",
    "XLP", "XLU", "XLB", "XLRE", "XLC",
]

INDUSTRIES = ["SMH", "IGV", "KRE", "XBI", "ITB", "XOP", "XRT", "ARKK"]

FACTORS = ["MTUM", "QUAL", "USMV", "VLUE", "IWF", "IWD", "SPHB", "SPLV", "IJR"]

CROSS_ASSET = ["TLT", "IEF", "HYG", "LQD", "GLD", "SLV", "USO", "UUP", "EFA", "EEM", "FXI", "IBIT"]

ETFS = BROAD + SECTORS + INDUSTRIES + FACTORS + CROSS_ASSET

CONSTITUENTS = [
    "NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "BRK B", "JPM",
    "LLY", "V", "ORCL", "MA", "NFLX", "XOM", "COST", "WMT", "PLTR", "HD",
    "JNJ", "ABBV", "BAC", "PG", "UNH", "AMD", "CRM", "KO", "CVX", "GE",
    "CSCO", "WFC", "IBM", "MU", "GS", "MS", "CAT", "PM", "LIN", "MRK",
    "INTU", "NOW", "ADBE", "QCOM", "TXN", "AMAT", "LRCX", "KLAC", "ANET", "PANW",
    "INTC", "ISRG", "AXP", "RTX", "UBER", "BKNG", "T", "VZ", "TMUS", "DIS",
    "PEP", "MCD", "LOW", "NKE", "SBUX", "TJX", "MDLZ", "MO", "CL", "PGR",
    "C", "SCHW", "BLK", "SPGI", "COF", "TMO", "ABT", "DHR", "AMGN", "GILD",
    "PFE", "BMY", "VRTX", "COP", "EOG", "SLB", "OXY", "MPC", "NEE", "SO",
    "DUK", "CEG", "VST", "HON", "UNP", "DE", "BA", "LMT", "ETN", "GEV",
    "PLD", "AMT", "EQIX", "WELL", "SHW", "APD", "FCX", "NEM", "COIN", "MSTR",
]


@dataclass(frozen=True)
class FactorPair:
    name: str
    long: str
    short: str
    description: str


FACTOR_PAIRS = [
    FactorPair("momentum", "MTUM", "USMV", "momentum vs low volatility"),
    FactorPair("high_beta", "SPHB", "SPLV", "high beta vs low volatility"),
    FactorPair("growth_value", "IWF", "IWD", "large-cap growth vs value"),
    FactorPair("size", "IWM", "SPY", "small caps vs large caps"),
    FactorPair("small_quality", "IJR", "IWM", "quality-screened small caps vs Russell 2000"),
    FactorPair("breadth", "RSP", "SPY", "equal weight vs cap weight"),
    FactorPair("megacap_tech", "QQQ", "RSP", "Nasdaq-100 vs equal-weight S&P"),
    FactorPair("cyclical_defensive", "XLY", "XLP", "discretionary vs staples"),
    FactorPair("tech_utilities", "XLK", "XLU", "tech vs utilities"),
    FactorPair("semis_software", "SMH", "IGV", "semiconductors vs software"),
    FactorPair("banks", "KRE", "XLF", "regional banks vs broad financials"),
    FactorPair("energy", "XLE", "SPY", "energy vs market"),
    FactorPair("quality", "QUAL", "SPY", "quality vs market"),
    FactorPair("value", "VLUE", "SPY", "value vs market"),
    FactorPair("credit", "HYG", "IEF", "high yield vs treasuries"),
]


@dataclass(frozen=True)
class LeveragedFund:
    symbol: str
    leverage: float
    exposure: str
    reference: str  # the ETF or stock in our universe whose daily return stands in for the fund's index


LEVERAGED = [
    LeveragedFund("TQQQ", 3, "Nasdaq-100", "QQQ"),
    LeveragedFund("SQQQ", -3, "Nasdaq-100", "QQQ"),
    LeveragedFund("QLD", 2, "Nasdaq-100", "QQQ"),
    LeveragedFund("UPRO", 3, "S&P 500", "SPY"),
    LeveragedFund("SPXL", 3, "S&P 500", "SPY"),
    LeveragedFund("SPXU", -3, "S&P 500", "SPY"),
    LeveragedFund("SSO", 2, "S&P 500", "SPY"),
    LeveragedFund("SOXL", 3, "Semiconductors", "SMH"),
    LeveragedFund("SOXS", -3, "Semiconductors", "SMH"),
    LeveragedFund("TNA", 3, "Russell 2000", "IWM"),
    LeveragedFund("TZA", -3, "Russell 2000", "IWM"),
    LeveragedFund("TECL", 3, "Technology", "XLK"),
    LeveragedFund("FAS", 3, "Financials", "XLF"),
    LeveragedFund("LABU", 3, "Biotech", "XBI"),
    LeveragedFund("TMF", 3, "20+ year Treasuries", "TLT"),
    LeveragedFund("NVDL", 2, "NVDA", "NVDA"),
    LeveragedFund("TSLL", 2, "TSLA", "TSLA"),
    LeveragedFund("MSTU", 2, "MSTR", "MSTR"),
]

# Cboe indexes pulled as daily bars beside the ETFs: VIX and its 3-month version, and the 10-year
# yield (TNX is the yield times 10).
INDEXES = {"VIX": "CBOE", "VIX3M": "CBOE", "TNX": "CBOE"}

# Daily bars only: the aggregate bond ETF for the pension rebalance estimate and the leveraged funds' own prices.
# None of these are idea legs or option underlyings.
BARS_ONLY = ["AGG"] + [f.symbol for f in LEVERAGED]


# Single-stock option chains only for the largest names, the ones that move the index; the list is
# roughly in market-cap order.
OPTION_STOCKS = CONSTITUENTS[:30]

# Index option underlyings: exchange, and the trading classes we pull. SPXW holds the dailies and weeklies
# (settled at the close); SPX holds the traditional monthlies, settled on the morning's opening prints. Most
# index dealer gamma sits here rather than in SPY. Not idea legs: an index can't be bought.
OPTION_INDEXES = {"SPX": ("CBOE", ("SPXW", "SPX"))}
AM_SETTLED_CLASSES = {"SPX"}


def option_universe() -> list[str]:
    return list(OPTION_INDEXES) + ETFS + OPTION_STOCKS


def idea_universe() -> list[str]:
    return ETFS + CONSTITUENTS


def bar_universe() -> list[str]:
    return ETFS + CONSTITUENTS + BARS_ONLY
