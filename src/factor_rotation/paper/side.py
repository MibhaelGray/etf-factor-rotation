"""Facts recorded beside each night's verdicts, which no research pass reads.

The question (2026-11-14): would more data help the swing book? Adding it to the passes now would change the
process while the ranking is still being scored, and nobody knows yet which data matters. So the facts are
recorded on the side and tested later: after the evening pass has recorded its verdicts, `fr paper side` pulls,
for every stock an idea is about, what Yahoo says about its short interest, its analysts' estimates, ratings
and targets, and its insiders' dealing, and writes them to journal/paper/side/<session>.json. Once the ideas'
plans finish, `fr paper side report` shows whether any fact told the winners from the losers.

Rules that keep the test clean:
  - No pass reads journal/paper/side or runs `fr paper side`, and the record isn't in `fr paper learn` or on the
    dashboard. A fact a pass has seen is no longer a side fact.
  - A name's facts are pulled once, before the session's open. After the open nothing is pulled: the facts would
    hold what happened after the entry.
  - The stock is the subject of an idea, the fund it is hedged with isn't: a fund has none of these facts.

Scoring is the ranking's (learn.rank_table): within each night, the rank correlation between a fact and how the
idea's stock went on to do against its hedge, averaged over the nights. An idea about a basket takes the average
of its members.
"""

import json
import logging
import time as clock
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import mean

from .. import journal
from ..config import NY
from ..universe import ETFS
from . import proposals

log = logging.getLogger(__name__)

OPEN = time(9, 30)
PAUSE = 0.4                   # seconds between names, to stay well inside Yahoo's limits
RECENT = timedelta(days=30)   # the window for rating and target changes

# fact -> what it is, for the report
FACTS = {
    "short_pct_float": "shares sold short, as a share of the float",
    "short_days_to_cover": "days of average volume to buy back the short",
    "short_change": "change in shares short from the report before",
    "est_change_30d": "change in this year's earnings estimate over 30 days",
    "est_change_7d": "change in this year's earnings estimate over 7 days",
    "est_net_up_30d": "analysts raising this year's estimate less those cutting, 30 days",
    "rating_net_30d": "upgrades less downgrades, 30 days",
    "target_net_30d": "price targets raised less lowered, 30 days",
    "target_gap": "average price target over the price, less 1",
    "rating_mean": "average rating, 1 strong buy to 5 sell",
    "insider_net_pct_6m": "insiders' net buying over 6 months, as a share of their holding",
    "insider_buys_6m": "insider purchases, 6 months",
    "insider_sales_6m": "insider sales, 6 months",
}


def path(session: date) -> Path:
    return proposals.root() / "side" / f"{session}.json"


def load(session: date) -> dict | None:
    p = path(session)
    return json.loads(p.read_text()) if p.exists() else None


def subjects(p: dict) -> list[str]:
    """The stocks an idea is about: the legs on the side it is named for, funds left out."""
    return [s for s in p.get(p["direction"]) or [] if s not in ETFS]


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 5) if f == f and abs(f) != float("inf") else None


def _change(now, then) -> float | None:
    now, then = _num(now), _num(then)
    return round((now - then) / abs(then), 5) if now is not None and then else None


def _cell(frame, row, col):
    try:
        return frame.loc[row, col]
    except Exception:  # noqa: BLE001 - a missing row or column is a missing fact
        return None


