"""The local dashboard with a Refresh button: serves dashboard/ on localhost and handles POST /api/refresh.

A refresh pulls bars from Yahoo, marks today's unscored ideas, and rebuilds dashboard/local.html. It reads
nothing from the request and only answers this page, so another website can't trigger it.
"""

import json
import logging
import threading
from datetime import datetime, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from . import bars, earnings, journal, localdash
from .config import NY
from .market import basket_library
from .universe import bar_universe

log = logging.getLogger(__name__)

EARNINGS_MAX_AGE = timedelta(hours=12)
_lock = threading.Lock()


def _symbols() -> list[str]:
    lib = basket_library()["baskets"]
    names = {*bar_universe(), *journal.pending_legs(), *bars.INDEXES, *(b["hedge"] for b in lib),
             *(m["symbol"] for b in lib for m in b["members"])}
    return sorted(names)


def _marks() -> dict | None:
    """Today's open and latest price for every leg not yet scored, in the shape journal.ledger expects."""
    now = datetime.now(NY)
    symbols = sorted({*journal.pending_legs(), "SPY"})
    frame = bars.load(symbols)
    if frame.empty:
        return None
    today = frame[frame["date"].dt.date == now.date()]
    prices = {r["symbol"]: {"open": float(r["open"]), "last": float(r["close"])} for _, r in today.iterrows()}
    return {"at": int(now.timestamp()), "prices": prices} if prices else None


def _earnings_stale() -> bool:
    pulled = earnings.load().get("pulled_at")
    return not pulled or datetime.now(NY) - datetime.fromisoformat(pulled) > EARNINGS_MAX_AGE


def refresh() -> dict:
    pulled = bars.collect(_symbols())
    if _earnings_stale():
        earnings.pull()
    info = localdash.build(marks=_marks())
    return {"failed": pulled["failed"], "suspect": pulled["suspect"], "ideas": info["ideas"], "market": info["market"]}


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

    def do_GET(self):
        if self.path in ("/", ""):
            self.send_response(302)
            self.send_header("Location", "/local.html")
            self.end_headers()
            return
        super().do_GET()

    def do_POST(self):
        if self.path != "/api/refresh":
            return self._json(404, {"error": "not found"})
        if self.headers.get("Origin") != f"http://{self.headers.get('Host')}":
            return self._json(403, {"error": "refresh only runs from this page"})
        if not _lock.acquire(blocking=False):
            return self._json(409, {"error": "a refresh is already running"})
        try:
            self._json(200, refresh())
        except Exception as e:
            log.exception("refresh_failed")
            self._json(500, {"error": str(e)})
        finally:
            _lock.release()


def run(port: int = 8765) -> None:
    localdash.build()
    server = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(localdash.DASHBOARD)))
    log.info("dashboard at http://localhost:%d/local.html", port)
    server.serve_forever()
