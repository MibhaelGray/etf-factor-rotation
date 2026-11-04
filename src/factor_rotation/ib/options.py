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
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
from ib_async import IB, Contract, Index, Option, Stock, Ticker

from .. import storage
from ..config import NY, settings
from ..universe import AM_SETTLED_CLASSES, ETFS, OPTION_INDEXES
from .session import connect
from .stream import harvest, option_ready, present, underlying_ready

log = logging.getLogger(__name__)

UNDERLYING_TICKS = "100,101,104,106,236"  # 236: shortable shares and the shortable indicator
INDEX_TICKS = "100,101,104,106"  # an index has no borrow
OPTION_TICKS = "101"
DEFAULT_IV = 0.30
CHAIN_TIMEOUT_S = 90


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


def _fund_like(symbol: str) -> bool:
    """ETFs and indexes get the longer expiry window and the narrower strike-band cap."""
    return symbol in ETFS or symbol in OPTION_INDEXES


def _is_monthly(d: date) -> bool:
    return 15 <= d.day <= 21 and d.weekday() in (3, 4)


def select_expiries(expiries: list[str], today: date, max_dte: int, near_count: int, monthly_count: int) -> list[str]:
    """The nearest expiries within 8 days (where most dealer gamma sits) plus the next few monthlies."""
    within = []
    for e in expiries:
        d = datetime.strptime(e, "%Y%m%d").date()
        dte = (d - today).days
        if 0 <= dte <= max_dte:
            within.append((dte, d, e))
    within.sort()
    listed = {d for _, d, _ in within}
    # The third Friday, or the Thursday before it when that Friday is a holiday and isn't listed.
    def monthly_expiry(d: date) -> bool:
        return _is_monthly(d) and (d.weekday() == 4 or d + timedelta(days=1) not in listed)
    near = [e for dte, d, e in within if dte <= 8][:near_count]
    monthly = [e for dte, d, e in within if dte > 8 and monthly_expiry(d)][:monthly_count]
    return sorted(near + monthly)


STRIKE_STEPS = [0.5, 1, 2.5, 5, 10, 25, 50, 100]


def coarse_step(spot: float) -> float:
    """Strike grid kept beyond the near band: the first round step of at least 0.6% of spot ($5 for SPY)."""
    return next((x for x in STRIKE_STEPS if x >= spot * 0.006), STRIKE_STEPS[-1])


async def _qualified(ib: IB, contracts: list[Contract]) -> list[Contract]:
    if not contracts:
        return []
    qualified = await asyncio.wait_for(ib.qualifyContractsAsync(*contracts), CHAIN_TIMEOUT_S)
    return [c for c in qualified if c is not None and c.conId]


async def snapshot_underlyings(ib: IB, symbols: list[str]) -> tuple[list[Underlying], pd.DataFrame]:
    s = settings()
    stocks = [Stock(sym, "SMART", "USD") for sym in symbols if sym not in OPTION_INDEXES]
    indexes = [Index(sym, OPTION_INDEXES[sym][0], "USD") for sym in symbols if sym in OPTION_INDEXES]
    passes = [(await _qualified(ib, stocks), UNDERLYING_TICKS)]
    try:  # an index failure skips the index, never the ETF and stock pulls
        passes.append((await _qualified(ib, indexes), INDEX_TICKS))
    except Exception as e:
        log.error("index_qualify_failed symbols=%s error=%r", [c.symbol for c in indexes], e)
    rows, underlyings = [], []
    for contracts, ticks in passes:
        async for t, complete in harvest(ib, contracts, ticks, underlying_ready, s.max_lines, s.tick_timeout_s):
            _underlying_row(t, complete, rows, underlyings)
    return underlyings, pd.DataFrame(rows)


def _underlying_row(t: Ticker, complete: bool, rows: list[dict], underlyings: list[Underlying]) -> None:
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
        "shortable": _num(t.shortable),
        "shortable_shares": _num(t.shortableShares),
        "market_data_type": t.marketDataType,
        "complete": complete,
    })
    if spot is None:
        log.warning("no_spot underlying=%s", sym)
        return
    iv = t.impliedVolatility if present(t.impliedVolatility) and t.impliedVolatility > 0 else DEFAULT_IV
    underlyings.append(Underlying(sym, t.contract, spot, float(iv)))


def _chain_rows(u: Underlying, chain, expiries: list[str]) -> list[dict]:
    """Candidate contracts built from a chain's own expiry and strike lists rather than looked up one expiry at
    a time: those lookups return every strike's full details, and dozens of them at once stall the gateway.
    The strike list is the union across expiries, so a few built contracts don't exist; qualify() drops them."""
    multiplier = int(chain.multiplier or 100)
    return [
        {
            "underlying": u.symbol,
            "con_id": None,
            "local_symbol": f"{chain.tradingClass} {e} {right}{float(k):g}",
            "trading_class": chain.tradingClass,
            "exchange": chain.exchange,
            "expiry": e,
            "strike": float(k),
            "right": right,
            "multiplier": multiplier,
        }
        for e in expiries for k in sorted(chain.strikes) for right in ("C", "P")
    ]


