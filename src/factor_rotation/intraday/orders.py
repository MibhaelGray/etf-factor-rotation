"""The intraday book's paper orders: Haiku's takes only, $10,000 a side, on the swing book's paper account.

At the tick that judges a take, both legs go out as market orders. They fill about a minute after the signal
bar closes, close to where the model fills it (the next bar's open). At the tick whose check ends the trade
(stop, target or time limit), and at the 3:46 PM tick whatever the model says, the filled shares go back out
the same way. A pair with one leg filled and the other dead is closed at the next tick. An exit that didn't
fill is sent again at the next tick.

Order refs start with the signal's id (I-MMDD-SYM|SYMBOL), which matches no swing book plan, so the swing
book's jobs never count these fills as theirs; held_now() tells the swing book's position check what the
intraday book holds. All of paper/broker.py's guards apply. With FR_PAPER_SEND=0 the orders are a dry run and
are only recorded. journal/paper/HALT stops new entries but not exits. The connection uses its own client id
(FR_INTRADAY_CLIENT_ID, default 72), so a tick can run while a swing book job is connected.

What happened lands on each signal as `paper`: its legs, the orders sent, the fills, its state
(entered, unfilled, exiting, closed, not sent, or dry run) and, once closed, its result after commissions.
"""

import asyncio
import json
import logging
import os
from dataclasses import replace
from datetime import date

from .. import journal
from ..paper import rules
from ..paper.broker import WORKING, DryRun, IBPaper, PaperSettings
from . import signals

log = logging.getLogger(__name__)

NOTIONAL = 10_000          # dollars a side
FLAT_CHECK = signals.FLAT_SLOT - 1   # the 3:45 PM bar: its tick closes whatever is still open
SETTLE = 3                 # seconds to wait after sending before pulling fills
HALT = journal.JOURNAL / "paper" / "HALT"


def settings() -> PaperSettings:
    return replace(PaperSettings(), client_id=int(os.environ.get("FR_INTRADAY_CLIENT_ID", "72")))


def broker():
    s = settings()
    return IBPaper(s) if s.send else DryRun(s)


def _legs(sig: dict) -> list[dict] | None:
    name, hedge = sig["symbol"], sig["hedge"]
    longs, shorts = ([name], [hedge]) if sig["direction"] == "long" else ([hedge], [name])
    return rules.size_legs(longs, shorts, {name: sig["numbers"]["price"], hedge: sig.get("hedge_price")}, NOTIONAL)


def _side(leg: dict, opening: bool) -> str:
    buy = (leg["side"] == "long") == opening
    return "BUY" if buy else "SELL"


def _held(p: dict) -> dict[str, int]:
    """symbol -> shares still held on each leg, from the fills (or from the plan, in a dry run)."""
    if p.get("dry_run"):
        return {} if p["state"] == "closed" else {leg["symbol"]: leg["shares"] for leg in p["legs"]}
    out = {}
    for leg in p["legs"]:
        got = [f for f in p.get("fills", []) if f["symbol"] == leg["symbol"]]
        opened = sum(f["shares"] for f in got if f["side"] == ("BOT" if leg["side"] == "long" else "SLD"))
        closed = sum(f["shares"] for f in got if f["side"] == ("SLD" if leg["side"] == "long" else "BOT"))
        out[leg["symbol"]] = int(round(opened - closed))
    return out


def _entered(p: dict) -> dict[str, int]:
    """symbol -> shares filled on entry."""
    return {leg["symbol"]: int(round(sum(f["shares"] for f in p.get("fills", []) if f["symbol"] == leg["symbol"]
                                         and f["side"] == ("BOT" if leg["side"] == "long" else "SLD"))))
            for leg in p["legs"]}


def result(p: dict) -> dict | None:
    """Dollars and basis points of one side, after commissions, once every leg is back to zero."""
    if p.get("dry_run") or any(_held(p).values()) or not any(_entered(p).values()):
        return None
    dollars = sum((-1 if f["side"] == "BOT" else 1) * f["shares"] * f["price"] for f in p.get("fills", []))  # flat, so cash is P/L
    commission = sum(f.get("commission") or 0 for f in p.get("fills", []))
    net = dollars - commission
    return {"dollars": round(net, 2), "commission": round(commission, 2), "bp": round(net / NOTIONAL * 1e4, 1)}


