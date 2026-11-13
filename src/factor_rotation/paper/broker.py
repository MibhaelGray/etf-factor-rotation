"""The only code in the project that can send an order, and the guards that keep it on the paper account.

Before any order goes out, every one of these must hold, or the run stops:
1. FR_PAPER_PORT is a paper gateway port (4002 for IB Gateway, 7497 for TWS), never a live one (4001, 7496)
   and never the collector's FR_IB_PORT.
2. FR_PAPER_ACCOUNT is set and looks like a paper account (DU, optionally one letter, or DF, then digits).
3. After connecting, every account the gateway manages is a paper account and FR_PAPER_ACCOUNT is one of them.
4. FR_PAPER_SEND=1. Without it every job is a dry run that prints the orders it would send.
Orders can only be market-on-open entries, market-on-close exits, a plain market order (the post-open check
uses it to close the filled legs of a trade whose other legs never filled), or a day limit order outside regular
hours (the early entries, paper/early.py: pre-market and after-hours on SMART, the overnight session on IBKR's
OVERNIGHT venue), for whole shares of a US stock or ETF, and each carries FR_PAPER_ACCOUNT and an orderRef naming
its trade and leg, so fills can be matched back.
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field

from ..config import settings

log = logging.getLogger(__name__)

PAPER_PORTS = {4002, 7497}
LIVE_PORTS = {4001, 7496}
PAPER_ACCOUNT = re.compile(r"^(DU[A-Z]?|DF)\d+$")
KINDS = {"MOO", "MOC", "MKT", "LMT"}
ORDER_TYPE = {"MOO": ("MKT", "OPG"), "MOC": ("MOC", "DAY"), "MKT": ("MKT", "DAY"), "LMT": ("LMT", "DAY")}   # kind -> (orderType, tif)
VENUES = {"extended", "overnight"}  # a limit order's session: pre-market or after-hours, or 8:00 PM-3:50 AM ET
WAITING = {"", "PendingSubmit", "ApiPending"}       # an order the gateway hasn't answered for yet
WORKING = {"PendingSubmit", "ApiPending", "PreSubmitted", "Submitted", "PendingCancel"}
COMMISSION_WAITS = 40                               # of 0.05 s, so 2 s at most, as long as a report ever took
CONNECT_TRIES, CONNECT_WAIT = 3, 10                 # the gateway may still be reconnecting when the Mac wakes
REQUEST_TIMEOUT = 30  # seconds; while the gateway has lost IBKR's servers these requests never answer


class GuardError(RuntimeError):
    """A paper-trading guard failed; nothing was sent."""


@dataclass(frozen=True)
class PaperSettings:
    host: str = field(default_factory=lambda: os.environ.get("FR_PAPER_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("FR_PAPER_PORT", "4002")))
    account: str = field(default_factory=lambda: os.environ.get("FR_PAPER_ACCOUNT", "").strip())
    client_id: int = field(default_factory=lambda: int(os.environ.get("FR_PAPER_CLIENT_ID", "71")))
    send: bool = field(default_factory=lambda: os.environ.get("FR_PAPER_SEND", "") == "1")


def check_settings(s: PaperSettings) -> None:
    if s.port in LIVE_PORTS or s.port not in PAPER_PORTS:
        raise GuardError(f"FR_PAPER_PORT={s.port} is not a paper port ({', '.join(map(str, sorted(PAPER_PORTS)))})")
    if s.port == settings().ib_port:
        raise GuardError(f"FR_PAPER_PORT={s.port} is the collector's port; the paper gateway needs its own")
    if not PAPER_ACCOUNT.match(s.account):
        raise GuardError("FR_PAPER_ACCOUNT must be the paper account number (it starts with DU)")


def check_accounts(managed: list[str], account: str) -> None:
    if not managed:
        raise GuardError("the gateway reported no accounts")
    live = [a for a in managed if not PAPER_ACCOUNT.match(a)]
    if live:
        raise GuardError(f"the gateway on this port manages a non-paper account ({', '.join(live)}); refusing to trade")
    if account not in managed:
        raise GuardError(f"FR_PAPER_ACCOUNT {account} is not on this gateway ({', '.join(managed)})")


def check_order(o: dict) -> None:
    if o["kind"] not in KINDS:
        raise GuardError(f"order kind {o['kind']} is not allowed; only {sorted(KINDS)}")
    if o["action"] not in ("BUY", "SELL"):
        raise GuardError(f"order action {o['action']}")
    if not isinstance(o["shares"], int) or o["shares"] < 1:
        raise GuardError(f"order shares {o['shares']}")
    if o["kind"] == "LMT":
        if not isinstance(o.get("limit"), (int, float)) or not o["limit"] > 0:
            raise GuardError(f"a limit order needs a positive limit price, not {o.get('limit')}")
        if o.get("venue") not in VENUES:
            raise GuardError(f"a limit order's venue must be one of {sorted(VENUES)}, not {o.get('venue')}")


def ref(trade_id: str, symbol: str) -> str:
    return f"{trade_id}|{symbol}"


def unref(order_ref: str) -> tuple[str, str] | None:
    trade_id, sep, symbol = (order_ref or "").partition("|")
    return (trade_id, symbol) if sep else None


class IBPaper:
    """The paper account through a paper IB Gateway. Use as `async with IBPaper() as b:`."""

    def __init__(self, s: PaperSettings | None = None, readonly: bool = False):
        self.s = s or PaperSettings()
        self.readonly = readonly
        self.ib = None

    async def __aenter__(self):
        from ib_async import IB, StartupFetch

        check_settings(self.s)
        self.errors: list[dict] = []
        for attempt in range(1, CONNECT_TRIES + 1):
            self.ib = IB()
            self.ib.errorEvent += self._error
            try:
                await self.ib.connectAsync(self.s.host, self.s.port, clientId=self.s.client_id, readonly=self.readonly,
                                           timeout=20, account=self.s.account,
                                           fetchFields=StartupFetch.POSITIONS | StartupFetch.EXECUTIONS)
                break
            except Exception as e:  # refused, timed out, or the client id is still held by the last connection
                self.ib.disconnect()
                if attempt == CONNECT_TRIES:
                    raise
                log.warning("paper_connect_retry attempt=%d error=%r", attempt, e)
                await asyncio.sleep(CONNECT_WAIT)
        try:
            check_accounts(self.ib.managedAccounts(), self.s.account)
        except GuardError:
            self.ib.disconnect()
            raise
        log.info("paper_connected port=%s account=%s readonly=%s", self.s.port, self.s.account, self.readonly)
        return self

    async def __aexit__(self, *exc):
        if self.ib:
            self.ib.disconnect()

    def _error(self, req_id, code, message, contract):
        if code in {2104, 2106, 2107, 2108, 2119, 2158}:
            return
        sym = getattr(contract, "symbol", "") if contract else ""
        self.errors.append({"req_id": req_id, "code": code, "symbol": sym, "message": message})
        log.warning("paper_ib_error req_id=%s code=%s symbol=%s %s", req_id, code, sym, message)

    async def place(self, orders: list[dict]) -> list[dict]:
        """Send each order; return its status once the gateway has answered (up to 12 seconds). Never called on
        a dry run."""
        from ib_async import Contract, Order, Stock

        if self.readonly or not self.s.send:
            raise GuardError("this connection may not send orders (read-only or FR_PAPER_SEND is not 1)")
        for o in orders:
            check_order(o)
        out, placed = [], []
        for o in orders:
            contract = Stock(o["symbol"].replace(".", " "), "SMART", "USD")
            q = await self.ib.qualifyContractsAsync(contract)
            if not q or not getattr(q[0], "conId", 0) or q[0].secType != "STK":
                out.append({**o, "status": "not sent", "note": "no US stock or ETF by that symbol"})
                continue
            order_type, tif = ORDER_TYPE[o["kind"]]
            order = Order(action=o["action"], totalQuantity=o["shares"], orderType=order_type, tif=tif,
                          account=self.s.account, orderRef=ref(o["trade_id"], o["symbol"]), outsideRth=False, transmit=True)
            contract = q[0]
            if o["kind"] == "LMT":
                order.lmtPrice, order.outsideRth = round(float(o["limit"]), 2), True
                if o["venue"] == "overnight":   # only day limit orders trade overnight, routed to that venue
                    contract = Contract(conId=q[0].conId, exchange="OVERNIGHT")
                elif o.get("good_till"):        # a pre-market order must not run on into the regular session
                    order.tif, order.goodTillDate = "GTD", o["good_till"]
            placed.append((o, self.ib.placeOrder(contract, order)))
        for second in range(12):  # rejections arrive a moment after the acknowledgement, so wait at least 4
            await asyncio.sleep(1)
            if second >= 3 and all(t.orderStatus.status not in WAITING for _, t in placed):
                break
        for o, t in placed:
            msgs = [e.message for e in t.log if e.errorCode]
            out.append({**o, "order_id": t.order.orderId, "status": t.orderStatus.status, "note": "; ".join(msgs)})
        return out

    async def cancel(self, trade_ids: set[str]) -> int:
        """Cancel the working orders of these trades on the paper account; returns how many were asked to cancel."""
        if self.readonly or not self.s.send:
            raise GuardError("this connection may not cancel orders (read-only or FR_PAPER_SEND is not 1)")
        live = await _answer(self.ib.reqAllOpenOrdersAsync(), "open orders")
        mine = [t for t in live if (key := unref(t.order.orderRef)) and key[0] in trade_ids
                and t.orderStatus.status in WORKING and (not t.order.account or t.order.account == self.s.account)]
        for t in mine:
            self.ib.cancelOrder(t.order)
        for _ in range(10):  # cancellations are confirmed a moment later
            if all(t.orderStatus.status not in WORKING for t in mine):
                break
            await asyncio.sleep(0.5)
        return len(mine)

    async def fills(self) -> list[dict]:
        """Today's executions on the paper account with their commissions."""
        await _answer(self.ib.reqExecutionsAsync(), "executions")
        for _ in range(COMMISSION_WAITS):  # commission reports arrive just after the executions, usually at once
            if all(f.commissionReport.execId for f in self.ib.fills()):
                break
            await asyncio.sleep(0.05)
        out = []
        for f in self.ib.fills():
            e = f.execution
            if e.acctNumber != self.s.account:
                continue
            # An order sent to the OVERNIGHT venue by contract id comes back with no symbol: take it from the order's ref.
            sym = f.contract.symbol or (e.orderRef.rsplit("|", 1)[-1] if "|" in (e.orderRef or "") else "")
            out.append({"order_ref": e.orderRef, "symbol": sym.replace(" ", "."), "side": e.side,
                        "shares": float(e.shares), "price": e.price, "commission": getattr(f.commissionReport, "commission", None),
                        "time": f.time.isoformat() if f.time else None, "exec_id": e.execId})
        return out

    async def orders(self) -> list[dict]:
        """Today's orders on the paper account with their latest status, finished and still working. The
        gateway keeps no reason for a cancelled order, only that it was cancelled."""
        done = await _answer(self.ib.reqCompletedOrdersAsync(False), "completed orders")
        live = await _answer(self.ib.reqAllOpenOrdersAsync(), "open orders")
        out = {}
        for t in [*done, *live]:  # a working order's status wins over a finished one with the same ref and side
            key = unref(t.order.orderRef)
            if not key or (t.order.account and t.order.account != self.s.account):
                continue
            out[(*key, t.order.action)] = {"trade_id": key[0], "symbol": key[1], "action": t.order.action,
                                           "shares": float(t.order.totalQuantity), "status": t.orderStatus.status}
        return list(out.values())

    async def positions(self) -> dict[str, float]:
        return {p.contract.symbol.replace(" ", "."): float(p.position) for p in self.ib.positions(self.s.account) if p.position}

    async def marks(self) -> dict[str, float]:
        """symbol -> IBKR's own price for each position the account holds, the prices its account value uses.
        {} if the gateway won't say: the books' accounts then fall back to the latest daily close."""
        try:
            await _answer(self.ib.reqAccountUpdatesAsync(self.s.account), "account updates")
            return {p.contract.symbol.replace(" ", "."): float(p.marketPrice) for p in self.ib.portfolio(self.s.account)
                    if p.position and p.marketPrice and p.marketPrice > 0}
        except Exception as e:  # noqa: BLE001 - the marks are a nicety, never a failed sync
            log.warning("paper_marks_failed %r", e)
            return {}

    async def net_liquidation(self) -> float | None:
        rows = await _answer(self.ib.accountSummaryAsync(self.s.account), "account summary")
        v = next((r.value for r in rows if r.tag == "NetLiquidation" and r.currency == "USD"), None)
        return float(v) if v is not None else None



async def _answer(request, what: str):
    """Awaits a gateway request, or raises once REQUEST_TIMEOUT passes with no answer."""
    try:
        return await asyncio.wait_for(request, REQUEST_TIMEOUT)
    except asyncio.TimeoutError:
        raise TimeoutError(f"the gateway didn't answer the {what} request in {REQUEST_TIMEOUT} s") from None


class DryRun:
    """Stands in for the broker when FR_PAPER_SEND isn't 1: prints what would be sent, sends nothing."""

    def __init__(self, s: PaperSettings | None = None):
        self.s = s or PaperSettings()
        self.errors: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def place(self, orders: list[dict]) -> list[dict]:
        for o in orders:
            check_order(o)
        return [{**o, "order_id": None, "status": "dry run", "note": ""} for o in orders]

    async def cancel(self, trade_ids: set[str]) -> int:
        return 0

    async def fills(self) -> list[dict]:
        return []

    async def orders(self) -> list[dict]:
        return []

    async def positions(self) -> dict[str, float]:
        return {}

    async def marks(self) -> dict[str, float]:
        return {}

    async def net_liquidation(self) -> float | None:
        return None
