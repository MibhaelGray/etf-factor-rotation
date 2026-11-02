"""Dealer positioning and vol surface metrics per underlying snapshot.

Dealer sign convention (an assumption, not an observation): dealers are long
calls and short puts, so call gamma counts positive and put gamma negative.
Open interest is published once a day by OCC, so intraday exposures use the
prior day's OI with intraday greeks.

Units:
  gex          $ change in dealer delta for a 1% move in the underlying
  vanna_exp    $ change in dealer delta for a 1 vol point rise in IV
  charm_exp    $ change in dealer delta per calendar day of decay
"""

import json

import numpy as np
import pandas as pd

from ..config import settings
from .greeks import bsm_greeks, enrich

DEALER_SIGN = {"C": 1.0, "P": -1.0}
UNUSUAL_MIN_VOLUME = 500


def _signed(df: pd.DataFrame) -> pd.Series:
    return df["right"].map(DEALER_SIGN) * df["open_interest"].fillna(0) * df["multiplier"]


def gamma_flip(df: pd.DataFrame, spot: float, r: float) -> float | None:
    usable = df[np.isfinite(df["iv"]) & (df["open_interest"] > 0)]
    if usable.empty:
        return None
    grid = spot * np.linspace(0.85, 1.15, 241)
    S = grid[None, :]
    K = usable["strike"].to_numpy(float)[:, None]
    T = usable["T"].to_numpy(float)[:, None]
    b = usable["b"].to_numpy(float)[:, None]
    sigma = usable["iv"].to_numpy(float)[:, None]
    is_call = (usable["right"] == "C").to_numpy()[:, None]
    gamma = bsm_greeks(S, K, T, r, b, sigma, is_call)["gamma"]
    weights = _signed(usable).to_numpy(float)[:, None]
    profile = np.nansum(gamma * weights, axis=0) * grid**2 * 0.01
    crossings = np.where(np.diff(np.sign(profile)) != 0)[0]
    if crossings.size == 0:
        return None
    i = crossings[np.argmin(np.abs(grid[crossings] - spot))]
    x0, x1, y0, y1 = grid[i], grid[i + 1], profile[i], profile[i + 1]
    return float(x0 - y0 * (x1 - x0) / (y1 - y0))


def _atm_iv(expiry_df: pd.DataFrame) -> float | None:
    F = expiry_df["F"].iloc[0]
    curve = expiry_df.dropna(subset=["iv"]).groupby("strike")["iv"].mean().sort_index()
    if len(curve) < 2:
        return None
    return float(np.interp(np.log(F), np.log(curve.index.to_numpy(float)), curve.to_numpy()))


def _delta_iv(expiry_df: pd.DataFrame, right: str, target: float) -> float | None:
    side = expiry_df[(expiry_df["right"] == right)].dropna(subset=["iv", "delta"]).sort_values("delta")
    if len(side) < 2 or not (side["delta"].min() <= target <= side["delta"].max()):
        return None
    return float(np.interp(target, side["delta"].to_numpy(), side["iv"].to_numpy()))


def _term_iv(atm: pd.Series, days: float) -> float | None:
    atm = atm.dropna()
    if atm.empty:
        return None
    t = atm.index.to_numpy(float)
    total_var = atm.to_numpy() ** 2 * t
    target = days / 365.25
    if target <= t.min():
        return float(atm.iloc[0])
    if target >= t.max():
        return float(atm.iloc[-1])
    return float(np.sqrt(np.interp(target, t, total_var) / target))


