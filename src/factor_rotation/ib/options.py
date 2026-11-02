"""Option chain snapshots from IBKR.

One run:
  1. snapshot every underlying (spot, IV index, aggregate option volume / OI)
  2. resolve each underlying's chain (cached per day): which expiries we keep
     and every listed strike for them
  3. keep strikes within an IV-scaled band around spot
  4. stream quotes, IBKR model greeks and open interest for every kept contract
  5. write one parquet file per underlying per snapshot
"""

import asyncio
import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd
from ib_async import IB, Contract, Option, Stock, Ticker

from .. import storage
from ..config import NY, settings
from ..universe import ETFS
from .session import connect
from .stream import harvest, option_ready, present, underlying_ready

log = logging.getLogger(__name__)

UNDERLYING_TICKS = "100,101,104,106"
OPTION_TICKS = "101"
DEFAULT_IV = 0.30


@dataclass
class Underlying:
    symbol: str
    contract: Contract
    spot: float
    iv: float


def _num(x) -> float | None:
    return float(x) if present(x) else None


def _spot(t: Ticker) -> float | None:
    if present(t.bid) and present(t.ask) and t.bid > 0 and t.ask >= t.bid:
        return (t.bid + t.ask) / 2
    for x in (t.last, t.close):
        if present(x) and x > 0:
            return float(x)
    return None


def _is_monthly(d: date) -> bool:
    return 15 <= d.day <= 21 and d.weekday() in (3, 4)


def select_expiries(expiries: list[str], today: date, max_dte: int, max_count: int) -> list[str]:
    within = []
    for e in expiries:
        d = datetime.strptime(e, "%Y%m%d").date()
        dte = (d - today).days
        if 0 <= dte <= max_dte:
            within.append((dte, d, e))
    within.sort()
    near = [e for dte, d, e in within if dte <= 8]
    monthly = [e for dte, d, e in within if dte > 8 and _is_monthly(d)]
    weekly = [e for dte, d, e in within if 8 < dte <= 35 and not _is_monthly(d)]
    chosen = (near + monthly)[:max_count]
    chosen += weekly[: max(0, max_count - len(chosen))]
    return sorted(chosen)


async def snapshot_underlyings(ib: IB, symbols: list[str]) -> tuple[list[Underlying], pd.DataFrame]:
    s = settings()
    stocks = [Stock(sym, "SMART", "USD") for sym in symbols]
    qualified = await ib.qualifyContractsAsync(*stocks)
    contracts = [c for c in qualified if c is not None and c.conId]
    rows, underlyings = [], []
    async for t, complete in harvest(ib, contracts, UNDERLYING_TICKS, underlying_ready, s.max_lines, s.tick_timeout_s):
        spot = _spot(t)
        sym = t.contract.symbol
        rows.append({
            "ts": datetime.now(NY),
            "underlying": sym,
            "con_id": t.contract.conId,
            "spot": spot,
            "bid": _num(t.bid),
            "ask": _num(t.ask),
            "last": _num(t.last),
            "close": _num(t.close),
            "volume": _num(t.volume),
            "iv": _num(t.impliedVolatility),
            "hv": _num(t.histVolatility),
            "call_volume": _num(t.callVolume),
            "put_volume": _num(t.putVolume),
            "call_oi": _num(t.callOpenInterest),
            "put_oi": _num(t.putOpenInterest),
            "market_data_type": t.marketDataType,
            "complete": complete,
        })
        if spot is None:
            log.warning("no_spot underlying=%s", sym)
            continue
        iv = t.impliedVolatility if present(t.impliedVolatility) and t.impliedVolatility > 0 else DEFAULT_IV
        underlyings.append(Underlying(sym, t.contract, spot, float(iv)))
    return underlyings, pd.DataFrame(rows)


async def _chain_contracts(ib: IB, u: Underlying, today: date) -> pd.DataFrame:
    s = settings()
    cache = f"contracts/date={today.isoformat()}/underlying={u.symbol.replace(' ', '_')}.parquet"
    if storage.exists(cache):
        return storage.read(cache)
    params = await ib.reqSecDefOptParamsAsync(u.symbol, "", "STK", u.contract.conId)
    smart = [p for p in params if p.exchange == "SMART"]
    if not smart:
        log.warning("no_chain underlying=%s", u.symbol)
        return pd.DataFrame()
    own_class = u.symbol.replace(" ", "")
    chain = next((p for p in smart if p.tradingClass == own_class), max(smart, key=lambda p: len(p.expirations)))
    max_dte = s.etf_max_dte if u.symbol in ETFS else s.stock_max_dte
    expiries = select_expiries(list(chain.expirations), today, max_dte, s.max_expiries)
    details = await asyncio.gather(*[
        ib.reqContractDetailsAsync(Option(u.symbol, e, exchange="SMART", currency="USD", tradingClass=chain.tradingClass))
        for e in expiries
    ])
    rows = [
        {
            "underlying": u.symbol,
            "con_id": d.contract.conId,
            "local_symbol": d.contract.localSymbol,
            "trading_class": d.contract.tradingClass,
            "expiry": d.contract.lastTradeDateOrContractMonth,
            "strike": float(d.contract.strike),
            "right": d.contract.right,
            "multiplier": int(d.contract.multiplier or 100),
        }
        for batch in details for d in batch
    ]
    df = pd.DataFrame(rows)
    if not df.empty:
        storage.write(df, cache)
    return df