def index_chains(params, classes: tuple[str, ...]) -> list:
    """One parameter set per configured trading class, preferring SMART routing over the home exchange."""
    picked = []
    for cls in classes:
        sets = [p for p in params if p.tradingClass == cls]
        if sets:
            picked.append(next((p for p in sets if p.exchange == "SMART"), sets[0]))
    return picked


def index_expiries(chain, today: date, max_dte: int, near: int, monthly: int) -> list[str]:
    """The dailies and weeklies class takes the usual nearest expiries plus monthlies; an AM-settled monthly
    class only lists monthlies, so it takes just those."""
    if chain.tradingClass in AM_SETTLED_CLASSES:
        return select_expiries(list(chain.expirations), today, max_dte, 0, monthly)
    return select_expiries(list(chain.expirations), today, max_dte, near, monthly)


async def _chain_contracts(ib: IB, u: Underlying, today: date) -> pd.DataFrame:
    s = settings()
    cache = f"contracts/date={today.isoformat()}/underlying={u.symbol.replace(' ', '_')}.parquet"
    if storage.exists(cache):
        return storage.read(cache)
    max_dte = s.etf_max_dte if _fund_like(u.symbol) else s.stock_max_dte
    if u.symbol in OPTION_INDEXES:
        params = await asyncio.wait_for(ib.reqSecDefOptParamsAsync(u.symbol, "", "IND", u.contract.conId), CHAIN_TIMEOUT_S)
        chains = index_chains(params, OPTION_INDEXES[u.symbol][1])
        log.info("index_chain underlying=%s sets=%s picked=%s", u.symbol,
                 sorted({(p.exchange, p.tradingClass) for p in params}), [(p.exchange, p.tradingClass) for p in chains])
        rows = [r for c in chains
                for r in _chain_rows(u, c, index_expiries(c, today, max_dte, s.near_expiries, s.monthly_expiries))]
    else:
        params = await asyncio.wait_for(ib.reqSecDefOptParamsAsync(u.symbol, "", "STK", u.contract.conId), CHAIN_TIMEOUT_S)
        smart = [p for p in params if p.exchange == "SMART"]
        own_class = u.symbol.replace(" ", "")
        chain = next((p for p in smart if p.tradingClass == own_class), max(smart, key=lambda p: len(p.expirations))) \
            if smart else None
        rows = _chain_rows(u, chain, select_expiries(list(chain.expirations), today, max_dte, s.near_expiries,
                                                     s.monthly_expiries)) if chain else []
    if not rows:
        log.warning("no_chain underlying=%s", u.symbol)
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    storage.write(df, cache)
    return df


QUALIFY_BATCH = 50


def _ids_path(symbol: str) -> str:
    return f"option_ids/underlying={symbol.replace(' ', '_')}.parquet"


def _exchange(x) -> str:
    return x if isinstance(x, str) and x else "SMART"


UNLISTED = -1  # cached con_id for a built contract IBKR doesn't list, so it isn't looked up every run
ID_KEY = ["expiry", "strike", "right", "trading_class"]  # SPX and SPXW share the third-Friday dates


def _known_ids(path: str, selected: pd.DataFrame, today: date) -> pd.DataFrame:
    cols = [*ID_KEY, "con_id", "local_symbol"]
    known = storage.read(path) if storage.exists(path) else pd.DataFrame(columns=cols)
    if "trading_class" not in known.columns:  # files from before index options held one class per underlying
        classes = selected["trading_class"].dropna().unique()
        known["trading_class"] = classes[0] if len(classes) == 1 else None
    return known[known["expiry"] >= today.strftime("%Y%m%d")][cols]


async def qualify(ib: IB, u: Underlying, selected: pd.DataFrame, today: date) -> pd.DataFrame:
    """IBKR contract ids for the selected options. Ids last until expiry, so they're kept per underlying and only
    new strikes and expiries are looked up, one small request each, in batches. Built options that aren't
    listed are cached as unlisted and dropped."""
    if selected.empty:
        return selected
    path = _ids_path(u.symbol)
    known = _known_ids(path, selected, today)
    base = selected.drop(columns=["con_id", "local_symbol"], errors="ignore")
    merged = base.merge(known, on=ID_KEY, how="left")
    missing = merged[merged["con_id"].isna()]
    found = []
    for start in range(0, len(missing), QUALIFY_BATCH):
        batch = missing.iloc[start:start + QUALIFY_BATCH]
        contracts = [Option(u.symbol, r.expiry, r.strike, r.right, _exchange(getattr(r, "exchange", None)),
                            multiplier=str(r.multiplier), currency="USD", tradingClass=r.trading_class)
                     for r in batch.itertuples()]
        qualified = await asyncio.wait_for(ib.qualifyContractsAsync(*contracts), CHAIN_TIMEOUT_S)
        # Results line up with the requests; None means IBKR doesn't list that strike for that expiry.
        for r, c in zip(batch.itertuples(), qualified):
            if c is not None and not isinstance(c, list) and c.conId:
                # Keyed by the request, not IBKR's reply: an AM-settled monthly can come back with its last
                # trading day rather than the listed expiry, and the selection must still match it.
                found.append({"expiry": r.expiry, "strike": float(r.strike), "right": r.right,
                              "trading_class": r.trading_class, "con_id": int(c.conId), "local_symbol": c.localSymbol})
            elif c is None:
                found.append({"expiry": r.expiry, "strike": float(r.strike), "right": r.right,
                              "trading_class": r.trading_class, "con_id": UNLISTED, "local_symbol": None})
    if found:
        known = pd.concat([known, pd.DataFrame(found)], ignore_index=True).drop_duplicates(ID_KEY, keep="last")
        storage.write(known, path)
        merged = base.merge(known, on=ID_KEY, how="left")
    out = merged[merged["con_id"].notna() & (merged["con_id"] != UNLISTED)].copy()
    out["con_id"] = out["con_id"].astype(int)
    log.info("qualified underlying=%s selected=%d looked_up=%d listed=%d", u.symbol, len(selected), len(missing), len(out))
    return out


