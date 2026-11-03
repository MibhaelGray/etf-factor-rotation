"""Performance statistics, including the deflated Sharpe ratio.

The deflated Sharpe ratio (Bailey and Lopez de Prado, 2014) is the
probability that a strategy's true Sharpe exceeds the best Sharpe we would
expect from luck alone, given how many variants we have tried and how
dispersed their Sharpes were. Every backtest appends to the trial registry so
that count can't be forgotten.
"""

import json
import math
import os
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

from ..config import NY

TRIALS = Path(os.environ.get("FR_JOURNAL_DIR", "journal")) / "research" / "trials.jsonl"
EULER_GAMMA = 0.5772156649
PERIODS = {"train": ("1990-01-01", "2014-12-31"), "validate": ("2015-01-01", "2022-12-31"), "holdout": ("2023-01-01", "2100-01-01")}


def summary(daily: pd.Series, turnover: pd.Series | None = None) -> dict:
    r = daily.dropna()
    r = r[r.ne(0).cummax()]
    if len(r) < 60:
        return {"n_days": len(r)}
    years = len(r) / 252
    ann_ret = r.mean() * 252
    ann_vol = r.std() * math.sqrt(252)
    sharpe = ann_ret / ann_vol if ann_vol else float("nan")
    equity = (1 + r).cumprod()
    out = {
        "start": str(r.index[0].date()),
        "n_days": len(r),
        "ann_return": ann_ret,
        "ann_vol": ann_vol,
        "sharpe": sharpe,
        "t_stat": sharpe * math.sqrt(years),
        "max_drawdown": float((equity / equity.cummax() - 1).min()),
        "hit_rate": float((r > 0).mean()),
        "skew": float(r.skew()),
        "kurtosis": float(r.kurt() + 3),
    }
    if turnover is not None:
        out["turnover_per_year"] = float(turnover.reindex(r.index).sum() / years)
    return out


def by_period(daily: pd.Series) -> dict[str, dict]:
    return {name: summary(daily.loc[a:b]) for name, (a, b) in PERIODS.items()}


def probabilistic_sharpe(sr: float, sr_benchmark: float, n: int, skew: float, kurt: float) -> float:
    denom = math.sqrt(max(1e-12, 1 - skew * sr + (kurt - 1) / 4 * sr**2))
    return float(norm.cdf((sr - sr_benchmark) * math.sqrt(n - 1) / denom))


def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    if n_trials < 2:
        return 0.0
    a = (1 - EULER_GAMMA) * norm.ppf(1 - 1 / n_trials)
    b = EULER_GAMMA * norm.ppf(1 - 1 / (n_trials * math.e))
    return math.sqrt(sr_variance) * (a + b)


def deflated_sharpe(daily: pd.Series, trial_daily_sharpes: list[float]) -> float:
    r = daily.dropna()
    r = r[r.ne(0).cummax()]
    sr = r.mean() / r.std()
    variance = float(np.var(trial_daily_sharpes)) if len(trial_daily_sharpes) > 1 else 0.0
    benchmark = expected_max_sharpe(len(trial_daily_sharpes), variance)
    return probabilistic_sharpe(sr, benchmark, len(r), float(r.skew()), float(r.kurt() + 3))


def record_trial(name: str, params: dict, daily: pd.Series) -> None:
    r = daily.dropna()
    r = r[r.ne(0).cummax()]
    entry = {
        "recorded_at": datetime.now(NY).isoformat(),
        "name": name,
        "params": params,
        "daily_sharpe": float(r.mean() / r.std()) if r.std() else 0.0,
        "n_days": len(r),
    }
    TRIALS.parent.mkdir(parents=True, exist_ok=True)
    with TRIALS.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def trial_sharpes() -> list[float]:
    if not TRIALS.exists():
        return []
    return [json.loads(line)["daily_sharpe"] for line in TRIALS.read_text().splitlines() if line.strip()]
