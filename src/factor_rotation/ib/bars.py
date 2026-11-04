"""Daily dividend-adjusted bars from IBKR.

ADJUSTED_LAST bars make close-to-close returns total returns, which matters
for pairs with very different yields (XLU vs XLK, HYG vs IEF). Within a single
day open and close share one adjustment factor, so open-to-close returns are
unaffected. History is rewritten on each pull because adjustment changes it.
"""

import asyncio
import logging

import pandas as pd
from ib_async import IB, Contract, Index, Stock

from .. import storage
from ..config import NY
from .session import connect

log = logging.getLogger(__name__)


def bars_path(symbol: str) -> str:
    return f"bars_daily/symbol={symbol.replace(' ', '_')}.parquet"


async def _pull(ib: IB, symbol: str, duration: str, sem: asyncio.Semaphore, contract: Contract | None = None) -> int:
    """Daily bars for a stock or ETF (dividend-adjusted), or for an index when `contract` is given."""
    async with sem:
        bars = await ib.reqHistoricalDataAsync(
            contract or Stock(symbol, "SMART", "USD"), endDateTime="", durationStr=duration, barSizeSetting="1 day",
            whatToShow="TRADES" if contract else "ADJUSTED_LAST", useRTH=True, formatDate=1, timeout=120,
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


async def collect(symbols: list[str], duration: str = "5 Y", indexes: dict[str, str] | None = None) -> dict:
    sem = asyncio.Semaphore(8)
    indexes = indexes or {}
    async with connect(client_id_offset=1) as ib:
        pulls = [_pull(ib, s, duration, sem) for s in symbols]
        pulls += [_pull(ib, s, duration, sem, Index(s, exchange, "USD")) for s, exchange in indexes.items()]
        results = await asyncio.gather(*pulls, return_exceptions=True)
    names = symbols + list(indexes)
    failed = [s for s, r in zip(names, results) if isinstance(r, Exception) or r == 0]
    for s, r in zip(names, results):
        if isinstance(r, Exception):
            log.error("bars_failed symbol=%s error=%r", s, r)
    stats = {"symbols": len(names), "failed": failed}
    log.info("bars_done %s", stats)
    return stats


async def _pull_intraday(ib: IB, symbol: str, day, sem: asyncio.Semaphore) -> pd.DataFrame:
    async with sem:
        bars = await ib.reqHistoricalDataAsync(
            Stock(symbol, "SMART", "USD"), endDateTime="", durationStr="1 D", barSizeSetting="15 mins",
            whatToShow="TRADES", useRTH=False, formatDate=2, timeout=120,
        )
    df = pd.DataFrame([
        {"ts": pd.Timestamp(b.date).tz_convert(NY), "open": b.open, "high": b.high, "low": b.low,
         "close": b.close, "volume": b.volume}
        for b in bars or []
    ])
    if df.empty:
        log.warning("no_intraday_bars symbol=%s", symbol)
        return df
    df = df.assign(time=df["ts"].dt.strftime("%H:%M"))
    df = df[(df["ts"].dt.date == day) & df["time"].ge("04:00") & df["time"].lt("16:00")]
    return df.drop(columns="ts").reset_index(drop=True)


async def collect_intraday(symbols: list[str], day) -> dict[str, pd.DataFrame]:
    """15-minute TRADES bars from 04:00 to 16:00 New York time on `day`; pull after the close."""
    sem = asyncio.Semaphore(4)
    async with connect(client_id_offset=2) as ib:
        results = await asyncio.gather(*[_pull_intraday(ib, s, day, sem) for s in symbols], return_exceptions=True)
    out = {}
    for s, r in zip(symbols, results):
        if isinstance(r, Exception):
            log.error("intraday_failed symbol=%s error=%r", s, r)
        else:
            out[s] = r
    return out


def load(symbols: list[str]) -> pd.DataFrame:
    frames = [storage.read(bars_path(s)) for s in symbols if storage.exists(bars_path(s))]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
