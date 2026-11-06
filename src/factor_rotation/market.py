"""Rows for the dashboard's "Tonight's market" section, from the night's brief and our stored data.

One JSON array; each row has a `section` saying what it is:
  meta        one row: as_of, when the brief was built, the options snapshot
  factor      one row per factor pair: returns, z-scores, RSI, and whether tonight's ideas use it
  regime      the last 60 sessions: breadth, sector correlation, SPY realized vol, VIX, VIX3M, 10y yield
  gamma       dealer gamma by strike for SPY and QQQ near spot, from the latest option snapshot
  gamma_sum   one row per underlying: spot, total gamma, flip, walls, next-expiry share
  expiry      each upcoming expiry's share of SPY and QQQ gamma
  calendar    expiries and period ends, in sessions from tonight
  leveraged   leveraged ETF rebalance per 1% move, by index
  volctl      one row: vol-control exposure and how a move tomorrow would change it
  trend       one row per asset: trend score and the nearest flip level
  basket      one row per themed basket in journal/reference/baskets.json: today's snapshot against its
              hedge, and from daily bars, when every member has them, the basket-vs-hedge spread's 5- and
              20-day moves and z-score
  basket_member  one row per member: why it belongs, liquidity, today's move, 5- and 20-day moves
  earnings    one row per report in the next 10 sessions or the last 5: dates from Yahoo Finance, the price
              reaction from our daily bars
"""

import json
from datetime import date, datetime

import numpy as np
import pandas as pd

from . import earnings, journal, storage
from .analytics.compute import latest_snapshot
from .bars import bars_path, load
from .config import NY
from .universe import INDEXES, SECTORS, idea_universe

GAMMA_UNDERLYINGS = ["SPY", "QQQ"]
GAMMA_BAND = 0.06
REGIME_SESSIONS = 60


def _num(x, digits: int = 4):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, digits) if np.isfinite(v) else None


def _brief(as_of: date) -> dict:
    path = journal.JOURNAL / "briefs" / f"{as_of}.json"
    if not path.exists():
        raise FileNotFoundError(f"no brief for {as_of}; build it with fr brief --as-of {as_of}")
    return json.loads(path.read_text())


def _idea_legs(as_of: date) -> list[tuple[str, str]]:
    """(long, short) for each idea recorded on as_of, for the next session."""
    path = journal.JOURNAL / "ideas" / f"{journal.next_trading_day(as_of)}.json"
    if not path.exists():
        return []
    return [(i.get("long") or "", i.get("short") or "") for i in json.loads(path.read_text())["ideas"]]


def _factors(brief: dict, legs: list[tuple[str, str]]) -> list[dict]:
    rows = []
    for p in brief.get("factor_pairs", []):
        used = ""
        for long, short in legs:
            if (long, short) == (p["long"], p["short"]):
                used = "long"
            elif (long, short) == (p["short"], p["long"]):
                used = "short"
        rows.append({"section": "factor", "pair": p["pair"], "long": p["long"], "short": p["short"],
                     "ret_1d": _num(p["ret_1d"]), "ret_5d": _num(p["ret_5d"]), "ret_20d": _num(p["ret_20d"]),
                     "z_50d": _num(p["z_50d"], 2), "z_200d": _num(p["z_200d"], 2), "rsi14": _num(p["rsi14"], 1),
                     "in_idea": used})
    return rows


def _regime(as_of: date) -> list[dict]:
    bars = load(idea_universe())
    bars = bars[bars["date"].dt.date <= as_of]
    close = bars.pivot_table(index="date", columns="symbol", values="close").sort_index()
    above = (close > close.rolling(50).mean()).where(close.rolling(50).count() >= 50)
    breadth = above.mean(axis=1)
    sectors = close[[s for s in SECTORS if s in close]].pct_change()
    idx = close.index[-REGIME_SESSIONS:]
    corr = {}
    for d in idx:
        window = sectors.loc[:d].tail(20).corr().to_numpy()
        corr[d] = window[np.triu_indices_from(window, k=1)].mean()
    rv = np.log(close["SPY"]).diff().rolling(20).std() * np.sqrt(252)
    index_close = load(list(INDEXES))
    index_close = index_close.pivot_table(index="date", columns="symbol", values="close") if not index_close.empty \
        else pd.DataFrame()
    rows = []
    for d in idx:
        get = lambda s: index_close.at[d, s] if s in index_close and d in index_close.index else None  # noqa: E731
        tnx = get("TNX")
        rows.append({"section": "regime", "date": d.strftime("%Y-%m-%d"), "pct_above_50": _num(breadth.get(d)),
                     "sector_corr_20d": _num(corr[d]), "spy_rv20": _num(rv.get(d)), "vix": _num(get("VIX"), 2),
                     "vix3m": _num(get("VIX3M"), 2), "ten_year": _num(tnx / 10 if tnx is not None else None, 3)})
    return rows


