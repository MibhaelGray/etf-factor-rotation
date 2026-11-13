"""The swing book's entries outside regular hours: `fr paper early`.

A take doesn't have to wait for the opening auction. Once the evening pass has recorded it, its legs go in as day
limit orders in whatever session is trading:
  after-hours  4:00-8:00 PM ET     SMART, outside regular hours; the order ends at 8:00 PM
  overnight    8:00 PM-3:50 AM ET  IBKR's OVERNIGHT venue, Sunday to Thursday nights; the order ends at 3:50 AM
  pre-market   4:00-9:10 AM ET     SMART, outside regular hours, good until 9:25 AM so it never meets the open
Each order crosses the book to fill at once: a buy is priced at the gateway's ask, a sell at its bid, and the
limit sits LIMIT_SLIP beyond that so a small move doesn't leave it resting. A leg whose spread is wider than
rules.MAX_CROSS_SPREAD (2%) isn't sent: it waits for the next run, or the opening auction. Without a quote from the gateway it
falls back to LIMIT_SLIP through the latest Yahoo price. (Since 2026-11-12: limits through Yahoo's price left
four of six legs resting overnight on 2026-11-11, because the bids had drifted under them.)

The job runs at 9:00 PM, 4:05 AM and 9:00 AM ET (after the pre-open pass). Each run cancels the early orders
still working, pulls the fills, and sends what is left of each take at the current price. A take stood down
by the pre-open pass is cancelled and not sent again. The 9:15 AM entry job (book.open_session) then cancels
whatever is still working, sends market-on-open orders for the shares that didn't fill early, and closes at the
open a take that was stood down after it filled.

The trades are sized at the first run that sees them, with the ids the entry job would have given them, and kept
in journal/paper/early/<session>.json with each run's orders, written before any order goes out. From then on
they are in the book (book.recorded), so the stops, the checks and the dashboard see them whether or not the
9:15 AM entry job runs. Fills go into journal/paper/fills/<session>.json;
IBKR dates a fill after 8:00 PM to the next day anyway. Only entries go out early: exits still go by the 3:45 PM
check and the market-on-close auction.
"""

import asyncio
import logging
from datetime import date, datetime, time

from .. import drawdown, journal
from ..config import NY
from . import proposals, rules
from . import book

log = logging.getLogger(__name__)

LIMIT_SLIP = 0.005
QUOTES = (3, 10)            # tries, seconds between
SETTLE = 3                  # seconds after sending before the fills are pulled
PREMARKET_END = time(9, 10)
GOOD_TILL = "09:25:00"


def venue(now: datetime) -> str | None:
    """Which session a limit order can trade in at `now` (New York), or None between sessions."""
    t, wd, today = now.time(), now.weekday(), now.date()
    if journal.is_trading_day(today) and time(16, 0) <= t < time(20, 0):
        return "extended"
    if (t >= time(20, 0) and wd in (6, 0, 1, 2, 3)) or (t < time(3, 50) and wd in (0, 1, 2, 3, 4)):
        return "overnight"
    if journal.is_trading_day(today) and time(4, 0) <= t < PREMARKET_END:
        return "extended"
    return None


def path(session: date):
    return book.ROOT / "early" / f"{session}.json"


def load(session: date) -> dict | None:
    return book._read(path(session))


def _takes(doc: dict | None) -> list[dict]:
    """The session's takes that still stand."""
    return [p for p in (doc or {}).get("proposals", []) if p["decision"] == "take"
            and (p.get("preopen") or {}).get("decision") != "stand_down"]


def entered(trade: dict) -> dict[str, int]:
    """symbol -> shares of each leg filled on entry so far, from the stored fills."""
    fills = book._fills_by_ref()
    out = {}
    for leg in trade["legs"]:
        side = "BOT" if leg["side"] == "long" else "SLD"
        out[leg["symbol"]] = int(round(sum(f["shares"] for f in fills.get((trade["id"], leg["symbol"]), []) if f["side"] == side)))
    return out


DATA_CLIENT_ID = 76         # the gateway quotes' own connection, apart from the intraday book's (73)


