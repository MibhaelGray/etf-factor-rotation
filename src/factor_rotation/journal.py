"""The ideas journal and scorecard.

Each night:
  brief   -> journal/briefs/<as_of>.json     every input the ideas were built from
  record  -> journal/ideas/<trade_date>.json the 2-3 ideas, reasoning, and the brief's hash
Before the open:
  check   -> journal/checks/<trade_date>.json  stands / stand down for each of last night's ideas
  morning -> journal/morning/ideas/<trade_date>.json  0-2 new ideas from the overnight session, a separate book
Next evening:
  score   -> journal/scores/<trade_date>.json realized spread returns per idea
             journal/morning/scores/<trade_date>.json the same for the morning book
When every idea in a session has finished its time limit:
          -> journal/outcomes/<trade_date>.json (and journal/morning/outcomes/) the result over the whole plan

Files are write-once: recording over an existing ideas or score file is refused,
so the record can't be edited after the outcome is known.

Every idea is an outright long, an outright short, or a dollar-neutral pair,
entered at the next session's open. Either side can be one fund or stock, or a named basket of 3-10
stocks held in equal weight. Two results are kept: the score, open to close on the first session (how
every idea has always been compared, with close-to-close alongside), and the outcome over the idea's own
plan: its time limit of 1-5 sessions, its stop checked on each close, and its skip-at-open levels.
Each new idea also carries its setup and the five steps of reasoning behind it (REASONING).
"""

import hashlib
import json
import os
import re
from datetime import date, datetime, time
from pathlib import Path

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal

from . import storage
from .analytics import flows
from .analytics.compute import latest_snapshot
from .analytics.technicals import factor_pair_technicals, regime, symbol_technicals
from .config import NY
from .bars import bars_path, load
from .universe import CONSTITUENTS, ETFS, FACTOR_PAIRS, INDEXES, OPTION_INDEXES, bar_universe, idea_universe

JOURNAL = Path(os.environ.get("FR_JOURNAL_DIR", "journal"))
EXPOSURE_FIELDS = [
    "underlying", "spot", "gex_total", "gex_0dte", "gamma_flip", "gamma_flip_distance", "call_wall", "put_wall",
    "vanna_exp_total", "charm_exp_total", "atm_iv_30d", "term_slope", "skew_25d", "pc_volume_ratio",
    "pc_oi_ratio", "call_premium", "put_premium", "zero_dte_volume_share", "unusual_count", "unusual_premium",
    "top_unusual",
]


def _calendar():
    return mcal.get_calendar("XNYS")


def is_trading_day(day: date) -> bool:
    return not _calendar().valid_days(day, day).empty


def next_trading_day(as_of: date) -> date:
    days = _calendar().valid_days(as_of + pd.Timedelta(days=1), as_of + pd.Timedelta(days=10))
    return days[0].date()


def previous_trading_day(day: date) -> date:
    days = _calendar().valid_days(day - pd.Timedelta(days=10), day - pd.Timedelta(days=1))
    return days[-1].date()


# Each book: where its ideas and scores live. The nightly book is recorded the night before and checked
# before the open; the morning book is recorded before the open from the overnight session and has no check.
BOOKS = {
    "nightly": ("ideas", "scores"),
    "morning": ("morning/ideas", "morning/scores"),
}


# A side of an idea is one symbol, or a named basket held in equal weight. Any US-listed stock or ETF can
# be a leg or a member, inside the standard universe or not; bars for names outside it are pulled before
# scoring (pending_legs). Indexes can't be bought, so they can't be legs.
BASKET_SIZE = (3, 10)
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,6}$")
NOT_TRADABLE = {*INDEXES, *OPTION_INDEXES, "NDX", "RUT", "DJI", "SOX", "VIX1D", "VIX9D", "VIX6M", "FVX", "TYX"}


def _write_once(path: Path, payload: dict, indent: int | None = 2) -> Path:
    if path.exists():
        raise FileExistsError(f"{path} already exists; journal entries are write-once")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=indent, default=_json_default) + "\n")
    return path


def _json_default(x):
    if isinstance(x, (np.floating, np.integer)):
        return None if isinstance(x, np.floating) and np.isnan(x) else x.item()
    if isinstance(x, (pd.Timestamp, datetime, date)):
        return x.isoformat()
    if isinstance(x, np.bool_):
        return bool(x)
    raise TypeError(type(x))


def _records(df: pd.DataFrame) -> list[dict]:
    df = df.copy()
    numeric = df.select_dtypes("number").columns
    df[numeric] = df[numeric].round(5)
    clean = df.replace([np.inf, -np.inf], np.nan).astype(object).where(df.notna(), None)
    return clean.to_dict("records")


LEVERAGED_ASSETS = JOURNAL / "reference" / "leveraged_assets.json"