def _gamma(as_of: date, brief: dict) -> list[dict]:
    snapshot = latest_snapshot(as_of.isoformat())
    if not snapshot:
        return []
    summary_path = f"exposures/date={as_of}/{snapshot}.parquet"
    summary = storage.read(summary_path) if storage.exists(summary_path) else pd.DataFrame()
    rows = []
    for u in GAMMA_UNDERLYINGS:
        path = f"exposures_by_strike/date={as_of}/underlying={u}/{snapshot}.parquet"
        s = summary[summary["underlying"] == u] if not summary.empty else summary
        if s.empty or not storage.exists(path):
            continue
        s = s.iloc[0]
        spot = float(s["spot"])
        by = storage.read(path)
        by = by[(by["strike"] / spot - 1).abs() <= GAMMA_BAND]
        for r in by.itertuples():
            rows.append({"section": "gamma", "underlying": u, "strike": _num(r.strike, 2),
                         "gex_net": _num(r.gex_net, 0), "gex_c": _num(getattr(r, "gex_c", None), 0),
                         "gex_p": _num(getattr(r, "gex_p", None), 0)})
        profile = json.loads(s["expiry_profile"]) if isinstance(s.get("expiry_profile"), str) else []
        rows.append({"section": "gamma_sum", "underlying": u, "spot": _num(spot, 2), "gex_total": _num(s["gex_total"], 0),
                     "gex_0dte": _num(s["gex_0dte"], 0), "gamma_flip": _num(s["gamma_flip"], 2),
                     "call_wall": _num(s["call_wall"], 2), "put_wall": _num(s["put_wall"], 2),
                     "atm_iv_30d": _num(s["atm_iv_30d"]), "skew_25d": _num(s["skew_25d"]),
                     "snapshot": snapshot})
        for e in profile:
            rows.append({"section": "expiry", "underlying": u, "expiry": e["expiry"], "dte": _num(e["dte"], 1),
                         "gross_gex_share": _num(e["gross_gex_share"]), "gex": _num(e["gex"], 0)})
    return rows


def _flows(brief: dict) -> list[dict]:
    ff = brief.get("forced_flows") or {}
    rows = []
    for key, e in (ff.get("calendar") or {}).items():
        if isinstance(e, dict):
            rows.append({"section": "calendar", "event": key, "date": e["date"], "sessions_away": e.get("sessions_away")})
    lev = ff.get("leveraged_etfs") or {}
    for g in lev.get("groups", []):
        rows.append({"section": "leveraged", "exposure": g["exposure"], "reference": g["reference"],
                     "rebalance_per_1pct": g["rebalance_per_1pct"], "rebalance_today": g["rebalance_today"],
                     "per_1pct_vs_reference_adv": g["per_1pct_vs_reference_adv"],
                     "net_assets_age_days": lev.get("net_assets_age_days")})
    vc = ff.get("vol_control") or {}
    if "exposure" in vc:
        s = vc.get("exposure_change_if_spy_moves", {})
        rows.append({"section": "volctl", "exposure": vc["exposure"], "exposure_change_1d": vc["exposure_change_1d"],
                     "exposure_change_5d": vc["exposure_change_5d"], "exposure_percentile_5y": vc["exposure_percentile_5y"],
                     "ewma_vol": vc["ewma_vol"], "if_down_3": s.get("-3%"), "if_down_2": s.get("-2%"),
                     "if_down_1": s.get("-1%"), "if_up_1": s.get("+1%"), "if_up_2": s.get("+2%")})
    for t in ff.get("trend", []):
        n = t["nearest_flip"]
        rows.append({"section": "trend", "symbol": t["symbol"], "close": t["close"], "score": t["score"],
                     "flip_days": n["days"], "flip_price": n["flip_price"], "flip_distance_atr": n["flip_distance_atr"]})
    return rows


