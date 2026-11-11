"""The nightly stock and basket scan: where a stock or basket idea could come from tonight.

It only lists candidates. The screen (`fr screen`) ranks them with the factor pairs and earnings setups, and
the research pass judges the shortlist.
"""

import json
from datetime import date, datetime

import numpy as np
import pandas as pd

from . import bars, earnings, journal
from .analytics import earnings_gaps
from .analytics.technicals import symbol_technicals
from .config import NY
from .market import _baskets, basket_library
from .universe import ETFS

MOVERS = 10
MIN_DOLLAR_VOLUME = 20e6


def _groups() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for b in basket_library()["baskets"]:
        for m in b["members"]:
            out.setdefault(m["symbol"], []).append(b)
    return out


def _gaps() -> dict[tuple[str, str], dict]:
    if not earnings_gaps.PATH.exists():
        return {}
    return {(r["gap"], r["cut"]): r for r in json.loads(earnings_gaps.PATH.read_text())["rows"]}


def _stock_frame(frame: pd.DataFrame, tech: pd.DataFrame, groups: dict) -> pd.DataFrame:
    frame = frame.sort_values(["symbol", "date"])
    g = frame.groupby("symbol")
    last, prev = g.tail(1).set_index("symbol"), g.nth(-2).set_index("symbol")
    frame["dollars"] = frame["close"] * frame["volume"]
    out = tech.set_index("symbol")[["close", "ret_1d", "ret_5d", "ret_20d", "atr_pct", "rsi14", "volume_z20",
                                    "rs_vs_spy_20d", "dist_sma200", "dist_52w_high"]].copy()
    out["gap"] = last["open"] / prev["close"] - 1
    out["open_to_close"] = last["close"] / last["open"] - 1
    out["move_atr"] = out["ret_1d"] / out["atr_pct"]
    out["usd_volume_20d"] = frame.groupby("symbol")["dollars"].apply(lambda s: s.tail(20).median())
    out["baskets"] = [", ".join(b["name"] for b in groups.get(s, [])) for s in out.index]
    return out.drop(index=[s for s in out.index if s in ETFS])


def _row(symbol: str, r: pd.Series) -> dict:
    return journal._records(pd.DataFrame([{"symbol": symbol, **r.to_dict()}]))[0]


def _reports(stocks: pd.DataFrame, as_of: date, next_session: date, evidence: dict) -> dict:
    tonight, traded = [], []
    prior = journal.previous_trading_day(as_of)
    for symbol, rows in earnings.load().get("names", {}).items():
        if symbol not in stocks.index:
            continue
        for r in rows:
            d, when = date.fromisoformat(r["date"]), r["session"]
            if (d == as_of and when == "after_close") or as_of < d < next_session \
                    or (d == next_session and when == "before_open"):
                tonight.append({**_row(symbol, stocks.loc[symbol]), "report": r["date"], "when": when,
                                "eps_estimate": r.get("eps_estimate")})
            elif (d == as_of and when == "before_open") or (d == prior and when == "after_close"):
                row = _row(symbol, stocks.loc[symbol])
                bucket = earnings_gaps.bucket_of(row["gap"])
                traded.append({**row, "report": r["date"], "when": when, "surprise_pct": r.get("surprise_pct"),
                               "gap_bucket": bucket, "history": evidence.get((bucket, "all"))})
    return {"tonight": sorted(tonight, key=lambda r: -(r["usd_volume_20d"] or 0)),
            "traded_today": sorted(traded, key=lambda r: -abs(r["gap"] or 0))}


def _baskets_ranked() -> list[dict]:
    rows = _baskets()
    members = [r for r in rows if r["section"] == "basket_member"]
    out = []
    for b in (r for r in rows if r["section"] == "basket"):
        mine = sorted((m for m in members if m["basket"] == b["id"] and m["change_pct"] is not None),
                      key=lambda m: m["change_pct"])
        out.append({k: b.get(k) for k in ("id", "name", "hedge", "members", "spread_today_pct", "ret_5d", "ret_20d", "z_50d")}
                   | {"worst": [mine[0]["symbol"], mine[0]["change_pct"]] if mine else None,
                      "best": [mine[-1]["symbol"], mine[-1]["change_pct"]] if mine else None,
                      "member_range_pct": round(mine[-1]["change_pct"] - mine[0]["change_pct"], 2) if mine else None})
    return sorted(out, key=lambda r: -abs(r["z_50d"] or 0))


def build() -> dict:
    groups = _groups()
    names = sorted({*earnings.symbols(), *groups, "SPY"})
    frame = bars.load(names)
    as_of = frame["date"].max().date()
    frame = frame[frame.groupby("symbol")["date"].transform("max") == frame["date"].max()]
    stocks = _stock_frame(frame, symbol_technicals(frame), groups)
    stocks = stocks[stocks["usd_volume_20d"] >= MIN_DOLLAR_VOLUME].replace([np.inf, -np.inf], np.nan)
    next_session = journal.next_trading_day(as_of)
    ranked = stocks.dropna(subset=["move_atr"]).sort_values("move_atr")
    return {
        "as_of": as_of.isoformat(),
        "built_at": datetime.now(NY).isoformat(timespec="seconds"),
        "bars_settled": journal._bars_settled(as_of),
        "next_session": next_session.isoformat(),
        "reports": _reports(stocks, as_of, next_session, _gaps()),
        "movers_up": [_row(s, r) for s, r in ranked.tail(MOVERS).iloc[::-1].iterrows()],
        "movers_down": [_row(s, r) for s, r in ranked.head(MOVERS).iterrows()],
        "baskets": _baskets_ranked(),
    }


def write() -> tuple[str, dict]:
    payload = build()
    path = journal.JOURNAL / "scans" / f"{payload['as_of']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, default=journal._json_default) + "\n")
    return str(path), payload
