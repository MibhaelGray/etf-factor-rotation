"""The intraday book outside regular hours: news movers after the close, overnight and before the open.

`fr intraday ext`, every 15 minutes from 4:46 PM to 9:31 AM ET. Each night has three sessions, each flat by its end:

  after-hours  4:00-8:00 PM ET     signals until 7:15 PM, flat by 7:45 PM
  overnight    8:00 PM-3:50 AM ET  IBKR's OVERNIGHT venue, Sunday to Thursday nights; signals until 3:00 AM, flat by 3:35 AM
  pre-market   4:00-9:30 AM ET     signals until 8:45 AM, flat by 9:20 AM

The signal is a news mover: a stock (funds are only hedges here) that has moved at least MOVE_ATR daily ATRs against its hedge since the
4:00 PM close (beta-adjusted), on at least MIN_SESSION_DOLLARS traded since the close when IBKR reports volume.
The direction is with the move. Haiku takes or passes with the name's headlines (model.py), as in the day book;
earnings reporters are not skipped here, since a report is the usual reason for the move. Names the swing book
holds or is entering early are skipped. Every signal is followed by the same exit rules on each tick's prices
(stop and target in ATRs as in signals.py, out at the session's flat time), so takes can be scored against passes.

Takes trade on the paper account: limit orders LIMIT_SLIP through the gateway's bid or ask, $10,000 a side, on SMART outside
regular hours or the OVERNIGHT venue. An entry still unfilled at the next tick is cancelled; a pair with one side
filled is closed. An exit that hasn't filled is cancelled and sent again at the new price at each tick, into the
next session if it must, and anything still held at the 9:31 AM tick goes out with a market order.

Prices come from the paper gateway (read-only, FR_EXT_DATA_CLIENT_ID, default 73): snapshot quotes, and 15-minute
bars for the session's volume. Before 8:00 PM and after 4:00 AM Yahoo stands in when the gateway has no data,
for finding signals and following them only: no order goes out without the gateway's own bid and ask for it, so
on Yahoo's prices a take is recorded as not sent, with the gateway's reason. Overnight there is no stand-in, so
the tick waits. The night's record is journal/intraday/nights/<session>.json,
named for the session the night leads into.
"""

import asyncio
import json
import logging
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .. import bars, drawdown, journal
from ..analytics.technicals import atr
from ..config import NY
from ..universe import ETFS
from ..paper import rules
from . import model, orders, replay, signals

log = logging.getLogger(__name__)

def root() -> Path:
    """Read from journal.JOURNAL each time, so a temporary journal in a test or FR_JOURNAL_DIR is honoured."""
    return journal.JOURNAL / "intraday" / "nights"
MOVE_ATR = 1.0
MIN_SESSION_DOLLARS = 1e6
MAX_SPREAD_ATR = 0.25      # the bid-ask spread a name may have, as a share of its daily ATR, to signal or trade
JUDGED_PER_TICK = 4
MAX_TAKES = 5              # a night
MAX_OPEN = 3
LIMIT_SLIP = 0.005
MAX_CROSS_SPREAD = 0.01    # widest spread an order crosses (the user's 1%, 2026-11-12); half the swing book's
SNAPSHOT_BATCH = 50
SESSIONS = {               # name -> (start, end, last signal, flat by)
    "after-hours": (time(16, 0), time(20, 0), time(19, 15), time(19, 45)),
    "overnight": (time(20, 0), time(3, 50), time(3, 0), time(3, 35)),
    "pre-market": (time(4, 0), time(9, 30), time(8, 45), time(9, 20)),
}
OPEN_CLEANUP = (time(9, 30), time(9, 45))


def _clock(t: time) -> str:
    return datetime.combine(date(2000, 1, 1), t).strftime("%-I:%M %p")


