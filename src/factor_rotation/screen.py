"""The candidate screen: where a research pass should look.

Code checks every stock, ETF, themed basket and factor pair we have bars for against a fixed list of setups
(journal.SETUPS) and writes the best 20 or so to journal/screens/<as_of>.json, each with the numbers that
triggered it and a one-line chart verdict. The pass researches only those. The screen never decides: it says
where to look, and every recorded idea carries its setup so results can be compared setup by setup.

Daily bars only for now; the intraday version for the daytime passes comes with the pass schedule.
"""

import json
from datetime import date, datetime

import numpy as np
import pandas as pd

from . import bars, earnings, journal
from .analytics.technicals import atr, rsi
from .config import NY
from .market import _regime, basket_library
from .universe import BARS_ONLY, CONSTITUENTS, ETFS, FACTOR_PAIRS

MIN_DOLLAR_VOLUME = 20e6
TOP = 20
MORE = 15
HEAVY_VOLUME = 1.5
# How much each setup counts when ranking, before its strength (0-1) is added. Earnings and relative strength
# carry the most information; a stretch with no turn is only context.
WEIGHTS = {
    "earnings_gap_held": 3.0, "earnings_gap_faded": 2.6, "relative_strength": 2.5, "relative_weakness": 2.5,
    "basket_breakout": 2.2, "basket_breakdown": 2.2, "breakout": 2.1, "breakdown": 2.1, "turn": 2.0,
    "reclaim_200": 2.0, "lost_200": 2.0, "basket_big_move": 1.8, "factor_turn": 1.6,
    "stretched": 1.0, "basket_stretched": 1.0, "factor_stretched": 1.0,
}
EARNINGS_SETUPS = {"earnings_gap_held", "earnings_gap_faded"}


def _pct(x: float, places: int = 1) -> str:
    return f"{x * 100:+.{places}f}%"


def _groups() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for b in basket_library()["baskets"]:
        for m in b["members"]:
            out.setdefault(m["symbol"], []).append(b)
    return out


class _Days:
    """Trading-session arithmetic around tonight."""

    def __init__(self, as_of: date):
        # One calendar lookup up front: building the exchange calendar per report is slow.
        self.as_of = as_of
        cal = journal._calendar().valid_days(as_of - pd.Timedelta(days=800), as_of + pd.Timedelta(days=90))
        self.sessions = [d.date() for d in cal]
        self.open = set(self.sessions)
        self.next = next(d for d in self.sessions if d > as_of)
        self.ahead = [d for d in self.sessions if d >= self.next]

    def reaction(self, report: dict) -> date | None:
        """The first session that trades on a report."""
        d = date.fromisoformat(report["date"])
        if report["session"] == "after_close":
            return next((s for s in self.sessions if s > d), None)
        return d if report["session"] == "before_open" and d in self.open else None

    def sessions_away(self, d: date) -> int | None:
        """1 = the next session."""
        return self.ahead.index(d) + 1 if d in self.ahead else None


def _next_report(symbol: str, reports: dict, days: _Days) -> dict | None:
    best = None
    for r in reports.get(symbol, []):
        d = days.reaction(r)
        if d and d > days.as_of and (best is None or d < best["reacts"]):
            best = {"date": r["date"], "when": r["session"], "reacts": d, "sessions_away": days.sessions_away(d)}
    return best


