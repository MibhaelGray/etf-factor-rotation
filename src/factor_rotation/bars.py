"""Daily and 15-minute bars from Yahoo Finance.

Daily bars are adjusted for splits and dividends, so close-to-close returns are total returns, which matters
for pairs with very different yields (XLU vs XLK, HYG vs IEF). Within a day open and close share one factor,
so open-to-close returns are unaffected. History is rewritten on each pull because adjustment changes it.

Yahoo can miss a split on its first day, which shows as a fake crash. `_fix_fresh_split` repairs that for
ETFs; any other implausible move is reported as `suspect`, not changed.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from functools import lru_cache

import pandas as pd

from . import storage
from .calendars import NYSE
from .config import NY
from .universe import ETFS

log = logging.getLogger(__name__)

INDEXES = {"VIX": "^VIX", "VIX3M": "^VIX3M", "TNX": "^TNX"}
SCALE = {"TNX": 10}  # stored as yield × 10, as elsewhere in the project
SUSPECT_MOVE = 0.40
CHUNK = 100
PULL_THREADS = 40    # Yahoo answers one request per symbol; 311 names take 5.6 s at yfinance's default and 2.6 s at 40
PULL_TIMEOUT = 3     # yfinance's default is 10 s, so one dropped reply held up a whole refresh
OVERLAP_TOLERANCE = 0.002
QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
QUOTE_FIELDS = "regularMarketOpen,regularMarketDayHigh,regularMarketDayLow,regularMarketPrice,regularMarketVolume,regularMarketTime"
_read_cache: dict[str, tuple[int, pd.DataFrame]] = {}
_yahoo = threading.Lock()  # yf.download keeps its results in shared state, so one call at a time


def bars_path(symbol: str) -> str:
    return f"bars_daily/symbol={symbol.replace(' ', '_')}.parquet"


def _ticker(symbol: str) -> str:
    return INDEXES.get(symbol, symbol.replace(" ", "-"))


def _read(path: str) -> pd.DataFrame | None:
    """A stored file, from memory when it hasn't changed on disk. One dashboard build asks for the same files hundreds
    of times. Treat the result as read-only."""
    stamp = storage.stamp(path)
    if stamp is None:
        return None
    hit = _read_cache.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    df = storage.read(path)
    _read_cache[path] = (stamp, df)
    return df


def _store(df: pd.DataFrame, path: str) -> None:
    storage.write(df, path)
    _read_cache[path] = (storage.stamp(path), df)


def load(symbols: list[str]) -> pd.DataFrame:
    frames = [df for s in symbols if (df := _read(bars_path(s))) is not None]
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


def _frame(symbol: str, raw: pd.DataFrame, repair: bool = True) -> pd.DataFrame:
    df = raw.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna(subset=["open", "close"])
    if df.empty:
        return df
    df = df.rename_axis("date").reset_index()
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
    df[["open", "high", "low", "close"]] *= SCALE.get(symbol, 1)
    df.insert(0, "symbol", symbol)
    return _fix_fresh_split(symbol, df) if repair else df


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


def _merge_recent(new: pd.DataFrame, stored: pd.DataFrame) -> pd.DataFrame | None:
    """The stored history with its last days replaced by a short pull, or None when the two don't line up and the
    name needs its full history again: too little overlap, or a dividend or split that re-based past prices. The
    newest day is left out of the comparison because it may be part-finished."""
    if new.empty or stored.empty or new["date"].max() < stored["date"].max():
        return None
    old = stored.assign(date=pd.to_datetime(stored["date"]))
    both = new[new["date"] < new["date"].max()].merge(old[["date", "close"]], on="date", suffixes=("", "_old"))
    if both.empty or ((both["close"] / both["close_old"] - 1).abs() > OVERLAP_TOLERANCE).any():
        return None
    merged = pd.concat([old[old["date"] < new["date"].min()], new], ignore_index=True)
    return None if new["symbol"].iloc[0] not in INDEXES and _suspect(merged) else merged


def collect_recent(symbols: list[str], days: int = 5) -> dict:
    """The dashboard's quick pull: the last few days for every name, merged into its stored history. Anything that
    doesn't line up gets the full pull, which also repairs splits and reports suspect moves. The 4:30 PM job still
    pulls five full years, so dividend adjustments reach the older history every night."""
    import yfinance as yf
    tickers = {_ticker(s): s for s in symbols}

    def pull(names: list[str]) -> dict[str, pd.DataFrame]:
        with _yahoo:
            data = yf.download(names, period=f"{days}d", interval="1d", auto_adjust=True, group_by="ticker",
                               threads=PULL_THREADS, timeout=PULL_TIMEOUT, progress=False)
        columns = set(data.columns.get_level_values(0)) if not data.empty else set()
        return {t: f for t in names if t in columns and not (f := _frame(tickers[t], data[t], repair=False)).empty}

    got = pull(list(tickers))
    if late := [t for t in tickers if t not in got]:
        got |= pull(late)

    def merge(ticker: str) -> str | None:
        """Merges one name and returns it if it needs the full pull."""
        symbol, path = tickers[ticker], bars_path(tickers[ticker])
        stored = _read(path) if ticker in got else None
        merged = _merge_recent(got[ticker], stored) if stored is not None else None
        if merged is None:
            return symbol
        _store(merged, path)
        return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        full = [s for s in pool.map(merge, tickers) if s]
    stats = collect(full) if full else {"failed": [], "suspect": []}
    log.info("recent_bars_done symbols=%d full=%d", len(symbols), len(full))
    return {"symbols": len(symbols), "full": full, "failed": stats["failed"], "suspect": stats["suspect"]}


@lru_cache(maxsize=8)
def _session_before(day: date) -> date:
    return NYSE.valid_days(day - timedelta(days=10), day - timedelta(days=1))[-1].date()


def _quotes(tickers: list[str], fields: str = QUOTE_FIELDS) -> dict[str, dict]:
    """Yahoo's bulk quote: every name in one request, through the signed-in session yfinance keeps. Not a public
    yfinance call, so any failure returns nothing and the names take the 5-day pull instead."""
    from yfinance.data import YfData

    try:
        with _yahoo:
            body = YfData().get_raw_json(QUOTE_URL, params={"symbols": ",".join(tickers), "fields": fields},
                                         timeout=PULL_TIMEOUT)
        return {q["symbol"]: q for q in body["quoteResponse"]["result"]}
    except Exception as e:
        log.warning("bulk_quote_failed error=%r", e)
        return {}


def _quote_bar(symbol: str, q: dict) -> dict | None:
    """One quote as a daily bar, or None if it's incomplete. Yahoo's open can sit outside its day range, because the
    two come from different feeds, so the range is widened to hold it."""
    o, h, l, c, t = (q.get(f"regularMarket{f}") for f in ("Open", "DayHigh", "DayLow", "Price", "Time"))
    if not all((o, h, l, c, t)):
        return None
    k = SCALE.get(symbol, 1)
    return {"symbol": symbol, "date": pd.Timestamp(datetime.fromtimestamp(t, NY).date()), "open": o * k,
            "high": max(o, h, c) * k, "low": min(o, l, c) * k, "close": c * k,
            "volume": float(q.get("regularMarketVolume") or 0)}


def _merge_quote(bar: dict, stored: pd.DataFrame, today: date) -> pd.DataFrame | None:
    """The stored history with today's bar replaced or added. Returns `stored` itself when the quote is nothing new,
    which includes every name before the open, and None when it can't be placed: the store is behind by more than a
    session, or the move is big enough to be a split. Those take the 5-day pull."""
    day, last = bar["date"].date(), stored["date"].iloc[-1].date()
    if day < last or (day == last and day != today):
        return stored
    if day != today or (day > last and _session_before(day) != last):
        return None
    keep = stored.iloc[:-1] if day == last else stored
    if bar["symbol"] not in INDEXES and not keep.empty and abs(bar["close"] / keep["close"].iloc[-1] - 1) >= SUSPECT_MOVE:
        return None
    return pd.concat([keep, pd.DataFrame([bar])[stored.columns].astype(stored.dtypes.to_dict())], ignore_index=True)


def collect_quotes(symbols: list[str]) -> dict:
    """The dashboard's fastest pull: today's bar for every name from one bulk quote. It can't see a dividend or split
    re-basing past prices, so the server runs `collect_recent` once a day behind it and the 4:30 PM job pulls five
    full years. Anything the quote can't place goes to `collect_recent` straight away."""
    tickers = {_ticker(s): s for s in symbols}
    quotes = _quotes(list(tickers))
    today = datetime.now(NY).date()

    behind, changed = [], []
    for ticker, symbol in tickers.items():
        path = bars_path(symbol)
        stored = _read(path)
        bar = _quote_bar(symbol, quotes[ticker]) if ticker in quotes else None
        merged = _merge_quote(bar, stored, today) if bar and stored is not None and not stored.empty else None
        if merged is None:
            behind.append(symbol)
        elif merged is not stored:
            changed.append((merged, path))
    with ThreadPoolExecutor(max_workers=8) as pool:  # only the writes: the merging is faster on one thread
        list(pool.map(lambda c: _store(*c), changed))
    stats = collect_recent(behind) if behind else {"full": [], "failed": [], "suspect": []}
    log.info("quotes_done symbols=%d behind=%d", len(symbols), len(behind))
    return {"symbols": len(symbols), "behind": behind, "full": stats["full"], "failed": stats["failed"], "suspect": stats["suspect"]}