def session_at(now: datetime) -> str | None:
    """The session trading at `now` (New York): after-hours, overnight, pre-market, "open" for the clean-up
    just after the 9:30 AM bell, or None."""
    t, today = now.time(), now.date()
    trading = journal.is_trading_day(today)
    if trading and time(16, 0) <= t < time(20, 0):
        return "after-hours"
    if t >= time(20, 0) and journal.next_trading_day(today) == today + timedelta(days=1):
        return "overnight"
    if t < time(3, 50) and trading:
        return "overnight"
    if trading and time(4, 0) <= t < time(9, 30):
        return "pre-market"
    if trading and OPEN_CLEANUP[0] <= t < OPEN_CLEANUP[1]:
        return "open"
    return None


def night_of(now: datetime) -> date:
    """The session this night leads into, which names its file."""
    today = now.date()
    if now.time() >= time(16, 0) or not journal.is_trading_day(today):
        return journal.next_trading_day(today)
    return today


def last_close_day(now: datetime) -> date:
    """The regular session whose 4:00 PM close the night's moves are measured from."""
    today = now.date()
    if journal.is_trading_day(today) and now.time() >= time(16, 0):
        return today
    return journal.previous_trading_day(today)


def path(night: date):
    return root() / f"{night}.json"


def load(night: date) -> dict:
    p = path(night)
    return json.loads(p.read_text()) if p.exists() else {"night": night.isoformat(), "ticks": [], "signals": []}


def _save(night: date, doc: dict) -> None:
    root().mkdir(parents=True, exist_ok=True)
    path(night).write_text(json.dumps(doc, indent=1) + "\n")


def _deadline(sess: str, now: datetime, which: int) -> datetime:
    """A session's time (2 = last signal, 3 = flat by) as a moment: overnight's fall after midnight."""
    day = now.date() + (timedelta(days=1) if sess == "overnight" and now.time() >= time(20, 0) else timedelta())
    return datetime.combine(day, SESSIONS[sess][which], NY)


def flat_by(sess: str, now: datetime) -> datetime:
    return _deadline(sess, now, 3)


def signals_open(sess: str, now: datetime) -> bool:
    return sess in SESSIONS and now <= _deadline(sess, now, 2)


# ---- What is known about each name before the night ---------------------------------------------------------
def context(close_day: date) -> dict[str, dict]:
    """name -> hedge, beta, daily ATR as a share of price, and the 4:00 PM closes, from daily bars through `close_day`."""
    names = replay.universe()
    daily = bars.load(names)
    if daily.empty:
        return {}
    daily = daily[daily["symbol"].isin(names) & (daily["date"] <= pd.Timestamp(close_day))].sort_values(["symbol", "date"])
    close = daily.pivot_table(index="date", columns="symbol", values="close").sort_index()
    if close.empty or close.index[-1].date() != close_day:
        return {}
    dollars = (daily.assign(d=daily["close"] * daily["volume"]).pivot_table(index="date", columns="symbol", values="d")
               .sort_index().rolling(20).median().iloc[-1])
    atr_pct = {s: float((atr(df.set_index("date")) / df.set_index("date")["close"]).iloc[-1]) for s, df in daily.groupby("symbol")}
    out = {}
    for sym, (hedge, beta) in replay.pick_hedges(close.pct_change().iloc[-60:], replay.basket_hedges()).items():
        a, c, hc = atr_pct.get(sym), close[sym].iloc[-1], close[hedge].iloc[-1] if hedge in close else np.nan
        if not (dollars.get(sym, 0) >= replay.MIN_DOLLARS and a and np.isfinite(a) and a > 0 and np.isfinite(c) and np.isfinite(hc)):
            continue
        out[sym] = {"hedge": hedge, "beta": round(beta, 3), "atr_pct": a, "close": float(c), "hedge_close": float(hc)}
    return out