def _verdict(f: dict) -> tuple[str, dict]:
    """One line on the chart, and the numbers behind it."""
    c = f["close"]
    if f["sma200"] is None:
        trend = "above the 50-day; under 200 days of history" if c > f["sma50"] else "below the 50-day; under 200 days of history"
    elif c > f["sma50"] and c > f["sma200"]:
        trend = "uptrend: above the 50 and 200-day"
    elif c < f["sma50"] and c < f["sma200"]:
        trend = "downtrend: below the 50 and 200-day"
    elif c > f["sma50"]:
        trend = "above the 50-day, below the 200-day"
    else:
        trend = "below the 50-day, above the 200-day"
    levels = [("20-day high", f["hi20"]), ("52-week high", f["hi52"]), ("20-day low", f["lo20"]), ("52-week low", f["lo52"]),
              ("50-day", f["sma50"]), ("200-day", f["sma200"])]
    above = [(v, k) for k, v in levels if v is not None and v > c]
    below = [(v, k) for k, v in levels if v is not None and v < c]
    parts = [trend, f"{f['dist20_atr']:+.1f} ATR from the 20-day, RSI {f['rsi']:.0f}", f"volume {f['vol_ratio']:.1f}x"]
    if above:
        v, k = min(above)
        parts.append(f"resistance {v:,.2f} ({k}, {(v / c - 1) * 100:+.1f}%)")
    if below:
        v, k = max(below)
        parts.append(f"support {v:,.2f} ({k}, {(v / c - 1) * 100:+.1f}%)")
    parts.append(f"ATR {f['atr']:,.2f} ({f['atr_pct'] * 100:.1f}%)")
    nums = {k: f[k] for k in ("close", "ret_1d", "ret_5d", "ret_20d", "atr", "atr_pct", "rsi", "dist20_atr", "vol_ratio",
                              "sma50", "sma200", "hi52", "lo52", "usd_volume")}
    return "; ".join(parts), journal._records(pd.DataFrame([nums]))[0]


def _features(df: pd.DataFrame) -> dict | None:
    df = df.sort_values("date").reset_index(drop=True)
    if len(df) < 60:
        return None
    c, o, h, lo, v = (df[k].to_numpy(float) for k in ("close", "open", "high", "low", "volume"))
    a = atr(df).to_numpy()
    sma = lambda n: df["close"].rolling(n).mean().to_numpy()  # noqa: E731
    s20, s50, s200 = sma(20), sma(50), sma(200)
    rng = h[-1] - lo[-1]
    prior_vol = v[-21:-1].mean()
    below200 = 0
    for i in range(len(c) - 2, -1, -1):
        if np.isnan(s200[i]) or c[i] >= s200[i]:
            break
        below200 += 1
    above200 = 0
    for i in range(len(c) - 2, -1, -1):
        if np.isnan(s200[i]) or c[i] <= s200[i]:
            break
        above200 += 1
    return {
        "df": df, "c": c, "o": o, "h": h, "l": lo, "v": v, "a": a, "dates": df["date"].dt.date.to_numpy(),
        "close": c[-1], "ret_1d": c[-1] / c[-2] - 1, "ret_5d": c[-1] / c[-6] - 1, "ret_20d": c[-1] / c[-21] - 1,
        "gap": o[-1] / c[-2] - 1, "atr": a[-1], "atr_pct": a[-1] / c[-1], "atr_pct_prev": a[-2] / c[-2],
        "rsi": float(rsi(df["close"]).iloc[-1]), "rsi_prev": float(rsi(df["close"]).iloc[-2]),
        "range_pos": (c[-1] - lo[-1]) / rng if rng > 0 else 0.5,
        "vol_ratio": v[-1] / prior_vol if prior_vol > 0 else 0.0, "usd_volume": float(np.median((c * v)[-20:])),
        "sma50": s50[-1], "sma200": None if np.isnan(s200[-1]) else s200[-1],
        "dist20_atr": (c[-1] - s20[-1]) / a[-1], "dist20_atr_prev": (c[-2] - s20[-2]) / a[-2],
        "hi20": h[-21:-1].max(), "lo20": lo[-21:-1].min(), "hi52": h[-253:-1].max(), "lo52": lo[-253:-1].min(),
        "hi52_close": c[-253:-1].max(), "lo52_close": c[-253:-1].min(),
        "days_below_200": below200, "days_above_200": above200, "above200_now": s200[-1] < c[-1] if not np.isnan(s200[-1]) else None,
        "down_streak": int(c[-2] < c[-3] < c[-4] < c[-5]), "up_streak": int(c[-2] > c[-3] > c[-4] > c[-5]),
    }


