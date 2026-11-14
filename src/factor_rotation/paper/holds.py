"""Which hold period pays: every idea scored at every hold from 1 to 10 sessions.

The pass gives each idea its own time limit, so the record only ever shows one hold per idea. This asks the
other question (2026-11-14): if every idea had been held 1 session, 2, ... 10, where does the result build and
where does it fade, and does the research's edge (takes over passes, the better-ranked over the worse) last 2
sessions or 10? `fr paper holds` prints it.

Nothing new is stored. Every proposal, taken or passed, is already in journal/paper/proposals with its legs and
session, and its legs get daily bars for a month after (book.symbols_held), so the whole table is rebuilt from the
journal and the bars each time, for every night back to the first.

How an idea is scored, the same way as its plan result (journal._outcome): in at the first session's open, each
side weighted evenly, the result at each later close in bp of one side, before costs. Twice over:
  - held: no stop, out at the close of session h.
  - with its stop: the idea's own stop checked at each close; once hit, that close is the result at every longer hold.

Reading it:
  - "the pass's own ideas" are the ones it found itself from what is coming (up to three a night); the rest came
    from the screen. An own idea the pass turned down with no legs or plan can't be scored and isn't counted.
  - A hold's column has only the ideas with that many sessions behind them, so the short holds have more ideas
    than the long ones. The clean comparison across holds is the ideas with all 10 sessions in, shown separately.
  - A fixed hold runs through earnings reports the pass's own limit would have stepped out before.
  - Ideas of one night move together, so the edge rows are worked night by night and then averaged, as the ranking
    is (learn.rank_table). An anecdote before about 20 nights.
  - No pass reads this (the skill says so): a pass that had seen "3 sessions is best" off a few nights would chase it.
"""

from datetime import date
from statistics import mean

from .. import bars, journal
from . import learn, proposals, rules

HOLDS = list(range(1, rules.MAX_HORIZON + 1))
THIRDS = ("top third", "middle third", "bottom third")


def _prices(symbols: list[str]) -> dict[str, dict[date, tuple[float, float]]]:
    """symbol -> day -> (open, close)."""
    px = bars.load(symbols)
    if px.empty:
        return {}
    return {s: {d.date(): (o, c) for d, o, c in zip(g["date"], g["open"], g["close"])} for s, g in px.groupby("symbol")}


def _path(p: dict, px: dict, settled) -> list[float] | None:
    """The idea's result in bp at the close of each session so far, up to 10, from the first open. None when the
    first session has no settled bars for every leg."""
    w = journal._weights({"long": p["long"] or None, "short": p["short"] or None})
    session = date.fromisoformat(p["session"])
    first = {s: px.get(s, {}).get(session) for s in w}
    if not w or any(v is None for v in first.values()):
        return None
    out = []
    for d in journal.sessions_from(session, HOLDS[-1]):
        today = {s: px[s].get(d) for s in w}
        if any(v is None for v in today.values()) or not settled(d):
            break
        out.append(round(sum(w[s] * (today[s][1] / first[s][0] - 1) for s in w) * 1e4, 1))
    return out or None


def _stopped(path: list[float], stop_pct: float | None) -> list[float]:
    """The path with the stop applied: from the close that is past the stop, the result stays there."""
    out, hit = [], None
    for v in path:
        if hit is None and stop_pct is not None and v / 100 <= stop_pct:
            hit = v
        out.append(v if hit is None else hit)
    return out


