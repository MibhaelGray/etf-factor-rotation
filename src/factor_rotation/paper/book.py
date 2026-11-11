"""The paper book's jobs and records, in journal/paper/:

    proposals/<session>.json  the research pass's verdict on each candidate (proposals.py); only takes are traded
    reviews/<session>.json    the pre-close review: hold, exit early or extend, per open automatic trade (learn.py)
    plans/<session>.json   write-once at 9:15 AM ET: the trades taken, the ones skipped and why, the orders sent
    checks/<session>.json  write-once at 9:40 AM ET: entries that never filled, and broken trades closed on the spot
    exits/<session>.json   write-once at 3:45 PM ET: each trade leaving at the close, why, and the orders sent
    fills/<session>.json   the day's fills, commissions, positions and account value from the paper account
    HALT                   the kill switch: while it exists no new trades open (exits still go out)

A trade's state comes from these files: planned, open once its entry fills are in, closing once an exit is
sent, closed once the exit fills are in. Results are in bp of one side's notional, next to a model result
from daily bars (the same shares, first open to last close), so the difference is the cost of real fills.

Each job's file carries a `warnings` list in plain words (a failed price pull, an order that wasn't accepted,
a stop that couldn't be checked); `alerts()` hands the latest to the dashboard and the report.
"""

import asyncio
import json
import logging
from datetime import date, datetime, time
from pathlib import Path
from time import sleep

import pandas as pd

from .. import bars, journal, storage
from ..config import NY
from . import proposals, rules
from .broker import WORKING, DryRun, GuardError, IBPaper, PaperSettings, unref

log = logging.getLogger(__name__)
ROOT = journal.JOURNAL / "paper"
HALT = ROOT / "HALT"
# Price pulls: (tries, seconds between). The Mac may have just woken for the job with no network yet. The
# paper gateway can't stand in: it has no market data while the live session is logged in.
OPEN_QUOTES = (4, 20)      # the 9:15 AM job must send by 9:27 AM
CLOSE_QUOTES = (3, 15)     # the 3:45 PM job must send by 3:49 PM
# An order that will never fill. A market-on-close order sent before the close sits at "PendingSubmit", so
# that status is no cause for alarm; a message from the gateway always is worth reading.
DEAD = {"Cancelled", "ApiCancelled", "Inactive", "not sent"}


def _read(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, default=journal._json_default) + "\n")
    return path


def _all(kind: str) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((ROOT / kind).glob("*.json"))]


# ---- Prices -----------------------------------------------------------------------------------------------
def _yahoo(symbols: list[str]) -> dict[str, float]:
    """One pull of Yahoo's 1-minute bars, pre-market included: today's latest price for each symbol it has.
    A bar from an earlier day is no quote, so a name that hasn't traded yet today is left out."""
    import yfinance as yf

    tickers = {bars._ticker(s): s for s in symbols}
    try:
        data = yf.download(list(tickers), period="1d", interval="1m", prepost=True, group_by="ticker", progress=False, threads=True)
    except Exception as e:  # no network yet, or Yahoo refusing
        log.warning("quotes_failed error=%r", e)
        return {}
    today = datetime.now(NY).date()
    out = {}
    for t, s in tickers.items():
        try:
            col = data[t]["Close"]
        except KeyError:
            try:
                col = data["Close"]
            except KeyError:
                continue
        if isinstance(col, pd.DataFrame):
            col = col.iloc[:, 0]
        col = col.dropna()
        if not len(col):
            continue
        stamp = col.index[-1]
        if getattr(stamp, "tzinfo", None) is not None and stamp.tz_convert(NY).date() != today:
            continue
        out[s] = float(col.iloc[-1])
    return out


def quotes(symbols: list[str], tries: int = 1, wait: float = 0) -> dict[str, float]:
    """The latest price today for each symbol, asking again for the ones still missing up to `tries` times."""
    out: dict[str, float] = {}
    for attempt in range(tries):
        missing = [s for s in symbols if s not in out]
        if not missing:
            break
        if attempt:
            log.warning("quotes_retry attempt=%d missing=%d", attempt + 1, len(missing))
            sleep(wait)
        out.update(_yahoo(missing))
    return out