# ---- Prices -------------------------------------------------------------------------------------------------
class IBMarket:
    """Snapshot quotes and session volume from the paper gateway, read-only, on its own client id."""

    def __init__(self, client_id: int | None = None):
        from ..paper.broker import PaperSettings
        s = PaperSettings()
        self.host, self.port = s.host, s.port
        self.client_id = client_id or int(os.environ.get("FR_EXT_DATA_CLIENT_ID", "73"))
        self.ib = None

    async def __aenter__(self):
        from ib_async import IB
        self.ib = IB()
        self.errors: dict[int, tuple[int, str]] = {}   # conId -> the gateway's last refusal for it
        self.ib.errorEvent += self._on_error
        await self.ib.connectAsync(self.host, self.port, clientId=self.client_id, readonly=True, timeout=15)
        self.ib.reqMarketDataType(1)
        return self

    def _on_error(self, req_id, code, message, contract):
        """Keeps what the gateway said about a contract, so a missing quote comes with its reason."""
        if contract is not None and getattr(contract, "conId", 0) and code not in QUOTE_NOISE:
            self.errors[contract.conId] = (code, message)

    async def __aexit__(self, *exc):
        if self.ib:
            self.ib.disconnect()

    async def _contracts(self, symbols: list[str], overnight: bool) -> dict:
        from ib_async import Contract, Stock
        out = {}
        for i in range(0, len(symbols), SNAPSHOT_BATCH):
            chunk = [Stock(s.replace(".", " "), "SMART", "USD") for s in symbols[i:i + SNAPSHOT_BATCH]]
            for c in await asyncio.wait_for(self.ib.qualifyContractsAsync(*chunk), 60):
                if c and getattr(c, "conId", 0):
                    out[c.symbol.replace(" ", ".")] = Contract(conId=c.conId, exchange="OVERNIGHT") if overnight else c
        return out

    async def quotes(self, symbols: list[str], overnight: bool, tries: int = 1, need_books: bool = False) -> dict[str, float]:
        """symbol -> latest price. Also sets `books` (symbol -> bid, ask) and `missing` (symbol -> why it has no
        bid and ask). `need_books` waits for the bid and ask, not just a price; `tries` asks again for the names
        still without a book, unless the gateway refused them for a reason that asking again won't change."""
        contracts = await self._contracts(symbols, overnight)
        out = {}
        self.books: dict[str, tuple[float, float]] = {}
        self.missing: dict[str, str] = {s: "IBKR has no contract for it" for s in symbols if s not in contracts}
        why = {}
        todo = list(contracts.items())
        for attempt in range(tries):
            if attempt:
                await asyncio.sleep(QUOTE_RETRY_WAIT)
            for i in range(0, len(todo), SNAPSHOT_BATCH):
                batch = todo[i:i + SNAPSHOT_BATCH]
                for _, c in batch:
                    self.errors.pop(c.conId, None)
                tickers = [(s, self.ib.reqMktData(c, "", False, False)) for s, c in batch]
                for _ in range(QUOTE_WAITS):
                    await asyncio.sleep(0.5)
                    if all((_book(t) if need_books else _price(t)) or t.contract.conId in self.errors for _, t in tickers):
                        break
                out.update({s: p for s, t in tickers if (p := _price(t))})
                self.books.update({s: b for s, t in tickers if (b := _book(t))})
                why.update({s: _why_no_book(t, self.errors.get(t.contract.conId)) for s, t in tickers if not _book(t)})
                for _, tk in tickers:
                    self.ib.cancelMktData(tk.contract)
            todo = [(s, c) for s, c in todo if s not in self.books and self.errors.get(c.conId, (0, ""))[0] not in QUOTE_REFUSED]
            if not todo:
                break
        self.missing.update({s: w for s, w in why.items() if s not in self.books})
        self._contracts_cache = contracts
        return out

    async def session_dollars(self, symbols: list[str], overnight: bool, since: datetime) -> dict[str, float | None]:
        """Dollars traded since the 4:00 PM close: SMART's bars outside regular hours, plus the OVERNIGHT venue's.
        None when every request failed; 0 when the requests worked and nothing traded."""
        from ib_async import Contract
        smart = await self._contracts(symbols, False)
        start = pd.Timestamp(since)
        out = {}
        for s in symbols:
            c = smart.get(s)
            if c is None:
                out[s] = None
                continue
            total, answered = 0.0, False
            for contract in [c, *([Contract(conId=c.conId, exchange="OVERNIGHT")] if overnight else [])]:
                try:
                    got = await asyncio.wait_for(self.ib.reqHistoricalDataAsync(contract, "", "2 D", "15 mins", "TRADES", useRTH=False), 30)
                except Exception:  # noqa: BLE001 - a missing figure only means the volume test is skipped
                    continue
                answered = True
                for b in got:
                    when = pd.Timestamp(b.date).tz_convert(NY)
                    regular = when.date() > since.date() and time(9, 30) <= when.time() < time(16, 0)
                    if when >= start and not (contract is c and regular):
                        total += b.close * b.volume
            out[s] = total if answered else None
        return out