def _symbol_hits(sym: str, f: dict, hedge: str, hedge_ret: pd.Series, spy_ret1: float, reports: dict,
                 days: _Days) -> list[dict]:
    hits = []

    def add(setup: str, direction: int, strength: float, why: str) -> None:
        hits.append({"setup": setup, "direction": "long" if direction > 0 else "short", "strength": float(abs(strength)), "why": why})

    ret1, atrp = f["ret_1d"], f["atr_pct_prev"]
    # Earnings in the last three sessions: gapped and held, or gapped and faded, or the reaction ran against the news.
    if sym not in ETFS:
        for r in reports.get(sym, []):
            k_day = days.reaction(r)
            if k_day is None or k_day not in set(f["dates"][-3:]):
                continue
            k = int(np.where(f["dates"] == k_day)[0][0])
            c, o, h, lo, v, a = f["c"], f["o"], f["h"], f["l"], f["v"], f["a"]
            gap = o[k] / c[k - 1] - 1
            gap_atr = gap / (a[k - 1] / c[k - 1])
            rng = h[k] - lo[k]
            pos = (c[k] - lo[k]) / rng if rng > 0 else 0.5
            volr = v[k] / v[max(0, k - 20):k].mean()
            day_ret = c[k] / c[k - 1] - 1
            rel = day_ret - float(hedge_ret.get(pd.Timestamp(k_day), 0.0))
            ago = len(c) - 1 - k
            when = {0: "today", 1: "yesterday"}.get(ago, f"{ago} sessions ago")
            head = f"reported {r['date']} {r['session'].replace('_', ' ')}, reacted {when}: gap {_pct(gap)} ({gap_atr:+.1f} ATR), " \
                   f"day {_pct(day_ret)}, {_pct(rel)} against {hedge}, volume {volr:.1f}x"
            if r.get("surprise_pct") is not None:
                head += f", EPS surprise {r['surprise_pct']:+.0f}%"
            if abs(gap_atr) >= 1.5:
                d = 1 if gap > 0 else -1
                strong = pos >= 2 / 3 if d > 0 else pos <= 1 / 3
                weak = pos <= 1 / 3 if d > 0 else pos >= 2 / 3
                mid = lo[k] + rng / 2
                held = d * (c[-1] - mid) > 0
                if strong and volr >= 2 and held:
                    add("earnings_gap_held", d, gap_atr, f"{head}; closed in the {'top' if d > 0 else 'bottom'} third and still "
                        f"{'above' if d > 0 else 'below'} the middle of that day's range ({mid:,.2f})")
                elif weak:
                    add("earnings_gap_faded", -d, gap_atr, f"{head}; closed in the {'bottom' if d > 0 else 'top'} third, "
                        f"against the gap")
            surprise = r.get("surprise_pct")
            rel_atr = rel / (a[k - 1] / c[k - 1])
            if surprise is not None and surprise > 0 and rel_atr <= -1:
                add("earnings_gap_faded", -1, rel_atr, f"{head}; beat estimates and still fell against {hedge} (good news sold)")
            elif surprise is not None and surprise < 0 and rel_atr >= 1:
                add("earnings_gap_faded", 1, rel_atr, f"{head}; missed estimates and still rose against {hedge} (bad news bought)")
    heavy = f["vol_ratio"] >= HEAVY_VOLUME
    move_atr = ret1 / atrp
    if heavy and f["close"] > f["hi52_close"]:
        add("breakout", 1, move_atr, f"closed at {f['close']:,.2f}, above the 52-week closing high {f['hi52_close']:,.2f}, "
            f"on {f['vol_ratio']:.1f}x volume ({_pct(ret1)}, {move_atr:+.1f} ATR)")
    elif heavy and f["close"] > f["hi20"] and ret1 > 0:
        add("breakout", 1, move_atr, f"closed at {f['close']:,.2f}, above the 20-day high {f['hi20']:,.2f}, on "
            f"{f['vol_ratio']:.1f}x volume ({_pct(ret1)}, {move_atr:+.1f} ATR)")
    if heavy and f["close"] < f["lo52_close"]:
        add("breakdown", -1, move_atr, f"closed at {f['close']:,.2f}, below the 52-week closing low {f['lo52_close']:,.2f}, "
            f"on {f['vol_ratio']:.1f}x volume ({_pct(ret1)}, {move_atr:+.1f} ATR)")
    elif heavy and f["close"] < f["lo20"] and ret1 < 0:
        add("breakdown", -1, move_atr, f"closed at {f['close']:,.2f}, below the 20-day low {f['lo20']:,.2f}, on "
            f"{f['vol_ratio']:.1f}x volume ({_pct(ret1)}, {move_atr:+.1f} ATR)")
    if heavy and f["above200_now"] and f["days_below_200"] >= 20:
        add("reclaim_200", 1, move_atr, f"closed above the 200-day ({f['sma200']:,.2f}) after {f['days_below_200']} sessions "
            f"below, on {f['vol_ratio']:.1f}x volume")
    if heavy and f["above200_now"] is False and f["days_above_200"] >= 20:
        add("lost_200", -1, move_atr, f"closed below the 200-day ({f['sma200']:,.2f}) after {f['days_above_200']} sessions "
            f"above, on {f['vol_ratio']:.1f}x volume")
    floor = max(atrp, 0.01)
    if spy_ret1 <= -0.01 and ret1 >= floor:
        add("relative_strength", 1, (ret1 - spy_ret1) / atrp, f"{_pct(ret1)} ({move_atr:+.1f} ATR) on a day SPY fell {_pct(spy_ret1)}")
    if spy_ret1 >= 0.01 and ret1 <= -floor:
        add("relative_weakness", -1, (ret1 - spy_ret1) / atrp, f"{_pct(ret1)} ({move_atr:+.1f} ATR) on a day SPY rose {_pct(spy_ret1)}")
    # Stretched: RSI or distance from the 20-day. A turn needs yesterday's stretch, a close in the day's top or
    # bottom quarter against it, and a losing (winning) streak or a move of a full ATR.
    was_low = f["rsi_prev"] <= 25 or f["dist20_atr_prev"] <= -3
    was_high = f["rsi_prev"] >= 75 or f["dist20_atr_prev"] >= 3
    if was_low and ret1 > 0 and f["range_pos"] >= 0.75 and (f["down_streak"] or move_atr >= 1):
        add("turn", 1, f["dist20_atr_prev"], f"was stretched down (RSI {f['rsi_prev']:.0f}, {f['dist20_atr_prev']:+.1f} ATR from "
            f"the 20-day) and turned: {_pct(ret1)}, closed in the top quarter of the day")
    elif was_high and ret1 < 0 and f["range_pos"] <= 0.25 and (f["up_streak"] or move_atr <= -1):
        add("turn", -1, f["dist20_atr_prev"], f"was stretched up (RSI {f['rsi_prev']:.0f}, {f['dist20_atr_prev']:+.1f} ATR from "
            f"the 20-day) and turned: {_pct(ret1)}, closed in the bottom quarter of the day")
    elif f["rsi"] <= 25 or f["dist20_atr"] <= -3:
        add("stretched", 1, f["dist20_atr"] / 3, f"stretched down: RSI {f['rsi']:.0f}, {f['dist20_atr']:+.1f} ATR from the 20-day, no turn yet")
    elif f["rsi"] >= 75 or f["dist20_atr"] >= 3:
        add("stretched", -1, f["dist20_atr"] / 3, f"stretched up: RSI {f['rsi']:.0f}, {f['dist20_atr']:+.1f} ATR from the 20-day, no turn yet")
    return hits