def _order_warnings(orders: list[dict]) -> list[str]:
    out = []
    for o in orders:
        order = f"{o['trade_id']}: {o['action']} {o['shares']} {o['symbol']}"
        if o["status"] in DEAD:
            out.append(f"{order} was not accepted ({o['status']}{': ' + o['note'] if o.get('note') else ''}).")
        elif o.get("note"):
            out.append(f"{order} went in with a message from the gateway: {o['note']}")
    return out


def _closes(symbols: list[str]) -> dict[str, float]:
    df = bars.load(symbols)
    if df.empty:
        return {}
    return df.sort_values("date").groupby("symbol")["close"].last().to_dict()


# ---- State ------------------------------------------------------------------------------------------------
def _fills_by_ref() -> dict[tuple[str, str], list[dict]]:
    out: dict[tuple[str, str], list[dict]] = {}
    seen = set()  # the gateway can hand back an earlier day's execution; count each once, on its first day
    for day in _all("fills"):
        for f in day["fills"]:
            key = unref(f["order_ref"])
            if key and f["exec_id"] not in seen:
                seen.add(f["exec_id"])
                out.setdefault(key, []).append({**f, "session": day["session"]})
    return out


def _avg(fills: list[dict]) -> tuple[int, float | None, float]:
    shares = sum(f["shares"] for f in fills)
    px = sum(f["shares"] * f["price"] for f in fills) / shares if shares else None
    return int(round(shares)), px, sum(f.get("commission") or 0 for f in fills)


def trades() -> list[dict]:
    """Every paper trade with its fills, exit and state, oldest first."""
    fills = _fills_by_ref()
    exits = {e["trade_id"]: {**e, "session": d["session"]} for d in [*_all("checks"), *_all("exits")] for e in d.get("exits", [])}
    synced = {d["session"] for d in _all("fills")}
    longer = {e["trade_id"]: e["sessions"] for d in _all("exits") for e in d.get("extended", [])}
    out = []
    for plan in _all("plans"):
        for t in plan["trades"]:
            t = {**t, "planned_horizon_days": t["horizon_days"], "horizon_days": t["horizon_days"] + longer.get(t["id"], 0),
                 "extended": t["id"] in longer}
            held, out_legs, commission = [], [], 0.0
            for leg in t["legs"]:
                entry_side, exit_side = ("BOT", "SLD") if leg["side"] == "long" else ("SLD", "BOT")
                fs = fills.get((t["id"], leg["symbol"]), [])
                n_in, px_in, c_in = _avg([f for f in fs if f["side"] == entry_side])
                n_out, px_out, c_out = _avg([f for f in fs if f["side"] == exit_side])
                commission += c_in + c_out
                if n_in:
                    held.append({"symbol": leg["symbol"], "side": leg["side"], "shares": n_in, "entry_price": px_in,
                                 "exit_shares": n_out, "exit_price": px_out})
                out_legs.append(leg["symbol"])
            ex = exits.get(t["id"])
            if ex and held and all(h["exit_shares"] >= h["shares"] for h in held):
                state = "closed"
            elif ex:
                state = "closing"
            elif held:
                state = "open"
            elif t["entry_date"] in synced:
                state = "unfilled"
            else:
                state = "planned"
            out.append({**t, "held": held, "state": state, "exit": ex, "commission": round(commission, 2)})
    return out


def open_trades() -> list[dict]:
    return [t for t in trades() if t["state"] == "open"]


def _session(t: dict, today: date) -> int:
    days = journal.sessions_from(date.fromisoformat(t["entry_date"]), 30)
    return days.index(today) + 1 if today in days else len(days) + 1


# ---- Jobs -------------------------------------------------------------------------------------------------
def _broker(readonly: bool = False):
    s = PaperSettings()
    return IBPaper(s, readonly=readonly) if s.send or readonly else DryRun(s)


