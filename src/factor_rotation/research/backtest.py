"""Vectorized spread-book backtester.

Pipeline for one signal at one horizon:
  raw score -> divide by its expanding standard deviation, clip to +/-3, scale to [-1, 1]
  -> smooth with an EWM of span = horizon (longer horizon, slower book)
  -> size each spread inversely to its volatility (EWM, 63-day half-life): equal risk per spread
  -> scale the book to the target volatility using its trailing realized vol (EWM, 126-day half-life)
  -> trade a spread only when its target moves more than `band` times the signal's typical size from what is held
  -> decide at the close of day t, trade at the open of t+1, earn open t+1 to open t+2
  -> charge each leg its cost (half spread plus impact) on every unit of position change.

All rolling estimates use data through the decision close only.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import data
from .signals import SIGNALS, Context
from .spreads import Spread, all_spreads, log_ratio, returns

TARGET_VOL = 0.10
DEFAULT_BAND = 0.25
TIGHT = {"SPY", "QQQ", "IWM", "DIA", "TLT", "IEF", "HYG", "LQD", "GLD", "EFA", "EEM", "XLK", "XLF", "XLE", "XLV",
         "XLI", "XLY", "XLP", "XLU", "XLB", "SMH", "KRE", "USO", "SLV", "FXI"}


def leg_cost_bps(symbol: str) -> float:
    return 1.0 if symbol in TIGHT else 2.5


@dataclass
class Research:
    spreads: list[Spread]
    ctx: Context
    oo: pd.DataFrame
    vol: pd.DataFrame
    cost: pd.Series
    raw: dict[str, pd.DataFrame] = field(default_factory=dict)


@dataclass
class Result:
    name: str
    horizon: int
    daily: pd.Series
    gross: pd.Series
    turnover: pd.Series
    positions: pd.DataFrame


def prepare() -> Research:
    panel = data.load()
    close, open_ = data.wide(panel, "close"), data.wide(panel, "open")
    sessions = close.index[close["SPY"].notna()]
    close, open_ = close.loc[sessions], open_.loc[sessions]
    spreads = [s for s in all_spreads() if s.long in close and s.short in close]
    rets = returns(close, open_, spreads)
    ctx = Context(close=close, ratio=log_ratio(close, spreads), ret=rets["close_to_close"], spreads=spreads)
    vol = np.sqrt((ctx.ret**2).ewm(halflife=63, min_periods=40).mean() * 252)
    cost = pd.Series({s.name: leg_cost_bps(s.long) + leg_cost_bps(s.short) for s in spreads})
    research = Research(spreads=spreads, ctx=ctx, oo=rets["open_to_open"], vol=vol, cost=cost)
    for name, fn in SIGNALS.items():
        research.raw[name] = fn(ctx).reindex(columns=ctx.ratio.columns)
    return research


def normalize(raw: pd.DataFrame) -> pd.DataFrame:
    scale = raw.expanding(min_periods=252).std()
    return (raw / scale).clip(-3, 3) / 3


def run(research: Research, name: str, score: pd.DataFrame, horizon: int,
        band: float = DEFAULT_BAND, target_vol: float = TARGET_VOL, cost_scale: float = 1.0) -> Result:
    smoothed = score.ewm(span=horizon, min_periods=1).mean() if horizon > 1 else score
    live = smoothed.notna() & research.vol.notna() & research.ctx.ret.notna()
    n_live = live.sum(axis=1).replace(0, np.nan)
    per_score = (0.10 / research.vol).where(live).div(n_live, axis=0)
    unit = (smoothed * per_score).fillna(0)
    unit_pnl = (unit.shift(1) * research.oo).sum(axis=1)
    realized = np.sqrt((unit_pnl**2).ewm(halflife=126, min_periods=60).mean() * 252).shift(1)
    leverage = (target_vol / realized).clip(upper=20).fillna(0)
    target = unit.mul(leverage, axis=0).to_numpy()
    typical = smoothed.abs().stack().expanding(min_periods=252).mean().groupby(level=0).last().reindex(score.index).ffill()
    step = per_score.mul(leverage * typical, axis=0).fillna(0).to_numpy() * band
    held = np.zeros_like(target)
    current = np.zeros(target.shape[1])
    for t in range(target.shape[0]):
        move = np.abs(target[t] - current) > step[t]
        current = np.where(move, target[t], current)
        current = np.where(live.iloc[t].to_numpy(), current, 0.0)
        held[t] = current
    positions = pd.DataFrame(held, index=score.index, columns=score.columns)
    gross = (positions.shift(1) * research.oo).sum(axis=1)
    trades = positions.diff().abs()
    turnover = trades.sum(axis=1)
    cost = (trades * research.cost.reindex(score.columns) * cost_scale / 1e4).sum(axis=1).shift(1).fillna(0)
    return Result(name=name, horizon=horizon, daily=gross - cost, gross=gross, turnover=turnover, positions=positions)


def combo_score(research: Research, names: list[str]) -> pd.DataFrame:
    parts = [normalize(research.raw[n]) for n in names]
    stacked = sum(p.fillna(0) for p in parts) / len(parts)
    present = sum(p.notna().astype(int) for p in parts)
    return stacked.where(present > 0)
