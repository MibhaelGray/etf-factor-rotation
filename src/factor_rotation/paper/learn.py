"""What the automatic book learns from: the pre-close review, post-mortems, and every proposal's result.

    journal/paper/reviews/<session>.json      the pre-close review: hold, exit early or extend, per open automatic trade
    journal/paper/midday/<session>.json       the midday check: hold or exit now, per open trade (book.midday_session acts)
    journal/paper/postmortems/<date>.json     a tag and a one-line lesson for each finished trade
    journal/paper/lessons.md                  the standing lessons, kept by the research pass and read at its start

Every proposal, taken or passed, is scored against its own plan on daily bars (first open to the stop or the
time limit, before costs), so the passes are a control for the takes: if the takes don't beat them, the
research isn't adding anything. A trade the review managed (closed early or extended) is also compared with
what its original plan would have made. `brief()` puts all of it on one page for the next pass to read.
"""

import json
from datetime import date, datetime, time
from pathlib import Path
from statistics import mean, median

import pandas as pd

from .. import bars, journal
from ..config import NY
from ..universe import ETFS
from . import book, proposals, rules

ACTIONS = ("hold", "exit", "extend")
THESIS = ("intact", "weakened", "broken", "played_out")
TAGS = {
    "right_reason": "made money, and for the reason given",
    "right_luck": "made money, but not for the reason given",
    "thesis_broke": "lost money because the reasoning was wrong",
    "noise": "lost money though the reasoning held: the stop, the tape or the timing",
}
REVIEW_CUTOFF = time(15, 44)  # the exit check runs at 3:45 PM ET
MIDDAY_WINDOW = (time(10, 0), time(14, 45))  # after the open has settled; later than this the pre-close review decides
MIDDAY_ACTIONS = ("hold", "exit")
MIDDAY_EXIT_THESIS = ("broken", "played_out")
SMALL = 30                    # below this many results a group's average is an anecdote
LESSONS = "lessons.md"


def _text(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _write(kind: str, name: str, payload: dict) -> Path:
    path = proposals.root() / kind / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, default=journal._json_default) + "\n")
    return path


# ---- The pre-close review ---------------------------------------------------------------------------------
def record_review(draft: dict, now: datetime | None = None) -> Path:
    """Record the pre-close review, before the 3:45 PM ET exit check reads it. One decision for every open
    automatic trade: hold, exit at today's close, or extend. Stops and time limits stay with the exit check;
    the review can only end a trade sooner, or ask for a winner on its last session to run a little longer.

    draft: {"session": "2026-11-10", "summary": "...",
            "reviews": [{"trade_id": "...", "action": "hold" | "exit" | "extend", "thesis": "intact" | "weakened" |
                         "broken" | "played_out", "reason": "...", "sessions": 2}]}   # sessions: extend only
    """
    now = now or datetime.now(NY)
    session = date.fromisoformat(draft["session"])
    if session != now.date() or not journal.is_trading_day(session):
        raise ValueError(f"the review is recorded during the session it is for, not for {session}")
    if now >= datetime.combine(session, REVIEW_CUTOFF, NY):
        raise ValueError("the review must be in by 3:44 PM ET, before the exit check; today's exits go by stops and time limits alone")
    path = proposals.root() / "reviews" / f"{session}.json"
    if path.exists():
        raise FileExistsError(f"{path} exists; today's review is already recorded")
    open_auto = {t["id"]: t for t in book.open_trades()}
    out = []
    for r in draft.get("reviews", []):
        t = open_auto.get(r.get("trade_id"))
        who = str(r.get("trade_id"))
        if not t:
            raise ValueError(f"{who} is not an open trade")
        if who in {x["trade_id"] for x in out}:
            raise ValueError(f"{who} is reviewed twice")
        if r.get("action") not in ACTIONS or r.get("thesis") not in THESIS or not _text(r.get("reason")):
            raise ValueError(f"{who}: needs action ({'/'.join(ACTIONS)}), thesis ({'/'.join(THESIS)}) and a reason")
        rec = {"trade_id": who, "action": r["action"], "thesis": r["thesis"], "reason": r["reason"].strip()}
        if r["action"] == "extend":
            n = r.get("sessions")
            planned = t.get("planned_horizon_days", t["horizon_days"])
            if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= rules.MAX_EXTENSION:
                raise ValueError(f"{who}: an extension is 1-{rules.MAX_EXTENSION} sessions")
            if t.get("extended"):
                raise ValueError(f"{who} has already been extended once")
            if book._session(t, session) < t["horizon_days"]:
                raise ValueError(f"{who}: only a trade on its last session can be extended; until then it is a hold")
            if planned + n > rules.MAX_HORIZON:
                raise ValueError(f"{who}: {planned} + {n} sessions is past the {rules.MAX_HORIZON}-session limit")
            rec["sessions"] = n
        out.append(rec)
    if missing := sorted(set(open_auto) - {r["trade_id"] for r in out}):
        raise ValueError(f"review every open trade; missing: {', '.join(missing)}")
    return _write("reviews", str(session), {"session": session.isoformat(), "reviewed_at": now.isoformat(),
                                             "summary": str(draft.get("summary", "")), "reviews": out})