QUOTE_WAITS = 16            # of 0.5 s: how long one request waits for its quote
QUOTE_RETRY_WAIT = 5        # seconds before asking again for the names still without a bid and ask
QUOTE_NOISE = {2104, 2106, 2107, 2108, 2119, 2158, 300, 10167}   # farm status lines, notices, the cancel of a refused request
# The gateway's refusals that asking again won't change, in plain words.
QUOTE_REFUSED = {
    10197: "the live IBKR login is in use somewhere else, and IBKR gives its quotes to one session at a time",
    10168: "the account has no market data subscription for it over the API",
    10089: "the account has no market data subscription for it over the API",
    10090: "the account has no market data subscription for it over the API",
    10091: "the account has no market data subscription for it over the API",
    354: "the account has no market data subscription for it over the API",
    200: "IBKR has no such contract on this venue",
}


def _book(t) -> tuple[float, float] | None:
    """The ticker's bid and ask when both are there and the bid isn't over the ask."""
    bid, ask = t.bid, t.ask
    if bid is None or ask is None or not (np.isfinite(bid) and np.isfinite(ask)):
        return None
    return (float(bid), float(ask)) if bid > 0 and ask >= bid else None


def _why_no_book(t, error: tuple[int, str] | None) -> str:
    """Why a name has no bid and ask, in a sentence fragment that follows "because"."""
    if error:
        code, message = error
        return f"{QUOTE_REFUSED.get(code) or message.strip()} (IBKR error {code})"
    have = lambda v: v is not None and np.isfinite(v) and v > 0   # noqa: E731
    bid, ask = have(t.bid), have(t.ask)
    if bid and ask:
        return f"its bid ({t.bid:g}) is above its ask ({t.ask:g})"
    if bid or ask:
        return f"the gateway sent {'a bid and no ask' if bid else 'an ask and no bid'}"
    return f"the gateway sent no bid or ask within {QUOTE_WAITS * 0.5:g} seconds and gave no reason"


def _price(t) -> float | None:
    for v in (t.last, (t.bid + t.ask) / 2 if t.bid and t.ask and t.bid > 0 and t.ask > 0 else None):
        if v is not None and np.isfinite(v) and v > 0:
            return float(v)
    return None


