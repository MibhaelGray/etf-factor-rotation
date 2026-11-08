"""The only code in the project that can send an order, and the guards that keep it on the paper account.

Before any order goes out, every one of these must hold, or the run stops:
1. FR_PAPER_PORT is a paper gateway port (4002 for IB Gateway, 7497 for TWS), never a live one (4001, 7496)
   and never the collector's FR_IB_PORT.
2. FR_PAPER_ACCOUNT is set and looks like a paper account (DU or DF and digits).
3. After connecting, every account the gateway manages is a paper account and FR_PAPER_ACCOUNT is one of them.
4. FR_PAPER_SEND=1. Without it every job is a dry run that prints the orders it would send.
Orders can only be market-on-open or market-on-close, for whole shares of a US stock or ETF, and each carries
FR_PAPER_ACCOUNT and an orderRef naming its trade and leg, so fills can be matched back.
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
PAPER_ACCOUNT = re.compile(r"^D[UF]\d+$")
KINDS = {"MOO", "MOC"}


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
        raise GuardError("FR_PAPER_ACCOUNT must be the paper account number (DU followed by digits)")


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
        self.ib = IB()
        self.ib.errorEvent += self._error
        self.errors: list[dict] = []
        await self.ib.connectAsync(self.s.host, self.s.port, clientId=self.s.client_id, readonly=self.readonly,
                                   timeout=20, account=self.s.account, fetchFields=StartupFetch.POSITIONS | StartupFetch.EXECUTIONS)
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
        """Send each order; return its status a few seconds later. Never called on a dry run."""
        from ib_async import Order, Stock

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
            order = Order(action=o["action"], totalQuantity=o["shares"], orderType="MKT" if o["kind"] == "MOO" else "MOC",
                          tif="OPG" if o["kind"] == "MOO" else "DAY", account=self.s.account, orderRef=ref(o["trade_id"], o["symbol"]),
                          outsideRth=False, transmit=True)
            placed.append((o, self.ib.placeOrder(q[0], order)))
        await asyncio.sleep(4)
        for o, t in placed:
            msgs = [e.message for e in t.log if e.errorCode]
            out.append({**o, "order_id": t.order.orderId, "status": t.orderStatus.status, "note": "; ".join(msgs)})
        return out

    async def fills(self) -> list[dict]:
        """Today's executions on the paper account with their commissions."""
        await self.ib.reqExecutionsAsync()
        await asyncio.sleep(2)  # commission reports arrive just after the executions
        out = []
        for f in self.ib.fills():
            e = f.execution
            if e.acctNumber != self.s.account:
                continue
            out.append({"order_ref": e.orderRef, "symbol": f.contract.symbol.replace(" ", "."), "side": e.side,
                        "shares": float(e.shares), "price": e.price, "commission": getattr(f.commissionReport, "commission", None),
                        "time": f.time.isoformat() if f.time else None, "exec_id": e.execId})
        return out

    async def positions(self) -> dict[str, float]:
        return {p.contract.symbol.replace(" ", "."): float(p.position) for p in self.ib.positions(self.s.account) if p.position}

    async def net_liquidation(self) -> float | None:
        rows = await self.ib.accountSummaryAsync(self.s.account)
        v = next((r.value for r in rows if r.tag == "NetLiquidation" and r.currency == "USD"), None)
        return float(v) if v is not None else None


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

    async def fills(self) -> list[dict]:
        return []

    async def positions(self) -> dict[str, float]:
        return {}

    async def net_liquidation(self) -> float | None:
        return None
