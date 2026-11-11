"""The journal's shared pieces: the trading calendar, the nightly brief, and how a plan is scored on daily bars.

  brief -> journal/briefs/<as_of>.json   the night's market inputs: regime, factor pairs, technicals, forced flows

The rest of the journal is written elsewhere: the screen (screen.py), the earnings radar (earnings.py) and the
paper book with its research proposals (paper/). This module holds what they share: SETUPS and REASONING (the
vocabulary of a proposal), what counts as a tradable symbol, and `_outcome`, which scores one plan (legs, a
time limit of some sessions, a stop checked on each close) from the first session's open on daily bars.

The nightly and morning idea books, their pre-open check, scores and ledger were retired on 2026-11-09. Their
files stay in journal/ideas, checks, morning, scores, outcomes and intraday as history; nothing reads them.
"""

import json
import os
import re
from datetime import date, datetime, time
from pathlib import Path

import numpy as np
import pandas as pd

from . import storage
from .analytics import flows
from .analytics.compute import latest_snapshot
from .analytics.technicals import factor_pair_technicals, regime, symbol_technicals
from .config import NY
from .bars import bars_path, load
from .calendars import NYSE
from .universe import CONSTITUENTS, ETFS, INDEXES, OPTION_INDEXES, bar_universe, idea_universe

JOURNAL = Path(os.environ.get("FR_JOURNAL_DIR", "journal"))
EXPOSURE_FIELDS = [
    "underlying", "spot", "gex_total", "gex_0dte", "gamma_flip", "gamma_flip_distance", "call_wall", "put_wall",
    "vanna_exp_total", "charm_exp_total", "atm_iv_30d", "term_slope", "skew_25d", "pc_volume_ratio",
    "pc_oi_ratio", "call_premium", "put_premium", "zero_dte_volume_share", "unusual_count", "unusual_premium",
    "top_unusual",
]


def _calendar():
    return NYSE


def is_trading_day(day: date) -> bool:
    return not _calendar().valid_days(day, day).empty


def next_trading_day(as_of: date) -> date:
    days = _calendar().valid_days(as_of + pd.Timedelta(days=1), as_of + pd.Timedelta(days=10))
    return days[0].date()


def previous_trading_day(day: date) -> date:
    days = _calendar().valid_days(day - pd.Timedelta(days=10), day - pd.Timedelta(days=1))
    return days[-1].date()


# A side of a trade is one symbol, or a basket held in equal weight. Any US-listed stock or ETF can be a leg or
# a member, inside the standard universe or not; bars for names outside it are pulled with the paper book's
# legs (book.symbols_held). Indexes can't be bought, so they can't be legs.
BASKET_SIZE = (3, 10)
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,6}$")
NOT_TRADABLE = {*INDEXES, *OPTION_INDEXES, "NDX", "RUT", "DJI", "SOX", "VIX1D", "VIX9D", "VIX6M", "FVX", "TYX"}


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

# The setup a proposal came from, kept with it so results can be compared by setup. The screen (`fr screen`)
# finds the first sixteen; "catalyst" and "other" cover the research pass's own ideas.
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

# The five steps behind every take, kept with it so the reasoning can be checked against the outcome.
REASONING = {
    "what_changed": "what the screen or the tape showed, with numbers",
    "story": "why it moved, with sources and dates",
    "chart": "what the technicals said, for and against",
    "coming": "events inside the time limit",
    "case_against": "the strongest case against, and why it didn't win",
}

def build_brief(as_of: date, write: bool = True) -> dict:
    """The night's market inputs, saved to the journal. `write=False` is the dashboard's unsaved version from the
    latest bars, which may be a part-finished day: the research passes read only what is saved."""
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
    if write:
        path = JOURNAL / "briefs" / f"{as_of}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(brief, indent=2, default=_json_default) + "\n")
    return brief


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


def _bars_settled(trade_date: date) -> bool:
    """Whether the stored bars were pulled after this session's close. A dashboard refresh during the session
    stores a partial bar for the day, which is no result."""
    pulled = storage.modified(bars_path("SPY"))
    close = datetime.combine(trade_date, time(16, 15), NY)
    return pulled is not None and pulled.astimezone(NY) >= close


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