# What an idea rests on, tagged when it's recorded so the dashboard can show which inputs earn their keep.
DRIVERS = {
    "stretched_pair": "a factor pair at an extreme (z-score, RSI, 20-day move)",
    "trend": "a pair or leg trending, expected to continue",
    "catalyst": "a scheduled event in the window: a release, a vote, a policy decision",
    "earnings": "earnings reported before the open or the night before",
    "macro": "a cross-asset move: rates, oil, the dollar, credit",
    "positioning": "crowding or de-grossing: prime book, CTA, retail, short interest",
    "options": "dealer gamma, walls, expiry or skew",
    "forced_flow": "leveraged ETF, rebalancing or vol-control flows",
    "base_rate": "a historical study of what usually happens next",
}


SLATE_MINIMUM = {"stock": 2, "basket": 1}
SLATE_KINDS = (*SLATE_MINIMUM, "etf", "factor")
SLATE_VERDICTS = ("recorded", "passed")

# The setup an idea came from, tagged when it's recorded so results can be compared by setup. The screen
# (`fr screen`) finds the first fifteen; "catalyst" and "other" cover ideas it didn't surface.
SETUPS = {
    "earnings_gap_held": "gapped at least 1.5 ATR on earnings, closed strong on heavy volume and held",
    "earnings_gap_faded": "gapped on earnings and closed weak, or good news sold / bad news bought",
    "breakout": "closed above its 52-week or 20-day high on heavy volume",
    "breakdown": "closed below its 52-week or 20-day low on heavy volume",
    "reclaim_200": "closed back above its 200-day after a month or more below, on heavy volume",
    "lost_200": "closed below its 200-day after a month or more above, on heavy volume",
    "relative_strength": "rose at least 1 ATR on a day SPY fell 1% or more",
    "relative_weakness": "fell at least 1 ATR on a day SPY rose 1% or more",
    "turn": "stretched (RSI or distance from the 20-day) and turned: a strong close against a losing streak",
    "stretched": "stretched with no turn yet",
    "basket_breakout": "a basket's spread against its hedge made a 20-day high on a big day",
    "basket_breakdown": "a basket's spread against its hedge made a 20-day low on a big day",
    "basket_big_move": "a basket's spread moved at least 2 of its daily standard deviations",
    "basket_stretched": "a basket's spread at least 2 standard deviations from its 50-day average",
    "factor_turn": "a stretched factor pair turned against the stretch",
    "factor_stretched": "a factor pair at least 2 standard deviations from its 50-day average",
    "catalyst": "built around a scheduled event",
    "other": "none of the above",
}

# The five steps behind every new idea, kept with it so the reasoning can be checked against the outcome.
REASONING = {
    "what_changed": "what the screen or the tape showed, with numbers",
    "story": "why it moved, with sources and dates",
    "chart": "what the technicals said, for and against",
    "coming": "events inside the time limit",
    "case_against": "the strongest case against, and why it didn't win",
}
MAX_HORIZON = 5
# Where each book's outcomes over the full time limit live. Scores stay open to close on the first session.
OUTCOMES = {"nightly": "outcomes", "morning": "morning/outcomes"}


def build_brief(as_of: date) -> dict:
    all_bars = load(bar_universe())
    all_bars = all_bars[all_bars["date"].dt.date <= as_of]
    bars = all_bars[all_bars["symbol"].isin(idea_universe())]
    techs = symbol_technicals(bars)
    pairs = factor_pair_technicals(bars)
    snapshot = latest_snapshot(as_of.isoformat())
    exposures = storage.read(f"exposures/date={as_of}/{snapshot}.parquet") if snapshot else pd.DataFrame()
    if not exposures.empty:
        exposures = exposures[[c for c in EXPOSURE_FIELDS if c in exposures]]
    reference = json.loads(LEVERAGED_ASSETS.read_text()) if LEVERAGED_ASSETS.exists() else None
    brief = {
        "as_of": as_of.isoformat(),
        "built_at": datetime.now(NY).isoformat(),
        "bars_through": str(bars["date"].max().date()) if not bars.empty else None,
        "options_snapshot": snapshot,
        "regime": regime(bars) if not bars.empty else {},
        "factor_pairs": _records(pairs),
        "etf_technicals": _records(techs[techs["symbol"].isin(ETFS)]),
        "constituent_technicals": _records(techs[techs["symbol"].isin(CONSTITUENTS)]),
        "options": _records(exposures) if not exposures.empty else [],
        "forced_flows": flows.build(all_bars, as_of, snapshot, reference) if not all_bars.empty else {},
    }
    path = JOURNAL / "briefs" / f"{as_of}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(brief, indent=2, default=_json_default) + "\n")
    return brief


