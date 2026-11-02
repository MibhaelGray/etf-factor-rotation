"""Daily dividend-adjusted bars from IBKR.

ADJUSTED_LAST bars make close-to-close returns total returns, which matters
for pairs with very different yields (XLU vs XLK, HYG vs IEF). Within a single
day open and close share one adjustment factor, so open-to-close returns are
unaffected. History is rewritten on each pull because adjustment changes it.
"""

import asyncio
import logging

import pandas as pd
from ib_async import IB, Stock

from .. import storage
from .session import connect

log = logging.getLogger(__name__)


def bars_path(symbol: str) -> str:
    return f"bars_daily/symbol={symbol.replace(' ', '_')}.parquet"


async def _pull(ib: IB, symbol: str, duration: str, sem: asyncio.Semaphore) -> int:
    async with sem:
        contract = Stock(symbol, "SMART", "USD")
        bars = await ib.reqHistoricalDataAsync(
            contract, endDateTime="", durationStr=duration, barSizeSetting="1 day",
            whatToShow="ADJUSTED_LAST", useRTH=True, formatDate=1, timeout=120,
        )
    if not bars:
        log.warning("no_bars symbol=%s", symbol)
        return 0
    df = pd.DataFrame([
        {"date": b.date, "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume}
        for b in bars
    ])
    df["date"] = pd.to_datetime(df["date"])
    df.insert(0, "symbol", symbol)
    storage.write(df, bars_path(symbol))
    return len(df)


async def collect(symbols: list[str], duration: str = "5 Y") -> dict:
    sem = asyncio.Semaphore(8)
    async with connect(client_id_offset=1) as ib:
        results = await asyncio.gather(*[_pull(ib, s, duration, sem) for s in symbols], return_exceptions=True)
    failed = [s for s, r in zip(symbols, results) if isinstance(r, Exception) or r == 0]
    for s, r in zip(symbols, results):
        if isinstance(r, Exception):
            log.error("bars_failed symbol=%s error=%r", s, r)
    stats = {"symbols": len(symbols), "failed": failed}
    log.info("bars_done %s", stats)
    return stats


def load(symbols: list[str]) -> pd.DataFrame:
    frames = [storage.read(bars_path(s)) for s in symbols if storage.exists(bars_path(s))]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