BASKETS = journal.JOURNAL / "reference" / "baskets.json"


def basket_library() -> dict:
    return json.loads(BASKETS.read_text()) if BASKETS.exists() else {"baskets": []}


def _day_move(closes: pd.Series, stored: float | None) -> float | None:
    """The latest session's move in percent from daily bars, or the stored snapshot before bars exist."""
    return (closes.iloc[-1] / closes.iloc[-2] - 1) * 100 if len(closes) > 1 else stored


def _baskets() -> list[dict]:
    """The basket watchlist. Daily-bar stats need every member's bars; until the collector has pulled
    a name, its basket shows only the snapshot and says how many names are waiting."""
    lib = basket_library()
    rows = []
    for b in lib["baskets"]:
        symbols = [m["symbol"] for m in b["members"]]
        bars = load([*symbols, b["hedge"]])
        closes = (bars.pivot_table(index="date", columns="symbol", values="close").sort_index()
                  if not bars.empty else pd.DataFrame())
        have = [s for s in symbols if s in closes.columns]
        rets = closes.pct_change() if not closes.empty else closes
        member_rows = []
        for m in b["members"]:
            s = m["symbol"]
            c = closes[s].dropna() if s in closes.columns else pd.Series(dtype=float)
            member_rows.append({
                "section": "basket_member", "basket": b["id"], "symbol": s, "why": m.get("why", ""),
                "avg_usd_volume_90d": m.get("avg_usd_volume_90d"), "change_pct": _num(_day_move(c, m.get("change_pct")), 2),
                "ret_5d": _num(c.iloc[-1] / c.iloc[-6] - 1) if len(c) > 5 else None,
                "ret_20d": _num(c.iloc[-1] / c.iloc[-21] - 1) if len(c) > 20 else None,
                "bars_through": c.index[-1].date().isoformat() if len(c) else None,
            })
        moves = [r["change_pct"] for r in member_rows if r["change_pct"] is not None]
        basket_today = sum(moves) / len(moves) if len(moves) == len(symbols) else None
        hedge_close = closes[b["hedge"]].dropna() if b["hedge"] in closes.columns else pd.Series(dtype=float)
        hedge_today = _day_move(hedge_close, b.get("hedge_change_pct"))
        pulled = storage.modified(bars_path(b["hedge"]))
        row = {
            "section": "basket", "id": b["id"], "name": b["name"], "hedge": b["hedge"], "story": b.get("story", ""),
            "watch": b.get("watch", ""), "caution": b.get("caution", ""), "source": b.get("source", ""),
            "members": ", ".join(symbols), "n_members": len(symbols), "missing_bars": len(symbols) - len(have),
            "snapshot_at": pulled.astimezone(NY).isoformat() if pulled else lib.get("snapshot_at"),
            "today_pct": _num(basket_today, 2), "hedge_today_pct": _num(hedge_today, 2),
            "spread_today_pct": _num(basket_today - hedge_today, 2) if basket_today is not None and hedge_today is not None else None,
            "ret_5d": None, "ret_20d": None, "z_50d": None, "bars_through": None,
        }
        if not have or b["hedge"] not in closes.columns or len(have) < len(symbols):
            rows += [row, *member_rows]
            continue
        # Equal weight, rebalanced daily: the basket's daily return is its members' average.
        spread = (rets[symbols].mean(axis=1, skipna=False) - rets[b["hedge"]]).dropna()
        idx = (1 + spread).cumprod()
        if len(idx) > 50:
            w = idx.iloc[-50:]
            row.update(ret_5d=_num(idx.iloc[-1] / idx.iloc[-6] - 1), ret_20d=_num(idx.iloc[-1] / idx.iloc[-21] - 1),
                       z_50d=_num((idx.iloc[-1] - w.mean()) / w.std(), 2), bars_through=idx.index[-1].date().isoformat())
        rows += [row, *member_rows]
    return rows