def rows() -> list[dict]:
    """One row per idea that could be traded as written: who it was, and its two paths."""
    props = {p["id"]: p for p in proposals.every()}
    px = _prices(sorted({s for p in props.values() for s in [*p["long"], *p["short"]]}))
    seen: dict[date, bool] = {}

    def settled(d: date) -> bool:
        if d not in seen:
            seen[d] = journal._bars_settled(d)
        return seen[d]

    out = []
    for r in learn.outcomes():
        p = props[r["id"]]
        if not p["plan"] or r["plan_status"] in (None, "skipped"):
            continue
        path = _path(p, px, settled)
        if not path:
            continue
        third = THIRDS[min(2, (r["rank"] - 1) * 3 // r["ranked"])] if r.get("rank") else None
        out.append({"id": r["id"], "session": r["session"], "name": r["name"], "setup": r["setup"], "taken": r["verdict"] == "take", "own": p["kind"] == "own",
                    "rank": r.get("rank"), "third": third, "limit": p["plan"]["horizon_days"], "plan_bps": r["plan_bps"],
                    "held": path, "stopped": _stopped(path, (p["plan"].get("stop") or {}).get("spread_pct"))})
    return out


def _by_hold(rs: list[dict], key: str) -> dict[int, dict]:
    return {h: learn._stat([r[key][h - 1] for r in rs if len(r[key]) >= h]) for h in HOLDS}


def _nights(values: list[float]) -> dict:
    """The average of one number per night, and its t."""
    if not values:
        return {"nights": 0}
    m = mean(values)
    sd = (sum((v - m) ** 2 for v in values) / (len(values) - 1)) ** 0.5 if len(values) > 2 else 0
    return {"nights": len(values), "mean": round(m, 3), "t": round(m / (sd / len(values) ** 0.5), 2) if sd else None}


def _edges(rs: list[dict]) -> dict:
    """Per hold, night by night: the rank correlation between the pass's rank and the held result, and the takes'
    average less the rest's."""
    nights: dict[str, list[dict]] = {}
    for r in rs:
        nights.setdefault(r["session"], []).append(r)
    rank, takes = {}, {}
    for h in HOLDS:
        ics, gaps = [], []
        for _, group in sorted(nights.items()):
            have = [r for r in group if len(r["held"]) >= h]
            ranked = [r for r in have if r["rank"]]
            ic = learn._spearman([-r["rank"] for r in ranked], [r["held"][h - 1] for r in ranked]) if len(ranked) >= learn.MIN_RANKED else None
            if ic is not None:
                ics.append(ic)
            took, rest = [r["held"][h - 1] for r in have if r["taken"]], [r["held"][h - 1] for r in have if not r["taken"]]
            if took and rest:
                gaps.append(mean(took) - mean(rest))
        rank[h], takes[h] = _nights(ics), _nights(gaps)
    return {"rank": rank, "takes_less_rest": takes}


def _cohort(rs: list[dict]) -> dict:
    groups = {"all ideas": rs, "takes": [r for r in rs if r["taken"]], "not taken": [r for r in rs if not r["taken"]],
              **{t: [r for r in rs if r["third"] == t] for t in THIRDS},
              # where the idea came from: a candidate the screen put up, or one the pass found itself (kind "own")
              "from the screen": [r for r in rs if not r["own"]], "the pass's own ideas": [r for r in rs if r["own"]]}
    done = lambda g: [r for r in g if r["plan_bps"] is not None]  # noqa: E731
    return {"ideas": len(rs),
            "held": {name: _by_hold(g, "held") for name, g in groups.items()},
            "stopped": {name: _by_hold(g, "stopped") for name, g in groups.items()},
            "own_limit": {name: {**learn._stat([r["plan_bps"] for r in done(g)]),
                                 **({"sessions": round(mean(r["limit"] for r in done(g)), 1)} if done(g) else {})} for name, g in groups.items()},
            "by_setup": {s: _by_hold([r for r in rs if r["setup"] == s], "held") for s in sorted({r["setup"] for r in rs})},
            "edges": _edges(rs)}


def table(rs: list[dict] | None = None) -> dict:
    """`so_far`: every idea, each hold counting the ideas that have reached it. `complete`: only the ideas with
    all 10 sessions in, so every hold is the same ideas."""
    rs = rows() if rs is None else rs
    return {"holds": HOLDS, "nights": len({r["session"] for r in rs}), "nights_needed": learn.RANK_NIGHTS,
            "so_far": _cohort(rs), "complete": _cohort([r for r in rs if len(r["held"]) == HOLDS[-1]])}


def _cells(by_hold: dict[int, dict], fmt=lambda s: f"{s['mean']:+.0f}") -> str:
    return "".join(f"{fmt(by_hold[h]) if by_hold[h].get('n') else '.':>7}" for h in HOLDS)


def _block(c: dict) -> list[str]:
    head = f"  {'sessions held':<22}" + "".join(f"{h:>7}" for h in HOLDS) + "   own limit"
    own = lambda name: (f"   {c['own_limit'][name]['mean']:+.0f} ({c['own_limit'][name]['sessions']:g} sessions, n={c['own_limit'][name]['n']})"  # noqa: E731
                        if c["own_limit"][name].get("n") else "   .")
    lines = ["  Average result, bp, held with no stop", head,
             f"  {'ideas counted':<22}" + _cells(c["held"]["all ideas"], lambda s: str(s["n"]))]
    lines += [f"  {name:<22}" + _cells(c["held"][name]) + own(name) for name in c["held"] if c["held"][name][1].get("n")]
    lines += ["", "  Average result, bp, with each idea's own stop", head]
    lines += [f"  {name:<22}" + _cells(c["stopped"][name]) for name in c["stopped"] if c["stopped"][name][1].get("n")]
    lines += ["", "  By setup, bp, held with no stop (all ideas)", head]
    lines += [f"  {name[:22]:<22}" + _cells(s) for name, s in c["by_setup"].items()]
    e = c["edges"]
    counted = lambda key: f"  {'  nights counted':<22}" + "".join(f"{e[key][h]['nights'] or '.':>7}" for h in HOLDS)  # noqa: E731
    lines += ["", "  The research's edge at each hold, worked night by night and averaged", head.split("   own")[0],
              f"  {'rank correlation':<22}" + "".join(f"{e['rank'][h]['mean']:>+7.2f}" if e["rank"][h]["nights"] else f"{'.':>7}" for h in HOLDS),
              counted("rank"),
              f"  {'takes less the rest':<22}" + "".join(f"{e['takes_less_rest'][h]['mean']:>+7.0f}" if e["takes_less_rest"][h]["nights"] else f"{'.':>7}" for h in HOLDS),
              counted("takes_less_rest")]
    return lines


def report() -> str:
    t = table()
    a, b = t["so_far"], t["complete"]
    lines = [f"Hold periods: {a['ideas']} ideas from {t['nights']} night{'s' if t['nights'] != 1 else ''}, each scored as if held 1 to {HOLDS[-1]} sessions "
             "from its first open. Before costs. No research pass sees this.",
             f"\"own limit\" is the idea's own plan: its time limit and its stop. An anecdote before about {t['nights_needed']} nights; a dot means no ideas yet.", ""]
    if not a["ideas"]:
        return "\n".join(lines + ["  no idea has a settled first session yet"])
    lines += ["Every idea so far (a hold counts only the ideas that have reached it, so the columns are not the same ideas)", *_block(a), ""]
    lines += ([f"The same {b['ideas']} ideas at every hold (all {HOLDS[-1]} sessions in): the clean comparison", *_block(b)] if b["ideas"]
              else [f"No idea has all {HOLDS[-1]} sessions in yet. Once some do, they are shown here on their own: the same ideas at every hold."])
    return "\n".join(lines)