def limit_price(action: str, price: float) -> float:
    return round(price * (1 + LIMIT_SLIP) if action == "BUY" else price * (1 - LIMIT_SLIP), 2)


def crossing(action: str, book_: tuple[float, float] | None, fallback: float | None) -> float | None:
    """The price an order crosses at: the ask for a buy, the bid for a sell; the fallback without a book."""
    if book_:
        return book_[1] if action == "BUY" else book_[0]
    return fallback


async def gateway_books(symbols: list[str], overnight: bool) -> dict[str, tuple[float, float]]:
    """symbol -> (bid, ask) from the paper gateway in the session that is trading; {} if it can't be had."""
    from ..intraday.extended import IBMarket
    try:
        async with IBMarket(client_id=DATA_CLIENT_ID) as m:
            await m.quotes(symbols, overnight)
            return m.books
    except Exception as e:  # noqa: BLE001 - no quote means the Yahoo fallback, not a failed run
        log.warning("early_gateway_quotes_failed %r", e)
        return {}


async def run(now: datetime | None = None, broker=None, dry: bool = False) -> dict:
    """One early-entry run. Returns the run's record, which is also appended to the session's file.
    `dry` is a run by hand: it sizes and prices the entries and prints them, and sends nothing."""
    now = now or datetime.now(NY)
    session = proposals.next_entry_session(now)
    where = venue(now)
    s = book.PaperSettings()
    dry = dry or (broker is None and not s.send)
    record = {"at": now.isoformat(timespec="seconds"), "venue": where, "dry_run": dry, "new": [], "stood_down": [],
              "cancelled": 0, "orders": [], "skipped": [], "warnings": []}
    state = load(session) or {"session": session.isoformat(), "trades": [], "runs": []}
    doc = proposals.load(session)
    if where is None:
        record["note"] = f"{now:%-I:%M %p} ET is between sessions; nothing was sent"
        return record
    if book.HALT.exists():
        record["note"] = f"halted: {book.HALT.read_text().strip() or 'journal/paper/HALT exists'}"
        return record
    if why := drawdown.paused("swing"):
        record["note"] = f"paused: {why}"
        return record
    if book._read(book.ROOT / "plans" / f"{session}.json"):
        record["note"] = f"the {session} entries already went out at the open"
        return record
    if doc is None and not state["trades"]:
        record["note"] = f"no proposals are recorded for {session} yet"
        return record

    standing = {p["id"] for p in _takes(doc)}
    for t in state["trades"]:
        if t["proposal_id"] not in standing and not t.get("stood_down"):
            p = next((p for p in (doc or {}).get("proposals", []) if p["id"] == t["proposal_id"]), {})
            t["stood_down"] = (p.get("preopen") or {}).get("reason") or "no longer a take"
            record["stood_down"].append(t["id"])
    active = [t for t in state["trades"] if not t.get("stood_down")]

    # New takes, sized now with the ids the entry job would give them, inside the same limits.
    have = {t["proposal_id"] for t in state["trades"]}
    fresh = [p for p in _takes(doc) if p["id"] not in have]
    mine = {t["id"] for t in state["trades"]}
    held = [t for t in book.open_trades() if t["id"] not in mine]   # this session's own early trades are `active`
    if fresh:
        symbols = sorted({x for p in fresh for x in [*p["long"], *p["short"]]})
        prices = {**book._closes(symbols), **await asyncio.to_thread(book.quotes, symbols, *QUOTES)}
        used = {x for t in held for x in rules.core_symbols(t)} | {x for t in active for x in rules.core_symbols(t)}
        made, skipped = rules.proposal_trades(fresh, session.isoformat(), prices, used, start=len(state["trades"]) + 1,
                                              room=rules.MAX_NEW - len(active))
        kept, capped = rules.apply_caps(made, [*held, *active])
        state["trades"] += kept
        active += kept
        record["new"] = [t["id"] for t in kept]
        record["skipped"] = [*skipped, *capped]

    if not state["trades"]:
        record["note"] = "no takes to enter"
        _save(session, state, record, dry)
        return record

    async with (broker or book._broker(dry=dry)) as b:
        record["cancelled"] = await b.cancel({t["id"] for t in state["trades"]})
        if not dry:
            await asyncio.sleep(1)
            await book._pull(b, session)
        left = []
        for t in active:
            got = entered(t)
            left += [(t, leg, leg["shares"] - got[leg["symbol"]]) for leg in t["legs"] if leg["shares"] > got[leg["symbol"]]]
        symbols = sorted({leg["symbol"] for _, leg, _ in left})
        live = await asyncio.to_thread(book.quotes, symbols, *QUOTES) if symbols else {}
        books = await gateway_books(symbols, where == "overnight") if symbols and not dry_quotes(broker) else {}
        # A leg whose book is wider than rules.MAX_CROSS_SPREAD isn't crossed. If nothing of its trade has filled
        # yet, the whole trade waits, so a pair never goes in one side at a time; otherwise only that leg waits.
        wide = {}
        for t, leg, _ in left:
            b_ = books.get(leg["symbol"])
            if b_ and not rules.crossable(b_):
                wide.setdefault(t["id"], []).append(leg["symbol"])
        def why(t):
            parts = [f"{s} {rules.spread_pct(books[s]):.1%}" if rules.spread_pct(books[s]) is not None else f"{s} no usable quote"
                     for s in wide[t["id"]]]
            return f"spread wider than {rules.MAX_CROSS_SPREAD:.0%} ({', '.join(parts)})"
        held_back = set()
        for t in active:
            if t["id"] in wide:
                none_in = not any(entered(t).values())
                if none_in:
                    held_back.add(t["id"])
                record["warnings"].append(f"{t['id']}: {why(t)}, so " + ("the whole trade waits" if none_in else "that leg waits")
                                          + " for the next run or the open.")
        orders = []
        for t, leg, n in left:
            action = "BUY" if leg["side"] == "long" else "SELL"
            book_ = books.get(leg["symbol"])
            if t["id"] in held_back or leg["symbol"] in wide.get(t["id"], []):
                continue
            px = crossing(action, book_, live.get(leg["symbol"]))
            if not px:
                record["warnings"].append(f"{t['id']}: no price for {leg['symbol']} in this session, so that leg waits for the next run or the open.")
                continue
            o = {"trade_id": t["id"], "symbol": leg["symbol"], "action": action, "shares": n, "kind": "LMT",
                 "limit": limit_price(action, px), "venue": where, "ref_price": round(px, 4),
                 "priced_on": "gateway bid/ask" if leg["symbol"] in books else "Yahoo",
                 **book_fields(book_)}
            if where == "extended" and now.time() < PREMARKET_END:
                o["good_till"] = f"{session:%Y%m%d} {GOOD_TILL} US/Eastern"
            orders.append(o)
        if orders:
            if not dry:
                _save(session, state, {**record, "orders": orders, "note": "sending"}, dry)  # on disk before anything goes
            record["orders"] = await b.place(orders)
            record["warnings"] += book._order_warnings(record["orders"])
            if not dry:  # an order can fill at once outside regular hours, so the books aren't left behind
                await asyncio.sleep(SETTLE)
                await book._pull(b, session)
    _save(session, state, record, dry)
    return record


def book_fields(book_: tuple[float, float] | None) -> dict:
    """The quote an order saw, kept with it, so the spread rule can be checked against what happened."""
    if not book_:
        return {}
    s = rules.spread_pct(book_)
    return {"bid": book_[0], "ask": book_[1], "spread_pct": round(s * 100, 3) if s is not None else None}


def dry_quotes(broker) -> bool:
    """A stand-in broker in tests: don't reach for the gateway's quotes."""
    return broker is not None and not hasattr(broker, "ib")


def _save(session: date, state: dict, record: dict, dry: bool) -> None:
    if dry:
        return
    runs = [r for r in state["runs"] if r["at"] != record["at"]]
    book._write(path(session), {**state, "runs": [*runs, record]})