def summarize(raw: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    r = settings().risk_free_rate
    df = enrich(raw)
    spot = float(df["S"].median())
    w = _signed(df)
    df["gex"] = w * df["gamma"] * spot**2 * 0.01
    df["vanna_exp"] = w * df["vanna"] * spot
    df["charm_exp"] = w * df["charm"] * spot
    df["premium"] = df["volume"].fillna(0) * df["mid"].fillna(df["last"]).fillna(0) * df["multiplier"]

    by_strike = (
        df.pivot_table(index="strike", columns="right", values=["gex", "open_interest", "volume"], aggfunc="sum", fill_value=0)
        .pipe(lambda p: p.set_axis([f"{a}_{b.lower()}" for a, b in p.columns], axis=1))
        .reset_index()
    )
    by_strike["gex_net"] = by_strike.get("gex_c", 0) + by_strike.get("gex_p", 0)
    by_strike.insert(0, "underlying", df["underlying"].iloc[0])

    expiry_t = df.groupby("expiry")["T"].first()
    atm = pd.Series({expiry_t[e]: _atm_iv(g) for e, g in df.groupby("expiry")}).sort_index()
    dte = expiry_t * 365.25
    skew_expiry = (dte[dte >= 7] - 30).abs().idxmin() if (dte >= 7).any() else None
    skew_df = df[df["expiry"] == skew_expiry] if skew_expiry else df.iloc[0:0]
    put25 = _delta_iv(skew_df, "P", -0.25)
    call25 = _delta_iv(skew_df, "C", 0.25)

    zero_dte = df["T"] * 365.25 < 1
    calls, puts = df[df["right"] == "C"], df[df["right"] == "P"]
    unusual = df[(df["volume"] >= UNUSUAL_MIN_VOLUME) & (df["volume"] > df["open_interest"].fillna(0))]
    top_unusual = unusual.nlargest(5, "premium")[["local_symbol", "volume", "open_interest", "premium", "iv", "delta"]]

    calls_gex = calls.groupby("strike")["gex"].sum()
    puts_gex = puts.groupby("strike")["gex"].sum()
    call_volume, put_volume = calls["volume"].sum(), puts["volume"].sum()
    call_oi, put_oi = calls["open_interest"].sum(), puts["open_interest"].sum()
    summary = {
        "underlying": df["underlying"].iloc[0],
        "snapshot_id": df["snapshot_id"].iloc[0],
        "ts": df["ts"].max(),
        "spot": spot,
        "contracts": len(df),
        "contracts_complete": int(df["complete"].sum()),
        "iv_coverage": float(df["own_iv"].notna().mean()),
        "gex_total": float(df["gex"].sum()),
        "gex_calls": float(calls["gex"].sum()),
        "gex_puts": float(puts["gex"].sum()),
        "gex_0dte": float(df.loc[zero_dte, "gex"].sum()),
        "vanna_exp_total": float(df["vanna_exp"].sum()),
        "charm_exp_total": float(df["charm_exp"].sum()),
        "gamma_flip": gamma_flip(df, spot, r),
        "call_wall": float(calls_gex.idxmax()) if not calls_gex.empty else None,
        "put_wall": float(puts_gex.idxmin()) if not puts_gex.empty else None,
        "atm_iv_front": float(atm.iloc[0]) if atm.notna().any() else None,
        "atm_iv_30d": _term_iv(atm, 30),
        "atm_iv_90d": _term_iv(atm, 90),
        "put_25d_iv": put25,
        "call_25d_iv": call25,
        "skew_25d": put25 - call25 if put25 is not None and call25 is not None else None,
        "call_volume": float(call_volume),
        "put_volume": float(put_volume),
        "pc_volume_ratio": float(put_volume / call_volume) if call_volume else None,
        "pc_oi_ratio": float(put_oi / call_oi) if call_oi else None,
        "call_premium": float(calls["premium"].sum()),
        "put_premium": float(puts["premium"].sum()),
        "zero_dte_volume_share": float(df.loc[zero_dte, "volume"].sum() / df["volume"].sum()) if df["volume"].sum() else None,
        "unusual_count": len(unusual),
        "unusual_premium": float(unusual["premium"].sum()),
        "top_unusual": json.dumps(top_unusual.to_dict("records"), default=float),
    }
    summary["gamma_flip_distance"] = summary["gamma_flip"] / spot - 1 if summary["gamma_flip"] else None
    summary["term_slope"] = (
        summary["atm_iv_90d"] - summary["atm_iv_30d"]
        if summary["atm_iv_90d"] is not None and summary["atm_iv_30d"] is not None else None
    )
    return summary, by_strike
