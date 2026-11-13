"""Both paper books as one portfolio, for the dashboard's Portfolio view: positions and profit-and-loss history.

A position is one symbol in one book: the shares still held across that book's open trades, their average cost,
the latest daily-bar price, and what they have made today and in all. The history is each book's running
profit and loss after commissions, from its fills, at each session's close (book.pnl_by_session) or at each
15-minute bar (book.pnl_intraday). "all" is the two books' fills together.

Both books trade one IBKR paper account, so each is given a share of it as its own capital (CAPITAL) and kept
as a sub-account: its value is that capital plus its realized and open profit and loss, after commissions,
from its own fills. Whatever the account is worth beyond the two books is "unassigned": the account's value
before the books began, interest, borrow fees, and any position no book claims. It should move slowly; a jump
there means something is off the books (accounts()).
"""

import json
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd

from . import bars, drawdown, journal
from .config import NY
from .paper import book

BOOKS = ("swing", "intraday")

# Each book's share of the paper account, set 2026-11-12. The swing book's limits (paper/rules.py: $900,000 gross)
# and the intraday book's (three takes at $10,000 a side) fit inside them.
CAPITAL = {"swing": 900_000.0, "intraday": 100_000.0}


def intraday_fills() -> list[dict]:
    """Every fill on the intraday book's orders, regular session and extended hours, oldest first."""
    root = journal.JOURNAL / "intraday"
    out, seen = [], set()
    for kind, key in (("days", "date"), ("nights", "night")):
        for path in sorted((root / kind).glob("*.json")) if (root / kind).exists() else []:
            doc = json.loads(path.read_text())
            for sig in doc["signals"]:
                p = sig.get("paper") or {}
                for f in p.get("fills", []) if not p.get("dry_run") else []:
                    if f["exec_id"] in seen:
                        continue
                    seen.add(f["exec_id"])
                    d = date.fromisoformat(doc[key])
                    at = datetime.fromisoformat(f["time"]).astimezone(NY) if f.get("time") else datetime.combine(d, time(9, 30), NY)
                    out.append({**f, "session": d, "at": at})
    return sorted(out, key=lambda f: f["at"])


def fills(which: str) -> list[dict]:
    swing = book._book_fills() if book.ROOT.exists() else []
    if which == "swing":
        return swing
    if which == "intraday":
        return intraday_fills()
    return sorted([*swing, *intraday_fills()], key=lambda f: f["at"])


def _held(fs: list[dict]) -> dict[str, dict]:
    """symbol -> signed shares held and their average cost, from fills in time order."""
    out: dict[str, dict] = {}
    for f in fs:
        _apply(out, f)
    return out


def deployed(fs: list[dict], now: datetime | None = None) -> dict:
    """How much of a book's money was at work: gross exposure at cost (longs plus shorts, each at its average
    cost) after every fill, averaged over the time since the book's first fill, the peak, and what is out now.
    Calendar time, so a book that is flat overnight (the intraday book) has its idle hours counted against it.
    Return on capital deployed is the book's profit and loss over this average."""
    if not fs:
        return {"now": 0.0, "peak": 0.0, "average": 0.0, "since": None}
    now = now or datetime.now(NY)
    state: dict[str, dict] = {}
    gross = peak = area = 0.0
    last = fs[0]["at"]
    for f in fs:
        area += gross * (f["at"] - last).total_seconds()
        _apply(state, f)
        gross = sum(abs(p["shares"]) * p["cost"] for p in state.values() if round(p["shares"]))
        peak, last = max(peak, gross), f["at"]
    area += gross * max((now - last).total_seconds(), 0)
    span = (now - fs[0]["at"]).total_seconds()
    return {"now": round(gross, 2), "peak": round(peak, 2), "average": round(area / span, 2) if span > 0 else round(gross, 2),
            "since": fs[0]["at"].isoformat()}


