"""Long-history research panel.

Daily dividend-adjusted OHLCV for every ETF in the universe plus VIX and
crude, from each fund's inception. History comes from Yahoo (IBKR serves only
a few years of daily bars per request); IBKR remains the source for live
collection. The panel is rebuilt in full on each fetch because adjustments
rewrite past prices.
"""

import logging

import pandas as pd
import yfinance as yf

from .. import storage
from ..universe import ETFS

log = logging.getLogger(__name__)

PANEL_PATH = "research/panel.parquet"
EXTRA = ["^VIX", "CL=F"]


def fetch(symbols: list[str] | None = None) -> pd.DataFrame:
    symbols = symbols or ETFS + EXTRA
    raw = yf.download(symbols, period="max", interval="1d", auto_adjust=True, group_by="ticker", progress=False, threads=True)
    frames = []
    for s in symbols:
        if s not in raw.columns.get_level_values(0):
            log.warning("no_history symbol=%s", s)
            continue
        d = raw[s].dropna(how="all").reset_index()
        d.columns = [str(c).lower() for c in d.columns]
        d["symbol"] = s
        frames.append(d[["date", "symbol", "open", "high", "low", "close", "volume"]])
    panel = pd.concat(frames, ignore_index=True)
    panel["date"] = pd.to_datetime(panel["date"]).dt.tz_localize(None)
    last_session = panel.loc[panel["symbol"] == "SPY", "date"].max()
    panel = panel[panel["date"] <= last_session]
    storage.write(panel, PANEL_PATH)
    log.info("panel_written symbols=%d rows=%d first=%s last=%s", panel["symbol"].nunique(), len(panel), panel["date"].min().date(), panel["date"].max().date())
    return panel


def load() -> pd.DataFrame:
    return storage.read(PANEL_PATH)


def wide(panel: pd.DataFrame, field: str) -> pd.DataFrame:
    return panel.pivot(index="date", columns="symbol", values=field).sort_index()
