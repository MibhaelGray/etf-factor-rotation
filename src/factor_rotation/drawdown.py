"""The 10% pause (PROJECT.md): when a paper book has lost 10% of its capital from its best value, it opens no new
trades until someone has looked at what went wrong and resumed it by hand. Exits still go out.

The drop is measured in dollars against the book's full capital (portfolio.CAPITAL): $90,000 for the swing book,
$10,000 for the intraday book. It is a drop from the book's high-water mark: its best value at any session's close
or now, and never less than its capital. So a book that is up and gives back 10% of its capital pauses too.

A pause is a file, journal/paper/PAUSE-<book>, written the first time a check finds the drop and kept until
`fr paper resume --book <book>`. Resuming starts the high-water mark again from the book's value at that moment
(drawdown.json), so a book that is still down doesn't pause again straight away.

The order jobs ask `paused(book)` before sending new entries. If the book's value can't be worked out, the check
says so in the log and doesn't stop trading: the hand-set HALT file is the kill switch that never fails open.
"""

import json
import logging
from datetime import date, datetime

from . import journal
from .config import NY

log = logging.getLogger(__name__)

LIMIT = 0.10               # of the book's capital, set 2026-11-12


def _root():
    return journal.JOURNAL / "paper"


def pause_path(book_: str):
    return _root() / f"PAUSE-{book_}"


def _restarts() -> dict:
    p = _root() / "drawdown.json"
    return json.loads(p.read_text()) if p.exists() else {}


def status(book_: str, value: float | None = None) -> dict:
    """{book, capital, limit, value, peak, drop, pause_at, paused}: dollars, from the book's fills."""
    from . import portfolio
    from .paper import book

    capital = portfolio.CAPITAL[book_]
    limit = round(capital * LIMIT, 2)
    if value is None:
        value = next(b["value"] for b in portfolio.accounts()["books"] if b["book"] == book_)
    restart = _restarts().get(book_)
    since = date.fromisoformat(restart["at"][:10]) if restart else None
    peak = restart["value"] if restart else capital
    for r in book.pnl_by_session(portfolio.fills(book_)) if portfolio.fills(book_) else []:
        if since is None or date.fromisoformat(r["date"]) > since:
            peak = max(peak, capital + r["pnl"])
    peak = max(peak, value)
    p = pause_path(book_)
    return {"book": book_, "capital": capital, "limit": limit, "value": round(value, 2), "peak": round(peak, 2),
            "drop": round(peak - value, 2), "pause_at": round(peak - limit, 2),
            "paused": p.read_text().strip() if p.exists() else None}


def paused(book_: str) -> str | None:
    """Why the book may not open new trades, or None. Writes the pause file the first time the drop reaches the
    limit, so the pause holds whatever the book does afterwards."""
    p = pause_path(book_)
    if p.exists():
        return p.read_text().strip() or f"the {book_} book is paused"
    try:
        s = status(book_)
    except Exception:
        log.exception("drawdown check for the %s book", book_)
        return None
    if s["drop"] < s["limit"]:
        return None
    why = (f"the {book_} book is down ${s['drop']:,.0f} from its high of ${s['peak']:,.0f}, past its "
           f"${s['limit']:,.0f} limit (10% of its capital); paused {datetime.now(NY):%Y-%m-%d %-I:%M %p} ET "
           f"until fr paper resume --book {book_}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(why + "\n")
    return why


def resume(book_: str) -> dict:
    """Lift the pause and start the high-water mark again from the book's value now."""
    s = status(book_)
    r = _restarts()
    r[book_] = {"at": datetime.now(NY).isoformat(timespec="seconds"), "value": s["value"], "was": s["paused"]}
    (_root() / "drawdown.json").write_text(json.dumps(r, indent=2) + "\n")
    pause_path(book_).unlink(missing_ok=True)
    return s
