"""The local dashboard: serves dashboard/ on localhost, with POST /api/refresh and the chart page's GET routes.

A refresh updates the whole page. Today's bar for every name comes from one bulk Yahoo quote; the paper account's
fills and the book's 15-minute P/L overlap the page build, which is every panel, the market brief and the screen
included. Once a day a 5-day pull runs behind it to catch dividends and splits that re-based past prices.
Those two are unsaved versions for display; the journal the research passes read is left alone. Earnings dates
change slowly and can take minutes, so when they are over 12 hours old they update in the background. It reads
nothing from the request and only answers this page, so another website can't trigger it. Charts change
nothing and only serve names on the dashboard.
"""

import asyncio
import json
import logging
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, time, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from multiprocessing import get_context
from time import perf_counter
from urllib.parse import parse_qs, urlsplit

from . import bars, charts, earnings, hormuz, journal, localdash
from .config import NY
from .market import basket_library
from .paper import book
from .paper.broker import IBPaper, PaperSettings
from .universe import bar_universe

log = logging.getLogger(__name__)

EARNINGS_MAX_AGE = timedelta(hours=12)
PAPER_CLIENT_ID = 72  # not the order jobs' id, so a refresh can't collide with them
PAPER_SYNC_LIMIT = 150  # seconds: the connect's retries plus the requests' own timeouts, so a stuck gateway can't hold the lock
_lock = threading.Lock()
_earnings_running = threading.Lock()
_check_running = threading.Lock()
_hormuz_running = threading.Lock()
_checked_on = None
_workers = None


def _symbols() -> list[str]:
    lib = basket_library()["baskets"]
    names = {*bar_universe(), *bars.INDEXES, *(b["hedge"] for b in lib),
             *(m["symbol"] for b in lib for m in b["members"]), *book.symbols_held()}
    return sorted(names)


def _chart_names() -> list[dict]:
    """Every stock and ETF on the dashboard, with the baskets it sits in as a label."""
    labels: dict[str, list[str]] = {}
    for b in basket_library()["baskets"]:
        for m in b["members"]:
            labels.setdefault(m["symbol"], []).append(b["name"])
    names = {*_symbols(), *earnings.symbols()} - set(bars.INDEXES)
    return [{"symbol": s, "label": ", ".join(labels.get(s, []))} for s in sorted(names)]


def _earnings_stale() -> bool:
    pulled = earnings.load().get("pulled_at")
    return not pulled or datetime.now(NY) - datetime.fromisoformat(pulled) > EARNINGS_MAX_AGE


def _sync_paper() -> str | None:
    """Today's fills, positions and account value, read-only. Returns a note when they couldn't be read.
    Before the open a sync would show orders still waiting for the auction as never filled."""
    now = datetime.now(NY)
    if not PaperSettings().account or not journal.is_trading_day(now.date()) or now.time() < time(9, 31):
        return None
    try:
        asyncio.run(asyncio.wait_for(book.sync(now.date(), IBPaper(PaperSettings(client_id=PAPER_CLIENT_ID), readonly=True)),
                                     PAPER_SYNC_LIMIT))
    except Exception as e:  # the rest of the refresh is still worth having
        log.warning("paper_sync_failed error=%r", e)
        return "Paper fills couldn't be read. Is the Docker gateway running?"
    return None


def _earnings_in_background() -> str | None:
    """Starts a pull of earnings dates when they are stale, and rebuilds the page when it finishes. One at a time."""
    if not _earnings_stale() or not _earnings_running.acquire(blocking=False):
        return None

    def work():
        try:
            earnings.pull()
            with _lock:
                localdash.build()
        except Exception:
            log.exception("earnings_refresh_failed")
        finally:
            _earnings_running.release()

    threading.Thread(target=work, daemon=True).start()
    return "Earnings dates are updating in the background and will show on your next refresh"


def _hormuz_in_background() -> None:
    """Ship transits and futures history for the Hormuz view, when over six hours old. The page is rebuilt after."""
    if not hormuz.stale() or not _hormuz_running.acquire(blocking=False):
        return

    def work():
        try:
            hormuz.pull()
            with _lock:
                localdash.build()
        except Exception:
            log.exception("hormuz_refresh_failed")
        finally:
            _hormuz_running.release()

    threading.Thread(target=work, daemon=True).start()