class YahooMarket:
    """The stand-in outside the overnight session: Yahoo's latest 1-minute price today, no volume."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def quotes(self, symbols: list[str], overnight: bool) -> dict[str, float]:
        from ..paper import book
        self.books: dict[str, tuple[float, float]] = {}   # Yahoo gives no quote, so no spread to judge
        return {} if overnight else await asyncio.to_thread(book.quotes, symbols, 2, 5)

    async def session_dollars(self, symbols, overnight, since):
        return {s: None for s in symbols}


async def _prices(symbols: list[str], sess: str, since: datetime, market=None) -> tuple[dict, object, str]:
    """Quotes, and the market they came from (kept open for volume)."""
    overnight = sess == "overnight"
    if market is not None:
        return await market.quotes(symbols, overnight), market, "given"
    try:
        m = IBMarket()
        await m.__aenter__()
        got = await m.quotes(symbols, overnight)
        if len(got) >= len(symbols) // 2:
            return got, m, "IBKR"
        await m.__aexit__()
    except Exception as e:  # noqa: BLE001 - no gateway, or no market data on it
        log.warning("ext_ibkr_quotes_failed error=%r", e)
    y = YahooMarket()
    return await y.quotes(symbols, overnight), y, "Yahoo"


# ---- The model's view of each signal ------------------------------------------------------------------------
def _spread(sig: dict, px: dict) -> float | None:
    e = sig.get("entry")
    s, h = px.get(sig["symbol"]), px.get(sig["hedge"])
    if not e or not s or not h:
        return None
    side = 1 if sig["direction"] == "long" else -1
    return side * ((s / e["name"] - 1) - (h / e["hedge"] - 1))


def advance(sig: dict, px: dict, now: datetime) -> None:
    """Moves a signal's model trade along on this tick's prices: out at its stop, its target or its flat time."""
    if "exit" in sig:
        return
    r = _spread(sig, px)
    due = now >= datetime.fromisoformat(sig["plan"]["flat_at"])
    stop, target = -sig["plan"]["stop_pct"] / 100, sig["plan"]["target_pct"] / 100
    why = "flat" if due else "stop" if r is not None and r <= -stop else "target" if r is not None and r >= target else None
    if r is not None:
        sig["so_far_bp"] = round(r * 1e4, 1)
    if why and (r is not None or due):
        sig.pop("so_far_bp", None)
        sig["exit"] = {"at": now.strftime("%-I:%M %p"), "reason": why,
                       **({"result_bp": round(r * 1e4, 1), "after_costs_bp": round((r - replay.COST) * 1e4, 1)} if r is not None else {})}


# ---- Paper orders -------------------------------------------------------------------------------------------
def _limit(action: str, px: float) -> float:
    return round(px * (1 + LIMIT_SLIP) if action == "BUY" else px * (1 - LIMIT_SLIP), 2)


def _order(sig, symbol, action, shares, sess, px, now, book_=None, exiting=False) -> dict | None:
    """One paper order, or None when there is no bid and ask to price it on: outside regular hours nothing is
    sent without the gateway's quote (2026-11-13; until then a missing quote fell back to Yahoo's last price)."""
    if sess == "open":
        return {"trade_id": sig["id"], "symbol": symbol, "action": action, "shares": shares, "kind": "MKT"}
    if rules.spread_pct(book_) is None:
        return None
    # Orders cross the book to fill at once: a buy at the ask, a sell at the bid (the limit is LIMIT_SLIP beyond,
    # so it still fills if the quote moves a little). The one exception is a book wider than MAX_CROSS_SPREAD
    # (1%): the order then rests at the mid.
    bid, ask = book_
    px = (ask if action == "BUY" else bid) if rules.crossable(book_, MAX_CROSS_SPREAD) else (bid + ask) / 2
    from ..paper.early import book_fields
    o = {"trade_id": sig["id"], "symbol": symbol, "action": action, "shares": shares, "kind": "LMT",
         "limit": _limit(action, px), "venue": "overnight" if sess == "overnight" else "extended", **book_fields(book_)}
    if sess == "pre-market":
        o["good_till"] = f"{now:%Y%m%d} 09:25:00 US/Eastern"
    return o


def _no_entry(x: dict, books: dict, missing: dict) -> str | None:
    """Why a take can't go in at this tick, or None when both legs have a bid and ask from the gateway."""
    parts = [f"no quote for {s} from the gateway" + (f" because {missing[s]}" if missing.get(s) else "")
             for s in (x["symbol"], x["hedge"]) if rules.spread_pct(books.get(s)) is None]
    return "; ".join(parts) or None


