"""Sliding-window market data harvesting.

IBKR caps simultaneous market data lines (100 by default). We keep up to
`max_lines` subscriptions open, harvest each ticker as soon as it has what we
need (or its timeout lapses), cancel it, and open the next one.
"""

import asyncio
import logging
import math
import time
from collections.abc import AsyncIterator, Callable, Iterable

from ib_async import IB, Contract, Ticker

log = logging.getLogger(__name__)


def present(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def option_ready(t: Ticker) -> bool:
    has_quote = present(t.bid) or present(t.ask) or present(t.last) or present(t.close)
    has_oi = present(t.callOpenInterest) or present(t.putOpenInterest)
    return t.modelGreeks is not None and has_quote and has_oi


def underlying_ready(t: Ticker) -> bool:
    has_price = present(t.last) or present(t.close) or (present(t.bid) and present(t.ask))
    return has_price and present(t.impliedVolatility) and present(t.callOpenInterest) and present(t.shortableShares)


async def harvest(
    ib: IB,
    contracts: Iterable[Contract],
    generic_ticks: str,
    ready: Callable[[Ticker], bool],
    max_lines: int,
    timeout_s: float,
) -> AsyncIterator[tuple[Ticker, bool]]:
    pending = iter(contracts)
    active: dict[int, tuple[Ticker, float]] = {}
    exhausted = False
    while True:
        while not exhausted and len(active) < max_lines:
            contract = next(pending, None)
            if contract is None:
                exhausted = True
                break
            ticker = ib.reqMktData(contract, generic_ticks, False, False)
            active[id(ticker)] = (ticker, time.monotonic())
        if not active:
            return
        await asyncio.sleep(0.2)
        now = time.monotonic()
        done = []
        for key, (ticker, started) in active.items():
            complete = ready(ticker)
            if complete or now - started > timeout_s:
                done.append((key, ticker, complete))
        for key, ticker, complete in done:
            ib.cancelMktData(ticker.contract)
            del active[key]
            yield ticker, complete
