"""One name's chart for the local dashboard: 15-minute bars and its technicals, pulled from Yahoo on request.

Nothing here is saved. Averages, RSI and ATR on 15-minute bars use the regular session only, because thin
pre-market and after-hours bars would distort them. The daily figures are the same ones the nightly brief uses,
worked out from a fresh daily pull, so they include today's session so far.
"""

import logging
from datetime import date, datetime

import numpy as np
import pandas as pd

from . import bars, earnings
from .analytics.technicals import atr, rsi, symbol_technicals
from .config import NY

log = logging.getLogger(__name__)

BAR_AVERAGES = (20, 50, 200)
DAY_AVERAGES = (20, 50, 200)
OPEN_MINUTE, CLOSE_MINUTE = 9 * 60 + 30, 16 * 60


def _history(symbol: str, **kwargs) -> pd.DataFrame:
    import yfinance as yf
    return yf.Ticker(bars._ticker(symbol)).history(**kwargs)


def _daily(symbol: str) -> pd.DataFrame:
    return bars._frame(symbol, _history(symbol, period="2y", interval="1d", auto_adjust=True))


def _intraday(symbol: str) -> pd.DataFrame:
    raw = _history(symbol, period="60d", interval="15m", prepost=True, auto_adjust=False)
    df = raw.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna(subset=["open", "close"])
    idx = pd.DatetimeIndex(df.index)
    idx = (idx.tz_localize("UTC") if idx.tz is None else idx).tz_convert(NY)
    minutes = idx.hour * 60 + idx.minute
    df = df.reset_index(drop=True)
    df["when"] = idx.tz_localize(None)
    df["regular"] = (minutes >= OPEN_MINUTE) & (minutes < CLOSE_MINUTE)
    # Yahoo reports no volume outside the session and its highs and lows there can be bad prints (NVDA showed
    # a 33% range on 2026-10-01), so those bars keep only their open and close.
    body = df[["open", "close"]]
    df["high"] = df["high"].where(df["regular"], body.max(axis=1))
    df["low"] = df["low"].where(df["regular"], body.min(axis=1))
    return df


def _with_indicators(df: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    session = df[df["regular"]]
    for n in BAR_AVERAGES:
        df[f"bar{n}"] = session["close"].rolling(n).mean()
    df["rsi"] = rsi(session["close"])
    df["atr"] = atr(session)
    closes = daily.set_index("date")["close"].sort_index()
    days = df["when"].dt.normalize()
    for n in DAY_AVERAGES:
        df[f"day{n}"] = closes.rolling(n).mean().reindex(days, method="ffill").to_numpy()
    return df


def _number(v, places: int = 4) -> float | None:
    return None if v is None or pd.isna(v) or np.isinf(v) else round(float(v), places)


def _bar_rows(df: pd.DataFrame) -> list[dict]:
    lines = [f"bar{n}" for n in BAR_AVERAGES] + [f"day{n}" for n in DAY_AVERAGES] + ["rsi", "atr"]
    seconds = (df["when"] - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)  # New York clock time
    rows = []
    for t, r in zip(seconds, df.to_dict("records")):
        row = {"t": int(t), "o": _number(r["open"]), "h": _number(r["high"]), "l": _number(r["low"]),
               "c": _number(r["close"]), "v": _number(r["volume"], 0), "regular": bool(r["regular"])}
        row.update({k: _number(r[k]) for k in lines})
        rows.append(row)
    return rows


def _daily_figures(symbol: str, daily: pd.DataFrame) -> dict | None:
    frames = [daily] if symbol == "SPY" else [daily, _daily("SPY")]
    table = symbol_technicals(pd.concat(frames, ignore_index=True))
    row = table[table["symbol"] == symbol]
    if row.empty:
        return None
    return {k: (str(v.date()) if isinstance(v, pd.Timestamp) else None if k == "symbol" else _number(v))
            for k, v in row.iloc[0].items() if k != "symbol"}


def _next_earnings(symbol: str, today: date) -> dict | None:
    rows = earnings.load().get("names", {}).get(symbol, [])
    ahead = [r for r in rows if r["date"] >= today.isoformat() and r.get("eps_reported") is None]
    return {"date": ahead[0]["date"], "session": ahead[0]["session"]} if ahead else None


def chart(symbol: str) -> dict:
    daily, intraday = _daily(symbol), _intraday(symbol)
    if daily.empty or intraday.empty:
        raise LookupError(f"Yahoo returned no bars for {symbol}")
    df = _with_indicators(intraday, daily)
    regular = df[df["regular"]]
    last = regular.iloc[-1]
    now = datetime.now(NY)
    return {
        "symbol": symbol,
        "pulled_at": now.isoformat(timespec="seconds"),
        "bars": _bar_rows(df),
        "daily": _daily_figures(symbol, daily),
        "intraday": {"close": _number(last["close"]), "rsi14": _number(last["rsi"]), "atr14": _number(last["atr"]),
                     "atr_pct": _number(last["atr"] / last["close"], 6),
                     **{f"dist_bar{n}": _number(last["close"] / last[f"bar{n}"] - 1, 6) for n in BAR_AVERAGES}},
        "next_earnings": _next_earnings(symbol, now.date()),
    }