async def trade(doc: dict, sess: str, px: dict, now: datetime, save, make_broker=orders.broker,
                books: dict | None = None, missing: dict | None = None) -> list[str]:
    """This tick's paper orders: new takes in, unfilled entries cancelled, exits out and sent again until flat.
    Outside regular hours every order is priced on the gateway's bid and ask (`books`). A take without a quote
    on both legs isn't sent; an exit without a quote waits for the next tick, or the market order at the open."""
    warnings: list[str] = []
    books, missing = books or {}, missing or {}
    new = [x for x in doc["signals"] if (x.get("verdict") or {}).get("decision") == "take" and "paper" not in x]
    live = [x for x in doc["signals"] if (x.get("paper") or {}).get("state") in ("entering", "open", "exiting", "dry run")]
    if new and orders.HALT.exists():
        for x in new:
            x["paper"] = {"state": "not sent", "why": "halted"}
        new = []
    if new and (why := drawdown.paused("intraday")):
        for x in new:
            x["paper"] = {"state": "not sent", "why": f"paused: {why}"}
        warnings.append(f"Haiku took {len(new)} trade{'' if len(new) == 1 else 's'}, but the intraday book is paused, so nothing went out: {why}.")
        new = []
    if new and sess == "open":
        for x in new:
            x["paper"] = {"state": "not sent", "why": "too late: the regular session has opened"}
        new = []
    for x in list(new):
        if why := _no_entry(x, books, missing):
            x["paper"] = {"state": "not sent", "why": why}
            warnings.append(f"{x['id']} {x['symbol']} vs {x['hedge']} wasn't sent: {why}.")
            new.remove(x)
    if not new and not live:
        return warnings
    async with make_broker() as b:
        dry = isinstance(b, orders.DryRun)
        if not dry and live:
            await b.cancel({x["id"] for x in live})
            await asyncio.sleep(1)
            await orders._pull_fills(b, live)
        out = []
        for x in live:
            p = x["paper"]
            if p["state"] == "dry run":
                if "exit" in x:
                    p["state"] = "closed"
                continue
            if p["state"] == "entering":
                got = orders._entered(p)
                if not any(got.values()):
                    p["state"] = "unfilled"
                    continue
                p["state"] = "open"
                if not all(got.values()):
                    p["exit_reason"] = "broken: one side never filled"
            if p["state"] == "open" and not p.get("exit_reason"):
                if "exit" in x:
                    p["exit_reason"] = x["exit"]["reason"]
                elif now >= datetime.fromisoformat(x["plan"]["flat_at"]) or sess == "open":
                    p["exit_reason"] = "flat"
            if p.get("exit_reason"):
                held = orders._held(p)
                if not any(held.values()):
                    p["state"], p["result"] = "closed", orders.result(p)
                    continue
                by = {leg["symbol"]: leg for leg in p["legs"]}
                for s, n in held.items():
                    if n <= 0:
                        continue
                    action = "SELL" if by[s]["side"] == "long" else "BUY"
                    o = _order(x, s, action, n, sess, px.get(s), now, books.get(s), exiting=True)
                    if o:
                        out.append(o)
                    else:
                        warnings.append(f"{x['id']}: the exit of {n} {s} waits, with no quote for it from the gateway"
                                        + (f" because {missing[s]}" if missing.get(s) else "")
                                        + ". It is tried again at the next tick and goes at the market at the open.")
                p["state"] = "exiting"
        for x in new:
            legs = orders._legs({**x, "numbers": {"price": px.get(x["symbol"]) or x["numbers"]["price"]},
                                 "hedge_price": px.get(x["hedge"]) or x["numbers"]["hedge_price"]})
            if not legs:
                x["paper"] = {"state": "not sent", "why": "a leg couldn't be sized"}
                continue
            x["paper"] = {"state": "dry run" if dry else "entering", "dry_run": dry, "legs": legs, "session": sess, "warnings": []}
            out += [_order(x, leg["symbol"], "BUY" if leg["side"] == "long" else "SELL", leg["shares"], sess,
                           px.get(leg["symbol"]) or leg["ref_price"], now, books.get(leg["symbol"])) for leg in legs]
        if not out:
            return warnings
        if not dry:
            save()
        sent = await b.place(out)
        for x in [*new, *live]:
            mine = [o for o in sent if o["trade_id"] == x["id"]]
            if mine and "paper" in x:
                x["paper"].setdefault("orders", []).extend({**o, "at": now.strftime("%-I:%M %p")} for o in mine)
                bad = [f"{o['symbol']} {o['status'].lower()}{': ' + o['note'] if o.get('note') else ''}" for o in mine
                       if o["status"] in ("not sent", "Cancelled", "ApiCancelled", "Inactive")]
                if bad:
                    warnings.append(f"{x['id']} {x['symbol']} vs {x['hedge']}: {'; '.join(bad)}.")
    return warnings


