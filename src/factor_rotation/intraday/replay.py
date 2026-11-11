"""The intraday setups run over the stored 15-minute bars, with no model and no news check.

It answers two questions before anything trades: how often the setups fire, and what the bare rules would
have made. Each day uses only what was known then: the hedge, beta, ATR and liquidity come from daily bars
through the session before, and the usual volume by time of day from the 20 sessions before.

Costs are an assumption, not a measurement: $1 an order on $10,000 a side is 4 bp for the four orders of a
pair, plus 1 bp a fill for the spread, 8 bp a trade in all.
"""

import json
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from .. import bars, earnings, journal
from ..analytics.technicals import atr, rsi
from ..config import NY
from ..market import basket_library
from ..screen import HEDGE_FUNDS, KEEP_BASKET_WITHIN, MIN_HEDGE_CORR
from ..universe import BARS_ONLY, CONSTITUENTS, ETFS
from . import bars15, signals

PATH = journal.JOURNAL / "intraday" / "replay.json"
MIN_DOLLARS = 100e6        # median daily dollar volume over 20 sessions; intraday needs tighter spreads than the daily screen
VOLUME_SESSIONS = 20
MIN_SESSIONS = 10          # of 15-minute history before a day is replayed
COST = 0.0008
MAX_PER_DAY = 5


def universe() -> list[str]:
    members = {m["symbol"] for b in basket_library()["baskets"] for m in b["members"]}
    return sorted({*ETFS, *CONSTITUENTS, *members, *earnings.symbols()} - set(BARS_ONLY))


def basket_hedges() -> dict[str, str]:
    out: dict[str, str] = {}
    for b in basket_library()["baskets"]:
        for m in b["members"]:
            out.setdefault(m["symbol"], b["hedge"])
    return out


def pick_hedges(r60: pd.DataFrame, baskets: dict[str, str]) -> dict[str, tuple[str, float]]:
    """{name: (hedge, beta)} from 60 sessions of daily returns, by the daily screen's rule: a fund against SPY,
    a stock against the fund it moves with most, its basket's hedge when that is nearly as good."""
    r60 = r60.dropna(axis=1, thresh=30)
    corr, cov = r60.corr(min_periods=30), r60.cov(min_periods=30)
    funds = [f for f in HEDGE_FUNDS if f in corr]
    out = {}
    for sym in corr.columns:
        if sym == "SPY" or "SPY" not in corr:
            continue
        hedge = "SPY"
        if sym not in ETFS:
            c = corr.loc[sym, [f for f in {*funds, baskets.get(sym, "SPY")} if f in corr]].dropna()
            if c.empty:
                continue
            best, own = c.idxmax(), baskets.get(sym)
            if own in c and c[own] >= c[best] - KEEP_BASKET_WITHIN and c[own] >= MIN_HEDGE_CORR:
                hedge = own
            elif c[best] >= MIN_HEDGE_CORR:
                hedge = best
        beta = cov.loc[sym, hedge] / cov.loc[hedge, hedge]
        if np.isfinite(beta):
            out[sym] = (hedge, float(beta))
    return out


def _reported(reports: dict, sym: str, day: str, prior: str) -> bool:
    return any((r["date"] == day and r["session"] == "before_open") or (r["date"] == prior and r["session"] != "before_open")
               for r in reports.get(sym, []))


def _rsi_grid(close: np.ndarray) -> np.ndarray:
    flat = close.ravel()
    out = np.full(flat.shape, np.nan)
    have = ~np.isnan(flat)
    out[have] = rsi(pd.Series(flat[have])).to_numpy()
    return out.reshape(close.shape)


def _day(g: dict, d: int) -> dict:
    return {k: v[d] for k, v in g.items()}


def slot_time(slot: int) -> str:
    """When a slot's bar closes, as New York clock time."""
    m = bars15.OPEN_MINUTE + 15 * (slot + 1)
    return f"{(m // 60 - 1) % 12 + 1}:{m % 60:02d} {'AM' if m < 720 else 'PM'}"