# ---- The midday check -------------------------------------------------------------------------------------
def record_midday(draft: dict, now: datetime | None = None) -> Path:
    """Record the midday check: hold or exit now for every open trade. It exists so a trade whose reason has
    visibly gone doesn't sit until the close. An exit is closed at the market by the next `fr paper midday` job
    (book.midday_session), so it needs the thesis broken or played out, not just weakened. It can't extend, add
    or open anything, and the pre-close review still runs on what is left.

    draft: {"session": "2026-11-12", "summary": "...",
            "reviews": [{"trade_id": "...", "action": "hold" | "exit", "thesis": "intact" | "weakened" | "broken" |
                         "played_out", "reason": "..."}]}
    """
    now = now or datetime.now(NY)
    session = date.fromisoformat(draft["session"])
    if session != now.date() or not journal.is_trading_day(session):
        raise ValueError(f"the midday check is recorded during the session it is for, not for {session}")
    if not MIDDAY_WINDOW[0] <= now.time() <= MIDDAY_WINDOW[1]:
        raise ValueError("the midday check is recorded between 10:00 AM and 2:45 PM ET; after that the pre-close review decides")
    path = proposals.root() / "midday" / f"{session}.json"
    if path.exists():
        raise FileExistsError(f"{path} exists; today's midday check is already recorded")
    open_auto = {t["id"]: t for t in book.open_trades()}
    out = []
    for r in draft.get("reviews", []):
        who = str(r.get("trade_id"))
        if who not in open_auto:
            raise ValueError(f"{who} is not an open trade")
        if who in {x["trade_id"] for x in out}:
            raise ValueError(f"{who} is reviewed twice")
        if r.get("action") not in MIDDAY_ACTIONS or r.get("thesis") not in THESIS or not _text(r.get("reason")):
            raise ValueError(f"{who}: needs action ({'/'.join(MIDDAY_ACTIONS)}), thesis ({'/'.join(THESIS)}) and a reason")
        if r["action"] == "exit" and r["thesis"] not in MIDDAY_EXIT_THESIS:
            raise ValueError(f"{who}: a midday exit needs the thesis {' or '.join(MIDDAY_EXIT_THESIS)}; otherwise hold, "
                             "and let the pre-close review and the stop do their jobs")
        out.append({"trade_id": who, "action": r["action"], "thesis": r["thesis"], "reason": r["reason"].strip()})
    if missing := sorted(set(open_auto) - {r["trade_id"] for r in out}):
        raise ValueError(f"check every open trade; missing: {', '.join(missing)}")
    return _write("midday", str(session), {"session": session.isoformat(), "reviewed_at": now.isoformat(),
                                            "summary": str(draft.get("summary", "")), "reviews": out})


