"""The research gate on the automatic book: one written verdict per candidate, and only the takes are traded.

    journal/paper/proposals/<session>.json

`<session>` is the day the entries go out. The evening pass judges every candidate on the screen for that
session: take or pass, a conviction from 1 to 5, and for a take the five steps of reasoning, what would prove
it wrong, the legs and the plan (time limit and stop, inside the outer limits in rules.py). The pre-open pass
then lets each take stand or stands it down, and may add verdicts of its own. Code copies the screen's numbers
into each record, fills in the mechanical legs and plan where the pass gave none, and records a candidate the
pass never got to as "not reviewed". So every candidate has a record, taken or not, and the ones passed on can
be scored the same way as the ones taken (learn.py).

The agent only writes this file, through `fr paper propose`. It never sizes a trade or sends an order: the
9:15 AM entry job reads the takes and does that under the same guards as before.
"""

import json
from datetime import date, datetime, time
from pathlib import Path

from .. import bars, earnings, journal
from ..config import NY
from ..universe import ETFS
from . import rules

PASSES = ("evening", "preopen")
DECISIONS = ("take", "pass")
CHECKS = ("stands", "stand_down")
CUTOFF = time(9, 14)  # the entry job runs at 9:15 AM ET


def root() -> Path:
    return journal.JOURNAL / "paper"


def screen_for(session: date) -> dict | None:
    """The screen whose next session is `session`."""
    for p in sorted((journal.JOURNAL / "screens").glob("*.json"), reverse=True):
        d = json.loads(p.read_text())
        if d.get("next_session") == session.isoformat():
            return d
    return None


def load(session: date) -> dict | None:
    path = root() / "proposals" / f"{session}.json"
    return json.loads(path.read_text()) if path.exists() else None


def every() -> list[dict]:
    """Every proposal ever recorded, oldest first."""
    return [p for path in sorted((root() / "proposals").glob("*.json")) for p in json.loads(path.read_text())["proposals"]]


def next_entry_session(now: datetime) -> date:
    """The session whose entries are still to go out: today before 9:14 AM ET on a trading day, else the next one."""
    today = now.date()
    if journal.is_trading_day(today) and now < datetime.combine(today, CUTOFF, NY):
        return today
    return journal.next_trading_day(today)


