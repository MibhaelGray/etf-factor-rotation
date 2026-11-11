"""Forced flows: trading that funds and dealers have to do whatever they think of the market.

Everything here comes from IBKR data we already store (daily bars, option snapshots, the underlying
snapshot's borrow ticks) plus the leveraged funds' net assets, pulled from IBKR into
journal/reference/leveraged_assets.json. Each number is a model estimate of a mechanical flow, not an
observation of anyone's positions; the brief labels them that way.
"""

import json
from datetime import date, timedelta

import numpy as np
import pandas as pd
from ..calendars import NYSE

from .. import storage
from ..universe import LEVERAGED

TREND_ASSETS = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD", "USO", "UUP"]
TREND_LOOKBACKS = [20, 60, 120, 250]
EXPIRY_UNDERLYINGS = ["SPX", "SPY", "QQQ", "IWM", "DIA", "SMH", "XLK", "NVDA", "TSLA", "AAPL", "MSFT", "AMZN", "META",
                      "GOOGL", "AVGO"]
VOL_TARGET = 0.10
VOL_CAP = 2.0
EWMA_LAMBDA = 0.94


def _wide(bars: pd.DataFrame, field: str = "close") -> pd.DataFrame:
    return bars.pivot_table(index="date", columns="symbol", values=field).sort_index()


def _num(x, digits: int = 5):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return None
    return round(float(x), digits)


# ---- calendar ---------------------------------------------------------------------------------------------

def _sessions(start: date, end: date) -> list[date]:
    return [d.date() for d in NYSE.valid_days(start, end)]


def _third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def _on_or_before(d: date, sessions: set[date]) -> date:
    while d not in sessions:
        d -= timedelta(days=1)
    return d