# ---- The tick -----------------------------------------------------------------------------------------------
def tick(now: datetime | None = None, decide=model.decide, news=model.headlines, market=None, broker=orders.broker,
         held: set[str] | None = None, dry: bool = False) -> dict:
    """One tick. `dry` is a look: prices are pulled and the model still judges, but nothing is written
    to the journal and no order leaves the machine. A run by hand is dry unless it says otherwise."""
    now = now or datetime.now(NY)
    sess = session_at(now)
    if sess is None:
        return {"skipped": f"{now:%-I:%M %p} ET is outside the after-hours, overnight and pre-market sessions"}
    night = night_of(now)
    doc = load(night)
    close_day = last_close_day(now)
    ctx = context(close_day)
    if not ctx:
        return {"night": night.isoformat(), "skipped": f"daily bars for {close_day} aren't in yet"}
    since = datetime.combine(close_day, time(16, 0), NY)
    seen = {x["symbol"] for x in doc["signals"]}
    open_syms = {s for x in doc["signals"] if "exit" not in x for s in (x["symbol"], x["hedge"])}
    names = sorted(set(ctx) | {c["hedge"] for c in ctx.values()} | open_syms)

    async def run():
        px, m, source = await _prices(names, sess, since, market)
        try:
            for x in doc["signals"]:
                advance(x, px, now)
            hits = []
            if signals_open(sess, now):
                hits = _signals(ctx, px, seen, held if held is not None else _held_elsewhere(night), sess, now, since,
                                getattr(m, "books", {}))
            if hits and source != "Yahoo":
                vol = await m.session_dollars([h["symbol"] for h in hits[:JUDGED_PER_TICK * 2]], sess == "overnight", since)
                for h in hits:
                    v = vol.get(h["symbol"])
                    h["numbers"]["session_dollars"] = round(v) if v is not None else None
                hits = [h for h in hits if h["numbers"].get("session_dollars") is None or h["numbers"]["session_dollars"] >= MIN_SESSION_DOLLARS]
            judged, takes = 0, sum((x.get("verdict") or {}).get("decision") == "take" for x in doc["signals"])
            open_takes = sum((x.get("paper") or {}).get("state") in ("entering", "open", "exiting", "dry run") for x in doc["signals"])
            to_judge = []
            for h in hits:
                if len(to_judge) >= JUDGED_PER_TICK:
                    h["verdict"] = {"decision": "not judged", "why": f"more than {JUDGED_PER_TICK} signals at this tick; the biggest moves went first"}
                elif takes >= MAX_TAKES or open_takes >= MAX_OPEN:
                    h["verdict"] = {"decision": "not judged", "why": f"the book is full ({takes} takes tonight, {open_takes} open)"}
                else:
                    to_judge.append(h)
            await asyncio.to_thread(model.judge_all, to_judge, decide, news)   # researched side by side
            judged = len(to_judge)
            for h in hits:
                if h in to_judge and h["verdict"]["decision"] == "take":
                    if takes >= MAX_TAKES or open_takes >= MAX_OPEN:   # judged together, so a later take can find the book full
                        h["paper"] = {"state": "not sent", "why": f"the book filled at this tick ({takes} takes tonight, {open_takes} open)"}
                    else:
                        open_takes += 1
                    takes += 1
                doc["signals"].append(h)
            save = (lambda: None) if dry else (lambda: _save(night, doc))
            warnings = await trade(doc, sess, px, now, save, (lambda: orders.DryRun()) if dry else broker,
                                   getattr(m, "books", {}), getattr(m, "missing", {}))
            return px, source, hits, judged, warnings
        finally:
            if market is None:
                await m.__aexit__()

    px, source, hits, judged, warnings = asyncio.run(run())
    doc["ticks"].append({"at": now.isoformat(timespec="seconds"), "session": sess, "prices": source, "quoted": len(px),
                         "signals": len(hits), "judged": judged, **({"warnings": warnings} if warnings else {})})
    if not dry:
        _save(night, doc)
    return {"night": night.isoformat(), "dry": dry, "session": sess, "prices": source, "quoted": len(px), "new": hits, "warnings": warnings,
            "paper": [{"id": x["id"], **{k: x["paper"].get(k) for k in ("state", "exit_reason", "result")}} for x in doc["signals"] if "paper" in x]}


