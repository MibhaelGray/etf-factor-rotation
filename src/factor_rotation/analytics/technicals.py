import numpy as np
import pandas as pd

from ..universe import FACTOR_PAIRS, SECTORS


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def _wide(bars: pd.DataFrame, field: str) -> pd.DataFrame:
    return bars.pivot_table(index="date", columns="symbol", values=field).sort_index()


def symbol_technicals(bars: pd.DataFrame, benchmark: str = "SPY") -> pd.DataFrame:
    close = _wide(bars, "close")
    log_ret = np.log(close).diff()
    bench = log_ret[benchmark]
    rows = []
    for symbol, df in bars.groupby("symbol"):
        df = df.sort_values("date").set_index("date")
        c = df["close"]
        if len(c) < 60:
            continue
        r = log_ret[symbol].reindex(c.index)
        a = atr(df)
        beta = r.tail(60).cov(bench.reindex(c.index).tail(60)) / bench.reindex(c.index).tail(60).var()
        rows.append({
            "symbol": symbol,
            "date": c.index[-1],
            "close": c.iloc[-1],
            "ret_1d": c.pct_change(1).iloc[-1],
            "ret_5d": c.pct_change(5).iloc[-1],
            "ret_20d": c.pct_change(20).iloc[-1],
            "ret_60d": c.pct_change(60).iloc[-1],
            "dist_sma20": c.iloc[-1] / c.rolling(20).mean().iloc[-1] - 1,
            "dist_sma50": c.iloc[-1] / c.rolling(50).mean().iloc[-1] - 1,
            "dist_sma200": c.iloc[-1] / c.rolling(200).mean().iloc[-1] - 1 if len(c) >= 200 else None,
            "rsi14": rsi(c).iloc[-1],
            "atr14": a.iloc[-1],
            "atr_pct": a.iloc[-1] / c.iloc[-1],
            "rv20": r.tail(20).std() * np.sqrt(252),
            "rv60": r.tail(60).std() * np.sqrt(252),
            "dist_52w_high": c.iloc[-1] / c.tail(252).max() - 1,
            "volume_z20": (df["volume"].iloc[-1] - df["volume"].tail(20).mean()) / df["volume"].tail(20).std(),
            "rs_vs_spy_20d": c.pct_change(20).iloc[-1] - close[benchmark].pct_change(20).iloc[-1],
            "beta_60d": beta,
        })
    return pd.DataFrame(rows)


def factor_pair_technicals(bars: pd.DataFrame) -> pd.DataFrame:
    close = _wide(bars, "close")
    rows = []
    for pair in FACTOR_PAIRS:
        if pair.long not in close or pair.short not in close:
            continue
        legs = close[[pair.long, pair.short]].dropna()
        ratio = np.log(legs[pair.long] / legs[pair.short])
        spread_ret = ratio.diff()
        rows.append({
            "pair": pair.name,
            "long": pair.long,
            "short": pair.short,
            "date": ratio.index[-1],
            "ret_1d": spread_ret.iloc[-1],
            "ret_5d": ratio.diff(5).iloc[-1],
            "ret_20d": ratio.diff(20).iloc[-1],
            "ret_60d": ratio.diff(60).iloc[-1],
            "z_50d": (ratio.iloc[-1] - ratio.tail(50).mean()) / ratio.tail(50).std(),
            "z_200d": (ratio.iloc[-1] - ratio.tail(200).mean()) / ratio.tail(200).std(),
            "rsi14": rsi(np.exp(ratio)).iloc[-1],
            "above_sma50": bool(ratio.iloc[-1] > ratio.tail(50).mean()),
            "spread_vol_20d": spread_ret.tail(20).std() * np.sqrt(252),
            "leg_corr_60d": legs.pct_change().tail(60).corr().iloc[0, 1],
            "up_days_10": int((spread_ret.tail(10) > 0).sum()),
        })
    return pd.DataFrame(rows)


def regime(bars: pd.DataFrame) -> dict:
    close = _wide(bars, "close")
    sectors = [s for s in SECTORS if s in close]
    rets = close[sectors].pct_change()
    corr = rets.tail(20).corr().to_numpy()
    avg_corr = corr[np.triu_indices_from(corr, k=1)].mean()
    corr_prior = rets.iloc[-80:-20].corr().to_numpy()
    above50 = (close.iloc[-1] > close.rolling(50).mean().iloc[-1]).mean()
    above200 = (close.iloc[-1] > close.rolling(200).mean().iloc[-1]).mean()
    return {
        "date": str(close.index[-1].date()),
        "sector_avg_corr_20d": float(avg_corr),
        "sector_avg_corr_prior_60d": float(corr_prior[np.triu_indices_from(corr_prior, k=1)].mean()),
        "sector_dispersion_20d": float(close[sectors].pct_change(20).iloc[-1].std()),
        "pct_above_sma50": float(above50),
        "pct_above_sma200": float(above200),
        "spy_rv20": float(np.log(close["SPY"]).diff().tail(20).std() * np.sqrt(252)),
    }
