import logging
from contextlib import asynccontextmanager

from ib_async import IB, StartupFetch

from ..config import settings

log = logging.getLogger(__name__)

BENIGN_ERRORS = {2104, 2106, 2107, 2108, 2158, 10167, 10197, 300}


@asynccontextmanager
async def connect(client_id_offset: int = 0):
    s = settings()
    ib = IB()
    ib.errorEvent += _log_error
    await ib.connectAsync(
        s.ib_host,
        s.ib_port,
        clientId=s.ib_client_id + client_id_offset,
        readonly=True,
        timeout=20,
        fetchFields=StartupFetch(0),
    )
    ib.reqMarketDataType(s.market_data_type)
    log.info("connected host=%s port=%s market_data_type=%s", s.ib_host, s.ib_port, s.market_data_type)
    try:
        yield ib
    finally:
        ib.disconnect()


def _log_error(req_id, code, message, contract):
    if code in BENIGN_ERRORS:
        return
    symbol = getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "")
    log.warning("ib_error req_id=%s code=%s contract=%s message=%s", req_id, code, symbol, message)
