"""The tradable spread universe: dollar-neutral long/short ETF pairs.

Style and factor pairs come from universe.FACTOR_PAIRS; sectors, industries
and cross-asset funds are each paired against SPY.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..universe import BROAD, CROSS_ASSET, FACTOR_PAIRS, INDUSTRIES, SECTORS


@dataclass(frozen=True)
class Spread:
    name: str
    long: str
    short: str
    family: str


def all_spreads() -> list[Spread]:
    spreads = [Spread(p.name, p.long, p.short, "style") for p in FACTOR_PAIRS]
    spreads += [Spread(f"{s.lower()}_spy", s, "SPY", "sector") for s in SECTORS]
    spreads += [Spread(f"{s.lower()}_spy", s, "SPY", "industry") for s in INDUSTRIES]
    spreads += [Spread(f"{s.lower()}_spy", s, "SPY", "broad") for s in BROAD if s not in ("SPY", "RSP", "IWM")]
    spreads += [Spread(f"{s.lower()}_spy", s, "SPY", "cross_asset") for s in CROSS_ASSET if s not in ("HYG", "IEF")]
    return spreads


def returns(close: pd.DataFrame, open_: pd.DataFrame, spreads: list[Spread]) -> dict[str, pd.DataFrame]:
    """Daily spread returns.

    close_to_close[t] is earned from close t-1 to close t; open_to_open[t] from
    open t to open t+1, which is what a position decided at close t-1 and
    traded at the open of t earns.
    """
    cc = close.pct_change(fill_method=None)
    oo = open_.shift(-1) / open_ - 1
    out = {}
    for label, r in (("close_to_close", cc), ("open_to_open", oo)):
        out[label] = pd.DataFrame({s.name: r[s.long] - r[s.short] for s in spreads if s.long in r and s.short in r})
    return out


def log_ratio(close: pd.DataFrame, spreads: list[Spread]) -> pd.DataFrame:
    return pd.DataFrame({s.name: np.log(close[s.long] / close[s.short]) for s in spreads if s.long in close and s.short in close})