# ---- Post-mortems -----------------------------------------------------------------------------------------
def postmortems() -> dict[str, dict]:
    return {m["trade_id"]: m for p in sorted((proposals.root() / "postmortems").glob("*.json"))
            for m in json.loads(p.read_text())["postmortems"]}


def record_postmortems(draft: dict, now: datetime | None = None) -> Path:
    """Tag finished automatic trades and say what each taught. The tag has to agree with the result: the
    question it answers is whether the reasoning, not the P&L, was right.

    draft: {"postmortems": [{"trade_id": "...", "tag": "right_reason" | "right_luck" | "thesis_broke" | "noise",
                             "what_happened": "...", "lesson": "..."}]}
    """
    now = now or datetime.now(NY)
    closed = {r["id"]: r for r in book.rows() if r["state"] == "closed"}
    have = postmortems()
    new = []
    for m in draft.get("postmortems", []):
        who = str(m.get("trade_id"))
        r = closed.get(who)
        if not r:
            raise ValueError(f"{who} is not a closed trade")
        if who in have or who in {x["trade_id"] for x in new}:
            raise ValueError(f"{who} already has a post-mortem")
        if m.get("tag") not in TAGS or not _text(m.get("what_happened")) or not _text(m.get("lesson")):
            raise ValueError(f"{who}: needs tag ({'/'.join(TAGS)}), what_happened and lesson")
        won = (r["bps"] or 0) > 0
        if won != m["tag"].startswith("right"):
            raise ValueError(f"{who} {'made' if won else 'lost'} {abs(r['bps'] or 0):.0f} bp, so its tag is one of: "
                             + ", ".join(t for t in TAGS if t.startswith("right") == won))
        new.append({"trade_id": who, "proposal_id": r.get("proposal_id"), "name": r["name"], "setup": r["setup"], "bps": r["bps"],
                    "tag": m["tag"], "what_happened": m["what_happened"].strip(), "lesson": m["lesson"].strip(), "at": now.isoformat()})
    if not new:
        raise ValueError("the draft has no postmortems")
    path = proposals.root() / "postmortems" / f"{now.date()}.json"
    old = json.loads(path.read_text())["postmortems"] if path.exists() else []
    return _write("postmortems", str(now.date()), {"postmortems": [*old, *new]})


def needs_postmortem() -> list[dict]:
    have = postmortems()
    return [r for r in book.rows() if r["state"] == "closed" and r["id"] not in have
            and not (r["reason"] or "").startswith("broken")]


# ---- Results ----------------------------------------------------------------------------------------------
def _verdict(p: dict) -> str:
    if not p["reviewed"]:
        return "not reviewed"
    if p["decision"] == "take" and (p.get("preopen") or {}).get("decision") == "stand_down":
        return "stood down"
    return p["decision"]