@dataclass
class Prepared:
    """Everything the setups need, loaded once: the 15-minute grids and the daily tables behind each day's context."""
    days: pd.DatetimeIndex
    grids: dict
    rsis: dict
    curves: dict
    rets: pd.DataFrame
    dollars: pd.DataFrame
    atr_pct: pd.DataFrame
    reports: dict
    baskets: dict


def prepare(today: pd.Timestamp | None = None) -> Prepared:
    """`today` adds a session still in progress as the last day; the replay leaves it out."""
    names = universe()
    daily = bars.load(names)
    daily = daily[daily["symbol"].isin(names)].sort_values(["symbol", "date"])
    close = daily.pivot_table(index="date", columns="symbol", values="close").sort_index()
    dollars = (daily.assign(d=daily["close"] * daily["volume"]).pivot_table(index="date", columns="symbol", values="d")
               .sort_index().rolling(20).median())
    atr_pct = pd.DataFrame({s: (atr(df.set_index("date")) / df.set_index("date")["close"]) for s, df in daily.groupby("symbol")})

    spy = bars15.load("SPY")
    counts = spy.groupby(spy["when"].dt.normalize()).size()
    full = counts[counts == bars15.SLOTS].index   # full sessions only: a half day has no 3:45 PM bar
    days = pd.DatetimeIndex(sorted({*(d for d in full if today is None or d < today), *([today] if today is not None else [])}))
    grids, rsis, curves = {}, {}, {}
    for s in names:
        df = bars15.load(s)
        if df is None or df.empty:
            continue
        g = bars15.grid(df, days)
        grids[s], rsis[s] = g, _rsi_grid(g["close"])
        cum = pd.DataFrame(np.nancumsum(g["volume"], axis=1))
        curves[s] = cum.rolling(VOLUME_SESSIONS, min_periods=MIN_SESSIONS).mean().shift(1).to_numpy()
    return Prepared(days, grids, rsis, curves, close.pct_change(), dollars, atr_pct,
                    earnings.load().get("names", {}), basket_hedges())


def candidates(p: Prepared, d: int):
    """Every name that can be traded on day d, with its hedge and what was known before the open:
    (name, hedge, its bars, the hedge's bars, 15-minute RSI, context, reported earnings since the last close)."""
    day, prior = p.days[d], p.days[d - 1]
    if prior not in p.rets.index:
        return
    for sym, (hedge, beta) in pick_hedges(p.rets.loc[:prior].iloc[-60:], p.baskets).items():
        if sym not in p.grids or hedge not in p.grids or not p.dollars.at[prior, sym] >= MIN_DOLLARS:
            continue
        s, h = _day(p.grids[sym], d), _day(p.grids[hedge], d)
        a = p.atr_pct.at[prior, sym]
        pc, hpc = p.grids[sym]["close"][d - 1, -1], p.grids[hedge]["close"][d - 1, -1]
        if not (np.isfinite(a) and a > 0 and np.isfinite(pc) and np.isfinite(hpc)) or np.isnan(s["open"][0]) or np.isnan(h["open"][0]):
            continue
        if np.isnan(p.curves[sym][d]).any() or abs(s["open"][0] / pc - 1) > 0.4:
            continue
        reported = sym not in ETFS and _reported(p.reports, sym, day.date().isoformat(), prior.date().isoformat())
        yield sym, hedge, s, h, p.rsis[sym][d], signals.Context(float(a), beta, float(pc), float(hpc), p.curves[sym][d]), reported


def run() -> dict:
    p = prepare()
    trades = []
    for d in range(MIN_SESSIONS, len(p.days)):
        for sym, hedge, s, h, rsi15, ctx, reported in candidates(p, d):
            hit = signals.first_hit(s, h, rsi15, ctx)
            if not hit:
                continue
            ruled, held = signals.score(hit, s, h, ctx.atr_pct), signals.score(hit, s, h, ctx.atr_pct, None, None)
            if not ruled or not held:
                continue
            trades.append({"date": p.days[d].date().isoformat(), "symbol": sym, "hedge": hedge, "beta": round(ctx.beta, 2),
                           "setup": hit["setup"], "direction": hit["direction"], "slot": hit["slot"],
                           "signal_at": slot_time(hit["slot"]), **hit["numbers"], "atr_pct": round(ctx.atr_pct, 4),
                           "reported": reported, "result": round(ruled["result"], 5), "exit": ruled["reason"],
                           "bars": ruled["bars"], "held_to_close": round(held["result"], 5)})
    replayed = [x.date().isoformat() for x in p.days[MIN_SESSIONS:]]
    return {"built_at": datetime.now(NY).isoformat(timespec="seconds"), "sessions": replayed, "names": len(p.grids),
            "cost_bp": COST * 1e4, "summary": summarise(trades, replayed), "trades": trades}