def strike_band(u: Underlying, expiry: str, today: date) -> float:
    s = settings()
    dte = max((datetime.strptime(expiry, "%Y%m%d").date() - today).days, 1)
    cap = s.etf_strike_band if _fund_like(u.symbol) else s.stock_strike_band
    return min(cap, max(s.near_strike_band, s.strike_sigmas * u.iv * math.sqrt(dte / 365)))


def select_contracts(u: Underlying, chain: pd.DataFrame, today: date) -> pd.DataFrame:
    """Every strike within the near band (3%) of spot, then only round strikes out to the IV-scaled band."""
    if chain.empty:
        return chain
    near_band = settings().near_strike_band
    step = coarse_step(u.spot)
    kept = []
    for expiry, g in chain.groupby("expiry"):
        distance = (g["strike"] / u.spot - 1).abs()
        g = g[distance <= strike_band(u, expiry, today)]
        distance = distance[g.index]
        near, far = g[distance <= near_band], g[distance > near_band]
        on_grid = ((far["strike"] / step) - (far["strike"] / step).round()).abs() < 1e-6
        if on_grid.any() or far.empty:
            far = far[on_grid]
        else:  # strikes off the round grid: thin to about the same spacing instead
            strikes = sorted(far["strike"].unique())
            spacing = float(np.median(np.diff(strikes))) if len(strikes) > 1 else step
            every = max(1, round(step / spacing))
            far = far[far["strike"].isin(strikes[::every])]
        kept += [near, far]
    return pd.concat(kept) if kept else chain.iloc[0:0]


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

        by_contract: dict[int, tuple[dict, Underlying]] = {}
        remaining: dict[str, int] = defaultdict(int)
        contracts: list[Contract] = []
        sem = asyncio.Semaphore(8)

        async def resolve(u: Underlying):
            async with sem:
                return await _chain_contracts(ib, u, today)

        chains = await asyncio.gather(*[resolve(u) for u in underlyings], return_exceptions=True)
        qualify_sem = asyncio.Semaphore(2)

        async def resolve_ids(u: Underlying, chain: pd.DataFrame):
            async with qualify_sem:
                return await qualify(ib, u, select_contracts(u, chain, today), today)

        live = [(u, c) for u, c in zip(underlyings, chains) if not isinstance(c, Exception)]
        for u, c in zip(underlyings, chains):
            if isinstance(c, Exception):
                log.error("chain_failed underlying=%s error=%r", u.symbol, c)
        selections = await asyncio.gather(*[resolve_ids(u, c) for u, c in live], return_exceptions=True)
        for (u, _), selected in zip(live, selections):
            if isinstance(selected, Exception):
                log.error("qualify_failed underlying=%s error=%r", u.symbol, selected)
                continue
            for meta in selected.to_dict("records"):
                contract = Contract(conId=meta["con_id"], exchange=_exchange(meta.get("exchange")))
                by_contract[id(contract)] = (meta, u)
                remaining[u.symbol] += 1
                contracts.append(contract)
            log.info("chain_selected underlying=%s contracts=%d", u.symbol, len(selected))

        rows: dict[str, list[dict]] = defaultdict(list)
        async for t, complete in harvest(ib, contracts, OPTION_TICKS, option_ready, s.max_lines, s.tick_timeout_s):
            meta, u = by_contract[id(t.contract)]
            row = _option_row(t, meta, u, snapshot_id, complete)
            if any(row[k] is not None for k in ("bid", "ask", "last", "close", "open_interest")):
                rows[u.symbol].append(row)
                stats["contracts"] += 1
                stats["complete"] += int(complete)
            remaining[u.symbol] -= 1
            if remaining[u.symbol] == 0:
                kept = rows.pop(u.symbol, [])
                if kept:
                    path = f"options_raw/date={today}/underlying={u.symbol.replace(' ', '_')}/{snapshot_id}.parquet"
                    storage.write(pd.DataFrame(kept), path)
                    stats["underlyings"] += 1
                    log.info("underlying_written underlying=%s contracts=%d path=%s", u.symbol, len(kept), path)
    stats["elapsed_s"] = round((datetime.now(NY) - now).total_seconds(), 1)
    log.info("snapshot_done %s", stats)
    return stats