def _spread_hits(name: str, spread: pd.Series, stretched_setup: str, big_setup: str | None) -> tuple[list[dict], dict]:
    """Setups on a spread's daily returns (a basket against its hedge, or a factor pair)."""
    spread = spread.dropna()
    idx = (1 + spread).cumprod()
    sd = spread.iloc[-21:-1].std()
    today = spread.iloc[-1]
    mean50, std50 = idx.rolling(50).mean(), idx.rolling(50).std()
    z = ((idx - mean50) / std50).iloc[-2:]
    z_prev, z_now = float(z.iloc[0]), float(z.iloc[1])
    nums = {"today": today, "sd_20d": sd, "today_sd": today / sd if sd else None, "ret_5d": idx.iloc[-1] / idx.iloc[-6] - 1,
            "ret_20d": idx.iloc[-1] / idx.iloc[-21] - 1, "z_50d": z_now, "z_50d_prev": z_prev,
            "new_20d_high": bool(idx.iloc[-1] > idx.iloc[-21:-1].max()), "new_20d_low": bool(idx.iloc[-1] < idx.iloc[-21:-1].min())}
    base = f"{name}: {_pct(today)} today ({today / sd:+.1f} sd), 5-day {_pct(nums['ret_5d'])}, 20-day {_pct(nums['ret_20d'])}, z {z_now:+.1f}"
    hits = []
    if big_setup:  # baskets
        if nums["new_20d_high"] and today >= sd:
            hits.append({"setup": "basket_breakout", "direction": "long", "strength": today / sd, "why": f"{base}; a 20-day high"})
        elif nums["new_20d_low"] and today <= -sd:
            hits.append({"setup": "basket_breakdown", "direction": "short", "strength": -today / sd, "why": f"{base}; a 20-day low"})
        elif abs(today) >= 2 * sd:
            hits.append({"setup": big_setup, "direction": "long" if today > 0 else "short", "strength": abs(today) / sd, "why": base})
    else:  # factor pairs: a stretch that turned against itself
        if abs(z_prev) >= 2 and np.sign(today) == -np.sign(z_prev) and abs(today) >= sd:
            hits.append({"setup": "factor_turn", "direction": "long" if today > 0 else "short", "strength": abs(z_prev),
                         "why": f"{base}; was stretched at z {z_prev:+.1f} and turned"})
    if not hits and abs(z_now) >= 2:
        hits.append({"setup": stretched_setup, "direction": "long" if z_now < 0 else "short", "strength": abs(z_now) / 2,
                     "why": f"{base}; stretched, no turn yet"})
    return hits, nums