async def open_session(today: date, broker=None) -> dict:
    """Plan today's entries and send them as market-on-open orders."""
    path = ROOT / "plans" / f"{today}.json"
    if path.exists():
        raise FileExistsError(f"{path} exists; today's entries already went out")
    s = PaperSettings()
    dry = broker is None and not s.send
    doc = proposals.load(today)
    props = doc["proposals"] if doc else []
    held = open_trades()
    plan = {"session": today.isoformat(), "built_at": datetime.now(NY).isoformat(), "screen": doc and doc["screen"],
            "proposals": {v: sum(p["decision"] == v for p in props) for v in proposals.DECISIONS} if doc else None,
            "dry_run": dry, "trades": [], "skipped": [], "orders": [], "prices": {}, "warnings": []}
    if HALT.exists():
        plan["halted"] = HALT.read_text().strip() or "halted"
    else:
        symbols = {x for p in props if p["decision"] == "take" for x in [*p["long"], *p["short"]]}
        live = quotes(sorted(symbols), *OPEN_QUOTES)
        prices = {**_closes(sorted(symbols)), **live}
        takes, take_skip = rules.proposal_trades(props, today.isoformat(), prices, {s for t in held for s in rules.core_symbols(t)})
        if doc is None:
            plan["warnings"].append(f"No research proposals were recorded for {today}, so the book took nothing.")
        kept, cap_skip = rules.apply_caps(takes, held)
        plan["trades"], plan["skipped"] = kept, [*take_skip, *cap_skip]
        used = sorted({leg["symbol"] for t in kept for leg in t["legs"]})
        stale = [x for x in used if x not in live]
        plan["prices"] = {"quoted": len(used) - len(stale), "last_close": stale}
        if used and len(stale) == len(used):
            plan["warnings"].append(f"The pre-market price pull failed after {OPEN_QUOTES[0]} tries, so all {len(kept)} trades were sized on the last close.")
        elif stale:
            plan["warnings"].append(f"No pre-market price for {', '.join(stale)}, so those legs were sized on the last close.")
        orders = [{"trade_id": t["id"], "symbol": leg["symbol"], "action": "BUY" if leg["side"] == "long" else "SELL",
                   "shares": leg["shares"], "kind": "MOO"} for t in kept for leg in t["legs"]]
        if orders:
            async with (broker or _broker()) as b:
                # Connected, so orders are about to go: the plan is on disk first, and if the job dies while
                # sending, a rerun is refused rather than sending the same orders twice.
                if not dry:
                    _write(path, plan)
                plan["orders"] = await b.place(orders)
            plan["warnings"] += _order_warnings(plan["orders"])
    if not dry:
        _write(path, plan)
    return plan


async def sync(today: date, broker=None) -> dict:
    """Pull today's fills, positions and account value; keep fills already stored."""
    async with (broker or IBPaper(readonly=True)) as b:
        return await _pull(b, today)


async def _pull(b, today: date) -> dict:
    """`sync` on a connection that is already open."""
    path = ROOT / "fills" / f"{today}.json"
    new = await b.fills()
    positions = await b.positions()
    nav = await b.net_liquidation()
    old = (_read(path) or {}).get("fills", [])
    seen = {f["exec_id"] for f in new}
    payload = {"session": today.isoformat(), "pulled_at": datetime.now(NY).isoformat(),
               "fills": [*[f for f in old if f["exec_id"] not in seen], *new], "positions": positions, "net_liquidation": nav}
    _write(path, payload)
    payload["mismatches"] = reconcile(positions)
    return payload


def _gaps(positions: dict[str, float]) -> dict[str, tuple[float, float]]:
    """symbol -> (account, books) wherever the account's position differs from what the books think they hold."""
    from ..intraday import orders as intraday   # the intraday book shares the account; it is flat by 3:45 PM

    expected: dict[str, float] = dict(intraday.held_now(datetime.now(NY).date()))
    for t in trades():
        for h in t["held"]:
            sign = 1 if h["side"] == "long" else -1
            expected[h["symbol"]] = expected.get(h["symbol"], 0) + sign * (h["shares"] - h["exit_shares"])
    names = {s for s, v in expected.items() if v} | set(positions)
    return {s: (positions.get(s, 0), expected.get(s, 0)) for s in sorted(names)
            if abs(positions.get(s, 0) - expected.get(s, 0)) > 1e-6}


def reconcile(positions: dict[str, float]) -> list[str]:
    """Differences between the account's positions and what the books think they hold."""
    return [f"{s}: account {a:g}, books {b:g}" for s, (a, b) in _gaps(positions).items()]