def _apply(out: dict[str, dict], f: dict) -> None:
    """One fill into the running positions (see _held)."""
    sign = 1 if f["side"] == "BOT" else -1
    p = out.setdefault(f["symbol"], {"shares": 0.0, "cost": 0.0, "realized": 0.0, "commission": 0.0, "since": f["at"],
                                     "since_session": f["session"]})
    p["commission"] += f.get("commission") or 0
    q, n = p["shares"], sign * f["shares"]
    if q == 0 or (q > 0) == (n > 0):          # opening or adding
        if q == 0:
            p["since"], p["since_session"] = f["at"], f["session"]
        p["cost"] = (abs(q) * p["cost"] + abs(n) * f["price"]) / (abs(q) + abs(n))
        p["shares"] = q + n
    else:                                       # closing, perhaps through zero
        closed = min(abs(q), abs(n))
        p["realized"] += closed * (f["price"] - p["cost"]) * (1 if q > 0 else -1)
        p["shares"] = q + n
        if abs(n) > abs(q):
            p["cost"], p["since"], p["since_session"] = f["price"], f["at"], f["session"]


def positions() -> list[dict]:
    """One row per symbol a book still holds."""
    by_book = {b: _held(fills(b)) for b in BOOKS}
    symbols = sorted({s for held in by_book.values() for s, p in held.items() if round(p["shares"])})
    px = bars.load(symbols) if symbols else pd.DataFrame()
    last, prev = {}, {}
    if not px.empty:
        for s, g in px.sort_values("date").groupby("symbol"):
            last[s] = (float(g["close"].iloc[-1]), g["date"].iloc[-1].date())
            if len(g) > 1:
                prev[s] = float(g["close"].iloc[-2])
    names = {}
    if book.ROOT.exists():
        for t in book.trades():
            if t["state"] != "closed":
                for leg in t["held"]:
                    names.setdefault(("swing", leg["symbol"]), []).append(t["name"])
    for (sym, name) in _intraday_names():
        names.setdefault(("intraday", sym), []).append(name)
    out = []
    for b, held in by_book.items():
        for s, p in held.items():
            q = round(p["shares"])
            if not q:
                continue
            price, on = last.get(s, (p["cost"], None))
            # A close from before the entry's session says nothing about it: a fill at 11:49 PM belongs to the next
            # day's session, so the close earlier that day is not its mark.
            if on is not None and on < p["since_session"]:
                price = p["cost"]
            base = prev.get(s) if on is not None and p["since_session"] < on and s in prev else p["cost"]
            value = q * price
            out.append({"book": b, "symbol": s, "side": "long" if q > 0 else "short", "shares": abs(q), "avg_cost": round(p["cost"], 4),
                        "last": round(price, 4), "price_date": on.isoformat() if on else None, "value": round(value, 2),
                        "pnl": round(q * (price - p["cost"]), 2), "pnl_pct": round((price / p["cost"] - 1) * (1 if q > 0 else -1) * 100, 2),
                        "day_pnl": round(q * (price - base), 2) + 0.0, "since": p["since"].date().isoformat(),
                        "trades": sorted(set(names.get((b, s), [])))})
    return sorted(out, key=lambda r: -abs(r["value"]))


def _intraday_names() -> list[tuple[str, str]]:
    """(symbol, trade name) for each leg of the intraday book's takes that have orders out, from its latest day
    and night files, so its open positions can be shown by trade."""
    out = []
    for kind in ("days", "nights"):
        root = journal.JOURNAL / "intraday" / kind
        files = sorted(root.glob("*.json")) if root.exists() else []
        for sig in json.loads(files[-1].read_text())["signals"] if files else []:
            p = sig.get("paper") or {}
            if p.get("dry_run") or not p.get("fills") or p.get("state") in ("closed", "not sent"):
                continue
            name = f"{'Long' if sig.get('direction') == 'long' else 'Short'} {sig['symbol']} vs {sig.get('hedge')}"
            out += [(s, name) for s in (sig["symbol"], sig.get("hedge")) if s]
    return out


def _syncs(newest_first: bool) -> list[Path]:
    root = book.ROOT / "fills"
    return sorted(root.glob("*.json"), reverse=newest_first) if root.exists() else []


def _last_sync() -> dict:
    """The newest fills file that carries the account's value: when, what it was worth, and IBKR's marks."""
    for path in _syncs(True):
        doc = json.loads(path.read_text())
        if doc.get("net_liquidation") is not None:
            return doc
    return {}


def _first_sync() -> dict:
    """The oldest fills file that carries the account's value: what the account was worth when the books began,
    the fixed base each book's return on the whole account is measured against."""
    for path in _syncs(False):
        doc = json.loads(path.read_text())
        if doc.get("net_liquidation") is not None:
            return doc
    return {}