def strike_band(u: Underlying, expiry: str, today: date) -> float:
    s = settings()
    dte = max((datetime.strptime(expiry, "%Y%m%d").date() - today).days, 1)
    cap = s.etf_strike_band if u.symbol in ETFS else s.stock_strike_band
    return min(cap, max(0.03, 2.5 * u.iv * math.sqrt(dte / 365)))


def select_contracts(u: Underlying, chain: pd.DataFrame, today: date) -> pd.DataFrame:
    if chain.empty:
        return chain
    bands = chain["expiry"].map(lambda e: strike_band(u, e, today))
    keep = (chain["strike"] / u.spot - 1).abs() <= bands
    return chain[keep]


def _option_row(t: Ticker, meta: dict, u: Underlying, snapshot_id: str, complete: bool) -> dict:
    g = t.modelGreeks
    oi = t.callOpenInterest if meta["right"] == "C" else t.putOpenInterest
    return {
        "ts": datetime.now(NY),
        "snapshot_id": snapshot_id,
        "underlying": u.symbol,
        "spot_at_start": u.spot,
        **meta,
        "bid": _num(t.bid),
        "ask": _num(t.ask),
        "bid_size": _num(t.bidSize),
        "ask_size": _num(t.askSize),
        "last": _num(t.last),
        "close": _num(t.close),
        "volume": _num(t.volume),
        "open_interest": _num(oi),
        "ib_iv": _num(g.impliedVol) if g else None,
        "ib_delta": _num(g.delta) if g else None,
        "ib_gamma": _num(g.gamma) if g else None,
        "ib_vega": _num(g.vega) if g else None,
        "ib_theta": _num(g.theta) if g else None,
        "ib_und_price": _num(g.undPrice) if g else None,
        "ib_opt_price": _num(g.optPrice) if g else None,
        "ib_pv_dividend": _num(g.pvDividend) if g else None,
        "market_data_type": t.marketDataType,
        "complete": complete,
    }


async def collect(symbols: list[str]) -> dict:
    s = settings()
    now = datetime.now(NY)
    today = now.date()
    snapshot_id = now.strftime("%H%M%S")
    stats = {"snapshot_id": snapshot_id, "date": today.isoformat(), "underlyings": 0, "contracts": 0, "complete": 0}
    async with connect() as ib:
        underlyings, und_df = await snapshot_underlyings(ib, symbols)
        if not und_df.empty:
            storage.write(und_df, f"options_underlying/date={today}/{snapshot_id}.parquet")
        log.info("underlyings_done count=%d of=%d", len(underlyings), len(symbols))

        by_con_id: dict[int, tuple[dict, Underlying]] = {}
        remaining: dict[str, int] = defaultdict(int)
        contracts: list[Contract] = []
        sem = asyncio.Semaphore(8)

        async def resolve(u: Underlying):
            async with sem:
                return await _chain_contracts(ib, u, today)

        chains = await asyncio.gather(*[resolve(u) for u in underlyings], return_exceptions=True)
        for u, chain in zip(underlyings, chains):
            if isinstance(chain, Exception):
                log.error("chain_failed underlying=%s error=%r", u.symbol, chain)
                continue
            selected = select_contracts(u, chain, today)
            for meta in selected.to_dict("records"):
                by_con_id[meta["con_id"]] = (meta, u)
                remaining[u.symbol] += 1
                contracts.append(Contract(conId=meta["con_id"], exchange="SMART"))
            log.info("chain_selected underlying=%s contracts=%d", u.symbol, len(selected))

        rows: dict[str, list[dict]] = defaultdict(list)
        async for t, complete in harvest(ib, contracts, OPTION_TICKS, option_ready, s.max_lines, s.tick_timeout_s):
            meta, u = by_con_id[t.contract.conId]
            rows[u.symbol].append(_option_row(t, meta, u, snapshot_id, complete))
            stats["contracts"] += 1
            stats["complete"] += int(complete)
            remaining[u.symbol] -= 1
            if remaining[u.symbol] == 0:
                path = f"options_raw/date={today}/underlying={u.symbol.replace(' ', '_')}/{snapshot_id}.parquet"
                storage.write(pd.DataFrame(rows.pop(u.symbol)), path)
                stats["underlyings"] += 1
                log.info("underlying_written underlying=%s path=%s", u.symbol, path)
    stats["elapsed_s"] = round((datetime.now(NY) - now).total_seconds(), 1)
    log.info("snapshot_done %s", stats)
    return stats