async def check_session(today: date, broker=None) -> dict:
    """A few minutes after the open: pull the fills and see what each entry order did. A trade with no fills
    wasn't taken. A trade with some legs filled and others dead is broken: its filled legs are closed now
    with plain market orders, so half a pair doesn't sit unhedged until the close."""
    path = ROOT / "checks" / f"{today}.json"
    if path.exists():
        raise FileExistsError(f"{path} exists; today's post-open check already ran")
    plan = _read(ROOT / "plans" / f"{today}.json")
    s = PaperSettings()
    dry = broker is None and not s.send
    out = {"session": today.isoformat(), "built_at": datetime.now(NY).isoformat(), "dry_run": dry, "unfilled": [],
           "waiting": [], "exits": [], "orders": [], "order_status": [], "warnings": []}
    if dry or not plan or not plan["trades"]:
        out["note"] = "dry run: nothing checked" if dry else f"no entries were sent for {today}"
        return out
    if not plan.get("orders"):
        out["warnings"].append("The plan has trades but no order statuses, so the entry job stopped while sending. "
                               "Legs that filled are handled here; the rest were never sent.")
    async with (broker or _broker()) as b:
        await _pull(b, today)
        status = {(o["trade_id"], o["symbol"], o["action"]): o["status"] for o in await b.orders()}
        now = datetime.now(NY).strftime("%-I:%M %p")
        orders = []
        for t in trades():
            if t["entry_date"] != today.isoformat() or t["exit"]:
                continue
            filled = {h["symbol"] for h in t["held"]}
            dead = {leg["symbol"]: status.get((t["id"], leg["symbol"], "BUY" if leg["side"] == "long" else "SELL"), "not found")
                    for leg in t["legs"] if leg["symbol"] not in filled}
            if not dead:
                continue
            out["order_status"] += [{"trade_id": t["id"], "symbol": x, "status": st} for x, st in dead.items()]
            what = ", ".join(f"{x} ({st.lower()})" for x, st in sorted(dead.items()))
            if any(st in WORKING for st in dead.values()):
                out["waiting"].append({"trade_id": t["id"], "symbols": sorted(dead)})
                out["warnings"].append(f"{t['id']} {t['name']}: the order for {what} is still working, perhaps a delayed open, "
                                       "so nothing was closed. The 3:45 PM ET check handles it.")
            elif not filled:
                out["unfilled"].append({"trade_id": t["id"], "name": t["name"], "symbols": sorted(dead)})
                out["warnings"].append(f"{t['id']} {t['name']}: no leg filled at the open ({what}), so the trade wasn't taken.")
            else:
                out["exits"].append({"trade_id": t["id"], "book": t["book"], "session_number": 1, "intraday": True,
                                     "reason": f"broken: {', '.join(sorted(dead))} never filled at the open; the rest closed at {now} ET"})
                out["warnings"].append(f"{t['id']} {t['name']}: {what} never filled at the open, so the filled side was closed at {now} ET.")
                orders += [{"trade_id": t["id"], "symbol": h["symbol"], "action": "SELL" if h["side"] == "long" else "BUY",
                            "shares": h["shares"] - h["exit_shares"], "kind": "MKT"} for h in t["held"] if h["shares"] > h["exit_shares"]]
        if orders:
            _write(path, out)  # on disk before anything is sent, as in open_session
            out["orders"] = await b.place(orders)
            out["warnings"] += _order_warnings(out["orders"])
            await asyncio.sleep(3)
            await _pull(b, today)  # the closing fills, so the trade shows as closed straight away
    _write(path, out)
    return out