def _exit_orders(sig: dict) -> list[dict]:
    p = sig["paper"]
    by = {leg["symbol"]: leg for leg in p["legs"]}
    return [{"trade_id": sig["id"], "symbol": s, "action": _side(by[s], opening=False), "shares": n, "kind": "MKT"}
            for s, n in _held(p).items() if n > 0]


def _why_exit(sig: dict, slot: int, status: dict) -> str | None:
    """Why an entered trade leaves at this tick, or None to hold it."""
    p = sig["paper"]
    filled = _entered(p)
    dead = [leg["symbol"] for leg in p["legs"] if not filled[leg["symbol"]]
            and status.get((sig["id"], leg["symbol"], _side(leg, True))) not in WORKING]
    if dead and any(filled.values()):
        return f"broken: {', '.join(dead)} never filled"
    if "exit" in sig:
        return sig["exit"]["reason"]
    if slot >= FLAT_CHECK:
        return "flat by 3:45 PM"
    return None


def step(doc: dict, slot: int, save, make_broker=broker) -> list[str]:
    """Send this tick's entries and exits for the day's record `doc`, and pull fills for the trades in flight.
    `save` writes doc; it is called once connected and before anything is sent, so a tick that dies while
    sending can't send the same entry twice. Returns warnings in plain words."""
    warnings: list[str] = []
    new = [x for x in doc["signals"] if (x.get("verdict") or {}).get("decision") == "take" and "paper" not in x]
    live = [x for x in doc["signals"] if (x.get("paper") or {}).get("state") in ("sending", "entered", "exiting", "dry run")]
    if not new and not live:
        return warnings
    if new and HALT.exists():
        for sig in new:
            sig["paper"] = {"state": "not sent", "why": f"halted: {HALT.read_text().strip() or 'journal/paper/HALT exists'}"}
        new = []
    if new and slot >= FLAT_CHECK:
        for sig in new:
            sig["paper"] = {"state": "not sent", "why": "too late: the book is flat by 3:45 PM"}
        new = []
    for sig in new:
        legs = _legs(sig)
        if not legs:
            sig["paper"] = {"state": "not sent", "why": "no price for a leg, or a leg rounds to zero shares"}
            warnings.append(f"{sig['id']}: Haiku took {sig['symbol']} vs {sig['hedge']}, but it couldn't be sized, so no order went out.")
    new = [x for x in new if "paper" not in x]
    if not new and not live:
        return warnings
    try:
        asyncio.run(_session(doc, new, live, slot, save, make_broker, warnings))
    except Exception as e:  # the gateway is down or didn't answer: entries are given up, exits wait for the next tick
        log.exception("intraday orders")
        for sig in new:
            p = sig.get("paper") or {}
            if p.get("saved") and "entry_orders" not in p:   # it may have gone out: the next tick's fills decide
                p["warnings"].append(f"The gateway failed while sending ({e}); the next tick checks the fills.")
            elif "entry_orders" not in p:
                sig["paper"] = {**p, "state": "not sent", "why": f"the paper gateway failed: {e}"}
        warnings.append(f"The paper gateway failed ({e}). "
                        + (f"{len(new)} take{'' if len(new) == 1 else 's'} went unsent. " if new else "")
                        + (f"{len(live)} open trade{'' if len(live) == 1 else 's'} will be checked again at the next tick." if live else ""))
    return warnings


