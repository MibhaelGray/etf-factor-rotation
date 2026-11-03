import pandas as pd

from . import stats
from .backtest import Research, Result, combo_score, normalize, prepare, run
from .signals import SIGNALS

HORIZONS = (1, 5, 20)


def evaluate(research: Research, name: str, score: pd.DataFrame, horizon: int, params: dict) -> tuple[Result, dict]:
    result = run(research, name, score, horizon)
    stats.record_trial(name, {"horizon": horizon, **params}, result.daily)
    row = {"signal": name, "horizon": horizon, **stats.summary(result.daily, result.turnover)}
    for period, s in stats.by_period(result.daily).items():
        row[f"{period}_sharpe"] = s.get("sharpe")
    return result, row


def run_all(signal_names: list[str] | None = None) -> tuple[pd.DataFrame, dict[str, Result]]:
    research = prepare()
    names = signal_names or list(SIGNALS)
    rows, results = [], {}
    for name in names:
        for h in HORIZONS:
            result, row = evaluate(research, name, normalize(research.raw[name]), h, {})
            rows.append(row)
            results[f"{name}@{h}"] = result
    for h in HORIZONS:
        result, row = evaluate(research, "combo_equal", combo_score(research, names), h, {"members": names})
        rows.append(row)
        results[f"combo_equal@{h}"] = result
    table = pd.DataFrame(rows)
    sharpes = stats.trial_sharpes()
    table["deflated_sharpe"] = [stats.deflated_sharpe(results[f"{r.signal}@{r.horizon}"].daily, sharpes) for r in table.itertuples()]
    table["n_trials"] = len(sharpes)
    return table, results


def format_table(table: pd.DataFrame) -> str:
    cols = ["signal", "horizon", "start", "sharpe", "t_stat", "ann_return", "max_drawdown", "turnover_per_year",
            "train_sharpe", "validate_sharpe", "holdout_sharpe", "deflated_sharpe"]
    t = table[[c for c in cols if c in table]].copy()
    for c in ("ann_return", "max_drawdown"):
        t[c] = (t[c] * 100).round(1)
    return t.round(2).to_string(index=False)
