"""The intraday book's 15-minute tick: signals are judged and tracked, and Haiku's takes are traded on the paper
account (orders.py).

Each tick, a minute after a bar closes:
  1. pulls today's 15-minute bars;
  2. moves every signal recorded earlier today along: its entry at the open after the signal, then each
     close checked against its stop, target and time limit, out at the open after the check that ends it;
  3. finds the setups that fired at the bar just closed, skips names that reported earnings since the last
     close and names the automatic book holds, and asks Haiku to take or pass on the strongest few;
  4. sends market orders for the new takes, and for the paper trades whose exit the model now sees.

Takes and passes are both tracked to their exit by the model, because the test of the model is whether its
takes beat its passes; a take's real fills are kept beside its model result. The day's record is journal/intraday/days/<date>.json.
"""

import json
import logging
from datetime import date, datetime

import numpy as np
import pandas as pd

from .. import journal
from ..calendars import NYSE
from ..config import NY
from . import bars15, model, orders, replay, signals

log = logging.getLogger(__name__)

ROOT = journal.JOURNAL / "intraday" / "days"
JUDGED_PER_TICK = 4        # the strongest signals at one bar go to the model; the rest are recorded unjudged
MAX_TAKES = 5              # a day
MAX_OPEN = 3               # takes held at once


def _mode() -> str:
    return "paper" if orders.settings().send else "rehearsal"


def path(day: date):
    return ROOT / f"{day}.json"


def load(day: date) -> dict:
    p = path(day)
    return json.loads(p.read_text()) if p.exists() else {"date": day.isoformat(), "mode": _mode(), "ticks": [], "signals": []}


