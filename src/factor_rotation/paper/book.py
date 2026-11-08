"""The paper books' jobs and records, in journal/paper/:

    plans/<session>.json   write-once at 9:22 AM ET: the trades taken, the ones skipped and why, the orders sent
    exits/<session>.json   write-once at 3:45 PM ET: each trade leaving at the close, why, and the orders sent
    fills/<session>.json   the day's fills, commissions, positions and account value from the paper account
    HALT                   the kill switch: while it exists no new trades open (exits still go out)

A trade's state comes from these files: planned, open once its entry fills are in, closing once an exit is
sent, closed once the exit fills are in. Results are in bp of one side's notional, next to a model result
from daily bars (the same shares, first open to last close), so the difference is the cost of real fills.
"""

import json
import logging
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from .. import bars, journal
from ..config import NY
from . import rules
from .broker import DryRun, GuardError, IBPaper, PaperSettings, unref

log = logging.getLogger(__name__)
ROOT = journal.JOURNAL / "paper"
HALT = ROOT / "HALT"


def _read(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, default=journal._json_default) + "\n")
    return path


def _all(kind: str) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((ROOT / kind).glob("*.json"))]


# ---- Prices -----------------------------------------------------------------------------------------------
def quotes(symbols: list[str]) -> dict[str, float]:
    """The latest price for each symbol from Yahoo's 1-minute bars, pre-market included."""
    import yfinance as yf

    if not symbols:
        return {}
    tickers = {bars._ticker(s): s for s in symbols}
    data = yf.download(list(tickers), period="1d", interval="1m", prepost=True, group_by="ticker", progress=False, threads=True)
    out = {}
    for t, s in tickers.items():
        try:
            col = data[t]["Close"] if len(tickers) > 1 else data["Close"]
            col = col.squeeze().dropna()
            if len(col):
                out[s] = float(col.iloc[-1])
        except KeyError:
            continue
    return out


def _closes(symbols: list[str]) -> dict[str, float]:
    df = bars.load(symbols)
    if df.empty:
        return {}
    return df.sort_values("date").groupby("symbol")["close"].last().to_dict()


# ---- State ------------------------------------------------------------------------------------------------
def _fills_by_ref() -> dict[tuple[str, str], list[dict]]:
    out: dict[tuple[str, str], list[dict]] = {}
    for day in _all("fills"):
        for f in day["fills"]:
            key = unref(f["order_ref"])
            if key:
                out.setdefault(key, []).append({**f, "session": day["session"]})
    return out


def _avg(fills: list[dict]) -> tuple[int, float | None, float]:
    shares = sum(f["shares"] for f in fills)
    px = sum(f["shares"] * f["price"] for f in fills) / shares if shares else None
    return int(round(shares)), px, sum(f.get("commission") or 0 for f in fills)


def trades() -> list[dict]:
    """Every paper trade with its fills, exit and state, oldest first."""
    fills = _fills_by_ref()
    exits = {e["trade_id"]: {**e, "session": d["session"]} for d in _all("exits") for e in d["exits"]}
    synced = {d["session"] for d in _all("fills")}
    out = []
    for plan in _all("plans"):
        for t in plan["trades"]:
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


def _screen_for(session: date) -> dict | None:
    for p in sorted((journal.JOURNAL / "screens").glob("*.json"), reverse=True):
        d = json.loads(p.read_text())
        if d.get("next_session") == session.isoformat():
            return d
    return None


def _candidate_symbols(screen: dict) -> set[str]:
    out = set()
    for c in screen.get("candidates", []):
        out |= {*(c.get("members") or []), c.get("long"), c.get("short"), c.get("hedge"), c["name"] if c["kind"] == "stock" else None}
    return {s for s in out if s}


async def open_session(today: date, broker=None) -> dict:
    """Plan today's entries and send them as market-on-open orders."""
    path = ROOT / "plans" / f"{today}.json"
    if path.exists():
        raise FileExistsError(f"{path} exists; today's entries already went out")
    s = PaperSettings()
    dry = broker is None and not s.send
    screen = _screen_for(today)
    ideas = []
    for ideas_dir in ("ideas", "morning/ideas"):
        d = _read(journal.JOURNAL / ideas_dir / f"{today}.json")
        ideas += (d or {}).get("ideas", [])
    held = open_trades()
    plan = {"session": today.isoformat(), "built_at": datetime.now(NY).isoformat(), "screen": screen and screen["as_of"],
            "dry_run": dry, "trades": [], "skipped": [], "orders": []}
    if HALT.exists():
        plan["halted"] = HALT.read_text().strip() or "halted"
    else:
        symbols = (_candidate_symbols(screen) if screen else set()) | {x for i in ideas for x in journal._legs(i)}
        live = quotes(sorted(symbols))
        prices = {**_closes(sorted(symbols)), **live}
        sel, sel_skip = rules.selective_trades(ideas, today.isoformat(), prices, journal._checks())
        for t in list(sel):
            why = rules.skip_at_open(t, live)
            if why:
                sel.remove(t)
                sel_skip.append({"name": t["id"], "reason": why})
        dyn_held = {s for t in held if t["book"] == "dynamic" for s in rules.core_symbols(t)}
        dyn, dyn_skip = rules.dynamic_trades(screen, today.isoformat(), prices, dyn_held) if screen else ([], [{"name": "screen", "reason": f"no screen for {today}"}])
        kept, cap_skip = rules.apply_caps([*sel, *dyn], held)
        plan["trades"], plan["skipped"] = kept, [*sel_skip, *dyn_skip, *cap_skip]
        orders = [{"trade_id": t["id"], "symbol": leg["symbol"], "action": "BUY" if leg["side"] == "long" else "SELL",
                   "shares": leg["shares"], "kind": "MOO"} for t in kept for leg in t["legs"]]
        if orders:
            async with (broker or _broker()) as b:
                plan["orders"] = await b.place(orders)
    if not dry:
        _write(path, plan)
    return plan