def accounts(pos: list[dict] | None = None) -> dict:
    """Each book as a sub-account of the paper account, and what's left over.
    Open positions are marked at IBKR's prices from the last sync, the prices the account's own value was struck
    at, so the books and the account agree to the cent when nothing is off the books; a symbol IBKR gave no
    price for falls back to its latest daily close (`pos`, from positions())."""
    sync, first = _last_sync(), _first_sync()
    base = first.get("net_liquidation")
    marks = sync.get("marks") or {}
    fallback = {(r["book"], r["symbol"]): r["last"] for r in (pos if pos is not None else positions())}
    books = []
    for b in BOOKS:
        fs = fills(b)
        held = _held(fs)
        use = deployed(fs)
        realized = sum(p["realized"] for p in held.values())
        commission = sum(p["commission"] for p in held.values())
        open_, at_close = 0.0, []
        for s, p in held.items():
            q = round(p["shares"])
            if not q:
                continue
            price = marks.get(s)
            if price is None:
                price = fallback.get((b, s), p["cost"])
                at_close.append(s)
            open_ += q * (price - p["cost"])
        value = CAPITAL[b] + realized + open_ - commission
        books.append({"book": b, "capital": CAPITAL[b], "realized": round(realized, 2), "open": round(open_, 2),
                      "commission": round(commission, 2), "pnl": round(value - CAPITAL[b], 2), "value": round(value, 2),
                      "return_pct": round((value / CAPITAL[b] - 1) * 100, 3),
                      "account_pct": round((value - CAPITAL[b]) / base * 100, 3) if base else None,
                      "deployed_now": use["now"], "deployed_peak": use["peak"], "deployed_avg": use["average"],
                      "deployed_pct": round((value - CAPITAL[b]) / use["average"] * 100, 3) if use["average"] else None,
                      "marked_at_close": sorted(at_close), **_pause(b, value)})
    nav = sync.get("net_liquidation")
    pnl = sum(x["pnl"] for x in books)
    out = {"net_liquidation": nav, "pulled_at": sync.get("pulled_at"), "books": books, "capital": sum(CAPITAL.values()),
           "start_value": base, "start_session": first.get("session"), "pnl": round(pnl, 2),
           "account_pct": round(pnl / base * 100, 3) if base else None}
    out["unassigned"] = round(nav - sum(x["value"] for x in books), 2) if nav is not None else None
    return out


def _pause(b: str, value: float) -> dict:
    """The 10% pause's numbers for the dashboard: the book's high, where it pauses, and the pause if it is on."""
    try:
        d = drawdown.status(b, value)
    except Exception:
        return {}
    return {"peak": d["peak"], "pause_at": d["pause_at"], "pause_limit": d["limit"], "paused": d["paused"]}


def rows() -> list[dict]:
    """The dashboard's rows: positions, each book's daily history, one summary per book, and the accounts."""
    pos = positions()
    out = [{"section": "portfolio_position", **r} for r in pos]
    acct = {"section": "portfolio_accounts", **accounts(pos)}
    out.append(acct)   # filled in below with each book's change on the day
    for which in ("all", *BOOKS):
        fs = fills(which)
        history = book.pnl_by_session(fs)
        # The day's change: the book's profit and loss now (IBKR's prices, as in its value) less what it was at the
        # close before the latest session. One figure, so the strip and the books agree.
        before = [r for r in history if r["date"] < history[-1]["date"]] if history else []
        now = acct["pnl"] if which == "all" else next(b["pnl"] for b in acct["books"] if b["book"] == which)
        target = acct if which == "all" else next(b for b in acct["books"] if b["book"] == which)
        target["day_pnl"] = round(now - before[-1]["pnl"], 2) if before else None
        if which == "all":
            acct["day_session"] = history[-1]["date"] if history else None
        out += [{"section": "portfolio_pnl", "book": which, **r} for r in history]
        held = _held(fs)
        out.append({"section": "portfolio_book", "book": which, "fills": len(fs),
                    "realized": round(sum(p["realized"] for p in held.values()), 2),
                    "commission": round(sum(p["commission"] for p in held.values()), 2),
                    "total": history[-1]["pnl"] if history else None})
    return out


def pnl_intraday(which: str) -> dict:
    return book.pnl_intraday(fills=fills(which if which in BOOKS else "all"))