def collect(symbols: list[str], period: str = "5y") -> dict:
    import yfinance as yf
    failed, suspect = [], []
    for i in range(0, len(symbols), CHUNK):
        chunk = symbols[i:i + CHUNK]
        tickers = {_ticker(s): s for s in chunk}
        with _yahoo:
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


def intraday_closes(symbols: list[str], sessions: int = 5) -> dict[str, pd.Series]:
    """Closes of the last `sessions` sessions' 15-minute regular-session bars from Yahoo, indexed by each bar's
    start in New York time. Nothing is stored."""
    import yfinance as yf
    tickers = {_ticker(s): s for s in symbols}
    with _yahoo:
        data = yf.download(list(tickers), period=f"{sessions}d", interval="15m", prepost=False, auto_adjust=False,
                           group_by="ticker", threads=PULL_THREADS, progress=False)
    out = {}
    for ticker, symbol in tickers.items():
        if ticker not in data.columns.get_level_values(0):
            continue
        close = data[ticker]["Close"].dropna()
        if close.empty:
            continue
        idx = pd.DatetimeIndex(close.index)
        idx = idx.tz_localize("UTC") if idx.tz is None else idx
        out[symbol] = pd.Series(close.to_numpy(float), index=idx.tz_convert(NY)).sort_index()
    return out


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