def _held_elsewhere(night: date) -> set[str]:
    """Names the swing book holds or is entering early for the coming session."""
    from ..paper import book, early
    out = {leg["symbol"] for t in book.trades() if t["state"] != "closed" for leg in t["legs"]} if book.ROOT.exists() else set()
    return out | {leg["symbol"] for t in (early.load(night) or {}).get("trades", []) for leg in t["legs"]}


def _wide(books: dict, sym: str, price: float, atr_pct: float) -> float | None:
    """The bid-ask spread as a share of price when it is too wide to judge a move by, else None. Outside the
    regular session a book can be several percent wide, and a mid taken from one says nothing about a move."""
    bid, ask = books.get(sym, (None, None))
    if bid is None or not price:
        return None
    spread = (ask - bid) / price
    return spread if spread > MAX_SPREAD_ATR * atr_pct else None


def _signals(ctx: dict, px: dict, seen: set, held: set, sess: str, now: datetime, since: datetime,
             books: dict | None = None) -> list[dict]:
    out = []
    books = books or {}
    for sym, c in ctx.items():
        s, h = px.get(sym), px.get(c["hedge"])
        if sym in ETFS or sym in seen or sym in held or not s or not h or abs(s / c["close"] - 1) > 0.4:
            continue    # a fund has no news of its own: overnight it only moves against SPY by its beta
        wide = _wide(books, sym, s, c["atr_pct"]) or _wide(books, c["hedge"], h, c["atr_pct"])
        if wide:
            continue
        name_r, hedge_r = s / c["close"] - 1, h / c["hedge_close"] - 1
        rel = name_r - c["beta"] * hedge_r
        move = rel / c["atr_pct"]
        if abs(move) < MOVE_ATR:
            continue
        stop = signals.stop_pct(c["atr_pct"])
        out.append({"id": f"X-{now:%m%d-%H%M}-{sym.replace(' ', '')}", "symbol": sym, "hedge": c["hedge"], "beta": c["beta"],
                    "setup": "news_mover", "direction": "long" if move > 0 else "short", "session": sess,
                    "signal_at": f"{now:%-I:%M %p} ET ({sess})",
                    "numbers": {"price": round(s, 2), "hedge_price": round(h, 2), "since_close_pct": round(name_r * 100, 2),
                                "hedge_since_close_pct": round(hedge_r * 100, 2), "move_atr": round(move, 2),
                                "daily_atr_pct": round(c["atr_pct"] * 100, 2), "last_close": round(c["close"], 2)},
                    "entry": {"at": f"{now:%-I:%M %p}", "name": s, "hedge": h},
                    "plan": {"stop_pct": round(-stop * 100, 2), "target_pct": round(stop * signals.TARGET_ATR / signals.STOP_ATR * 100, 2),
                             "flat_by": f"{_clock(SESSIONS[sess][3])} ({sess})", "flat_at": flat_by(sess, now).isoformat()}})
    return sorted(out, key=lambda x: -abs(x["numbers"]["move_atr"]))


def held_now(night: date) -> dict[str, float]:
    """symbol -> signed shares the extended-hours book holds, for the swing book's position check."""
    p = path(night)
    if not p.exists():
        return {}
    out: dict[str, float] = {}
    for x in json.loads(p.read_text())["signals"]:
        pp = x.get("paper") or {}
        if pp.get("dry_run") or "legs" not in pp:
            continue
        sides = {leg["symbol"]: 1 if leg["side"] == "long" else -1 for leg in pp["legs"]}
        for s, n in orders._held(pp).items():
            if n:
                out[s] = out.get(s, 0) + sides[s] * n
    return out


def report() -> dict:
    """Every night so far: what the takes, the passes and the unjudged signals made by the same rules."""
    groups: dict[str, list[float]] = {"take": [], "pass": [], "undecided": [], "not judged": []}
    files = sorted(root().glob("*.json")) if root().exists() else []
    for f in files:
        for x in json.loads(f.read_text())["signals"]:
            if "result_bp" in (x.get("exit") or {}):
                groups[(x.get("verdict") or {}).get("decision", "not judged")].append(x["exit"]["result_bp"] / 1e4)
    return {"nights": len(files), **{k: replay._stats(pd.Series(v, dtype=float), max(len(files), 1)) for k, v in groups.items()}}