def _reaction(symbol: str, hedge: str, report: date, session: str) -> dict:
    """The first session that traded on the report: its gap and move, alone and against the hedge."""
    bars = load([symbol, hedge])
    if bars.empty or symbol not in set(bars["symbol"]):
        return {}
    px = {s: g.assign(date=pd.to_datetime(g["date"])).set_index("date").sort_index() for s, g in bars.groupby("symbol")}
    d = px[symbol]
    after = d.index[d.index > pd.Timestamp(report)] if session == "after_close" else d.index[d.index >= pd.Timestamp(report)]
    if not len(after) or d.index.get_loc(after[0]) == 0:
        return {}
    t = after[0]
    prev = d["close"].iloc[d.index.get_loc(t) - 1]
    day = d.at[t, "close"] / prev - 1
    out = {"reaction_date": t.date().isoformat(), "gap_pct": _num((d.at[t, "open"] / prev - 1) * 100, 2), "day_pct": _num(day * 100, 2)}
    if hedge in px and t in px[hedge].index:
        c = px[hedge]["close"]
        out["vs_hedge_pct"] = _num((day - (c.at[t] / c.iloc[c.index.get_loc(t) - 1] - 1)) * 100, 2)
    return out


def _earnings(as_of: date) -> list[dict]:
    lib = earnings.load()
    member_of: dict[str, list[dict]] = {}
    for b in basket_library()["baskets"]:
        for m in b["members"]:
            member_of.setdefault(m["symbol"], []).append(b)
    sessions = [d.date() for d in journal._calendar().valid_days(as_of - pd.Timedelta(days=12), as_of + pd.Timedelta(days=25))]
    i = sessions.index(as_of)
    window_end, recent_start = sessions[min(i + 10, len(sessions) - 1)], sessions[max(i - 4, 0)]
    next_session = sessions[i + 1]
    path = journal.JOURNAL / "ideas" / f"{next_session}.json"
    legs = set()
    if path.exists():
        for idea in json.loads(path.read_text())["ideas"]:
            for side in ("long", "short"):
                v = idea.get(side) or []
                legs.update(v if isinstance(v, list) else [v])
    rows = []
    for symbol, reports in lib.get("names", {}).items():
        groups = member_of.get(symbol, [])
        hedge = groups[0]["hedge"] if groups else "SPY"
        for r in reports:
            d = date.fromisoformat(r["date"])
            # A report tonight hasn't traded yet, so it stays upcoming until a session has reacted to it.
            done = d < as_of or (d == as_of and r["session"] != "after_close")
            if not recent_start <= d <= window_end:
                continue
            row = {"section": "earnings", "kind": "recent" if done else "upcoming", "symbol": symbol, "date": r["date"],
                   "when": r["session"], "baskets": ", ".join(b["name"] for b in groups), "hedge": hedge,
                   "eps_estimate": r.get("eps_estimate"), "eps_reported": r.get("eps_reported"),
                   "surprise_pct": r.get("surprise_pct"), "idea_leg": symbol in legs,
                   "sessions_away": sum(1 for s in sessions if as_of < s <= d), "tonight": d == as_of,
                   "pulled_at": lib.get("pulled_at")}
            rows.append(row | _reaction(symbol, hedge, d, r["session"]) if done else row)
    return sorted(rows, key=lambda r: (r["date"], r["symbol"]))


def _live_pairs(brief: dict, as_of: date) -> dict:
    """The factor board from the latest bars, when a refresh has brought bars newer than the brief."""
    bars = load(idea_universe())
    if bars.empty or bars["date"].max().date() <= as_of:
        return brief
    return {"factor_pairs": journal._records(journal.factor_pair_technicals(bars))}


def _latest_session() -> date:
    today = datetime.now(NY).date()
    return journal._calendar().valid_days(today - pd.Timedelta(days=10), today)[-1].date()


def build(as_of: date) -> list[dict]:
    brief = _brief(as_of)
    snapshot = latest_snapshot(as_of.isoformat())
    meta = {"section": "meta", "as_of": brief["as_of"], "built_at": brief["built_at"],
            "bars_through": brief.get("bars_through"), "options_snapshot": snapshot,
            "next_session": journal.next_trading_day(as_of).isoformat()}
    pairs = _live_pairs(brief, as_of)
    meta["factor_live"] = pairs is not brief
    return [meta, *_factors(pairs, _idea_legs(as_of)), *_regime(as_of), *_gamma(as_of, brief),
            *_flows(brief), *_baskets(), *_earnings(_latest_session())]
