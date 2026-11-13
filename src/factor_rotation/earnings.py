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
from .universe import CONSTITUENTS, INDEX_STOCKS

log = logging.getLogger(__name__)

PATH = journal.JOURNAL / "reference" / "earnings.json"
OPEN, CLOSE = time(9, 30), time(16, 0)


def load() -> dict:
    return json.loads(PATH.read_text()) if PATH.exists() else {"names": {}}


def symbols() -> list[str]:
    """Every basket member, every stock the swing screen covers and any stock the paper book holds."""
    from .market import basket_library
    from .paper import book
    from .universe import ETFS
    members = [m["symbol"] for b in basket_library()["baskets"] for m in b["members"]]
    return sorted({*members, *CONSTITUENTS, *INDEX_STOCKS, *(book.symbols_held() if book.ROOT.exists() else set())} - set(ETFS))


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


# Yahoo starts refusing after about 500 quick calls (2026-11-12), so a night's pull is kept near 400:
# - the core names (the largest stocks, basket members, anything the paper book holds) every night, as before;
# - from the wider swing tier (universe.INDEX_STOCKS), any name with no dates yet or a report within REPORT_WINDOW
#   days either side of today, then the names pulled longest ago, up to WIDE_PER_NIGHT in all.
# Each name's last good pull is kept in `pulled`; a name that fails keeps its last good dates. Names that fail are
# tried again, fewer at a time, after a pause.
WORKERS = 4
RETRIES = ((30, 2), (90, 1))   # (seconds to wait, workers) for each retry pass
WIDE_PER_NIGHT = 150
REPORT_WINDOW = (3, 10)        # days before today, days after


def _due(names: list[str], kept: dict, pulled: dict, today) -> list[str]:
    """The names to pull tonight, core first (see above)."""
    from datetime import date, timedelta
    wide = set(INDEX_STOCKS) - set(_core())
    core = [s for s in names if s not in wide]
    lo, hi = today - timedelta(days=REPORT_WINDOW[0]), today + timedelta(days=REPORT_WINDOW[1])

    def near(s: str) -> bool:
        return any(lo <= date.fromisoformat(r["date"]) <= hi for r in kept.get(s, []))

    rest = [s for s in names if s in wide]
    urgent = [s for s in rest if s not in kept or near(s)]
    stale = sorted((s for s in rest if s not in urgent), key=lambda s: (pulled.get(s, ""), s))
    return core + (urgent + stale)[:WIDE_PER_NIGHT]


def _core() -> set[str]:
    from .market import basket_library
    from .paper import book
    members = {m["symbol"] for b in basket_library()["baskets"] for m in b["members"]}
    return {*members, *CONSTITUENTS, *(book.symbols_held() if book.ROOT.exists() else set())}


def pull(sleep=None, now=None) -> dict:
    from time import sleep as _sleep
    sleep = sleep or _sleep
    now = now or datetime.now(NY)
    old = load()
    kept, pulled = old.get("names", {}), old.get("pulled", {})
    names = symbols()
    todo = _due(names, kept, pulled, now.date())
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        got = dict(zip(todo, pool.map(_dates, todo)))
    for wait, workers in RETRIES:
        again = [s for s, rows in got.items() if rows is None]
        if not again:
            break
        log.info("earnings_retry names=%d wait=%ds", len(again), wait)
        sleep(wait)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            got.update(dict(zip(again, pool.map(_dates, again))))
    failed = sorted(s for s, rows in got.items() if rows is None)
    stamp = now.isoformat(timespec="seconds")
    wanted = set(names)
    payload = {"pulled_at": stamp, "source": "Yahoo Finance via yfinance",
               "names": {s: got[s] if got.get(s) is not None else kept[s] for s in sorted(wanted)
                         if got.get(s) is not None or s in kept},
               "pulled": {s: stamp if got.get(s) is not None else pulled[s] for s in sorted(wanted)
                          if got.get(s) is not None or s in pulled},
               "failed": failed}
    PATH.write_text(json.dumps(payload, indent=1) + "\n")
    return {"path": str(PATH), "names": len(payload["names"]), "pulled": len(todo), "failed": failed}