def _text(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _side(v, who: str, side: str) -> list[str]:
    names = [v] if isinstance(v, str) else list(v or [])
    if any(not isinstance(s, str) or not journal.SYMBOL.match(s) for s in names) or len(set(names)) != len(names):
        raise ValueError(f"{who}: the {side} side must be a ticker symbol or a list of distinct ones")
    if len(names) > journal.BASKET_SIZE[1]:
        raise ValueError(f"{who}: the {side} side holds more than {journal.BASKET_SIZE[1]} names")
    if bad := sorted(set(names) & journal.NOT_TRADABLE):
        raise ValueError(f"{who}: {', '.join(bad)} is an index and can't be traded")
    return names


def _plan(plan, legs: list[str], who: str) -> dict:
    """A plan inside the outer limits: a time limit of 1 to MAX_HORIZON sessions and a stop on the trade's loss
    from entry between the two STOP_LIMITS, optionally with price levels on a leg's close."""
    if not isinstance(plan, dict):
        raise ValueError(f"{who}: a take needs a plan: {{\"horizon_days\": 3, \"stop\": {{\"spread_pct\": -4}}}}")
    h = plan.get("horizon_days")
    if not isinstance(h, int) or isinstance(h, bool) or not 1 <= h <= rules.MAX_HORIZON:
        raise ValueError(f"{who}: horizon_days must be 1-{rules.MAX_HORIZON} sessions, counting the first")
    stop = plan.get("stop")
    tight, wide = rules.STOP_LIMITS
    pct = stop.get("spread_pct") if isinstance(stop, dict) else None
    if not isinstance(pct, (int, float)) or isinstance(pct, bool) or not -wide <= pct <= -tight:
        raise ValueError(f"{who}: stop.spread_pct must be between -{tight:g} and -{wide:g}: the loss from entry, in percent, that ends the trade")
    out = {"horizon_days": h, "stop": {"spread_pct": float(pct)}}
    if stop.get("rules"):
        for r in stop["rules"]:
            sides = [k for k in ("below", "above") if isinstance(r, dict) and k in r]
            if len(sides) != 1 or r.get("symbol") not in legs or not isinstance(r[sides[0]], (int, float)) or r[sides[0]] <= 0:
                raise ValueError(f"{who}: stop rule {r!r} needs a leg's symbol and one positive below or above price")
        out["stop"]["rules"] = stop["rules"]
    return out


def reports_inside(symbols: list[str], session: date, horizon: int) -> list[str]:
    """Stock legs whose earnings reaction falls inside the time limit, as plain sentences."""
    from ..screen import _Days, _next_report

    stocks = [s for s in symbols if s not in ETFS]
    reports = earnings.load().get("names", {})
    if not stocks or not reports:
        return []
    days = _Days(journal.previous_trading_day(session))
    out = []
    for s in stocks:
        n = _next_report(s, reports, days)
        if n and n["sessions_away"] is not None and n["sessions_away"] <= horizon:
            out.append(f"{s} reports {n['date']} {n['when'].replace('_', ' ')}, which trades on session {n['sessions_away']}")
    return out


def pair_stats(longs: list[str], shorts: list[str]) -> dict | None:
    """How well the short side hedges the long side: correlation and beta of daily returns over 60 sessions.
    None for a one-sided trade or when the bars aren't there."""
    if not longs or not shorts:
        return None
    try:
        df = bars.load([*longs, *shorts])
        rets = df.pivot_table(index="date", columns="symbol", values="close").sort_index().pct_change().iloc[-60:]
        a, b = rets[longs].mean(axis=1, skipna=False), rets[shorts].mean(axis=1, skipna=False)
    except Exception:
        return None
    both = (a.notna() & b.notna())
    if both.sum() < 30 or not b[both].var():
        return None
    a, b = a[both], b[both]
    return {"corr_60d": round(float(a.corr(b)), 2), "beta_60d": round(float(a.cov(b) / b.var()), 2), "sessions": int(both.sum())}


def _signal(c: dict) -> dict:
    """What the screen saw, kept with the verdict so the two can be compared later."""
    return {k: c[k] for k in ("why", "chart", "numbers", "score", "adjust", "hedge", "hedge_fit", "moved_with", "members",
                                 "next_earnings", "also") if k in c}


def _from_screen(c: dict, rank: int, screen: dict) -> dict:
    sides = rules._sides(c)
    return {"name": c["name"], "kind": c["kind"], "setup": c["setup"], "direction": c["direction"],
            "source": f"screen {screen['as_of']} #{rank}", "signal": _signal(c),
            "long": sides[0] if sides else [], "short": sides[1] if sides else [], "plan": rules.default_plan(c) if sides else None}


def _proposal(raw: dict, session: date, screen: dict | None, cands: dict) -> dict:
    who = str(raw.get("candidate") or raw.get("name") or "a proposal")
    if raw.get("candidate"):
        if raw["candidate"] not in cands:
            held = next((c for c in (screen or {}).get("in_book", []) if c["name"] == raw["candidate"]), None)
            if held:
                raise ValueError(f"{who} was left out of the screen for {session}: {held['in_book']}. Leave it out of the draft; "
                                 "the pre-close review handles names the book holds")
            raise ValueError(f"{who} is not a candidate on the screen for {session}. Record it as your own idea: "
                             "name, setup, direction, long and/or short")
        p = _from_screen(cands[raw["candidate"]][1], cands[raw["candidate"]][0], screen)
    else:
        if not _text(raw.get("name")) or raw.get("setup") not in journal.SETUPS or raw.get("direction") not in ("long", "short"):
            raise ValueError(f"{who}: an own idea needs name, direction (long or short) and setup, one of: {', '.join(journal.SETUPS)}")
        p = {"name": raw["name"].strip(), "kind": "own", "setup": raw["setup"], "direction": raw["direction"], "source": "own",
             "signal": None, "long": [], "short": [], "plan": None}
    if raw.get("decision") not in DECISIONS:
        raise ValueError(f"{who}: decision must be take or pass")
    c = raw.get("conviction")
    if not isinstance(c, int) or isinstance(c, bool) or not 1 <= c <= 5:
        raise ValueError(f"{who}: conviction must be 1-5, how strongly you believe the trade works, for a pass too")
    if not _text(raw.get("reason")):
        raise ValueError(f"{who}: give the reason for the verdict in a sentence")
    take = raw["decision"] == "take"
    if take and c < rules.MIN_CONVICTION:
        raise ValueError(f"{who}: a take needs conviction {rules.MIN_CONVICTION} or more; below that it is a pass")
    if "hedge" in raw:  # the candidate against a different fund, or with "hedge": null, on its own
        c0 = cands[raw["candidate"]][1] if raw.get("candidate") else None
        if not c0 or c0["kind"] == "factor" or (c0["kind"] == "basket" and not raw["hedge"]):
            raise ValueError(f"{who}: hedge swaps the fund against a stock, ETF or basket candidate; otherwise give long and short")
        hedge = _side(raw["hedge"], who, "hedge")[:1]
        sides = rules._sides({**c0, "hedge": hedge[0] if hedge else c0["name"]})
        if sides is None:
            raise ValueError(f"{who}: fewer than 3 basket members are left after dropping the ones reporting; give long and short")
        p["long"], p["short"] = sides
    for side in ("long", "short"):  # a side the pass gives replaces that side; "short": [] drops it
        if side in raw:
            p[side] = _side(raw[side], who, side)
    if set(p["long"]) & set(p["short"]):
        raise ValueError(f"{who}: a name is on both sides")
    legs = [*p["long"], *p["short"]]
    if "plan" in raw or take:
        if not legs:
            raise ValueError(f"{who}: give the legs (long and/or short); the screen has no tradable default for it")
        p["plan"] = _plan(raw.get("plan"), legs, who)
    p.update({"decision": raw["decision"], "conviction": c, "reason": raw["reason"].strip(), "reviewed": True,
              "reasoning": None, "wrong_if": None, "hedge_why": None, "through_earnings": None, "preopen": None})
    if take:
        reasoning = raw.get("reasoning")
        if not isinstance(reasoning, dict) or any(not _text(reasoning.get(k)) for k in journal.REASONING):
            raise ValueError(f"{who}: a take needs reasoning with each of: {', '.join(journal.REASONING)}")
        if not _text(raw.get("wrong_if")):
            raise ValueError(f"{who}: a take needs wrong_if: what you would see, inside the time limit, that proves it wrong")
        if p["long"] and p["short"] and not _text(raw.get("hedge_why")):
            raise ValueError(f"{who}: a two-sided take needs hedge_why: why this is the right thing on the other side")
        if (inside := reports_inside(legs, session, p["plan"]["horizon_days"])) and not _text(raw.get("through_earnings")):
            raise ValueError(f"{who}: {'; '.join(inside)}, inside the {p['plan']['horizon_days']}-session time limit. Shorten the "
                             "time limit, or give through_earnings saying why the trade holds through the report")
        p.update({"reasoning": {k: reasoning[k].strip() for k in journal.REASONING}, "wrong_if": raw["wrong_if"].strip(),
                  "hedge_why": (raw.get("hedge_why") or "").strip() or None, "through_earnings": (raw.get("through_earnings") or "").strip() or None})
    elif isinstance(raw.get("reasoning"), dict):
        p["reasoning"] = {k: str(v).strip() for k, v in raw["reasoning"].items() if k in journal.REASONING and _text(v)}
    p["stats"] = pair_stats(p["long"], p["short"])
    return p


def _ranks(raws: list[dict], kind: str) -> list[int | None]:
    """The pass's ranking of the verdicts in one draft: 1 is the idea it believes in most. The evening pass ranks
    every verdict, passes too, so each night gives a whole list to score against the ideas' plan results
    (learn.rank_table) and not just a take or two. Another pass may rank its verdicts or leave all of them unranked."""
    given = [r.get("rank") for r in raws]
    if kind != "evening" and all(g is None for g in given):
        return given
    n = len(raws)
    if any(not isinstance(g, int) or isinstance(g, bool) for g in given) or sorted(given) != list(range(1, n + 1)):
        raise ValueError(f"rank every verdict in the draft from 1 (the idea you believe in most) to {n} (the least), passes and "
                         "own ideas too, with no ties and no gaps: the ranking is scored against each idea's plan result")
    return given


def record(draft: dict, now: datetime | None = None) -> tuple[Path, dict, list[str]]:
    """Add a pass's verdicts to the session's proposals. Returns the path, the file and notes for the pass.

    draft: {"session": "2026-11-10", "pass": "evening" | "preopen", "summary": "...", "market": {...},
            "proposals": [{"candidate": "ABNB", "rank": 1, "decision": "take", "conviction": 3, "reason": "...", ...}],
            "checks": [{"id": "2026-11-10-P01", "decision": "stands" | "stand_down", "reason": "..."}]}
    """
    now = now or datetime.now(NY)
    session = date.fromisoformat(draft["session"])
    kind = draft.get("pass")
    if kind not in PASSES:
        raise ValueError(f"pass must be one of: {', '.join(PASSES)}")
    if session != (expected := next_entry_session(now)):
        raise ValueError(f"proposals are for the next entries, {expected}, not {session}: they must be in by 9:14 AM ET, "
                         "before the 9:15 AM entry job")
    if (root() / "plans" / f"{session}.json").exists():
        raise ValueError(f"the entries for {session} have already gone out")
    path = root() / "proposals" / f"{session}.json"
    doc = load(session) or {"session": session.isoformat(), "screen": None, "passes": [], "proposals": []}
    screen = screen_for(session)
    cands = {c["name"]: (i, c) for i, c in enumerate(screen["candidates"], 1)} if screen else {}
    if kind == "evening" and not screen:
        raise FileNotFoundError(f"no screen with next session {session}; run `fr screen` on settled bars first")
    have = {p["name"]: p for p in doc["proposals"]}
    new, replaced, notes = [], 0, []
    raws = draft.get("proposals", [])
    ranks = _ranks(raws, kind)
    ranked = []
    for raw, rank in zip(raws, ranks):
        p = {**_proposal(raw, session, screen, cands), "session": session.isoformat(), "pass": kind, "recorded_at": now.isoformat(),
             "rank": rank, "ranked": len(raws) if rank else None}
        ranked.append(p)
        old = have.get(p["name"])
        if p["name"] in {x["name"] for x in new} or (old and old["reviewed"]):
            raise ValueError(f"{p['name']} already has a verdict for {session}; a verdict is not rewritten")
        if old:  # recorded as not reviewed by an earlier pass: this is its first real verdict
            doc["proposals"][doc["proposals"].index(old)] = {**p, "id": old["id"]}
            replaced += 1
        else:
            new.append(p)
    order = sorted((p for p in ranked if p["rank"]), key=lambda p: p["rank"])
    for a, b in zip(order, order[1:]):  # the rank is the conviction, drawn finer: the two can't disagree
        if b["conviction"] > a["conviction"]:
            raise ValueError(f"{b['name']} is ranked {b['rank']} with conviction {b['conviction']}, below {a['name']} at rank "
                             f"{a['rank']} with conviction {a['conviction']}: an idea with more conviction ranks higher")
    if kind == "evening":
        judged = set(have) | {p["name"] for p in new}
        for name, (rank, c) in cands.items():
            if name not in judged:
                new.append({**_from_screen(c, rank, screen), "session": session.isoformat(), "pass": kind, "recorded_at": now.isoformat(),
                            "decision": "pass", "conviction": None, "reason": "not reviewed", "reviewed": False, "reasoning": None,
                            "wrong_if": None, "hedge_why": None, "through_earnings": None, "preopen": None, "stats": None,
                            "rank": None, "ranked": None})
                notes.append(f"{name} (screen #{rank}) had no verdict, so it is recorded as not reviewed")
    for k, p in enumerate(new, len(doc["proposals"]) + 1):
        p["id"] = f"{session}-P{k:02d}"
    doc["proposals"] += new
    by_id = {p["id"]: p for p in doc["proposals"]}
    checks = draft.get("checks", [])
    for chk in checks:
        p = by_id.get(chk.get("id"))
        if not p or p["decision"] != "take":
            raise ValueError(f"check {chk.get('id')!r}: no take with that id for {session}")
        if chk.get("decision") not in CHECKS or not _text(chk.get("reason")):
            raise ValueError(f"check {chk['id']}: decision must be stands or stand_down, with a reason")
        p["preopen"] = {"decision": chk["decision"], "reason": chk["reason"].strip(), "at": now.isoformat()}
    if not new and not replaced and not checks:
        raise ValueError("the draft has no proposals and no checks")
    if kind == "preopen":
        notes += [f"{p['id']} {p['name']} was not checked, so it stands" for p in doc["proposals"] if p["decision"] == "take" and not p["preopen"]]
    doc["screen"] = screen["as_of"] if screen else doc["screen"]
    doc["passes"].append({"pass": kind, "at": now.isoformat(), "summary": str(draft.get("summary", "")), "market": draft.get("market", {}),
                          "proposals": len(new) + replaced, "checks": len(checks)})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, default=journal._json_default) + "\n")
    return path, doc, notes


def reviews(session: date) -> dict[str, dict]:
    """The pre-close review's decisions for a session, by trade id (learn.record_review writes them)."""
    path = root() / "reviews" / f"{session}.json"
    return {r["trade_id"]: r for r in json.loads(path.read_text())["reviews"]} if path.exists() else {}