def build() -> dict:
    groups = _groups()
    baskets = basket_library()["baskets"]
    names = sorted({*ETFS, *CONSTITUENTS, *groups, *earnings.symbols()} - set(BARS_ONLY))
    frame = bars.load(names)
    as_of_ts = frame.loc[frame["symbol"] == "SPY", "date"].max()
    as_of = as_of_ts.date()
    frame = frame[frame["date"] <= as_of_ts]
    days = _Days(as_of)
    reports = earnings.load().get("names", {})
    close = frame.pivot_table(index="date", columns="symbol", values="close").sort_index()
    rets = close.pct_change()
    spy_ret1 = float(rets["SPY"].iloc[-1])

    found, feats, held_back = [], {}, []
    for sym, df in frame.groupby("symbol"):
        if df["date"].max() != as_of_ts:
            continue
        f = _features(df)
        if f is None:
            continue
        feats[sym] = f
        is_etf = sym in ETFS
        if not is_etf and f["usd_volume"] < MIN_DOLLAR_VOLUME:
            continue
        hedge = (groups[sym][0]["hedge"] if sym in groups else "SPY") if sym != "SPY" else "SPY"
        hits = _symbol_hits(sym, f, hedge, rets[hedge] if hedge in rets else rets["SPY"], spy_ret1, reports, days)
        if not hits:
            continue
        hits.sort(key=lambda x: -(WEIGHTS[x["setup"]] + min(x["strength"], 6) / 6))
        best = hits[0]
        verdict, nums = _verdict(f)
        nxt = _next_report(sym, reports, days) if not is_etf else None
        cand = {"name": sym, "kind": "etf" if is_etf else "stock", **best, "hedge": None if sym == "SPY" else hedge,
                "baskets": [b["name"] for b in groups.get(sym, [])], "chart": verdict, "numbers": nums,
                "next_earnings": {k: v for k, v in nxt.items() if k != "reacts"} if nxt else None,
                "also": [f"{h['setup']} ({h['direction']})" for h in hits[1:]]}
        if nxt and nxt["sessions_away"] is not None and nxt["sessions_away"] <= 1 and best["setup"] not in EARNINGS_SETUPS:
            held_back.append({**cand, "held_back": f"reports {nxt['date']} {nxt['when'].replace('_', ' ')}, inside the next session"})
            continue
        found.append(cand)

    for b in baskets:
        members = [m["symbol"] for m in b["members"]]
        if any(s not in feats for s in members) or b["hedge"] not in rets:
            continue
        spread = rets[members].mean(axis=1, skipna=False) - rets[b["hedge"]]
        hits, nums = _spread_hits(f"{b['name']} against {b['hedge']}", spread.iloc[-260:], "basket_stretched", "basket_big_move")
        if not hits:
            continue
        today = sorted(((s, feats[s]["ret_1d"]) for s in members), key=lambda x: x[1])
        up50 = sum(feats[s]["close"] > feats[s]["sma50"] for s in members)
        chart = (f"{up50} of {len(members)} members above their 50-day; best {today[-1][0]} {_pct(today[-1][1])}, "
                 f"worst {today[0][0]} {_pct(today[0][1])}; spread {nums['today_sd']:+.1f} sd today, z {nums['z_50d']:+.1f}")
        reporting = [s for s in members if (n := _next_report(s, reports, days)) and n["sessions_away"] is not None
                     and n["sessions_away"] <= 5]
        found.append({"name": b["name"], "basket_id": b["id"], "kind": "basket", "members": members, "hedge": b["hedge"],
                      **hits[0], "chart": chart, "numbers": journal._records(pd.DataFrame([nums]))[0],
                      "members_reporting_within_5": reporting, "also": [], "members_flagged": []})

    for p in FACTOR_PAIRS:
        if p.long not in rets or p.short not in rets:
            continue
        hits, nums = _spread_hits(f"{p.long}/{p.short} ({p.description})", (rets[p.long] - rets[p.short]).iloc[-260:],
                                  "factor_stretched", None)
        if hits:
            found.append({"name": f"{p.long}/{p.short}", "factor": p.name, "kind": "factor", "long": p.long, "short": p.short,
                          **hits[0], "chart": f"{p.description}; z {nums['z_50d']:+.1f} vs 50-day, 20-day {_pct(nums['ret_20d'])}",
                          "numbers": journal._records(pd.DataFrame([nums]))[0], "also": []})

    # A stock moving with its flagged basket is folded into the basket, unless its own news (earnings) is the setup.
    flagged = {c["name"]: c for c in found if c["kind"] == "basket"}
    kept = []
    for c in found:
        if c["kind"] == "stock" and c["setup"] not in EARNINGS_SETUPS:
            home = next((flagged[b] for b in c["baskets"] if b in flagged and flagged[b]["direction"] == c["direction"]), None)
            if home:
                home["members_flagged"].append(f"{c['name']}: {c['setup']}")
                continue
        kept.append(c)
    for c in kept:
        c["score"] = round(WEIGHTS[c["setup"]] + min(c["strength"], 6) / 6, 3)
        c["strength"] = round(c["strength"], 2)
    kept.sort(key=lambda c: -c["score"])
    counts: dict[str, int] = {}
    for c in kept:
        counts[c["setup"]] = counts.get(c["setup"], 0) + 1

    liquid = {s for s, f in feats.items() if s not in ETFS and f["usd_volume"] >= MIN_DOLLAR_VOLUME}
    upcoming = []
    for s in liquid:
        n = _next_report(s, reports, days)
        if n and n["sessions_away"] is not None and n["sessions_away"] <= 5:
            upcoming.append({"symbol": s, "date": n["date"], "when": n["when"], "sessions_away": n["sessions_away"],
                             "baskets": [b["name"] for b in groups.get(s, [])], "usd_volume": round(feats[s]["usd_volume"])})
    upcoming.sort(key=lambda r: (r["sessions_away"], -r["usd_volume"]))

    brief_path = journal.JOURNAL / "briefs" / f"{as_of}.json"
    calendar = json.loads(brief_path.read_text()).get("forced_flows", {}).get("calendar", {}) if brief_path.exists() else {}
    events = [{"event": k, **v} for k, v in calendar.items() if isinstance(v, dict) and v.get("sessions_away", 99) <= 5]

    return {
        "as_of": as_of.isoformat(),
        "built_at": datetime.now(NY).isoformat(timespec="seconds"),
        "bars_settled": journal._bars_settled(as_of),
        "next_session": days.next.isoformat(),
        "screened": len(feats),
        "hits_by_setup": counts,
        "spy_ret_1d": round(spy_ret1, 5),
        "candidates": kept[:TOP],
        "more": [{k: c[k] for k in ("name", "kind", "setup", "direction", "score", "why")} for c in kept[TOP:TOP + MORE]],
        "held_back": held_back,
        "earnings_ahead": upcoming[:40],
        "calendar_ahead": events,
        "regime": _regime_notes(as_of),
    }


