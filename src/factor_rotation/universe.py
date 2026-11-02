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


def option_universe() -> list[str]:
    return ETFS + CONSTITUENTS


def bar_universe() -> list[str]:
    return ETFS + CONSTITUENTS
