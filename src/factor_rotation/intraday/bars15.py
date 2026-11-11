"""15-minute bars, regular session only, one file per name: bars_15m/symbol=SYM.parquet.

Yahoo serves the last 60 days at this size. Each pull is merged into what is stored, so the history grows past
60 days once the job has run for a while. Prices are as traded (not adjusted for dividends), because the book
never holds overnight.
"""

import logging

import numpy as np
import pandas as pd

from .. import bars, storage
from ..config import NY

log = logging.getLogger(__name__)

OPEN_MINUTE = 9 * 60 + 30
SLOTS = 26           # 9:30 AM to 4:00 PM in 15-minute bars; slot i closes at 9:45 AM plus 15 minutes times i
CHUNK = 60
FIELDS = ["open", "high", "low", "close", "volume"]


def path(symbol: str) -> str:
    return f"bars_15m/symbol={symbol.replace(' ', '_')}.parquet"


def _frame(symbol: str, raw: pd.DataFrame) -> pd.DataFrame:
    df = raw.rename(columns=str.lower)[FIELDS].dropna(subset=["open", "close"])
    if df.empty:
        return pd.DataFrame()
    idx = pd.DatetimeIndex(df.index)
    idx = (idx.tz_localize("UTC") if idx.tz is None else idx).tz_convert(NY).tz_localize(None)
    df = df.reset_index(drop=True)
    df.insert(0, "when", idx)
    df.insert(0, "symbol", symbol)
    slot = (idx.hour * 60 + idx.minute - OPEN_MINUTE) // 15
    return df[(slot >= 0) & (slot < SLOTS)].reset_index(drop=True)


def merge(new: pd.DataFrame, stored: pd.DataFrame | None) -> pd.DataFrame:
    """Stored bars with everything from the new pull's first bar on replaced by the pull."""
    if stored is None or stored.empty:
        return new
    return pd.concat([stored[stored["when"] < new["when"].min()], new], ignore_index=True)


def collect(symbols: list[str], period: str = "60d") -> dict:
    import yfinance as yf
    tickers = {bars._ticker(s): s for s in symbols}
    saved, failed = 0, []
    names = list(tickers)
    for i in range(0, len(names), CHUNK):
        chunk = names[i:i + CHUNK]
        with bars._yahoo:
            data = yf.download(chunk, period=period, interval="15m", auto_adjust=False, prepost=False,
                               group_by="ticker", threads=bars.PULL_THREADS, timeout=10, progress=False)
        have = set(data.columns.get_level_values(0)) if not data.empty else set()
        for t in chunk:
            df = _frame(tickers[t], data[t]) if t in have else pd.DataFrame()
            if df.empty:
                failed.append(tickers[t])
                continue
            p = path(tickers[t])
            storage.write(merge(df, storage.read(p) if storage.exists(p) else None), p)
            saved += 1
    if failed:
        log.warning("bars15_failed symbols=%s", ",".join(failed))
    return {"saved": saved, "failed": failed}


def load(symbol: str) -> pd.DataFrame | None:
    p = path(symbol)
    return storage.read(p) if storage.exists(p) else None


def grid(df: pd.DataFrame, days: pd.DatetimeIndex) -> dict[str, np.ndarray]:
    """One name's bars as arrays of shape (days, SLOTS), NaN where a bar is missing."""
    day = df["when"].dt.normalize()
    row = days.get_indexer(day)
    col = ((df["when"].dt.hour * 60 + df["when"].dt.minute - OPEN_MINUTE) // 15).to_numpy()
    keep = row >= 0
    out = {}
    for f in FIELDS:
        a = np.full((len(days), SLOTS), np.nan)
        a[row[keep], col[keep]] = df[f].to_numpy(float)[keep]
        out[f] = a
    return out