def _stats(results: pd.Series, sessions: int) -> dict:
    n = len(results)
    if not n:
        return {"trades": 0, "per_day": 0.0}
    bp = results * 1e4
    return {"trades": n, "per_day": round(n / sessions, 1), "win_rate": round(float((results > 0).mean()), 2),
            "mean_bp": round(float(bp.mean()), 1), "median_bp": round(float(bp.median()), 1),
            "after_cost_bp": round(float(bp.mean() - COST * 1e4), 1),
            "t": round(float(bp.mean() / (bp.std() / n ** 0.5)), 1) if n > 2 and bp.std() else None}


def summarise(trades: list[dict], sessions: list[str]) -> dict:
    df = pd.DataFrame(trades)
    n = len(sessions)
    if df.empty:
        return {"all": _stats(pd.Series(dtype=float), n)}
    per_day = df.groupby("date").size().reindex(sessions, fill_value=0)
    capped = df.sort_values(["date", "slot", "rvol"], ascending=[True, True, False]).groupby("date").head(MAX_PER_DAY)
    quiet = df[~df["reported"]]
    return {
        "all": _stats(df["result"], n),
        "held_to_close": _stats(df["held_to_close"], n),
        "by_setup": {f"{k} {d}": _stats(g["result"], n) for (k, d), g in df.groupby(["setup", "direction"])},
        "by_setup_held_to_close": {f"{k} {d}": _stats(g["held_to_close"], n) for (k, d), g in df.groupby(["setup", "direction"])},
        "no_earnings": _stats(quiet["result"], n),
        "earnings_days": _stats(df[df["reported"]]["result"], n),
        "exits": df["exit"].value_counts().to_dict(),
        "signals_per_day": {"median": float(per_day.median()), "min": int(per_day.min()), "max": int(per_day.max()),
                            "days_with_none": int((per_day == 0).sum())},
        f"first_{MAX_PER_DAY}_a_day": _stats(capped["result"], n),
    }


def write() -> tuple[str, dict]:
    payload = run()
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(payload, indent=1) + "\n")
    return str(PATH), payload


def text(payload: dict) -> str:
    s = payload["summary"]
    lines = [f"{len(payload['sessions'])} sessions ({payload['sessions'][0]} to {payload['sessions'][-1]}), {payload['names']} names, "
             f"costs assumed {payload['cost_bp']:.0f} bp a trade"]

    def row(label: str, x: dict) -> str:
        if not x["trades"]:
            return f"  {label:<28} none"
        return (f"  {label:<28} {x['trades']:>5} trades {x['per_day']:>5} a day  win {x['win_rate']:.0%}  mean {x['mean_bp']:+6.1f} bp  "
                f"median {x['median_bp']:+6.1f}  after costs {x['after_cost_bp']:+6.1f}  t {x['t']}")
    lines += [row("all, by the rules", s["all"]), row("all, held to 3:45 PM", s["held_to_close"]),
              row(f"first {MAX_PER_DAY} a day", s[f"first_{MAX_PER_DAY}_a_day"]), row("not an earnings day", s["no_earnings"]),
              row("earnings days", s["earnings_days"]), "by setup, by the rules:"]
    lines += [row(k, v) for k, v in s["by_setup"].items()]
    lines += ["by setup, held to 3:45 PM:"] + [row(k, v) for k, v in s["by_setup_held_to_close"].items()]
    p = s["signals_per_day"]
    lines.append(f"signals a day: median {p['median']:.0f}, from {p['min']} to {p['max']}, {p['days_with_none']} days with none; exits {s['exits']}")
    return "\n".join(lines)
