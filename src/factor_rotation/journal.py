"""The ideas journal and scorecard.

Each night:
  brief   -> journal/briefs/<as_of>.json     every input the ideas were built from
  record  -> journal/ideas/<trade_date>.json the 2-3 ideas, reasoning, and the brief's hash
Next evening:
  score   -> journal/scores/<trade_date>.json realized spread returns per idea

Files are write-once: recording over an existing ideas or score file is refused,
so the record can't be edited after the outcome is known.

Every idea is an outright long, an outright short, or a dollar-neutral pair,
entered at the next session's open and exited at its close. Close-to-close
(from the as_of close) is recorded alongside for comparison.
"""

import hashlib
import json
import os
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal

from . import storage
from .analytics.compute import latest_snapshot
from .analytics.technicals import factor_pair_technicals, regime, symbol_technicals
from .config import NY
from .ib.bars import load
from .universe import ETFS, FACTOR_PAIRS, bar_universe

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


def _write_once(path: Path, payload: dict) -> Path:
    if path.exists():
        raise FileExistsError(f"{path} already exists; journal entries are write-once")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n")
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


def build_brief(as_of: date) -> dict:
    bars = load(bar_universe())
    bars = bars[bars["date"].dt.date <= as_of]
    techs = symbol_technicals(bars)
    pairs = factor_pair_technicals(bars)
    snapshot = latest_snapshot(as_of.isoformat())
    exposures = storage.read(f"exposures/date={as_of}/{snapshot}.parquet") if snapshot else pd.DataFrame()
    if not exposures.empty:
        exposures = exposures[[c for c in EXPOSURE_FIELDS if c in exposures]]
    brief = {
        "as_of": as_of.isoformat(),
        "built_at": datetime.now(NY).isoformat(),
        "bars_through": str(bars["date"].max().date()) if not bars.empty else None,
        "options_snapshot": snapshot,
        "regime": regime(bars) if not bars.empty else {},
        "factor_pairs": _records(pairs),
        "etf_technicals": _records(techs[techs["symbol"].isin(ETFS)]),
        "constituent_technicals": _records(techs[~techs["symbol"].isin(ETFS)]),
        "options": _records(exposures) if not exposures.empty else [],
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
    universe = set(bar_universe())
    for i, idea in enumerate(ideas, 1):
        for field in ("factor", "thesis", "confidence", "invalidation", "key_signals"):
            if field not in idea:
                raise ValueError(f"idea {i} missing {field}")
        legs = _legs(idea)
        if not legs:
            raise ValueError(f"idea {i} needs a long leg, a short leg, or both")
        if any(s not in universe for s in legs):
            raise ValueError(f"idea {i} legs must be in the universe")
        if idea["confidence"] not in (1, 2, 3, 4, 5):
            raise ValueError(f"idea {i} confidence must be 1-5")
        idea["kind"] = "pair" if len(legs) == 2 else ("long" if idea.get("long") else "short")
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


def _legs(idea: dict) -> list[str]:
    return [idea[k] for k in ("long", "short") if idea.get(k)]


def _bar(bars: pd.DataFrame, symbol: str, day: date) -> pd.Series | None:
    row = bars[(bars["symbol"] == symbol) & (bars["date"].dt.date == day)]
    return row.iloc[0] if not row.empty else None


def score() -> list[Path]:
    written = []
    for ideas_path in sorted((JOURNAL / "ideas").glob("*.json")):
        score_path = JOURNAL / "scores" / ideas_path.name
        if score_path.exists():
            continue
        entry = json.loads(ideas_path.read_text())
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
    rows = []
    for path in sorted((JOURNAL / "scores").glob("*.json")):
        entry = json.loads(path.read_text())
        for r in entry["results"]:
            if r.get("missing_bars"):
                continue
            rows.append({"trade_date": entry["trade_date"], **r})
    return pd.DataFrame(rows)


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

    overall = block(df).to_frame("all").T
    by_conf = df.groupby("confidence").apply(block, include_groups=False)
    by_factor = df.groupby("factor").apply(block, include_groups=False).sort_values("total_oc_bps", ascending=False)
    recent = df.tail(10)[["trade_date", "long", "short", "confidence", "open_to_close_bps", "hit"]].fillna("-")
    known = {p.name for p in FACTOR_PAIRS}
    unlisted = sorted(set(df["factor"]) - known)
    parts = [
        "OVERALL", overall.round(2).to_string(),
        "\nBY CONFIDENCE", by_conf.round(2).to_string(),
        "\nBY FACTOR", by_factor.round(2).to_string(),
        "\nLAST 10", recent.to_string(index=False),
    ]
    if unlisted:
        parts += ["\nFactors outside the standard pair list: " + ", ".join(unlisted)]
    return "\n".join(parts)