async def _session(doc, new, live, slot, save, make_broker, warnings) -> None:
    async with make_broker() as b:
        dry = isinstance(b, DryRun)
        for sig in new:
            sig["paper"] = {"state": "sending", "dry_run": dry, "legs": _legs(sig), "warnings": []}
        exits: dict[str, str] = {}
        if not dry and live:
            await _pull_fills(b, live)
            status = {(o["trade_id"], o["symbol"], o["action"]): o["status"] for o in await b.orders()}
        else:
            status = {}
        for sig in live:
            p = sig["paper"]
            if p["state"] == "sending":     # a tick died after saving and before recording what it sent
                p["state"] = "entered"
                p["warnings"].append("The tick that sent the entry stopped before recording it; the fills decide what happened.")
            if p["state"] in ("entered", "dry run"):
                if not p.get("dry_run") and not any(_entered(p).values()) and not any(
                        status.get((sig["id"], leg["symbol"], _side(leg, True))) in WORKING for leg in p["legs"]):
                    p["state"] = "unfilled"
                    warnings.append(f"{sig['id']}: neither leg of {sig['symbol']} vs {sig['hedge']} filled, so the trade wasn't taken.")
                    continue
                why = _why_exit(sig, slot, status)
                if why:
                    exits[sig["id"]] = why
            elif p["state"] == "exiting":
                if not any(_held(p).values()):
                    p["state"] = "closed"
                    p["result"] = result(p)
                elif not any(status.get((sig["id"], o["symbol"], o["action"])) in WORKING for o in _exit_orders(sig)):
                    exits[sig["id"]] = p["exit_reason"]
                    p["warnings"].append("The exit didn't fill, so it was sent again.")
                    warnings.append(f"{sig['id']}: the exit for {sig['symbol']} vs {sig['hedge']} didn't fill, so it was sent again.")
        orders = [{"trade_id": sig["id"], "symbol": leg["symbol"], "action": _side(leg, True), "shares": leg["shares"], "kind": "MKT"}
                  for sig in new for leg in sig["paper"]["legs"]]
        by_id = {sig["id"]: sig for sig in live}
        for sid, why in exits.items():
            sig = by_id[sid]
            sig["paper"]["exit_reason"] = why
            orders += _exit_orders(sig)
        if not orders:
            return
        for sig in new:
            sig["paper"]["saved"] = True
        save()
        sent = await b.place(orders)
        for sig in [*new, *(by_id[s] for s in exits)]:
            mine = [o for o in sent if o["trade_id"] == sig["id"]]
            p = sig["paper"]
            if sig in new:
                p["entry_orders"] = mine
                p["state"] = "dry run" if dry else "entered"
            else:
                p.setdefault("exit_orders", []).extend(mine)
                p["state"] = "closed" if dry else "exiting"
            bad = [f"{o['symbol']} {o['status'].lower()}{': ' + o['note'] if o.get('note') else ''}"
                   for o in mine if o["status"] in ("not sent", "Cancelled", "ApiCancelled", "Inactive")]
            if bad:
                p["warnings"].append("; ".join(bad))
                warnings.append(f"{sig['id']} {sig['symbol']} vs {sig['hedge']}: {'; '.join(bad)}.")
        if not dry:
            await asyncio.sleep(SETTLE)
            await _pull_fills(b, [*new, *(by_id[s] for s in exits)])
            for sig in (by_id[s] for s in exits):
                p = sig["paper"]
                if not any(_held(p).values()):
                    p["state"] = "closed"
                    p["result"] = result(p)


async def _pull_fills(b, sigs: list[dict]) -> None:
    """Today's executions for each signal, matched by order ref; replaces what was stored."""
    fills = await b.fills()
    for sig in sigs:
        mine = [f for f in fills if (f.get("order_ref") or "").startswith(sig["id"] + "|")]
        if mine or "fills" not in sig["paper"]:
            sig["paper"]["fills"] = mine


def held_now(day: date) -> dict[str, float]:
    """symbol -> signed shares the intraday book holds today, for the swing book's position check."""
    path = journal.JOURNAL / "intraday" / "days" / f"{day}.json"
    if not path.exists():
        return {}
    out: dict[str, float] = {}
    for sig in json.loads(path.read_text())["signals"]:
        p = sig.get("paper") or {}
        if p.get("dry_run") or "legs" not in p:
            continue
        sides = {leg["symbol"]: 1 if leg["side"] == "long" else -1 for leg in p["legs"]}
        for s, n in _held(p).items():
            if n:
                out[s] = out.get(s, 0) + sides[s] * n
    return out
