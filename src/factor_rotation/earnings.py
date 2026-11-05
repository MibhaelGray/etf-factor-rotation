"""Earnings dates for the radar, from Yahoo Finance, in journal/reference/earnings.json.

Dates and EPS figures only: nothing here feeds scoring. yfinance is unofficial, so a name that fails keeps
its last good entry.
"""

import json
import logging
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time

import pandas as pd

from . import journal
from .config import NY
from .universe import CONSTITUENTS

log = logging.getLogger(__name__)

PATH = journal.JOURNAL / "reference" / "earnings.json"
OPEN, CLOSE = time(9, 30), time(16, 0)


def load() -> dict:
    return json.loads(PATH.read_text()) if PATH.exists() else {"names": {}}


def symbols() -> list[str]:
    """Every basket member, the largest stocks and any leg of an unscored idea that is a stock."""
    from .market import basket_library
    from .universe import ETFS
    members = [m["symbol"] for b in basket_library()["baskets"] for m in b["members"]]
    return sorted({*members, *CONSTITUENTS, *journal.pending_legs()} - set(ETFS))


def session(ts: pd.Timestamp) -> str:
    t = ts.tz_convert(NY).time()
    if t == time(0, 0):
        return "unknown"
    return "before_open" if t < OPEN else "after_close" if t >= CLOSE else "during"


def _num(v) -> float | None:
    return None if pd.isna(v) else round(float(v), 2)


def _dates(symbol: str) -> list[dict] | None:
    import yfinance as yf
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            df = yf.Ticker(symbol.replace(" ", "-")).get_earnings_dates(limit=6)
    except Exception as e:
        log.warning("earnings_failed symbol=%s error=%r", symbol, e)
        return None
    if df is None or df.empty:
        log.warning("earnings_empty symbol=%s", symbol)
        return None
    rows = []
    for ts, r in df.sort_index().iterrows():
        rows.append({"date": ts.tz_convert(NY).date().isoformat(), "session": session(ts),
                     "eps_estimate": _num(r.get("EPS Estimate")), "eps_reported": _num(r.get("Reported EPS")),
                     "surprise_pct": _num(r.get("Surprise(%)"))})
    return rows


def pull() -> dict:
    names = symbols()
    with ThreadPoolExecutor(max_workers=4) as pool:
        got = dict(zip(names, pool.map(_dates, names)))
    kept = load().get("names", {})
    failed = sorted(s for s, rows in got.items() if rows is None)
    payload = {"pulled_at": datetime.now(NY).isoformat(timespec="seconds"), "source": "Yahoo Finance via yfinance",
               "names": {s: rows if rows is not None else kept[s] for s, rows in got.items()
                         if rows is not None or s in kept},
               "failed": failed}
    PATH.write_text(json.dumps(payload, indent=1) + "\n")
    return {"path": str(PATH), "names": len(payload["names"]), "failed": failed}