def record_ideas(draft_path: Path) -> Path:
    draft = json.loads(draft_path.read_text())
    as_of = date.fromisoformat(draft["as_of"])
    brief_path = JOURNAL / "briefs" / f"{as_of}.json"
    if not brief_path.exists():
        raise FileNotFoundError(f"build the brief for {as_of} first")
    ideas = draft["ideas"]
    if not 1 <= len(ideas) <= 3:
        raise ValueError("record 1-3 ideas")
    _validate(ideas, ("stand_down_if",))
    _validate_slate(draft.get("slate"))
    for i, idea in enumerate(ideas, 1):
        rules = idea["stand_down_if"]
        if not isinstance(rules, list) or not rules or not all(isinstance(r, str) and r.strip() for r in rules):
            raise ValueError(f"idea {i} stand_down_if must list the overnight conditions that cancel it")
        idea["id"] = f"{as_of}-{i}"
    trade_date = next_trading_day(as_of)
    payload = {
        "as_of": as_of.isoformat(),
        "trade_date": trade_date.isoformat(),
        "recorded_at": datetime.now(NY).isoformat(),
        "brief_sha256": hashlib.sha256(brief_path.read_bytes()).hexdigest(),
        "macro_context": draft.get("macro_context", ""),
        "slate": draft["slate"],
        "ideas": ideas,
    }
    return _write_once(JOURNAL / "ideas" / f"{trade_date}.json", payload)


def _validate_slate(slate) -> None:
    """Every night's ideas come with the stock and basket candidates considered, so the ETF table can't be the
    only place the night looked. A verdict of "passed" is fine; skipping the look isn't."""
    if not isinstance(slate, list):
        raise ValueError(f"the draft needs a slate: at least {SLATE_MINIMUM} considered candidates")
    for i, c in enumerate(slate, 1):
        if c.get("kind") not in SLATE_KINDS or c.get("verdict") not in SLATE_VERDICTS or not str(c.get("name", "")).strip() \
                or not str(c.get("reason", "")).strip():
            raise ValueError(f"slate entry {i} needs name, reason, kind ({'/'.join(SLATE_KINDS)}) "
                             f"and verdict ({'/'.join(SLATE_VERDICTS)})")
    for kind, need in SLATE_MINIMUM.items():
        if sum(c["kind"] == kind for c in slate) < need:
            raise ValueError(f"the slate needs at least {need} {kind} candidate(s)")


def _validate(ideas: list[dict], extra: tuple[str, ...] = ()) -> None:
    """Checks shared by both books; sets each idea's kind. A leg is any ticker symbol: one outside the
    standard universe is checked with IBKR before recording (a US listing and about $20M a day of
    volume), and its bars are pulled with the pending legs before scoring."""
    for i, idea in enumerate(ideas, 1):
        for field in ("factor", "thesis", "confidence", "invalidation", "key_signals", "drivers", *extra):
            if field not in idea:
                raise ValueError(f"idea {i} missing {field}")
        drivers = idea["drivers"]
        if not isinstance(drivers, list) or not drivers or any(d not in DRIVERS for d in drivers):
            raise ValueError(f"idea {i} drivers must list what the idea rests on, from: {', '.join(DRIVERS)}")
        if not _legs(idea):
            raise ValueError(f"idea {i} needs a long leg, a short leg, or both")
        for side in ("long", "short"):
            v = idea.get(side)
            if isinstance(v, list):
                lo, hi = BASKET_SIZE
                if not lo <= len(v) <= hi:
                    raise ValueError(f"idea {i} {side} basket must hold {lo}-{hi} names")
                if any(not isinstance(m, str) or not SYMBOL.match(m) for m in v) or len(set(v)) != len(v):
                    raise ValueError(f"idea {i} {side} basket must list distinct ticker symbols")
                if not str(idea.get(f"{side}_label", "")).strip():
                    raise ValueError(f"idea {i} {side} basket needs a {side}_label naming its theme")
            elif v and (not isinstance(v, str) or not SYMBOL.match(v)):
                raise ValueError(f"idea {i} {side} leg must be a ticker symbol or a basket list")
            if bad := sorted(set(_side(idea, side)) & NOT_TRADABLE):
                raise ValueError(f"idea {i} {side} side holds an index ({', '.join(bad)}), which can't be traded")
        if set(_side(idea, "long")) & set(_side(idea, "short")):
            raise ValueError(f"idea {i} has a name on both sides")
        if idea["confidence"] not in (1, 2, 3, 4, 5):
            raise ValueError(f"idea {i} confidence must be 1-5")
        _validate_plan(idea, i)
        idea["kind"] = "pair" if idea.get("long") and idea.get("short") else ("long" if idea.get("long") else "short")
        idea["basket"] = any(isinstance(idea.get(side), list) for side in ("long", "short"))


def _price_rules(rules, legs: list[str], what: str, i: int) -> None:
    if not isinstance(rules, list):
        raise ValueError(f"idea {i} {what} must be a list of {{symbol, below or above}} rules")
    for r in rules:
        sides = [k for k in ("below", "above") if k in r]
        if not isinstance(r, dict) or r.get("symbol") not in legs or len(sides) != 1 \
                or not isinstance(r[sides[0]], (int, float)) or r[sides[0]] <= 0:
            raise ValueError(f"idea {i} {what} rule {r!r} needs a leg's symbol and one positive below or above price")


