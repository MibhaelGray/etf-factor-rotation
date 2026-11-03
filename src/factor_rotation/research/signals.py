"""Signal library.

Every signal is a pure function of a Context and returns a DataFrame
(dates x spreads) of raw scores that use only information available at that
date's close. Positive means long the spread (long leg over short leg). The
backtester normalizes scores, so only sign and relative size matter.

Signals register themselves in SIGNALS with a fixed prior sign: none of them
has a sign fitted to the data.
"""

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .spreads import Spread


@dataclass
class Context:
    close: pd.DataFrame
    ratio: pd.DataFrame
    ret: pd.DataFrame
    spreads: list[Spread]

    @property
    def index(self) -> pd.DatetimeIndex:
        return self.ratio.index


SIGNALS: dict[str, Callable[[Context], pd.DataFrame]] = {}


def signal(fn):
    SIGNALS[fn.__name__] = fn
    return fn


def _beta(ret: pd.DataFrame, driver: pd.Series, window: int = 252) -> pd.DataFrame:
    cov = ret.rolling(window, min_periods=window // 2).cov(driver)
    var = driver.rolling(window, min_periods=window // 2).var()
    return cov.div(var, axis=0)


@signal
def momentum_12_1(ctx: Context) -> pd.DataFrame:
    return ctx.ratio.shift(21) - ctx.ratio.shift(252)


@signal
def momentum_3m(ctx: Context) -> pd.DataFrame:
    return ctx.ratio - ctx.ratio.shift(63)


@signal
def reversal_5d(ctx: Context) -> pd.DataFrame:
    return -(ctx.ratio - ctx.ratio.shift(5))


@signal
def trend_100d(ctx: Context) -> pd.DataFrame:
    return ctx.ratio - ctx.ratio.rolling(100, min_periods=100).mean()


@signal
def credit_lead(ctx: Context) -> pd.DataFrame:
    credit = np.log(ctx.close["HYG"] / ctx.close["IEF"])
    impulse = credit - credit.shift(5)
    beta = _beta(ctx.ret, credit.diff())
    return beta.mul(impulse, axis=0)


@signal
def turn_of_month(ctx: Context) -> pd.DataFrame:
    dates = ctx.index
    month = pd.Series(dates.to_period("M"), index=dates)
    position_in_month = month.groupby(month).cumcount()
    days_left = month.groupby(month).cumcount(ascending=False)
    upcoming = ((days_left <= 1) | (position_in_month <= 1)).astype(float)
    spy = ctx.close["SPY"].pct_change(fill_method=None)
    beta = _beta(ctx.ret, spy)
    return beta.mul(upcoming, axis=0)