def _hormuz_quotes() -> None:
    try:
        hormuz.pull_quotes()
    except Exception as e:
        log.warning("hormuz_quotes_failed error=%r", e)


def _intraday_pnl() -> dict | None:
    try:
        return book.pnl_intraday()
    except Exception as e:  # the chart falls back to asking for it when the page opens
        log.warning("paper_pnl_failed error=%r", e)
        return None


def _daily_check_in_background(symbols: list[str]) -> None:
    """Once a day, behind a refresh: the 5-day pull that catches past prices Yahoo has re-based for a dividend or
    split, and replaces today's bar from the chart feed. The page is rebuilt only if a name needed its full history."""
    global _checked_on
    today = datetime.now(NY).date()
    if _checked_on == today or not _check_running.acquire(blocking=False):
        return

    def work():
        global _checked_on
        try:
            with _lock:
                out = bars.collect_recent(symbols)
                _checked_on = today
                if out["full"]:
                    localdash.build()
            if out["failed"] or out["suspect"]:
                log.warning("daily_check failed=%s suspect=%s", out["failed"], out["suspect"])
        except Exception:
            log.exception("daily_check_failed")
        finally:
            _check_running.release()

    threading.Thread(target=work, daemon=True).start()


def refresh() -> dict:
    started = perf_counter()
    symbols = _symbols()
    with ThreadPoolExecutor(max_workers=2) as pool:
        synced = pool.submit(_sync_paper)
        pulled = bars.collect_quotes(symbols)
        _hormuz_quotes()
        _hormuz_in_background()
        note = _earnings_in_background()
        paper = synced.result()  # the book's panels, the shortlist and the P/L all count today's fills
        info = localdash.build(pnl_intraday=pool.submit(_intraday_pnl), workers=_workers)
    _daily_check_in_background(symbols)
    return {"failed": pulled["failed"], "suspect": pulled["suspect"], "market": info["market"], "paper": paper,
            "earnings": note, "seconds": round(perf_counter() - started, 1)}


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info(fmt, *args)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _chart(self, query: str) -> None:
        symbol = parse_qs(query).get("symbol", [""])[0]
        if symbol not in {n["symbol"] for n in _chart_names()}:
            return self._json(404, {"error": f"{symbol or 'That name'} isn't on the dashboard"})
        try:
            self._json(200, charts.chart(symbol))
        except Exception as e:
            log.exception("chart_failed symbol=%s", symbol)
            self._json(502, {"error": str(e)})

    def _paper_pnl(self):
        from .paper import book

        try:
            self._json(200, book.pnl_intraday())
        except Exception as e:
            log.exception("paper_pnl_failed")
            self._json(502, {"error": str(e)})

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path in ("/", ""):
            self.send_response(302)
            self.send_header("Location", "/local.html")
            self.end_headers()
            return
        if url.path == "/api/chart/names":
            return self._json(200, {"names": _chart_names()})
        if url.path == "/api/chart":
            return self._chart(url.query)
        if url.path == "/api/paper/pnl":
            return self._paper_pnl()
        super().do_GET()

    def do_POST(self):
        if self.path != "/api/refresh":
            return self._json(404, {"error": "not found"})
        if self.headers.get("Origin") != f"http://{self.headers.get('Host')}":
            return self._json(403, {"error": "refresh only runs from this page"})
        if not _lock.acquire(timeout=20):  # the daily check or an earnings rebuild may be finishing
            return self._json(409, {"error": "a refresh is already running"})
        try:
            self._json(200, refresh())
        except Exception as e:
            log.exception("refresh_failed")
            self._json(500, {"error": str(e)})
        finally:
            _lock.release()


def _warm_worker() -> None:
    """Imports the heavy libraries and reads the bars into this worker's memory, so the first refresh is as fast."""
    from . import screen  # noqa: F401

    bars.load(_symbols())


def _start_workers() -> None:
    global _workers
    _workers = ProcessPoolExecutor(max_workers=2, mp_context=get_context("spawn"))
    for _ in range(2):
        _workers.submit(_warm_worker)


def run(port: int = 8765) -> None:
    localdash.build()
    _start_workers()
    _hormuz_in_background()
    server = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(localdash.DASHBOARD)))
    log.info("dashboard at http://localhost:%d/local.html", port)
    server.serve_forever()