def pull(symbol: str, now: datetime) -> dict:
    """One name's facts from Yahoo. Every part is tried on its own, so one missing table costs only its facts."""
    import yfinance as yf

    t = yf.Ticker(symbol)
    facts, raw, errors = {}, {}, []

    def part(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - Yahoo's tables come and go; record it and keep the rest
            errors.append(f"{name}: {type(e).__name__}: {e}"[:160])

    def info():
        i = t.info or {}
        raw["type"] = i.get("quoteType")
        facts["short_pct_float"] = _num(i.get("shortPercentOfFloat"))
        facts["short_days_to_cover"] = _num(i.get("shortRatio"))
        facts["short_change"] = _change(i.get("sharesShort"), i.get("sharesShortPriorMonth"))
        if i.get("dateShortInterest"):
            raw["short_as_of"] = datetime.fromtimestamp(i["dateShortInterest"], NY).date().isoformat()
        price, target = _num(i.get("currentPrice") or i.get("regularMarketPrice")), _num(i.get("targetMeanPrice"))
        facts["target_gap"] = round(target / price - 1, 5) if price and target else None
        facts["rating_mean"] = _num(i.get("recommendationMean"))
        raw["analysts"] = i.get("numberOfAnalystOpinions")

    def estimates():
        trend, revs = t.eps_trend, t.eps_revisions
        facts["est_change_30d"] = _change(_cell(trend, "0y", "current"), _cell(trend, "0y", "30daysAgo"))
        facts["est_change_7d"] = _change(_cell(trend, "0y", "current"), _cell(trend, "0y", "7daysAgo"))
        up, down = _num(_cell(revs, "0y", "upLast30days")), _num(_cell(revs, "0y", "downLast30days"))
        facts["est_net_up_30d"] = up - down if up is not None and down is not None else None
        raw["eps_trend"] = {r: {c: _num(_cell(trend, r, c)) for c in ("current", "7daysAgo", "30daysAgo", "90daysAgo")}
                            for r in ("0q", "+1q", "0y", "+1y") if r in getattr(trend, "index", [])}

    def ratings():
        d = t.upgrades_downgrades
        if d is None or not len(d):
            return
        cutoff = (now - RECENT).replace(tzinfo=None)
        d = d[d.index >= cutoff]
        facts["rating_net_30d"] = float((d["Action"] == "up").sum() - (d["Action"] == "down").sum())
        facts["target_net_30d"] = float((d["priceTargetAction"] == "Raises").sum() - (d["priceTargetAction"] == "Lowers").sum())
        raw["rating_actions"] = [{"date": str(i.date()), "firm": r["Firm"], "action": r["Action"], "to": r["ToGrade"],
                                  "target": _num(r["currentPriceTarget"]), "was": _num(r["priorPriceTarget"])}
                                 for i, r in d.head(12).iterrows()]

    def insiders():
        d = t.insider_purchases
        if d is None or not len(d):
            return
        rows = {str(r.iloc[0]): r for _, r in d.iterrows()}
        get = lambda name, col: _num(rows[name][col]) if name in rows else None  # noqa: E731
        facts["insider_net_pct_6m"] = get("% Net Shares Purchased (Sold)", "Shares")
        facts["insider_buys_6m"] = get("Purchases", "Trans")
        facts["insider_sales_6m"] = get("Sales", "Trans")

    part("info", info)
    if raw.get("type") in (None, "EQUITY"):
        for name, fn in (("estimates", estimates), ("ratings", ratings), ("insiders", insiders)):
            part(name, fn)
    out = {"at": now.isoformat(timespec="seconds"), "facts": {k: v for k, v in facts.items() if v is not None}, "raw": raw}
    return {**out, "errors": errors} if errors else out


def record(now: datetime | None = None, puller=pull, dry: bool = False, pause: float = PAUSE) -> dict:
    """Pull the facts for the next session's ideas that don't have them yet. Returns what the run did."""
    now = now or datetime.now(NY)
    session = proposals.next_entry_session(now)
    out = {"session": session.isoformat(), "at": now.isoformat(timespec="seconds"), "pulled": [], "failed": [], "dry_run": dry}
    if now >= datetime.combine(session, OPEN, NY):
        return {**out, "note": "the session has opened, so nothing is pulled"}
    props = (proposals.load(session) or {}).get("proposals") or []
    if not props:
        return {**out, "note": f"no verdicts recorded for {session} yet"}
    doc = load(session) or {"session": session.isoformat(), "note": "Side facts: no research pass reads this file (paper/side.py).",
                            "symbols": {}, "runs": []}
    want = sorted({s for p in props for s in subjects(p)})
    todo = [s for s in want if not (doc["symbols"].get(s) or {}).get("facts")]
    out["have"] = len(want) - len(todo)
    if dry:
        return {**out, "note": f"a look: {len(todo)} names to pull", "todo": todo}
    for i, s in enumerate(todo):
        if i:
            clock.sleep(pause)
        got = puller(s, now)
        doc["symbols"][s] = got
        (out["pulled"] if got.get("facts") else out["failed"]).append(s)
    if todo:
        doc["runs"].append({k: out[k] for k in ("at", "pulled", "failed")})
        path(session).parent.mkdir(parents=True, exist_ok=True)
        path(session).write_text(json.dumps(doc, indent=1) + "\n")
    return out


# ---- Scoring ----------------------------------------------------------------------------------------------
def table(rows: list[dict] | None = None) -> dict:
    """For each fact, whether it told which stocks went on to do well. Within a night, the rank correlation
    between the fact and the stock's move against its hedge (the idea's plan result, turned over for a short), over
    the ideas whose plans have finished; then the nights' average and its t, as learn.rank_table does. A positive
    number means the names with more of the fact did better."""
    from . import learn

    rows = learn.outcomes() if rows is None else rows
    subj = {p["id"]: subjects(p) for p in proposals.every()}
    docs: dict[str, dict] = {}
    nights: dict[str, list[tuple[dict, float]]] = {}
    for r in rows:
        if r["plan_bps"] is None or not subj.get(r["id"]):
            continue
        if r["session"] not in docs:
            docs[r["session"]] = (load(date.fromisoformat(r["session"])) or {}).get("symbols") or {}
        have = [docs[r["session"]][s]["facts"] for s in subj[r["id"]] if (docs[r["session"]].get(s) or {}).get("facts")]
        if have:
            values = {f: mean(v) for f in FACTS if (v := [h[f] for h in have if f in h])}
            nights.setdefault(r["session"], []).append((values, float(r["plan_bps"]) * (1 if r["direction"] == "long" else -1)))
    out = {}
    for f in FACTS:
        ics, ideas = [], 0
        for _, rs in sorted(nights.items()):
            pairs = [(v[f], move) for v, move in rs if f in v]
            ic = learn._spearman([a for a, _ in pairs], [b for _, b in pairs]) if len(pairs) >= learn.MIN_RANKED else None
            if ic is not None:
                ics.append(ic)
                ideas += len(pairs)
        sd = (sum((v - mean(ics)) ** 2 for v in ics) / (len(ics) - 1)) ** 0.5 if len(ics) > 2 else 0
        out[f] = {"nights": len(ics), "ideas": ideas, "ic": round(mean(ics), 3) if ics else None,
                  "t": round(mean(ics) / (sd / len(ics) ** 0.5), 2) if sd else None}
    return {"recorded_nights": len(list((proposals.root() / "side").glob("*.json"))), "scored_nights": len(nights),
            "nights_needed": learn.RANK_NIGHTS, "facts": out}


def report() -> str:
    from . import learn

    t = table()
    lines = [f"Side facts: {t['recorded_nights']} night{'s' if t['recorded_nights'] != 1 else ''} recorded, {t['scored_nights']} with finished plans. No research pass sees these.",
             "Rank correlation, night by night, between each fact and how the idea's stock then did against its hedge.",
             f"Positive: names with more of it did better. An anecdote before about {t['nights_needed']} nights.", ""]
    for f, s in t["facts"].items():
        lines.append(f"  {f:<22} " + (f"{s['ic']:+.2f} over {s['nights']} night{'s' if s['nights'] != 1 else ''}, {s['ideas']} ideas" + (f", t {s['t']:+.1f}" if s["t"] is not None else "")
                                     if s["nights"] else f"no night with {learn.MIN_RANKED} finished ideas yet") + f"   ({FACTS[f]})")
    return "\n".join(lines)