def _validate_plan(idea: dict, i: int) -> None:
    """The plan every new idea carries: its setup, time limit, stop, optional skip-at-open levels, and the
    five steps of reasoning behind it."""
    if idea.get("setup") not in SETUPS:
        raise ValueError(f"idea {i} setup must be one of: {', '.join(SETUPS)}")
    h = idea.get("horizon_days")
    if not isinstance(h, int) or isinstance(h, bool) or not 1 <= h <= MAX_HORIZON:
        raise ValueError(f"idea {i} horizon_days must be 1-{MAX_HORIZON} sessions, counting the first")
    reasoning = idea.get("reasoning")
    if not isinstance(reasoning, dict) or any(not str(reasoning.get(k, "")).strip() for k in REASONING):
        raise ValueError(f"idea {i} reasoning needs each of: {', '.join(REASONING)}")
    legs = _legs(idea)
    stop = idea.get("stop")
    if not isinstance(stop, dict) or not (stop.get("rules") or "spread_pct" in stop):
        raise ValueError(f"idea {i} stop needs price rules, a spread_pct, or both")
    _price_rules(stop.get("rules", []), legs, "stop", i)
    if "spread_pct" in stop and (not isinstance(stop["spread_pct"], (int, float)) or stop["spread_pct"] >= 0):
        raise ValueError(f"idea {i} stop spread_pct must be negative: the loss from entry, in percent, that ends the idea")
    if "skip_if_open" in idea:
        _price_rules(idea["skip_if_open"], legs, "skip_if_open", i)


def record_morning(draft_path: Path, now: datetime | None = None) -> Path:
    """Record the morning book: 1-2 new ideas for today's session, built from what happened overnight,
    before the 09:30 open. It is scored apart from the nightly book, so the record shows whether
    reacting to the overnight session adds anything. Last night's ideas are untouched."""
    draft = json.loads(draft_path.read_text())
    trade_date = date.fromisoformat(draft["trade_date"])
    now = now or datetime.now(NY)
    if not is_trading_day(trade_date):
        raise ValueError(f"{trade_date} is not a trading day")
    if now.date() != trade_date:
        raise ValueError(f"morning ideas are recorded on the morning of the session, not for {trade_date}")
    if now >= datetime.combine(trade_date, time(9, 30), NY):
        raise ValueError(f"morning ideas must be recorded before the {trade_date} open")
    ideas = draft["ideas"]
    if not 1 <= len(ideas) <= 2:
        raise ValueError("record 1-2 morning ideas")
    _validate(ideas, ("overnight",))
    for i, idea in enumerate(ideas, 1):
        if not str(idea["overnight"]).strip():
            raise ValueError(f"idea {i} overnight must say what happened since last night that this reacts to")
        idea["id"] = f"{trade_date}-m{i}"
    as_of = previous_trading_day(trade_date)
    brief_path = JOURNAL / "briefs" / f"{as_of}.json"
    payload = {
        "book": "morning",
        "as_of": as_of.isoformat(),
        "trade_date": trade_date.isoformat(),
        "recorded_at": now.isoformat(),
        "brief_sha256": hashlib.sha256(brief_path.read_bytes()).hexdigest() if brief_path.exists() else None,
        "summary": draft.get("summary", ""),
        "premarket": draft.get("premarket", {}),
        "macro_context": draft.get("macro_context", ""),
        "ideas": ideas,
    }
    return _write_once(JOURNAL / BOOKS["morning"][0] / f"{trade_date}.json", payload)


def record_check(draft_path: Path) -> Path:
    """Record the pre-open check: for each of the session's ideas, whether it stands or is stood
    down under the stand_down_if rules written the night before. The ideas themselves never change."""
    draft = json.loads(draft_path.read_text())
    trade_date = date.fromisoformat(draft["trade_date"])
    ideas_path = JOURNAL / "ideas" / f"{trade_date}.json"
    if not ideas_path.exists():
        raise FileNotFoundError(f"no ideas recorded for {trade_date}")
    now = datetime.now(NY)
    open_at = datetime.combine(trade_date, time(9, 30), NY)
    if now >= open_at:
        raise ValueError(f"the check must be recorded before the {trade_date} open")
    ids = [i["id"] for i in json.loads(ideas_path.read_text())["ideas"]]
    decisions = draft["decisions"]
    if sorted(d["id"] for d in decisions) != sorted(ids):
        raise ValueError(f"give exactly one decision for each idea: {', '.join(ids)}")
    for d in decisions:
        if d["decision"] not in ("stands", "stand_down"):
            raise ValueError(f"{d['id']}: decision must be stands or stand_down")
        if not str(d.get("reason", "")).strip():
            raise ValueError(f"{d['id']}: give a reason")
    payload = {
        "trade_date": trade_date.isoformat(),
        "checked_at": now.isoformat(),
        "summary": draft.get("summary", ""),
        "premarket": draft.get("premarket", {}),
        "decisions": decisions,
    }
    return _write_once(JOURNAL / "checks" / f"{trade_date}.json", payload)


