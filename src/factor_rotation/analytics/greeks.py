"""Our own greeks, computed consistently across every chain.

Model: generalized Black-Scholes with cost of carry b, where the forward per
expiry is implied from put-call parity near the money. That absorbs dividends
and borrow without needing a dividend feed. Implied vol is solved from the
quote mid on the out-of-the-money side of each strike (where early exercise
premium is negligible) and applied to both the call and the put.
"""

import numpy as np
import pandas as pd
from scipy.special import ndtr

from ..config import NY, settings

MIN_T = 15 / (365.25 * 24 * 60)


def _pdf(x):
    return np.exp(-0.5 * x * x) / np.sqrt(2 * np.pi)


def bsm_price(S, K, T, r, b, sigma, is_call):
    sqrt_t = np.sqrt(T)
    d1 = (np.log(S / K) + (b + 0.5 * sigma**2) * T) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    carry = np.exp((b - r) * T)
    disc = np.exp(-r * T)
    call = S * carry * ndtr(d1) - K * disc * ndtr(d2)
    put = K * disc * ndtr(-d2) - S * carry * ndtr(-d1)
    return np.where(is_call, call, put)


def implied_vol(price, S, K, T, r, b, is_call, iterations=64):
    lo = np.full_like(price, 1e-4, dtype=float)
    hi = np.full_like(price, 5.0, dtype=float)
    for _ in range(iterations):
        mid = 0.5 * (lo + hi)
        too_high = bsm_price(S, K, T, r, b, mid, is_call) > price
        hi = np.where(too_high, mid, hi)
        lo = np.where(too_high, lo, mid)
    iv = 0.5 * (lo + hi)
    intrinsic = bsm_price(S, K, T, r, b, np.full_like(price, 1e-6), is_call)
    valid = np.isfinite(price) & (price > intrinsic) & (iv < 4.99)
    return np.where(valid, iv, np.nan)


def bsm_greeks(S, K, T, r, b, sigma, is_call) -> dict[str, np.ndarray]:
    sqrt_t = np.sqrt(T)
    d1 = (np.log(S / K) + (b + 0.5 * sigma**2) * T) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    carry = np.exp((b - r) * T)
    disc = np.exp(-r * T)
    n1 = _pdf(d1)
    delta = np.where(is_call, carry * ndtr(d1), carry * (ndtr(d1) - 1))
    gamma = carry * n1 / (S * sigma * sqrt_t)
    vega = S * carry * n1 * sqrt_t / 100
    vanna = -carry * n1 * d2 / sigma / 100
    charm_core = n1 * (b / (sigma * sqrt_t) - d2 / (2 * T))
    charm = np.where(
        is_call,
        -carry * (charm_core + (b - r) * ndtr(d1)),
        -carry * (charm_core - (b - r) * ndtr(-d1)),
    ) / 365.25
    theta_core = -S * carry * n1 * sigma / (2 * sqrt_t)
    theta = np.where(
        is_call,
        theta_core - (b - r) * S * carry * ndtr(d1) - r * K * disc * ndtr(d2),
        theta_core + (b - r) * S * carry * ndtr(-d1) + r * K * disc * ndtr(-d2),
    ) / 365.25
    return {"delta": delta, "gamma": gamma, "vega": vega, "theta": theta, "vanna": vanna, "charm": charm}


def _mid(df: pd.DataFrame) -> pd.Series:
    ok = (df["bid"] > 0) & (df["ask"] >= df["bid"])
    return ((df["bid"] + df["ask"]) / 2).where(ok)


def _years_to_expiry(df: pd.DataFrame) -> pd.Series:
    expiry_close = pd.to_datetime(df["expiry"], format="%Y%m%d").dt.tz_localize(NY) + pd.Timedelta(hours=16)
    ts = pd.to_datetime(df["ts"])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(NY)
    years = (expiry_close - ts.dt.tz_convert(NY)).dt.total_seconds() / (365.25 * 86400)
    return years.clip(lower=MIN_T)


def _implied_forward(group: pd.DataFrame, r: float) -> float:
    S = group["S"].median()
    T = group["T"].iloc[0]
    calls = group[group["right"] == "C"].set_index("strike")["mid"]
    puts = group[group["right"] == "P"].set_index("strike")["mid"]
    both = pd.concat([calls.rename("c"), puts.rename("p")], axis=1).dropna()
    if both.empty:
        return S * np.exp(r * T)
    nearest = both.iloc[(both.index.to_series() - S).abs().argsort()[:5]]
    forwards = nearest.index.to_numpy() + (nearest["c"] - nearest["p"]).to_numpy() * np.exp(r * T)
    return float(np.median(forwards))


def enrich(raw: pd.DataFrame) -> pd.DataFrame:
    r = settings().risk_free_rate
    df = raw.copy()
    df["S"] = df["ib_und_price"].where(df["ib_und_price"] > 0, df["spot_at_start"])
    df["T"] = _years_to_expiry(df)
    df["mid"] = _mid(df)
    forwards = df.groupby("expiry").apply(lambda g: _implied_forward(g, r), include_groups=False)
    df["F"] = df["expiry"].map(forwards)
    df["b"] = np.log(df["F"] / df["S"]) / df["T"]
    is_call = (df["right"] == "C").to_numpy()
    own_iv = implied_vol(
        df["mid"].to_numpy(float), df["S"].to_numpy(float), df["strike"].to_numpy(float),
        df["T"].to_numpy(float), r, df["b"].to_numpy(float), is_call,
    )
    df["own_iv"] = own_iv
    otm = np.where(df["strike"] >= df["F"], df["right"] == "C", df["right"] == "P")
    surface = df[otm & df["own_iv"].notna()].groupby(["expiry", "strike"])["own_iv"].first()
    keys = pd.MultiIndex.from_frame(df[["expiry", "strike"]])
    df["iv"] = surface.reindex(keys).to_numpy()
    df["iv"] = df["iv"].fillna(df["own_iv"]).fillna(df["ib_iv"])
    sigma = df["iv"].to_numpy(float)
    greeks = bsm_greeks(
        df["S"].to_numpy(float), df["strike"].to_numpy(float), df["T"].to_numpy(float),
        r, df["b"].to_numpy(float), sigma, is_call,
    )
    for name, values in greeks.items():
        df[name] = np.where(np.isfinite(sigma), values, np.nan)
    return df