def _save(day: date, doc: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    path(day).write_text(json.dumps(doc, indent=1) + "\n")


def last_slot(now: datetime) -> int:
    """The newest bar that has closed: slot i closes at 9:45 AM plus 15 minutes times i."""
    return (now.hour * 60 + now.minute - bars15.OPEN_MINUTE) // 15 - 1


def full_session(day: date) -> bool:
    """False on a half day, which has no 3:45 PM bar to be flat by; the book sits those out."""
    import pandas_market_calendars as mcal
    sched = mcal.get_calendar("XNYS").schedule(day, day)
    return not sched.empty and sched["market_close"].iloc[0].tz_convert(NY).hour == 16


def known(arrays: dict, slot: int) -> dict:
    """A day's bars as they stood just after `slot` closed: later bars blank, except the next bar's open,
    which is where a trade decided now is filled."""
    out = {}
    for k, v in arrays.items():
        v = v.copy()
        v[slot + (2 if k == "open" else 1):] = np.nan
        out[k] = v
    return out


def _advance(sig: dict, s: dict, h: dict) -> None:
    """Fill in a signal's entry and exit once the bars that decide them exist."""
    e = sig["slot"] + 1
    if "entry" not in sig and e < signals.SLOTS and not (np.isnan(s["open"][e]) or np.isnan(h["open"][e])):
        sig["entry"] = {"at": replay.slot_time(e - 1), "name": round(float(s["open"][e]), 4), "hedge": round(float(h["open"][e]), 4)}
    if "exit" in sig or "entry" not in sig:
        return
    seen = np.flatnonzero(~np.isnan(s["close"]) & ~np.isnan(h["close"]))
    if len(seen) and seen[-1] >= e:
        side = 1 if sig["direction"] == "long" else -1
        j = seen[-1]
        sig["so_far_bp"] = round(side * ((s["close"][j] / s["open"][e] - 1) - (h["close"][j] / h["open"][e] - 1)) * 1e4, 1)
    out = signals.score(sig, s, h, sig["atr_pct"])
    if out:
        sig.pop("so_far_bp", None)
        sig["exit"] = {"at": replay.slot_time(out["exit_slot"] - 1), "reason": out["reason"], "bars": out["bars"],
                       "result_bp": round(out["result"] * 1e4, 1),
                       "after_costs_bp": round((out["result"] - replay.COST) * 1e4, 1)}


def _refresh(today: date) -> None:
    spy = bars15.load("SPY")
    prior = journal.previous_trading_day(today)
    stale = spy is None or spy["when"].dt.date.max() < prior
    bars15.collect(replay.universe(), period="60d" if stale else "1d")


def tick(now: datetime | None = None, decide=model.decide, news=model.headlines, pull=_refresh, held=None,
         broker=orders.broker, dry: bool = False) -> dict:
    """One tick. `dry` is a look: the setups run and the model still judges, but nothing is written to
    the journal and no order leaves the machine. A run by hand is dry unless it says otherwise."""
    now = now or datetime.now(NY)
    today, slot = now.date(), min(last_slot(now), signals.SLOTS - 1)
    if not journal.is_trading_day(today) or not full_session(today) or slot < 0:
        return {"date": today.isoformat(), "skipped": "market closed, a half day, or before the first bar closed"}
    pull(today)
    p = replay.prepare(pd.Timestamp(today))
    d = len(p.days) - 1
    doc = load(today)
    by_name = {sig["symbol"]: sig for sig in doc["signals"]}
    if held is None:
        from ..paper import book
        held = {leg["symbol"] for t in book.trades() if t["state"] != "closed" for leg in t["legs"]} if book.ROOT.exists() else set()

    hits = []
    for sym, hedge, s, h, rsi15, ctx, reported in replay.candidates(p, d):
        s, h = known(s, slot), known(h, slot)
        if sym in by_name:
            _advance(by_name[sym], s, h)
            continue
        if slot > signals.LAST_SIGNAL or np.isnan(s["close"][slot]) or np.isnan(h["close"][slot]):
            continue
        hit = signals.first_hit(s, h, rsi15, ctx)
        if not hit or hit["slot"] != slot:
            continue
        skip = "reported earnings since the last close" if reported else "held by the swing book" if sym in held else None
        stop = signals.stop_pct(ctx.atr_pct)
        hits.append({"id": f"I-{today:%m%d}-{sym.replace(' ', '')}", "symbol": sym, "hedge": hedge, "beta": round(ctx.beta, 2),
                     "setup": hit["setup"], "direction": hit["direction"], "slot": slot, "signal_at": replay.slot_time(slot),
                     "numbers": {**hit["numbers"], "price": round(float(s["close"][slot]), 2),
                                 "name_day_pct": round((s["close"][slot] / ctx.prev_close - 1) * 100, 2),
                                 "hedge_day_pct": round((h["close"][slot] / ctx.hedge_prev_close - 1) * 100, 2)},
                     "atr_pct": round(ctx.atr_pct, 4), "hedge_price": round(float(h["close"][slot]), 4),
                     "plan": {"stop_pct": round(-stop * 100, 2), "target_pct": round(stop * signals.TARGET_ATR / signals.STOP_ATR * 100, 2),
                              "max_bars": signals.MAX_BARS.get(hit["setup"]), "flat_by": "3:45 PM"},
                     **({"skipped": skip} if skip else {})})

    takes = [x for x in doc["signals"] if (x.get("verdict") or {}).get("decision") == "take"]
    open_takes = sum(1 for x in takes if "exit" not in x)
    ranked = sorted(hits, key=lambda x: -x["numbers"]["rvol"])
    to_judge = []
    for sig in ranked:
        if "skipped" in sig:
            sig["verdict"] = {"decision": "not judged", "why": sig["skipped"]}
        elif len(to_judge) >= JUDGED_PER_TICK:
            sig["verdict"] = {"decision": "not judged", "why": f"more than {JUDGED_PER_TICK} signals at this bar; the heaviest volume went first"}
        elif len(takes) >= MAX_TAKES or open_takes >= MAX_OPEN:
            sig["verdict"] = {"decision": "not judged", "why": f"the book is full ({len(takes)} takes today, {open_takes} open)"}
        else:
            to_judge.append(sig)
    model.judge_all(to_judge, decide, news)   # researched side by side: a verdict takes a minute or two
    judged = len(to_judge)
    for sig in ranked:
        if sig in to_judge and sig["verdict"]["decision"] == "take":
            if len(takes) >= MAX_TAKES or open_takes >= MAX_OPEN:   # judged together, so a later take can find the book full
                sig["paper"] = {"state": "not sent", "why": f"the book filled at this bar ({len(takes)} takes today, {open_takes} open)"}
            else:
                open_takes += 1
            takes.append(sig)
        doc["signals"].append(sig)

    save = (lambda: None) if dry else (lambda: _save(today, doc))
    warnings = orders.step(doc, slot, save, (lambda: orders.DryRun()) if dry else broker)
    doc["ticks"].append({"at": now.isoformat(timespec="seconds"), "bar_closed": replay.slot_time(slot), "signals": len(hits), "judged": judged,
                         **({"warnings": warnings} if warnings else {})})
    if not dry:
        _save(today, doc)
    return {"date": today.isoformat(), "dry": dry, "bar_closed": replay.slot_time(slot), "new": hits, "open": sum(1 for x in doc["signals"] if "exit" not in x),
            "total": len(doc["signals"]), "warnings": warnings,
            "paper": [{"id": x["id"], **{k: x["paper"].get(k) for k in ("state", "exit_reason", "result")}} for x in doc["signals"] if "paper" in x]}


def report() -> dict:
    """Every rehearsal day so far: what the takes, the passes and the unjudged signals made by the same rules."""
    groups: dict[str, list[float]] = {"take": [], "pass": [], "undecided": [], "not judged": []}
    days = sorted(ROOT.glob("*.json")) if ROOT.exists() else []
    for f in days:
        for sig in json.loads(f.read_text())["signals"]:
            if "exit" in sig:
                groups[(sig.get("verdict") or {}).get("decision", "not judged")].append(sig["exit"]["result_bp"] / 1e4)
    return {"days": len(days), **{k: replay._stats(pd.Series(v, dtype=float), max(len(days), 1)) for k, v in groups.items()}}


SHOWN_DAYS = 5


def rows() -> list[dict]:
    """The dashboard's rows: one summary and every signal of the last few rehearsal days, newest first."""
    import os
    files = sorted(ROOT.glob("*.json")) if ROOT.exists() else []
    docs = [json.loads(f.read_text()) for f in files[-SHOWN_DAYS:]]
    last = docs[-1] if docs else None
    tick = last["ticks"][-1] if last and last["ticks"] else None
    out = [{"section": "intraday", "kind": "summary", "mode": _mode(), "cost_bp": replay.COST * 1e4,
            "last_tick": tick and tick["at"], "last_bar": tick and tick["bar_closed"], "last_day": last and last["date"],
            "model": model.MODEL, "signed_in": bool(os.environ.get("FR_CLAUDE_TOKEN")), "max_takes": MAX_TAKES, "score": report()}]
    for doc in reversed(docs):
        for sig in sorted(doc["signals"], key=lambda x: -x["slot"]):
            v = sig.get("verdict") or {}
            out.append({"section": "intraday", "kind": "signal", "date": doc["date"],
                        **{k: sig.get(k) for k in ("id", "symbol", "hedge", "setup", "direction", "signal_at", "slot", "numbers", "plan",
                                                    "entry", "exit", "so_far_bp", "headlines")},
                        "paper": sig.get("paper") and {k: sig["paper"].get(k) for k in ("state", "why", "legs", "exit_reason", "result", "warnings")},
                        "decision": v.get("decision", "not judged"), "conviction": v.get("conviction"), "why": v.get("why") or v.get("error"),
                        "against": v.get("against"), "catalyst": v.get("catalyst"), "coming": v.get("coming"),
                        "sources": v.get("sources"), "researched": v.get("prompt", 0) >= 4 and v.get("seconds")})
    return out