def _checks() -> dict[str, dict]:
    """Pre-open decisions by idea id."""
    out = {}
    for path in sorted((JOURNAL / "checks").glob("*.json")):
        for d in json.loads(path.read_text())["decisions"]:
            out[d["id"]] = d
    return out


def _entries(book: str) -> list[tuple[Path, dict]]:
    """(score path, ideas entry) for each recorded session of a book, oldest first."""
    ideas_dir, scores_dir = BOOKS[book]
    return [(JOURNAL / scores_dir / p.name, json.loads(p.read_text())) for p in sorted((JOURNAL / ideas_dir).glob("*.json"))]


def intraday_symbols(trade_date: date) -> list[str]:
    legs = set()
    for ideas_dir, _ in BOOKS.values():
        path = JOURNAL / ideas_dir / f"{trade_date}.json"
        if path.exists():
            legs |= {s for i in json.loads(path.read_text())["ideas"] for s in _legs(i)}
    return sorted(legs | {"SPY"}) if legs else []


def write_intraday(trade_date: date, bars: dict[str, pd.DataFrame]) -> Path:
    """Store 15-minute bars (pre-market through the close, New York time) for the session's legs."""
    payload = {
        "trade_date": trade_date.isoformat(),
        "pulled_at": datetime.now(NY).isoformat(),
        "bar": "15 mins",
        "bars": {
            s: {"time": df["time"].tolist(), **{c: df[c].round(4).tolist() for c in ("open", "high", "low", "close")},
                "volume": df["volume"].astype(int).tolist()}
            for s, df in bars.items() if not df.empty
        },
    }
    return _write_once(JOURNAL / "intraday" / f"{trade_date}.json", payload, indent=None)


def _spread_path(trade_date: str, idea: dict) -> list[list] | None:
    """The idea's return from the 09:30 bar's open, in bp, at each regular-session 15-minute close."""
    path = JOURNAL / "intraday" / f"{trade_date}.json"
    if not path.exists():
        return None
    bars = json.loads(path.read_text())["bars"]
    legs = _weights(idea)
    if any(s not in bars for s in legs):
        return None
    session = {s: [(t, o, c) for t, o, c in zip(bars[s]["time"], bars[s]["open"], bars[s]["close"]) if "09:30" <= t < "16:00"]
               for s in legs}
    if any(not v or v[0][0] != "09:30" for v in session.values()):
        return None
    closes = {s: {t: c for t, _, c in v} for s, v in session.items()}
    times = sorted(set.intersection(*(set(c) for c in closes.values())))
    opens = {s: v[0][1] for s, v in session.items()}
    return [[t, round(sum(sign * (closes[s][t] / opens[s] - 1) for s, sign in legs.items()) * 1e4, 1)] for t in times]


def _side(idea: dict, side: str) -> list[str]:
    v = idea.get(side)
    return list(v) if isinstance(v, list) else ([v] if v else [])


def _legs(idea: dict) -> list[str]:
    return [*_side(idea, "long"), *_side(idea, "short")]


def _weights(idea: dict) -> dict[str, float]:
    """Each symbol's weight: +1 split evenly over the long side, -1 over the short side."""
    w = {}
    for side, sign in (("long", 1), ("short", -1)):
        members = _side(idea, side)
        for s in members:
            w[s] = sign / len(members)
    return w


def _label(idea: dict, side: str) -> str:
    v = idea.get(side)
    if isinstance(v, list):
        return idea.get(f"{side}_label") or ", ".join(v)
    return v or ""


def _side_return(rets: dict[str, float], idea: dict, side: str) -> float | None:
    members = _side(idea, side)
    return sum(rets[s] for s in members) / len(members) if members else None


def pending_legs() -> list[str]:
    """Every leg of an idea not yet scored, in either book: the bars scoring will need."""
    legs = set()
    for book in BOOKS:
        for score_path, entry in _entries(book):
            if not score_path.exists():
                legs |= {s for i in entry["ideas"] for s in _legs(i)}
    return sorted(legs)


def _bar(bars: pd.DataFrame, symbol: str, day: date) -> pd.Series | None:
    row = bars[(bars["symbol"] == symbol) & (bars["date"].dt.date == day)]
    return row.iloc[0] if not row.empty else None


def _bars_settled(trade_date: date) -> bool:
    """Whether the stored bars were pulled after this session's close. A dashboard refresh during the session
    stores a partial bar for the day, and scores are write-once."""
    pulled = storage.modified(bars_path("SPY"))
    close = datetime.combine(trade_date, time(16, 15), NY)
    return pulled is not None and pulled.astimezone(NY) >= close