async def close_session(today: date, flatten: bool = False, broker=None) -> dict:
    """Check each open trade at 3:45 PM ET and send market-on-close exits for those leaving today."""
    path = ROOT / "exits" / f"{today}.json"
    if path.exists():
        raise FileExistsError(f"{path} exists; today's exits already went out")
    s = PaperSettings()
    dry = broker is None and not s.send
    exits, extended, retried, unchecked, orders, warnings = [], [], [], [], [], []
    review = {} if flatten else proposals.reviews(today)
    moc = lambda t: [{"trade_id": t["id"], "symbol": h["symbol"], "action": "SELL" if h["side"] == "long" else "BUY",  # noqa: E731
                      "shares": h["shares"] - h["exit_shares"], "kind": "MOC"} for h in t["held"] if h["shares"] > h["exit_shares"]]
    async with (broker or _broker()) as b:
        off: set[str] = set()
        if not dry:
            synced = await _pull(b, today)  # today's entry fills, so day-one stops and broken pairs are seen
            off = set(_gaps(synced["positions"]))
        held = open_trades()
        stop_of = lambda t: t.get("stop") or {}  # noqa: E731
        symbols = sorted({h["symbol"] for t in held for h in t["held"]} | {r["symbol"] for t in held for r in stop_of(t).get("rules", [])})
        prices = await asyncio.to_thread(quotes, symbols, *CLOSE_QUOTES) if symbols else {}
        for t in held:
            k = _session(t, today)
            reason = "flatten: every trade closed by hand" if flatten else rules.exit_reason(t, k, t["held"], prices)
            # Stops, broken pairs and the time limit come first. The review only adds an earlier exit, or lets
            # a trade at its time limit run on if it is in profit and hasn't been extended before.
            r = review.get(t["id"]) or {}
            if r.get("action") == "exit" and not reason:
                reason = f"review: {r['reason']}"
            elif r.get("action") == "extend" and reason and reason.startswith("time limit"):
                pnl = rules.spread_return(t["held"], prices)
                if pnl is not None and pnl > 0 and not t["extended"]:
                    extended.append({"trade_id": t["id"], "sessions": r["sessions"], "reason": r["reason"]})
                    reason = None
                else:
                    reason += "; the extension asked for was refused: " + ("no price to show it's in profit" if pnl is None else "it isn't in profit" if pnl <= 0 else "it was extended before")
            if not reason:
                need = ({h["symbol"] for h in t["held"]} if "spread_pct" in stop_of(t) else set()) | {r["symbol"] for r in stop_of(t).get("rules", [])}
                if need - set(prices):
                    unchecked.append(t["id"])
                continue
            exits.append({"trade_id": t["id"], "book": t["book"], "session_number": k, "reason": reason})
            orders += moc(t)
        # An exit sent earlier whose fills never arrived leaves its trade "closing" for good. Send it again, but
        # only if the account still holds what the books say: if it doesn't, the fills were missed, not the exit.
        for t in ([] if dry else [t for t in trades() if t["state"] == "closing"]):
            if any(h["symbol"] in off for h in t["held"]):
                warnings.append(f"{t['id']} {t['name']}: the exit sent {t['exit']['session']} shows no fills, but the account's positions "
                                "don't match the books, so it wasn't sent again. Run fr paper status.")
                continue
            retried.append({"trade_id": t["id"], "reason": f"the exit sent {t['exit']['session']} never filled; sent again"})
            warnings.append(f"{t['id']} {t['name']}: the exit sent {t['exit']['session']} never filled, so it was sent again for today's close.")
            orders += moc(t)
        if unchecked:
            missing = sorted(set(symbols) - set(prices))
            warnings.append(f"No price for {', '.join(missing)} after {CLOSE_QUOTES[0]} tries, so the stop on {len(unchecked)} "
                            f"trade{'' if len(unchecked) == 1 else 's'} wasn't checked today: {', '.join(unchecked)}.")
        payload = {"session": today.isoformat(), "built_at": datetime.now(NY).isoformat(), "dry_run": dry, "prices": prices,
                   "exits": exits, "extended": extended, "retried": retried, "stops_not_checked": unchecked, "reviewed": sorted(review),
                   "orders": [], "warnings": warnings}
        if orders:
            if not dry:
                _write(path, payload)  # on disk before anything is sent, as in open_session
            payload["orders"] = await b.place(orders)
            payload["warnings"] += _order_warnings(payload["orders"])
    if not dry:
        _write(path, payload)
    return payload


def alerts() -> list[str]:
    """The warnings from the latest session's jobs, in the order the jobs ran."""
    plans = sorted((ROOT / "plans").glob("*.json"))
    if not plans:
        return []
    return [w for kind in ("plans", "checks", "exits") for w in (_read(ROOT / kind / plans[-1].name) or {}).get("warnings", [])]


