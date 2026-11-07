"""What stocks have done after earnings gaps: context for stock ideas, not a signal.

Each event is a report in journal/reference/earnings.json and the first session that traded on it. Returns are
against SPY, from that session's open to its close (how ideas are scored) and to the close four sessions later.
The names are today's universe, so the sample leans toward stocks that survived and did well.
"""

import pandas as pd

from .. import bars, earnings
from ..universe import ETFS

GAP_EDGES = [-1, -0.10, -0.05, -0.02, 0.02, 0.05, 0.10, 10]
GAP_LABELS = ["down 10%+", "down 5-10%", "down 2-5%", "flat ±2%", "up 2-5%", "up 5-10%", "up 10%+"]
SPLIT_YEAR = 2024
MIN_EVENTS = 15
HOLD_SESSIONS = 4


def events() -> pd.DataFrame:
    spy = bars.load(["SPY"]).set_index("date").sort_index()
    spy_up_trend = spy["close"].shift() > spy["close"].rolling(50).mean().shift()
    rows = []
    for symbol, reports in earnings.load().get("names", {}).items():
        if symbol in ETFS:
            continue
        df = bars.load([symbol])
        if df.empty:
            continue
        df = df.set_index("date").sort_index()
        days = df.index
        for r in reports:
            if r.get("eps_reported") is None or r["session"] not in ("before_open", "after_close"):
                continue
            day = pd.Timestamp(r["date"])
            i = days.searchsorted(day, side="left" if r["session"] == "before_open" else "right")
            if i == 0 or i >= len(days) or days[i] not in spy.index:
                continue
            d, today, prev = days[i], df.iloc[i], df.iloc[i - 1]
            oc = today["close"] / today["open"] - 1
            spy_day = spy.loc[d]
            row = {"symbol": symbol, "session": d, "gap": today["open"] / prev["close"] - 1,
                   "rel_oc": oc - (spy_day["close"] / spy_day["open"] - 1),
                   "surprise_pct": r.get("surprise_pct"), "spy_up_trend": bool(spy_up_trend.get(d, False)),
                   "rel_hold": None}
            if i + HOLD_SESSIONS < len(days) and days[i + HOLD_SESSIONS] in spy.index:
                end = days[i + HOLD_SESSIONS]
                row["rel_hold"] = (df["close"].iloc[i + HOLD_SESSIONS] / today["open"]
                                   - spy.loc[end, "close"] / spy_day["open"])
            rows.append(row)
    out = pd.DataFrame(rows)
    out["bucket"] = pd.cut(out["gap"], GAP_EDGES, labels=GAP_LABELS)
    return out


def _stats(df: pd.DataFrame) -> dict:
    hold = pd.to_numeric(df["rel_hold"]).dropna()
    return {"n": len(df), "sessions": int(df["session"].nunique()),
            "mean_bp": round(df["rel_oc"].mean() * 1e4, 1), "median_bp": round(df["rel_oc"].median() * 1e4, 1),
            "up_share": round((df["rel_oc"] > 0).mean(), 3),
            "hold_mean_bp": round(hold.mean() * 1e4, 1) if len(hold) else None,
            "hold_up_share": round((hold > 0).mean(), 3) if len(hold) else None}


def table(df: pd.DataFrame) -> list[dict]:
    """A row per gap size, alone and split by surprise, market backdrop and period. Thin cuts are left out."""
    cuts = {"all": lambda d: d,
            "beat": lambda d: d[d["surprise_pct"] > 0],
            "miss": lambda d: d[d["surprise_pct"] < 0],
            "spy above 50-day": lambda d: d[d["spy_up_trend"]],
            "spy below 50-day": lambda d: d[~d["spy_up_trend"]],
            f"before {SPLIT_YEAR}": lambda d: d[d["session"].dt.year < SPLIT_YEAR],
            f"{SPLIT_YEAR} on": lambda d: d[d["session"].dt.year >= SPLIT_YEAR]}
    rows = []
    for label in GAP_LABELS:
        group = df[df["bucket"] == label]
        for cut, pick in cuts.items():
            part = pick(group)
            if len(part) >= MIN_EVENTS:
                rows.append({"gap": label, "cut": cut, **_stats(part)})
    return rows


def bucket_of(gap: float) -> str:
    return str(pd.cut([gap], GAP_EDGES, labels=GAP_LABELS)[0])


PATH = earnings.PATH.with_name("earnings_gaps.json")