def score() -> list[Path]:
    written = []
    for score_path, entry in [e for book in BOOKS for e in _entries(book)]:
        if score_path.exists():
            continue
        trade_date = date.fromisoformat(entry["trade_date"])
        as_of = date.fromisoformat(entry["as_of"])
        legs = {s for i in entry["ideas"] for s in _legs(i)} | {"SPY"}
        bars = load(sorted(legs))
        if bars.empty or _bar(bars, "SPY", trade_date) is None or not _bars_settled(trade_date):
            continue
        results = []
        for idea in entry["ideas"]:
            base = {"id": idea["id"], "long": _label(idea, "long") or None, "short": _label(idea, "short") or None}
            if idea.get("basket"):
                base |= {"long_members": _side(idea, "long"), "short_members": _side(idea, "short")}
            today = {s: _bar(bars, s, trade_date) for s in _legs(idea)}
            prior = {s: _bar(bars, s, as_of) for s in _legs(idea)}
            missing = sorted(s for s in today if today[s] is None or prior[s] is None)
            if missing:
                results.append({**base, "missing_bars": True, "missing": missing})
                continue
            oc = {s: today[s]["close"] / today[s]["open"] - 1 for s in today}
            cc = {s: today[s]["close"] / prior[s]["close"] - 1 for s in today}
            w = _weights(idea)
            oc_ret = sum(w[s] * oc[s] for s in oc)
            cc_ret = sum(w[s] * cc[s] for s in cc)
            long_oc, short_oc = _side_return(oc, idea, "long"), _side_return(oc, idea, "short")
            results.append({
                **base,
                "kind": idea.get("kind", "pair"),
                "factor": idea["factor"],
                "confidence": idea["confidence"],
                "open_to_close_bps": round(oc_ret * 1e4, 1),
                "close_to_close_bps": round(cc_ret * 1e4, 1),
                "long_oc_bps": round(long_oc * 1e4, 1) if long_oc is not None else None,
                "short_oc_bps": round(short_oc * 1e4, 1) if short_oc is not None else None,
                "hit": bool(oc_ret > 0),
            })
        # Score files are write-once, so a leg whose bars haven't arrived (often a basket member pulled late)
        # is retried for a few days before the idea is written off as missing.
        if any(r.get("missing_bars") for r in results) and (datetime.now(NY).date() - trade_date).days < 5:
            continue
        spy = _bar(bars, "SPY", trade_date)
        payload = {
            "trade_date": trade_date.isoformat(),
            "scored_at": datetime.now(NY).isoformat(),
            "spy_open_to_close_bps": round((spy["close"] / spy["open"] - 1) * 1e4, 1),
            "results": results,
        }
        written.append(_write_once(score_path, payload))
    return written + write_outcomes()


def sessions_from(start: date, n: int) -> list[date]:
    """The first n trading sessions starting with `start`."""
    return [d.date() for d in _calendar().valid_days(start, start + pd.Timedelta(days=n * 2 + 10))[:n]]


def _rule_hit(rule: dict, price: float) -> bool:
    return price < rule["below"] if "below" in rule else price > rule["above"]


def rule_text(rule: dict) -> str:
    side = "below" if "below" in rule else "above"
    return f"{rule['symbol']} {side} {rule[side]:,.2f}"


def stop_text(idea: dict) -> str:
    stop = idea.get("stop") or {}
    parts = [f"closes {rule_text(r)}" for r in stop.get("rules", [])]
    if "spread_pct" in stop:
        parts.append(f"the idea is down {abs(stop['spread_pct']):g}% from entry at a close")
    return " or ".join(parts)


def _outcome(idea: dict, trade_date: date, bars: pd.DataFrame) -> dict | None:
    """The idea over its whole time limit. It enters at the first session's open unless a skip_if_open rule
    is met there, and exits at the close of the session where a stop is hit (stops are checked on closes)
    or of the last session. None until the first session has settled bars; status "open" while sessions
    remain. Ideas recorded before time limits existed count as one session with no stop."""
    horizon = idea.get("horizon_days", 1)
    legs = _legs(idea)
    names = [*legs, "SPY"]
    px = {s: g.assign(day=g["date"].dt.date).set_index("day") for s, g in bars[bars["symbol"].isin(names)].groupby("symbol")}

    def bar(s: str, d: date):
        g = px.get(s)
        return g.loc[d] if g is not None and d in g.index else None

    first = {s: bar(s, trade_date) for s in names}
    if any(v is None for v in first.values()) or not _bars_settled(trade_date):
        return None
    for rule in idea.get("skip_if_open", []):
        o = first[rule["symbol"]]["open"]
        if _rule_hit(rule, o):
            return {"status": "skipped", "note": f"{rule['symbol']} opened at {o:,.2f}, past the skip level ({rule_text(rule)})",
                    "horizon_days": horizon, "sessions": 0, "hit": None}
    w = _weights(idea)
    entry = {s: first[s]["open"] for s in names}
    stop = idea.get("stop") or {}
    path, status, note = [], "open", ""
    for k, d in enumerate(sessions_from(trade_date, horizon), 1):
        today = {s: bar(s, d) for s in names}
        if any(v is None for v in today.values()) or not _bars_settled(d):
            break
        ret = sum(w[s] * (today[s]["close"] / entry[s] - 1) for s in legs)
        path.append((d, ret, today["SPY"]["close"] / entry["SPY"] - 1))
        hits = [f"{r['symbol']} closed at {today[r['symbol']]['close']:,.2f} ({rule_text(r)})"
                for r in stop.get("rules", []) if _rule_hit(r, today[r["symbol"]]["close"])]
        if "spread_pct" in stop and ret * 100 <= stop["spread_pct"]:
            hits.append(f"down {abs(ret) * 100:.1f}% from entry, past the {abs(stop['spread_pct']):g}% stop")
        if hits:
            status, note = "stopped", f"Stopped on session {k}: " + "; ".join(hits)
            break
        if k == horizon:
            status, note = "time", f"Held {k} session{'s' if k > 1 else ''} to the time limit"
    if not path:
        return None
    d, ret, spy = path[-1]
    rets = [p[1] for p in path]
    return {"status": status, "note": note, "exit_date": d.isoformat(), "sessions": len(path), "horizon_days": horizon,
            "bps": round(ret * 1e4, 1), "spy_bps": round(spy * 1e4, 1), "best_bps": round(max(rets) * 1e4, 1),
            "worst_bps": round(min(rets) * 1e4, 1), "hit": bool(ret > 0)}