# ---- Results ----------------------------------------------------------------------------------------------
def rows() -> list[dict]:
    """One row per trade for the dashboard and the report."""
    ts = trades()
    symbols = sorted({leg["symbol"] for t in ts for leg in t["legs"]})
    px = bars.load(symbols) if symbols else pd.DataFrame()
    bar = {(r.symbol, r.date.date()): r for r in px.itertuples()} if not px.empty else {}
    last = {}
    if not px.empty:
        latest = px.sort_values("date").groupby("symbol").last()
        last = {s: (r.close, r.date.date()) for s, r in latest.iterrows()}
    today = datetime.now(NY).date()
    out = []
    for t in ts:
        pnl = model = room = None
        days = journal.sessions_from(date.fromisoformat(t["entry_date"]), t["horizon_days"])
        missing = sorted({leg["symbol"] for leg in t["legs"]} - {h["symbol"] for h in t["held"]}) if t["held"] else []
        stop_pct = (t.get("stop") or {}).get("spread_pct")
        detail = []
        if t["held"]:
            closed = t["state"] == "closed"
            for h in t["held"]:
                close, on = last.get(h["symbol"], (None, None))
                # A close from before the entry says nothing about this trade: it is still at its fill.
                px = h["exit_price"] if closed else close if close is not None and on >= date.fromisoformat(t["entry_date"]) else h["entry_price"]
                detail.append({"symbol": h["symbol"], "side": h["side"], "shares": h["shares"], "entry": h["entry_price"], "last": px,
                               "pnl": round((1 if h["side"] == "long" else -1) * h["shares"] * (px - h["entry_price"]), 2)})
            gross = sum(leg["pnl"] for leg in detail)
            pnl = gross - t["commission"]
            if not closed and not missing and stop_pct is not None:
                room = round(gross / t["notional"] * 100 - stop_pct, 1)
            if closed and not t["exit"].get("intraday"):  # the model is open to close; a trade closed at 9:40 AM has none
                d0, d1 = date.fromisoformat(t["entry_date"]), date.fromisoformat(t["exit"]["session"])
                try:
                    model = sum((1 if h["side"] == "long" else -1) * h["shares"] *
                                (bar[(h["symbol"], d1)].close - bar[(h["symbol"], d0)].open) for h in t["held"])
                except KeyError:
                    model = None
        n = t["notional"]
        out.append({
            "id": t["id"], "book": t["book"], "name": t["name"], "setup": t["setup"], "direction": t["direction"],
            "entry_date": t["entry_date"], "exit_date": (t["exit"] or {}).get("session"), "state": t["state"],
            "horizon_days": t["horizon_days"], "stop": journal.stop_text(t) or "—", "reason": (t["exit"] or {}).get("reason"),
            "legs": ", ".join(f"{'+' if l['side'] == 'long' else '−'}{l['shares']} {l['symbol']}" for l in t["legs"]),
            "pnl": None if pnl is None else round(pnl, 2), "bps": None if pnl is None else round(pnl / n * 1e4, 1),
            "model_bps": None if model is None else round(model / n * 1e4, 1),
            "slippage_bps": None if model is None or pnl is None else round((pnl - model) / n * 1e4, 1),
            "commission": t["commission"], "why": t["why"], "source": t["source"],
            "proposal_id": t.get("proposal_id"), "conviction": t.get("conviction"), "wrong_if": t.get("wrong_if"),
            "planned_horizon_days": t["planned_horizon_days"], "extended": t["extended"],
            "leg_detail": detail, "missing": missing, "session_number": sum(d <= today for d in days), "exit_by": days[-1].isoformat(),
            "stop_pct": stop_pct, "stop_room_pct": room,
        })
    return out


# ---- Profit and loss over time ------------------------------------------------------------------------------
def _book_fills() -> list[dict]:
    """Every fill on one of the book's orders with its session and time, oldest first."""
    out = []
    for fs in _fills_by_ref().values():
        for f in fs:
            d = date.fromisoformat(f["session"])
            at = datetime.fromisoformat(f["time"]).astimezone(NY) if f.get("time") else datetime.combine(d, time(9, 30), NY)
            out.append({**f, "session": d, "at": at})
    return sorted(out, key=lambda f: f["at"])


def _pnl_of(fills: list[dict], mark) -> float:
    """Cash from these fills after commissions, plus what is still held at `mark(symbol, its latest fill)`."""
    cash, held, latest = 0.0, {}, {}
    for f in fills:
        sign = 1 if f["side"] == "BOT" else -1
        cash -= sign * f["shares"] * f["price"] + (f.get("commission") or 0)
        held[f["symbol"]] = held.get(f["symbol"], 0.0) + sign * f["shares"]
        latest[f["symbol"]] = f
    return round(cash + sum(q * mark(s, latest[s]) for s, q in held.items() if q), 2)