async def sync(today: date, broker=None) -> dict:
    """Pull today's fills, positions and account value; keep fills already stored."""
    path = ROOT / "fills" / f"{today}.json"
    async with (broker or IBPaper(readonly=True)) as b:
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


def reconcile(positions: dict[str, float]) -> list[str]:
    """Differences between the account's positions and what the books think they hold."""
    expected: dict[str, float] = {}
    for t in trades():
        for h in t["held"]:
            sign = 1 if h["side"] == "long" else -1
            expected[h["symbol"]] = expected.get(h["symbol"], 0) + sign * (h["shares"] - h["exit_shares"])
    names = {s for s, v in expected.items() if v} | set(positions)
    return [f"{s}: account {positions.get(s, 0):g}, books {expected.get(s, 0):g}" for s in sorted(names)
            if abs(positions.get(s, 0) - expected.get(s, 0)) > 1e-6]


async def close_session(today: date, flatten: bool = False, broker=None) -> dict:
    """Check each open trade at 3:45 PM ET and send market-on-close exits for those leaving today."""
    path = ROOT / "exits" / f"{today}.json"
    if path.exists():
        raise FileExistsError(f"{path} exists; today's exits already went out")
    s = PaperSettings()
    dry = broker is None and not s.send
    if not dry:
        await sync(today, broker)  # today's entry fills, so day-one stops and broken pairs are seen
    held = open_trades()
    prices = quotes(sorted({h["symbol"] for t in held for h in t["held"]}))
    exits, orders = [], []
    for t in held:
        k = _session(t, today)
        reason = "flatten: every trade closed by hand" if flatten else rules.exit_reason(t, k, t["held"], prices)
        if not reason:
            continue
        exits.append({"trade_id": t["id"], "book": t["book"], "session_number": k, "reason": reason})
        orders += [{"trade_id": t["id"], "symbol": h["symbol"], "action": "SELL" if h["side"] == "long" else "BUY",
                    "shares": h["shares"] - h["exit_shares"], "kind": "MOC"} for h in t["held"] if h["shares"] > h["exit_shares"]]
    payload = {"session": today.isoformat(), "built_at": datetime.now(NY).isoformat(), "dry_run": dry,
               "prices": prices, "exits": exits, "orders": []}
    if orders:
        async with (broker or _broker()) as b:
            payload["orders"] = await b.place(orders)
    if not dry:
        _write(path, payload)
    return payload


# ---- Results ----------------------------------------------------------------------------------------------
def rows() -> list[dict]:
    """One row per trade for the dashboard and the report."""
    ts = trades()
    symbols = sorted({leg["symbol"] for t in ts for leg in t["legs"]})
    px = bars.load(symbols) if symbols else pd.DataFrame()
    bar = {(r.symbol, r.date.date()): r for r in px.itertuples()} if not px.empty else {}
    last = {}
    if not px.empty:
        last = px.sort_values("date").groupby("symbol")["close"].last().to_dict()
    out = []
    for t in ts:
        pnl = model = None
        if t["held"]:
            closed = t["state"] == "closed"
            pnl = sum((1 if h["side"] == "long" else -1) * h["shares"] *
                      ((h["exit_price"] if closed else last.get(h["symbol"], h["entry_price"])) - h["entry_price"]) for h in t["held"])
            pnl -= t["commission"]
            if closed:
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
        })
    return out


def summary_text() -> str:
    rs = rows()
    if not rs:
        return "No paper trades yet."
    df = pd.DataFrame(rs)
    lines = []
    for book, g in df.groupby("book"):
        closed = g[g["state"] == "closed"]
        slip = closed["slippage_bps"].dropna()
        lines.append(f"{book}: {len(g)} trades, {int((g['state'] == 'open').sum())} open, {len(closed)} closed"
                     + (f", hit {(closed['pnl'] > 0).mean():.0%}, P&L ${closed['pnl'].sum():,.0f}, avg {closed['bps'].mean():+.0f} bp" if len(closed) else "")
                     + (f", fills vs model {slip.mean():+.1f} bp over {len(slip)}" if len(slip) else ""))
    return "\n".join(lines)