def write_outcomes() -> list[Path]:
    """Write each session's outcomes once every idea in it has finished: stopped, skipped at the open, or held
    to its time limit. Write-once like scores. An idea whose bars never arrive is written off after two weeks."""
    written = []
    for book in BOOKS:
        for score_path, entry in _entries(book):
            path = JOURNAL / OUTCOMES[book] / score_path.name
            if path.exists():
                continue
            trade_date = date.fromisoformat(entry["trade_date"])
            bars = load(sorted({s for i in entry["ideas"] for s in _legs(i)} | {"SPY"}))
            if bars.empty:
                continue
            late = (datetime.now(NY).date() - trade_date).days >= 14
            results = []
            for idea in entry["ideas"]:
                o = _outcome(idea, trade_date, bars)
                if o is None and late:
                    results.append({"id": idea["id"], "missing_bars": True})
                elif o is None or o["status"] == "open":
                    break
                else:
                    results.append({"id": idea["id"], **o})
            else:
                written.append(_write_once(path, {"trade_date": trade_date.isoformat(),
                                                  "scored_at": datetime.now(NY).isoformat(), "results": results}))
    return written


def scorecard() -> pd.DataFrame:
    checks = _checks()
    rows = []
    for book, (_, scores_dir) in BOOKS.items():
        for path in sorted((JOURNAL / scores_dir).glob("*.json")):
            entry = json.loads(path.read_text())
            for r in entry["results"]:
                if r.get("missing_bars"):
                    continue
                check = checks.get(r["id"], {}).get("decision")
                rows.append({"trade_date": entry["trade_date"], "book": book, **r, "check": check})
    return pd.DataFrame(rows).sort_values(["trade_date", "book", "id"], kind="stable") if rows else pd.DataFrame(rows)


def _trade_type(idea: dict) -> str:
    """Basket if either side is one, else single stock if either side is, else ETF."""
    sides = [idea.get("long"), idea.get("short")]
    if any(isinstance(s, list) for s in sides):
        return "Basket"
    return "Single stock" if any(s and s not in ETFS for s in sides) else "ETF"