def pnl_by_session() -> list[dict]:
    """The book's running profit and loss after commissions at each session's close, starting from a zero on
    the session before its first fill. `final` is False until that session's bars have been pulled after the close."""
    fills = _book_fills()
    if not fills:
        return []
    px = bars.load(sorted({"SPY", *(f["symbol"] for f in fills)}))
    closes = {s: g.assign(day=g["date"].dt.date).set_index("day")["close"].sort_index() for s, g in px.groupby("symbol")} if not px.empty else {}
    today = datetime.now(NY).date()
    first = fills[0]["session"]
    traded = {f["session"] for f in fills}
    spy = set(closes["SPY"].index) if "SPY" in closes else set()
    days = [d for d in journal.sessions_from(first, (today - first).days + 1) if d <= today and (d in spy or d in traded)]
    out = [{"date": journal.previous_trading_day(first).isoformat(), "pnl": 0.0, "final": True}]
    for d in days:
        def mark(symbol: str, latest: dict) -> float:
            s = closes.get(symbol)
            s = s[s.index <= d] if s is not None else None
            return float(s.iloc[-1]) if s is not None and len(s) and s.index[-1] >= latest["session"] else latest["price"]
        out.append({"date": d.isoformat(), "pnl": _pnl_of([f for f in fills if f["session"] <= d], mark),
                    "final": d in spy and journal._bars_settled(d)})
    return out


def pnl_intraday(sessions: int = 5) -> dict:
    """The book's running profit and loss at the end of each 15-minute bar of the last `sessions` sessions, from
    Yahoo and not stored. `missing` lists held names Yahoo gave no bars for; they are marked at their last fill."""
    fills = _book_fills()
    if not fills:
        return {"points": [], "missing": []}
    symbols = {f["symbol"] for f in fills}
    closes = bars.intraday_closes(sorted({"SPY", *symbols}), sessions)
    times = sorted({t for s in closes.values() for t in s.index})
    last_bar = {}
    for t in times:
        last_bar[t.date()] = t
    bar = pd.Timedelta(minutes=15)
    now = pd.Timestamp(datetime.now(NY))
    points, missing = [], set()
    for t in times:
        # The closing auction prints just after the last bar ends, so the session's last point counts all its fills.
        cutoff = datetime.combine(t.date(), time(23, 59), NY) if t == last_bar[t.date()] else t + bar
        done = [f for f in fills if f["at"] <= cutoff]
        if not done:
            continue

        def mark(symbol: str, latest: dict) -> float:
            s = closes.get(symbol)
            if s is None:
                missing.add(symbol)
                return latest["price"]
            s = s[s.index <= t]
            return float(s.iloc[-1]) if len(s) and s.index[-1] + bar >= latest["at"] else latest["price"]
        points.append({"t": min(t + bar, now).isoformat(), "session": t.date().isoformat(), "pnl": _pnl_of(done, mark)})
    return {"points": points, "missing": sorted(missing)}


def marks_info() -> dict:
    """When the prices marking open trades were pulled, and whether that was during the session."""
    pulled = storage.modified(bars.bars_path("SPY"))
    if pulled is None:
        return {"marks_at": None, "marks_intraday": False}
    p = pulled.astimezone(NY)
    return {"marks_at": p.isoformat(), "marks_intraday": journal.is_trading_day(p.date()) and time(9, 30) <= p.time() < time(16, 0)}


def symbols_held() -> set[str]:
    """Every leg of a trade that hasn't closed, so a dashboard refresh marks them all, and every leg of a
    proposal from the last month, taken or passed, so its plan can be scored on daily bars."""
    since = (datetime.now(NY).date() - pd.Timedelta(days=31)).isoformat()
    recent = {x for p in proposals.every() if p["session"] >= since for x in [*p["long"], *p["short"]]}
    return {leg["symbol"] for t in trades() if t["state"] != "closed" for leg in t["legs"]} | recent


def summary_text() -> str:
    rs = rows()
    if not rs:
        return "No paper trades yet."
    g = pd.DataFrame(rs)
    closed = g[g["state"] == "closed"]
    slip = closed["slippage_bps"].dropna()
    return (f"{len(g)} trades, {int((g['state'] == 'open').sum())} open, {len(closed)} closed"
            + (f", hit {(closed['pnl'] > 0).mean():.0%}, P&L ${closed['pnl'].sum():,.0f}, avg {closed['bps'].mean():+.0f} bp" if len(closed) else "")
            + (f", fills vs model {slip.mean():+.1f} bp over {len(slip)}" if len(slip) else ""))
