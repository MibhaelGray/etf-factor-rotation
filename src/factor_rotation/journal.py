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

Files are write-once: recording over an existing ideas or score file is refused,
so the record can't be edited after the outcome is known.

Every idea is an outright long, an outright short, or a dollar-neutral pair,
entered at the next session's open and exited at its close. Close-to-close
(from the as_of close) is recorded alongside for comparison.
"""

import hashlib
import json
import os
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
from .ib.bars import load
from .universe import CONSTITUENTS, ETFS, FACTOR_PAIRS, bar_universe, idea_universe

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
        "ideas": ideas,
    }
    return _write_once(JOURNAL / "ideas" / f"{trade_date}.json", payload)


def _validate(ideas: list[dict], extra: tuple[str, ...] = ()) -> None:
    """Checks shared by both books; sets each idea's kind."""
    universe = set(idea_universe())
    for i, idea in enumerate(ideas, 1):
        for field in ("factor", "thesis", "confidence", "invalidation", "key_signals", "drivers", *extra):
            if field not in idea:
                raise ValueError(f"idea {i} missing {field}")
        drivers = idea["drivers"]
        if not isinstance(drivers, list) or not drivers or any(d not in DRIVERS for d in drivers):
            raise ValueError(f"idea {i} drivers must list what the idea rests on, from: {', '.join(DRIVERS)}")
        legs = _legs(idea)
        if not legs:
            raise ValueError(f"idea {i} needs a long leg, a short leg, or both")
        if any(s not in universe for s in legs):
            raise ValueError(f"idea {i} legs must be in the universe")
        if idea["confidence"] not in (1, 2, 3, 4, 5):
            raise ValueError(f"idea {i} confidence must be 1-5")
        idea["kind"] = "pair" if len(legs) == 2 else ("long" if idea.get("long") else "short")


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
    legs = {idea.get("long"): 1, idea.get("short"): -1}
    legs.pop(None, None)
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


def _legs(idea: dict) -> list[str]:
    return [idea[k] for k in ("long", "short") if idea.get(k)]


def _bar(bars: pd.DataFrame, symbol: str, day: date) -> pd.Series | None:
    row = bars[(bars["symbol"] == symbol) & (bars["date"].dt.date == day)]
    return row.iloc[0] if not row.empty else None


def score() -> list[Path]:
    written = []
    for score_path, entry in [e for book in BOOKS for e in _entries(book)]:
        if score_path.exists():
            continue
        trade_date = date.fromisoformat(entry["trade_date"])
        as_of = date.fromisoformat(entry["as_of"])
        legs = {s for i in entry["ideas"] for s in _legs(i)} | {"SPY"}
        bars = load(sorted(legs))
        if bars.empty or _bar(bars, "SPY", trade_date) is None:
            continue
        results = []
        for idea in entry["ideas"]:
            base = {"id": idea["id"], "long": idea.get("long"), "short": idea.get("short")}
            today = {s: _bar(bars, s, trade_date) for s in _legs(idea)}
            prior = {s: _bar(bars, s, as_of) for s in _legs(idea)}
            if any(b is None for b in [*today.values(), *prior.values()]):
                results.append({**base, "missing_bars": True})
                continue
            oc = {s: today[s]["close"] / today[s]["open"] - 1 for s in today}
            cc = {s: today[s]["close"] / prior[s]["close"] - 1 for s in today}
            sign = {idea.get("long"): 1, idea.get("short"): -1}
            oc_ret = sum(sign[s] * oc[s] for s in oc)
            cc_ret = sum(sign[s] * cc[s] for s in cc)
            results.append({
                **base,
                "kind": idea.get("kind", "pair"),
                "factor": idea["factor"],
                "confidence": idea["confidence"],
                "open_to_close_bps": round(oc_ret * 1e4, 1),
                "close_to_close_bps": round(cc_ret * 1e4, 1),
                "long_oc_bps": round(oc[idea["long"]] * 1e4, 1) if idea.get("long") else None,
                "short_oc_bps": round(oc[idea["short"]] * 1e4, 1) if idea.get("short") else None,
                "hit": bool(oc_ret > 0),
            })
        spy = _bar(bars, "SPY", trade_date)
        payload = {
            "trade_date": trade_date.isoformat(),
            "scored_at": datetime.now(NY).isoformat(),
            "spy_open_to_close_bps": round((spy["close"] / spy["open"] - 1) * 1e4, 1),
            "results": results,
        }
        written.append(_write_once(score_path, payload))
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


def ledger(marks: dict | None = None) -> list[dict]:
    """One row per recorded idea for the dashboard: the final score where one exists, else an
    intraday mark from `marks` ({"at": unix seconds, "prices": {symbol: {"open", "last"}}})."""
    checks = _checks()
    rows = []
    for book, score_path, entry in [(b, *e) for b in BOOKS for e in _entries(b)]:
        scored = json.loads(score_path.read_text()) if score_path.exists() else None
        results = {r["id"]: r for r in (scored or {}).get("results", [])}
        prices = (marks or {}).get("prices", {})
        for idea in entry["ideas"]:
            check = checks.get(idea["id"], {})
            if book == "morning":
                check = {"decision": "morning", "reason": "Recorded the morning of the session, so there is no pre-open check."}
            row = {
                "id": idea["id"], "book": book.capitalize(), "trade_date": entry["trade_date"], "as_of": entry["as_of"],
                "kind": idea.get("kind"), "long": idea.get("long") or "", "short": idea.get("short") or "",
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
                bps = {s: round((prices[s]["last"] / prices[s]["open"] - 1) * 1e4, 1) for s in legs}
                long_bps, short_bps = bps.get(idea.get("long")), bps.get(idea.get("short"))
                row.update(status="Intraday", oc_bps=round((long_bps or 0) - (short_bps or 0), 1),
                           long_bps=long_bps, short_bps=short_bps, spy_bps=bps["SPY"],
                           marked_at=datetime.fromtimestamp(marks["at"], NY).isoformat())
            row["hit"] = None if row["oc_bps"] is None else row["oc_bps"] > 0
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