def calendar(as_of: date) -> dict:
    """Option expiries, VIX expiry and month/quarter ends after as_of, counted in sessions (1 = the next session)."""
    sessions = _sessions(as_of + timedelta(days=1), as_of + timedelta(days=130))
    held = set(_sessions(as_of - timedelta(days=10), as_of + timedelta(days=130)))
    index = {d: i + 1 for i, d in enumerate(sessions)}

    def entry(d: date, **extra) -> dict:
        return {"date": d.isoformat(), "sessions_away": index.get(d), **extra}

    months = [(as_of.year + (as_of.month - 1 + k) // 12, (as_of.month - 1 + k) % 12 + 1) for k in range(5)]
    opex = [_on_or_before(_third_friday(y, m), held) for y, m in months]
    opex = [d for d in opex if d > as_of]
    quarterly = next(d for d in opex if d.month in (3, 6, 9, 12))
    vix = []
    for y, m in months:
        ny, nm = (y + m // 12, m % 12 + 1)
        vix.append(_on_or_before(_third_friday(ny, nm) - timedelta(days=30), held))
    vix_next = next(d for d in vix if d > as_of)
    month_ends = [max(d for d in sessions if (d.year, d.month) == ym) for ym in months[:4]
                  if any((d.year, d.month) == ym for d in sessions)]
    month_end = month_ends[0]
    quarter_end = next(d for d in month_ends if d.month in (3, 6, 9, 12))
    return {
        "next_session": sessions[0].isoformat(),
        "monthly_opex": entry(opex[0]),
        "quarterly_opex": entry(quarterly, note="quad witching; S&P 500 and Nasdaq-100 quarterly rebalances trade "
                                                "at this close"),
        "vix_expiry": entry(vix_next, note="VIX options and futures settle on this morning's SPX opening quotes"),
        "month_end": entry(month_end),
        "quarter_end": entry(quarter_end),
    }


# ---- dealer gamma by expiry ---------------------------------------------------------------------------------

def options_expiry(as_of: date, snapshot: str | None) -> list[dict]:
    """For the index ETFs and the largest single names, how much gamma rolls off in each of the next expiries."""
    if not snapshot:
        return []
    path = f"exposures/date={as_of}/{snapshot}.parquet"
    if not storage.exists(path):
        return []
    df = storage.read(path)
    if "expiry_profile" not in df:
        return []
    out = []
    for row in df[df["underlying"].isin(EXPIRY_UNDERLYINGS)].itertuples():
        profile = json.loads(row.expiry_profile) if isinstance(row.expiry_profile, str) else []
        out.append({"underlying": row.underlying, "spot": _num(row.spot, 2), "gex_total": _num(row.gex_total, 0),
                    "expiries": [{k: _num(v, 4) if isinstance(v, float) else v for k, v in e.items()}
                                 for e in profile]})
    order = {s: i for i, s in enumerate(EXPIRY_UNDERLYINGS)}
    return sorted(out, key=lambda r: order[r["underlying"]])


# ---- leveraged ETF rebalancing ------------------------------------------------------------------------------

def leveraged(bars: pd.DataFrame, as_of: date, reference: dict | None) -> dict:
    """End-of-day hedge the leveraged and inverse funds must trade, from IBKR's net assets.

    A fund with leverage L and net assets A must add A * L * (L - 1) * r of index exposure at the close
    after the index returns r. L * (L - 1) is positive for inverse funds too, so every fund trades in the
    direction of the day's move. Net assets are scaled from the reference pull by the fund's own price."""
    if not reference or not reference.get("funds"):
        return {"note": "no journal/reference/leveraged_assets.json; pull net assets from IBKR first", "groups": []}
    close, volume = _wide(bars), _wide(bars, "volume")
    pulled = date.fromisoformat(reference["as_of"])
    groups: dict[str, dict] = {}
    for f in LEVERAGED:
        ref = reference["funds"].get(f.symbol)
        if not ref or f.symbol not in close or f.reference not in close:
            continue
        fund, index = close[f.symbol].dropna(), close[f.reference].dropna()
        if len(fund) < 2 or len(index) < 2:
            continue
        scale = ref["total_net_assets"] / ref["price"]
        assets_now, assets_prev = fund.iloc[-1] * scale, fund.iloc[-2] * scale
        r = index.iloc[-1] / index.iloc[-2] - 1
        k = f.leverage * (f.leverage - 1)
        g = groups.setdefault(f.exposure, {
            "exposure": f.exposure, "reference": f.reference, "funds": [], "net_assets": 0.0,
            "reference_return": float(r), "rebalance_today": 0.0, "rebalance_per_1pct": 0.0,
            "reference_adv20": float((close[f.reference] * volume[f.reference]).tail(20).mean()),
        })
        g["funds"].append(f"{f.symbol} {f.leverage:+g}x")
        g["net_assets"] += assets_now
        g["rebalance_today"] += assets_prev * k * r
        g["rebalance_per_1pct"] += assets_now * k * 0.01
    rows = []
    for g in sorted(groups.values(), key=lambda g: -g["rebalance_per_1pct"]):
        adv = g["reference_adv20"]
        rows.append({**g,
                     "net_assets": _num(g["net_assets"], 0),
                     "reference_return": _num(g["reference_return"]),
                     "rebalance_today": _num(g["rebalance_today"], 0),
                     "rebalance_per_1pct": _num(g["rebalance_per_1pct"], 0),
                     "reference_adv20": _num(adv, 0),
                     "per_1pct_vs_reference_adv": _num(g["rebalance_per_1pct"] / adv) if adv else None})
    return {"net_assets_as_of": pulled.isoformat(), "net_assets_age_days": (as_of - pulled).days,
            "units": "USD of index exposure traded at the close; positive = buy", "groups": rows}


# ---- month and quarter end rebalancing -----------------------------------------------------------------------

def _period_drift(close: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    eq = close.loc[end, "SPY"] / close.loc[start, "SPY"] - 1
    bd = close.loc[end, "AGG"] / close.loc[start, "AGG"] - 1
    weight = 0.6 * (1 + eq) / (0.6 * (1 + eq) + 0.4 * (1 + bd))
    return {"spy_return": float(eq), "agg_return": float(bd), "spread": float(eq - bd),
            "drift_60_40_pp": float((weight - 0.6) * 100)}


def pension(bars: pd.DataFrame, as_of: date) -> dict:
    """Month- and quarter-to-date drift of a 60/40 SPY/AGG portfolio, ranked against past periods.

    Positive drift means stocks are overweight, so balanced funds sell stocks and buy bonds into the
    period end."""
    close = _wide(bars[bars["symbol"].isin(["SPY", "AGG"])]).dropna()
    if close.empty or "AGG" not in close:
        return {"note": "no AGG bars yet"}
    dates = close.index
    last = dates[-1]
    out = {}
    for label, key in (("month", lambda d: (d.year, d.month)), ("quarter", lambda d: (d.year, (d.month - 1) // 3))):
        keys = pd.Series([key(d) for d in dates], index=dates)
        ends = keys.index[keys.ne(keys.shift(-1))]  # last session of each period
        prior_end = ends[ends < last]
        if prior_end.empty:
            continue
        current = _period_drift(close, prior_end[-1], last)
        history = [abs(_period_drift(close, a, b)["spread"]) for a, b in zip(prior_end[:-1], prior_end[1:])]
        history = history[-60:] if label == "month" else history[-20:]
        current["abs_spread_percentile"] = float(np.mean([h <= abs(current["spread"]) for h in history])) \
            if history else None
        current["history_periods"] = len(history)
        out[label] = {k: _num(v) if isinstance(v, float) else v for k, v in current.items()}
    out["note"] = ("positive drift = stocks overweight vs 60/40, so rebalancing sells stocks into the period end; "
                   "the percentile ranks the period so far against completed periods, so it reads low early on")
    return out


# ---- volatility-targeting funds -------------------------------------------------------------------------------

def vol_control(bars: pd.DataFrame) -> dict:
    """Equity exposure of a 10% vol-target fund on SPY, using RiskMetrics EWMA vol, and how a move in the
    next session would change it."""
    close = _wide(bars[bars["symbol"] == "SPY"])["SPY"].dropna()
    r = np.log(close).diff().dropna()
    var = r.pow(2).ewm(alpha=1 - EWMA_LAMBDA, adjust=False).mean() * 252
    vol = np.sqrt(var)
    exposure = (VOL_TARGET / vol).clip(upper=VOL_CAP)
    now = exposure.iloc[-1]
    scenarios = {}
    for move in (-0.03, -0.02, -0.01, 0.01, 0.02):
        v = np.sqrt(EWMA_LAMBDA * var.iloc[-1] + (1 - EWMA_LAMBDA) * np.log(1 + move) ** 2 * 252)
        scenarios[f"{move:+.0%}"] = _num(min(VOL_TARGET / v, VOL_CAP) / now - 1)
    return {
        "ewma_vol": _num(vol.iloc[-1]),
        "rv20": _num(r.tail(20).std() * np.sqrt(252)),
        "rv60": _num(r.tail(60).std() * np.sqrt(252)),
        "exposure": _num(now),
        "exposure_change_1d": _num(now / exposure.iloc[-2] - 1),
        "exposure_change_5d": _num(now / exposure.iloc[-6] - 1),
        "exposure_percentile_5y": _num((exposure.tail(1260) <= now).mean()),
        "exposure_change_if_spy_moves": scenarios,
        "note": f"model: {VOL_TARGET:.0%} vol target, EWMA lambda {EWMA_LAMBDA}, leverage cap {VOL_CAP}x; "
                "a negative change means these funds sell at the close",
    }


# ---- trend followers ------------------------------------------------------------------------------------------

def trend(bars: pd.DataFrame) -> list[dict]:
    """A plain trend-following model: the sign of the 20/60/120/250-day return, averaged, and the price at which
    each lookback flips in the next session (the close that drops out of its window)."""
    rows = []
    for symbol in TREND_ASSETS:
        df = bars[bars["symbol"] == symbol].sort_values("date")
        c = df["close"].reset_index(drop=True)
        if len(c) < max(TREND_LOOKBACKS) + 1:
            continue
        prev = c.shift()
        tr = pd.concat([df["high"].reset_index(drop=True) - df["low"].reset_index(drop=True),
                        (df["high"].reset_index(drop=True) - prev).abs(),
                        (df["low"].reset_index(drop=True) - prev).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
        last = c.iloc[-1]
        lookbacks = []
        for n in TREND_LOOKBACKS:
            flip = c.iloc[-n]  # tomorrow's n-day return is measured from this close
            lookbacks.append({"days": n, "signal": int(np.sign(last / c.iloc[-1 - n] - 1)),
                              "flip_price": _num(flip, 2), "flip_distance_atr": _num((flip - last) / atr, 2)})
        nearest = min(lookbacks, key=lambda x: abs(x["flip_distance_atr"]))
        rows.append({"symbol": symbol, "close": _num(last, 2), "score": _num(np.mean([x["signal"] for x in lookbacks]), 2),
                     "nearest_flip": nearest, "lookbacks": lookbacks})
    return rows


# ---- borrow -----------------------------------------------------------------------------------------------

def _underlying_snapshot(day: date) -> pd.DataFrame:
    files = storage.list_files(f"options_underlying/date={day}")
    return storage.read(files[-1]) if files else pd.DataFrame()


def borrow(bars: pd.DataFrame, as_of: date, lookback_sessions: int = 5, top: int = 15) -> dict:
    """Shortable shares from the underlying snapshot (IBKR generic tick 236), against five sessions earlier and
    against average daily volume. Names where borrow is tightening fastest come first."""
    now = _underlying_snapshot(as_of)
    if now.empty or "shortable_shares" not in now or now["shortable_shares"].notna().sum() == 0:
        return {"note": "no borrow data in today's underlying snapshot", "names": []}
    sessions = _sessions(as_of - timedelta(days=lookback_sessions * 2 + 10), as_of)
    then = pd.DataFrame()
    if len(sessions) > lookback_sessions:
        then = _underlying_snapshot(sessions[-1 - lookback_sessions])
    adv = _wide(bars, "volume").tail(20).mean()
    df = now[["underlying", "shortable", "shortable_shares"]].dropna(subset=["shortable_shares"]).copy()
    if not then.empty and "shortable_shares" in then:
        df = df.merge(then[["underlying", "shortable_shares"]].rename(columns={"shortable_shares": "before"}),
                      on="underlying", how="left")
        df["change"] = df["shortable_shares"] / df["before"] - 1
    else:
        df["change"] = np.nan
    df["shortable_vs_adv"] = df["shortable_shares"] / df["underlying"].map(adv)
    df["hard_to_borrow"] = df["shortable"] <= 2.5
    flagged = df[df["hard_to_borrow"] | (df["change"] <= -0.3) | (df["shortable_vs_adv"] < 0.5)]
    flagged = flagged.sort_values(["change", "shortable_vs_adv"], na_position="last").head(top)
    names = [{"symbol": r.underlying, "shortable_shares": _num(r.shortable_shares, 0),
              "change": _num(r.change, 3), "shortable_vs_adv": _num(r.shortable_vs_adv, 3),
              "hard_to_borrow": bool(r.hard_to_borrow)} for r in flagged.itertuples()]
    return {"compared_with": sessions[-1 - lookback_sessions].isoformat() if not then.empty else None,
            "covered": int(len(df)), "names": names,
            "note": "change = shortable shares vs five sessions earlier; shortable_vs_adv = shortable shares over "
                    "20-day average volume; hard_to_borrow = IBKR's shortable indicator at or below 2.5"}


def build(bars: pd.DataFrame, as_of: date, snapshot: str | None, reference: dict | None) -> dict:
    return {
        "calendar": calendar(as_of),
        "options_expiry": options_expiry(as_of, snapshot),
        "leveraged_etfs": leveraged(bars, as_of, reference),
        "pension_rebalance": pension(bars, as_of),
        "vol_control": vol_control(bars),
        "trend": trend(bars),
        "borrow": borrow(bars, as_of),
    }