def _regime_notes(as_of: date) -> list[str]:
    rows = [r for r in _regime(as_of) if r.get("pct_above_50") is not None]
    if len(rows) < 6:
        return []
    now, prev, wk = rows[-1], rows[-2], rows[-6]
    notes = []
    if (now["pct_above_50"] - 0.5) * (prev["pct_above_50"] - 0.5) < 0:
        notes.append(f"Breadth crossed 50%: {prev['pct_above_50']:.0%} to {now['pct_above_50']:.0%} of our names above their 50-day")
    if now.get("sector_corr_20d") is not None and wk.get("sector_corr_20d") is not None \
            and abs(now["sector_corr_20d"] - wk["sector_corr_20d"]) >= 0.08:
        notes.append(f"Sector correlation moved from {wk['sector_corr_20d']:.2f} to {now['sector_corr_20d']:.2f} in five sessions")
    ratio = lambda r: r["vix"] / r["vix3m"] if r.get("vix") and r.get("vix3m") else None  # noqa: E731
    if ratio(now) and ratio(prev) and (ratio(now) - 1) * (ratio(prev) - 1) < 0:
        notes.append(f"VIX/VIX3M crossed 1: {ratio(prev):.2f} to {ratio(now):.2f}")
    elif ratio(now) and ratio(now) >= 1:
        notes.append(f"VIX/VIX3M still inverted at {ratio(now):.2f}")
    return notes


def write() -> tuple[str, dict]:
    payload = build()
    path = journal.JOURNAL / "screens" / f"{payload['as_of']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, default=journal._json_default) + "\n")
    return str(path), payload


def summary(payload: dict) -> str:
    lines = [f"Screen for {payload['as_of']} (next session {payload['next_session']}): {payload['screened']} names, "
             f"{sum(payload['hits_by_setup'].values())} hits"]
    for c in payload["candidates"]:
        lines.append(f"{c['score']:.2f}  {c['name']:<28} {c['setup']:<20} {c['direction']:<5}  {c['why']}")
    if payload["held_back"]:
        lines.append("Held back (earnings inside the next session): " + ", ".join(c["name"] for c in payload["held_back"]))
    lines += payload["regime"]
    return "\n".join(lines)