def outcomes() -> list[dict]:
    """One row per proposal: the verdict, the result of its own plan on daily bars (`plan_bps`, before costs,
    None until it finishes), and for a take that was traded, the paper result after commissions."""
    props = proposals.every()
    if not props:
        return []
    paper = {r["proposal_id"]: r for r in book.rows() if r.get("proposal_id")}
    tags = postmortems()
    symbols = sorted({s for p in props for s in [*p["long"], *p["short"]]} | {"SPY"})
    px = bars.load(symbols)
    out = []
    for p in props:
        plan = None
        if p["plan"] and (p["long"] or p["short"]) and not px.empty:
            idea = {"long": p["long"] or None, "short": p["short"] or None, **p["plan"]}
            plan = journal._outcome(idea, date.fromisoformat(p["session"]), px)
        done = bool(plan) and plan["status"] in ("time", "stopped")
        r = paper.get(p["id"])
        reason = (r or {}).get("reason") or ""
        managed = "early exit" if reason.startswith(("review:", "midday check:")) else "extended" if (r or {}).get("extended") else None
        m = tags.get((r or {}).get("id"))
        out.append({
            "id": p["id"], "session": p["session"], "name": p["name"], "kind": p["kind"], "setup": p["setup"], "direction": p["direction"],
            "source": p["source"], "verdict": _verdict(p), "decision": p["decision"], "conviction": p["conviction"], "reason": p["reason"],
            "legs": " / ".join(filter(None, [", ".join(p["long"]) and f"long {', '.join(p['long'])}", ", ".join(p["short"]) and f"short {', '.join(p['short'])}"])),
            "horizon_days": (p["plan"] or {}).get("horizon_days"), "stop_pct": ((p["plan"] or {}).get("stop") or {}).get("spread_pct"),
            "stats": p.get("stats"), "wrong_if": p.get("wrong_if"), "reasoning": p.get("reasoning"), "hedge_why": p.get("hedge_why"),
            "preopen": p.get("preopen"), "why": (p.get("signal") or {}).get("why"),
            "plan_status": plan["status"] if plan else None, "plan_bps": plan["bps"] if done else None,
            "plan_so_far_bps": plan["bps"] if plan and not done and "bps" in plan else None,
            "plan_note": plan.get("note") if plan else None, "plan_exit": plan.get("exit_date") if done else None,
            "trade_id": r and r["id"], "state": r and r["state"], "paper_bps": r["bps"] if r and r["state"] == "closed" else None,
            "exit_reason": reason or None, "managed": managed, "tag": m and m["tag"], "lesson": m and m["lesson"],
        })
    return out


def _stat(values: list[float]) -> dict:
    return {"n": len(values), "mean": round(mean(values), 1), "median": round(median(values), 1),
            "winners": round(sum(v > 0 for v in values) / len(values), 2)} if values else {"n": 0}


def tables(rows: list[dict] | None = None) -> dict:
    """Averages of the finished proposals' plan results by verdict, conviction and setup, the paper results
    of the trades taken, the trades the review managed against their original plans, and post-mortem tags."""
    rows = outcomes() if rows is None else rows
    done = [r for r in rows if r["plan_bps"] is not None]
    by = lambda key: {k: _stat([r["plan_bps"] for r in done if key(r) == k]) for k in sorted({key(r) for r in done}, key=str)}  # noqa: E731
    traded = [r for r in rows if r["paper_bps"] is not None]
    managed = [r for r in traded if r["managed"] and r["plan_bps"] is not None]
    return {
        "proposals": len(rows), "finished": len(done),
        "by_verdict": by(lambda r: r["verdict"]),
        "by_conviction": by(lambda r: r["conviction"] if r["conviction"] is not None else "none"),
        "by_setup": {f"{setup} · {verdict}": s for (setup, verdict), s in by(lambda r: (r["setup"], "take" if r["verdict"] == "take" else "not taken")).items()},
        "paper": _stat([r["paper_bps"] for r in traded]),
        "managed": {kind: {"paper": _stat([r["paper_bps"] for r in managed if r["managed"] == kind]),
                           "plan": _stat([r["plan_bps"] for r in managed if r["managed"] == kind])}
                    for kind in sorted({r["managed"] for r in managed})},
        "by_tag": {tag: sum(r["tag"] == tag for r in rows) for tag in TAGS if any(r["tag"] == tag for r in rows)},
    }


def _line(label: str, s: dict) -> str:
    if not s.get("n"):
        return f"  {label:<34} none finished"
    return (f"  {label:<34} n={s['n']:<4} mean {s['mean']:+7.1f} bp  median {s['median']:+7.1f}  winners {s['winners']:.0%}"
            + ("   (small sample)" if s["n"] < SMALL else ""))


