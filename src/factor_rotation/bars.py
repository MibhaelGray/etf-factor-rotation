"""Daily and 15-minute bars from Yahoo Finance.

Daily bars are adjusted for splits and dividends, so close-to-close returns are total returns, which matters
for pairs with very different yields (XLU vs XLK, HYG vs IEF). Within a day open and close share one factor,
so open-to-close returns are unaffected. History is rewritten on each pull because adjustment changes it.

Yahoo can miss a split on its first day, which shows as a fake crash. `_fix_fresh_split` repairs that for
ETFs; any other implausible move is reported as `suspect`, not changed.
"""

import logging

import pandas as pd

from . import storage
from .config import NY
from .universe import ETFS

log = logging.getLogger(__name__)

INDEXES = {"VIX": "^VIX", "VIX3M": "^VIX3M", "TNX": "^TNX"}
SCALE = {"TNX": 10}  # stored as yield × 10, as elsewhere in the project
SUSPECT_MOVE = 0.40
CHUNK = 100


def bars_path(symbol: str) -> str:
    return f"bars_daily/symbol={symbol.replace(' ', '_')}.parquet"


def _ticker(symbol: str) -> str:
    return INDEXES.get(symbol, symbol.replace(" ", "-"))


def load(symbols: list[str]) -> pd.DataFrame:
    frames = [storage.read(bars_path(s)) for s in symbols if storage.exists(bars_path(s))]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _fix_fresh_split(symbol: str, df: pd.DataFrame) -> pd.DataFrame:
    """Scale an ETF's history when its latest day dropped by a whole-number multiple, as a missed split does."""
    if symbol not in ETFS or len(df) < 2:
        return df
    prev, last = df["close"].iloc[-2], df["close"].iloc[-1]
    if not last / prev - 1 <= -SUSPECT_MOVE:
        return df
    ratio = round(prev / last)
    if ratio < 2 or abs(prev / last / ratio - 1) > 0.15:
        return df
    log.warning("split_repaired symbol=%s ratio=%d", symbol, ratio)
    df = df.copy()
    df.loc[df.index[:-1], ["open", "high", "low", "close"]] /= ratio
    df.loc[df.index[:-1], "volume"] *= ratio
    return df


def _frame(symbol: str, raw: pd.DataFrame) -> pd.DataFrame:
    df = raw.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna(subset=["open", "close"])
    if df.empty:
        return df
    df = df.rename_axis("date").reset_index()
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
    df[["open", "high", "low", "close"]] *= SCALE.get(symbol, 1)
    df.insert(0, "symbol", symbol)
    return _fix_fresh_split(symbol, df)


def _save(symbol: str, df: pd.DataFrame) -> bool:
    """Write a pull unless it holds less than we already have, so a bad response can't shorten the history."""
    path = bars_path(symbol)
    if df.empty:
        return False
    if storage.exists(path) and pd.to_datetime(storage.read(path)["date"]).max() > df["date"].max():
        log.warning("bars_older_than_stored symbol=%s", symbol)
        return False
    storage.write(df, path)
    return True


def _suspect(df: pd.DataFrame) -> bool:
    return len(df) > 1 and bool((df["close"].pct_change().tail(5).abs() >= SUSPECT_MOVE).any())


def collect(symbols: list[str], period: str = "5y") -> dict:
    import yfinance as yf
    failed, suspect = [], []
    for i in range(0, len(symbols), CHUNK):
        chunk = symbols[i:i + CHUNK]
        tickers = {_ticker(s): s for s in chunk}
        data = yf.download(list(tickers), period=period, interval="1d", auto_adjust=True, group_by="ticker",
                           threads=True, progress=False)
        for ticker, symbol in tickers.items():
            df = pd.DataFrame()
            if ticker in data.columns.get_level_values(0):
                df = _frame(symbol, data[ticker])
            if not _save(symbol, df):
                failed.append(symbol)
            elif symbol not in INDEXES and _suspect(df):
                suspect.append(symbol)
    stats = {"symbols": len(symbols), "failed": failed, "suspect": suspect}
    log.info("bars_done %s", stats)
    return stats


def collect_intraday(symbols: list[str], day) -> dict[str, pd.DataFrame]:
    """15-minute bars from 04:00 to 16:00 New York time on `day`; pull after the close."""
    import yfinance as yf
    tickers = {_ticker(s): s for s in symbols}
    data = yf.download(list(tickers), start=day, end=day + pd.Timedelta(days=1), interval="15m", prepost=True,
                       auto_adjust=False, group_by="ticker", threads=True, progress=False)
    out = {}
    for ticker, symbol in tickers.items():
        if ticker not in data.columns.get_level_values(0):
            log.warning("no_intraday_bars symbol=%s", symbol)
            continue
        df = data[ticker].rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])
        idx = pd.DatetimeIndex(df.index)
        idx = idx.tz_localize("UTC") if idx.tz is None else idx
        idx = idx.tz_convert(NY)
        df = df.assign(time=idx.strftime("%H:%M"))
        out[symbol] = df[(idx.date == day) & df["time"].ge("04:00") & df["time"].lt("16:00")].reset_index(drop=True)
    return out