def ledger(marks: dict | None = None) -> list[dict]:
    """One row per recorded idea for the dashboard: the final score where one exists, else an
    intraday mark from `marks` ({"at": unix seconds, "prices": {symbol: {"open", "last"}}})."""
    checks = _checks()
    rows = []
    for book, score_path, entry in [(b, *e) for b in BOOKS for e in _entries(b)]:
        scored = json.loads(score_path.read_text()) if score_path.exists() else None
        results = {r["id"]: r for r in (scored or {}).get("results", [])}
        outcome_path = JOURNAL / OUTCOMES[book] / score_path.name
        if outcome_path.exists():
            outcomes = {r["id"]: r for r in json.loads(outcome_path.read_text())["results"]}
        else:
            live = load(sorted({s for i in entry["ideas"] for s in _legs(i)} | {"SPY"}))
            trade_date = date.fromisoformat(entry["trade_date"])
            outcomes = {i["id"]: _outcome(i, trade_date, live) for i in entry["ideas"]} if not live.empty else {}
        prices = (marks or {}).get("prices", {})
        for idea in entry["ideas"]:
            check = checks.get(idea["id"], {})
            if book == "morning":
                check = {"decision": "morning", "reason": "Recorded the morning of the session, so there is no pre-open check."}
            row = {
                "id": idea["id"], "book": book.capitalize(), "trade_date": entry["trade_date"], "as_of": entry["as_of"],
                "kind": idea.get("kind"), "long": _label(idea, "long"), "short": _label(idea, "short"),
                "long_members": ", ".join(_side(idea, "long")) if isinstance(idea.get("long"), list) else "",
                "short_members": ", ".join(_side(idea, "short")) if isinstance(idea.get("short"), list) else "",
                "trade_type": _trade_type(idea),
                "factor": idea["factor"], "confidence": idea["confidence"],
                "thesis": idea["thesis"], "invalidation": idea["invalidation"],
                "stand_down_if": " · ".join(idea.get("stand_down_if", [])),
                "drivers": ",".join(idea.get("drivers", [])),
                "check": {"stands": "Stands", "stand_down": "Stood down", "morning": "No check"}.get(
                    check.get("decision"), "Not checked"),
                "overnight": idea.get("overnight", ""),
                "check_reason": check.get("reason", ""),
                "status": "Open", "oc_bps": None, "long_bps": None, "short_bps": None, "spy_bps": None,
                "marked_at": None, "path": _spread_path(entry["trade_date"], idea),
            }
            r = results.get(idea["id"])
            legs = [*_legs(idea), "SPY"]
            if r and not r.get("missing_bars"):
                row.update(status="Final", oc_bps=r["open_to_close_bps"], long_bps=r["long_oc_bps"],
                           short_bps=r["short_oc_bps"], spy_bps=scored["spy_open_to_close_bps"],
                           marked_at=scored["scored_at"])
            elif not scored and all(s in prices for s in legs):
                bps = {s: (prices[s]["last"] / prices[s]["open"] - 1) * 1e4 for s in legs}
                long_bps, short_bps = _side_return(bps, idea, "long"), _side_return(bps, idea, "short")
                long_bps = round(long_bps, 1) if long_bps is not None else None
                short_bps = round(short_bps, 1) if short_bps is not None else None
                bps["SPY"] = round(bps["SPY"], 1)
                row.update(status="Intraday", oc_bps=round((long_bps or 0) - (short_bps or 0), 1),
                           long_bps=long_bps, short_bps=short_bps, spy_bps=bps["SPY"],
                           marked_at=datetime.fromtimestamp(marks["at"], NY).isoformat())
            row["hit"] = None if row["oc_bps"] is None else row["oc_bps"] > 0
            reasoning = idea.get("reasoning") or {}
            o = outcomes.get(idea["id"]) or {}
            row.update(
                setup=idea.get("setup", ""), horizon_days=idea.get("horizon_days", 1), stop=stop_text(idea),
                skip_if_open=" or ".join(f"opens {rule_text(r)}" for r in idea.get("skip_if_open", [])),
                **{f"why_{k}": reasoning.get(k, "") for k in REASONING},
                outcome_status={"open": "Open", "stopped": "Stopped", "time": "Time limit", "skipped": "Skipped"}.get(
                    o.get("status"), "Missing bars" if o.get("missing_bars") else None),
                outcome_bps=o.get("bps"), outcome_spy_bps=o.get("spy_bps"), outcome_note=o.get("note", ""),
                exit_date=o.get("exit_date"), sessions_held=o.get("sessions"), best_bps=o.get("best_bps"),
                worst_bps=o.get("worst_bps"), outcome_final=outcome_path.exists())
            rows.append(row)
    return sorted(rows, key=lambda r: (r["trade_date"], r["book"] != "Nightly", r["id"]))


def scorecard_report() -> str:
    df = scorecard()
    if df.empty:
        return "No scored ideas yet."

    def block(g: pd.DataFrame) -> pd.Series:
        return pd.Series({
            "n": len(g),
            "hit_rate": g["hit"].mean(),
            "mean_oc_bps": g["open_to_close_bps"].mean(),
            "total_oc_bps": g["open_to_close_bps"].sum(),
            "mean_cc_bps": g["close_to_close_bps"].mean(),
        })

    by_conf = df.groupby("confidence").apply(block, include_groups=False)
    by_factor = df.groupby("factor").apply(block, include_groups=False).sort_values("total_oc_bps", ascending=False)
    recent = df.tail(10)[["trade_date", "book", "long", "short", "confidence", "open_to_close_bps", "hit"]].fillna("-")
    known = {p.name for p in FACTOR_PAIRS}
    unlisted = sorted(set(df["factor"]) - known)
    nightly = df[df["book"] == "nightly"]
    morning = df[df["book"] == "morning"]
    kept = nightly[nightly["check"] != "stand_down"]
    stood_down = nightly[nightly["check"] == "stand_down"]
    overall = block(nightly).to_frame("nightly").T
    overall = pd.concat([overall, block(kept).to_frame("after_check").T])
    if not stood_down.empty:
        overall = pd.concat([overall, block(stood_down).to_frame("stood_down").T])
    if not morning.empty:
        overall = pd.concat([overall, block(morning).to_frame("morning").T, block(df).to_frame("both_books").T])
    parts = [
        "OVERALL", overall.round(2).to_string(),
        "\nBY CONFIDENCE", by_conf.round(2).to_string(),
        "\nBY FACTOR", by_factor.round(2).to_string(),
        "\nLAST 10", recent.to_string(index=False),
    ]
    if unlisted:
        parts += ["\nFactors outside the standard pair list: " + ", ".join(unlisted)]
    return "\n".join(parts)