def brief() -> str:
    """One page for the start of a research pass: the record so far, the open trades, what's waiting for a
    post-mortem, and the standing lessons."""
    rows = outcomes()
    t = tables(rows)
    lines = [f"Automatic book record, {datetime.now(NY):%Y-%m-%d %-I:%M %p} ET: {t['proposals']} proposals, {t['finished']} with a finished plan result.",
             "Plan result = the proposal's own plan on daily bars, first open to its stop or time limit, before costs. "
             "The passes are the control: the takes have to beat them.", "", "By verdict"]
    lines += [_line(k, s) for k, s in t["by_verdict"].items()] or ["  nothing finished yet"]
    lines += ["", "By conviction"] + ([_line(f"conviction {k}", s) for k, s in t["by_conviction"].items()] or ["  nothing finished yet"])
    lines += ["", "By setup"] + ([_line(k, s) for k, s in t["by_setup"].items()] or ["  nothing finished yet"])
    lines += ["", "Paper trades closed (after commissions)", _line("all", t["paper"])]
    for kind, s in t["managed"].items():
        lines += [_line(f"{kind}: paper result", s["paper"]), _line(f"{kind}: what the plan would have made", s["plan"])]
    if t["by_tag"]:
        lines += ["", "Post-mortem tags: " + ", ".join(f"{tag} {n}" for tag, n in t["by_tag"].items())]
    recent = sorted(postmortems().values(), key=lambda m: m["at"])[-10:]
    if recent:
        lines += ["", "Latest post-mortems"] + [f"  {m['trade_id']} {m['name']} ({m['setup']}, {m['bps']:+.0f} bp, {m['tag']}): {m['lesson']}" for m in recent]
    if waiting := needs_postmortem():
        lines += ["", "Closed trades with no post-mortem yet"] + [f"  {r['id']} {r['name']} ({r['setup']}): {r['bps']:+.0f} bp, {r['reason']}" for r in waiting]
    open_auto = [r for r in book.rows() if r["state"] in ("open", "planned")]
    if open_auto:
        lines += ["", "Open trades"]
        for r in open_auto:
            lines.append(f"  {r['id']} {r['name']} ({r['setup']}, {r['legs']}): session {r['session_number']} of {r['horizon_days']}, "
                         + (f"{r['bps']:+.0f} bp" if r["bps"] is not None else "no fills yet")
                         + (f", {r['stop_room_pct']:+.1f}% from its stop" if r["stop_room_pct"] is not None else "")
                         + (f". Wrong if: {r['wrong_if']}" if r.get("wrong_if") else ""))
    path = proposals.root() / LESSONS
    lines += ["", f"Standing lessons ({path})", path.read_text().strip() if path.exists() else "  none written yet"]
    return "\n".join(lines)


# ---- Choosing a hedge -------------------------------------------------------------------------------------
def hedges(symbols: list[str], top: int = 6) -> list[dict]:
    """How each ETF we track has moved with these names (equal weight) over 60 sessions, best match first, with
    SPY always shown. A hedge the names barely move with hedges nothing: it adds a second bet."""
    df = bars.load(sorted({*symbols, *ETFS}))
    rets = df.pivot_table(index="date", columns="symbol", values="close").sort_index().pct_change().iloc[-60:]
    if missing := [s for s in symbols if s not in rets]:
        raise ValueError(f"no bars for {', '.join(missing)}")
    a = rets[symbols].mean(axis=1, skipna=False)
    out = []
    for etf in ETFS:
        if etf in symbols or etf not in rets:
            continue
        both = a.notna() & rets[etf].notna()
        if both.sum() < 30 or not rets[etf][both].var():
            continue
        out.append({"hedge": etf, "corr_60d": round(float(a[both].corr(rets[etf][both])), 2),
                    "beta_60d": round(float(a[both].cov(rets[etf][both]) / rets[etf][both].var()), 2)})
    out.sort(key=lambda r: -r["corr_60d"])
    return out[:top] + [r for r in out[top:] if r["hedge"] == "SPY"]
