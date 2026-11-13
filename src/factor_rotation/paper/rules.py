"""The paper book's rules, as plain functions with no broker or file access, so they can be tested alone.

A trade is a dict:
    {"id", "book", "source", "proposal_id", "conviction", "name", "setup", "direction", "why", "wrong_if",
     "entry_date", "horizon_days", "stop": {"spread_pct": -3.0} and/or {"rules": [...]},
     "notional": 10000, "legs": [{"symbol", "side": "long" | "short", "shares", "ref_price"}], "hedge"}

Each side of a trade gets `notional` dollars: a pair is $10,000 long and $10,000 short, an outright trade
$10,000 on one side, and a basket side splits its $10,000 evenly across its members. Results are quoted in bp
of one side's notional (long return minus short return). `book` is always "dynamic", the automatic book's name
in the files; the selective book, which traded the recorded ideas as they stood, was retired on 2026-11-09.
"""

import math

from ..universe import ETFS

# Sizing and caps. Paper money, but the caps keep a bug from building a silly book. The book trades only what
# the research pass takes (proposals.py), so it holds few trades and each is large enough that the broker's $1
# minimum commission is about 4 bp a round trip, not the 16 bp it costs at $2,500 a side.
NOTIONAL = 10_000          # dollars per side
MAX_NEW = 5                # takes opened per session, strongest conviction first
MAX_OPEN = 60              # open trades
MAX_GROSS = 900_000        # dollars, long plus short
MAX_SYMBOL = 30_000        # dollars of net exposure to one symbol
MIN_CONVICTION = 3         # a take below this is a pass
# The outer limits on a plan the research pass writes. Inside them the plan is the pass's call; the pass can
# end a trade early at the pre-close review but can never widen a stop, and can extend only a winner, once.
# How every order outside the regular session is priced, in both books (the user's rule, 2026-11-12): to fill at
# once, a buy goes in at the ask and a sell at the bid, with the limit a little beyond so it fills even if the
# quote moves. The one exception is a book wider than a limit, as a share of the midpoint (see crossable).
# Crossing costs about half the spread on the way in. The swing book's exits are in regular hours, where spreads
# are tiny, so a 2% spread costs it about 1% against stops of 4-8% and moves over days. The intraday book's
# overnight trades pay on the way out too, so its limit is half that (intraday/extended.py).
# The user's numbers, 2026-11-12: swing 2%, intraday 1%. (Briefly 1% and 0.5% that day, which on IBKR's quotes
# would have held back all three of the night's off-hours entries, sending two to the open.)
MAX_CROSS_SPREAD = 0.02


def spread_pct(book_: tuple[float, float] | None) -> float | None:
    """The bid-ask spread as a share of the midpoint, or None without a usable book."""
    if not book_:
        return None
    bid, ask = book_
    mid = (bid + ask) / 2
    return (ask - bid) / mid if bid > 0 and ask >= bid else None


def crossable(book_: tuple[float, float] | None, limit: float = MAX_CROSS_SPREAD) -> bool:
    """Whether an order should cross this book: a usable quote no wider than `limit`."""
    s = spread_pct(book_)
    return s is not None and s <= limit


MAX_HORIZON = 10           # sessions, counting the first
STOP_LIMITS = (1.0, 8.0)   # percent loss from entry: no tighter than the first, no wider than the second
MAX_EXTENSION = 3          # sessions

# How long each setup is held and how far the spread may fall before it is stopped, as a multiple of the
# trade's typical daily move (ATR % for a stock, the spread's daily sd for a basket or factor pair). The
# earnings horizon matches the drift measured in the gap table (next open through four sessions).
HORIZON = {
    "earnings_gap_held": 4, "earnings_gap_faded": 4, "breakout": 5, "breakdown": 5, "reclaim_200": 5,
    "lost_200": 5, "relative_strength": 3, "relative_weakness": 3, "turn": 3, "stretched": 3,
    "basket_breakout": 5, "basket_breakdown": 5, "basket_big_move": 2, "basket_stretched": 3,
    "factor_turn": 5, "factor_stretched": 3,
}
STOP_MOVES = 1.5
STOP_RANGE = (2.0, 8.0)    # percent
DEFAULT_STOP = 3.0         # percent, when the screen gives no daily move


def _stop_pct(daily_move: float | None) -> float:
    if not daily_move or math.isnan(daily_move):
        return -DEFAULT_STOP
    lo, hi = STOP_RANGE
    return -round(min(hi, max(lo, STOP_MOVES * daily_move * 100)), 1)


def default_plan(c: dict) -> dict:
    """The mechanical plan for a screen candidate: its setup's time limit and a stop sized on its daily move.
    A candidate the research pass passes on is scored against this plan."""
    n = c.get("numbers") or {}
    daily = n.get("atr_pct") if c["kind"] in ("stock", "etf") else n.get("sd_20d")
    return {"horizon_days": HORIZON.get(c["setup"], 3), "stop": {"spread_pct": _stop_pct(daily)}}


def _sides(c: dict) -> tuple[list[str], list[str]] | None:
    """(long symbols, short symbols) for a screen candidate, or None if it can't be traded as a pair."""
    long_dir = c["direction"] == "long"
    if c["kind"] == "factor":
        a, b = c["long"], c["short"]
        return ([a], [b]) if long_dir else ([b], [a])
    if c["kind"] == "basket":
        reporting = set(c.get("members_reporting_within_5") or [])
        members = [m for m in c["members"] if m not in reporting]
        if len(members) < 3:
            return None
        return (members, [c["hedge"]]) if long_dir else ([c["hedge"]], members)
    name, hedge = c["name"], c.get("hedge") or "SPY"
    if name == hedge:
        return ([name], []) if long_dir else ([], [name])
    return ([name], [hedge]) if long_dir else ([hedge], [name])


def size_legs(longs: list[str], shorts: list[str], prices: dict[str, float], notional: float = NOTIONAL) -> list[dict] | None:
    """Whole-share legs for each side; None if a price is missing or a leg rounds to zero shares."""
    legs = []
    for side, names in (("long", longs), ("short", shorts)):
        for s in names:
            px = prices.get(s)
            if not px or px <= 0:
                return None
            shares = math.floor(notional / len(names) / px)
            if shares < 1:
                return None
            legs.append({"symbol": s, "side": side, "shares": shares, "ref_price": round(px, 4)})
    return legs


def core_symbols(trade: dict) -> set[str]:
    """The symbols a trade is about, without a hedge ETF on the other side."""
    sides = {s: {l["symbol"] for l in trade["legs"] if l["side"] == s} for s in ("long", "short")}
    if trade.get("hedge") and len(sides["long"]) + len(sides["short"]) > 1:
        return (sides["long"] | sides["short"]) - {trade["hedge"]}
    return sides["long"] | sides["short"]


def _hedge(longs: list[str], shorts: list[str]) -> str | None:
    """The side that is one ETF hedging stocks or a basket on the other. Two ETFs against each other are a
    pair with no hedge: the trade is about both."""
    for hedge, other in ((shorts, longs), (longs, shorts)):
        if len(hedge) == 1 and hedge[0] in ETFS and other and not all(s in ETFS for s in other):
            return hedge[0]
    return None


def proposal_trades(proposals: list[dict], entry_date: str, prices: dict[str, float], held: set[str],
                    start: int = 1, room: int = MAX_NEW) -> tuple[list[dict], list[dict]]:
    """The research pass's takes as trades, strongest conviction first, up to MAX_NEW, each with the plan
    the pass wrote. Skips a take stood down before the open, one whose stock, basket members or pair funds are
    already in an open automatic trade or one taken today (`held` is those core symbols), and anything it
    can't size. `start` numbers the ids after trades already made for the session (the early entries), and
    `room` is how many of the session's MAX_NEW are left. Returns (trades, skipped with reasons)."""
    trades, skipped, used = [], [], set(held)
    takes = [p for p in proposals if p["decision"] == "take"]
    for p in sorted(takes, key=lambda p: -p["conviction"]):  # a stable sort: the pass's own order within a conviction
        if (p.get("preopen") or {}).get("decision") == "stand_down":
            skipped.append({"name": p["id"], "reason": f"stood down before the open: {p['preopen']['reason']}"})
            continue
        if len(trades) >= room:
            skipped.append({"name": p["id"], "reason": f"the {MAX_NEW} takes with the most conviction came first"})
            continue
        longs, shorts = p["long"], p["short"]
        hedge = _hedge(longs, shorts)
        symbols = {*longs, *shorts} - {hedge}
        if clash := symbols & used:
            skipped.append({"name": p["id"], "reason": f"already holds {', '.join(sorted(clash))}"})
            continue
        legs = size_legs(longs, shorts, prices)
        if legs is None:
            skipped.append({"name": p["id"], "reason": "no price, or a share costs more than its side's notional"})
            continue
        trades.append({
            "id": f"{entry_date}-A{len(trades) + start:02d}", "book": "dynamic", "source": f"proposal {p['id']}", "proposal_id": p["id"],
            "conviction": p["conviction"], "name": p["name"], "setup": p["setup"], "direction": p["direction"], "why": p["reason"],
            "wrong_if": p["wrong_if"], "entry_date": entry_date, "horizon_days": p["plan"]["horizon_days"], "stop": p["plan"]["stop"],
            "notional": NOTIONAL, "legs": legs, "hedge": hedge,
        })
        used |= symbols
    return trades, skipped


def exposure(trades: list[dict]) -> tuple[float, dict[str, float]]:
    """Gross dollars and net dollars by symbol, at reference prices."""
    gross, net = 0.0, {}
    for t in trades:
        for leg in t["legs"]:
            v = leg["shares"] * leg["ref_price"]
            gross += v
            net[leg["symbol"]] = net.get(leg["symbol"], 0.0) + (v if leg["side"] == "long" else -v)
    return gross, net


def apply_caps(new: list[dict], open_trades: list[dict]) -> tuple[list[dict], list[dict]]:
    """Keep new trades, in the order given, while every cap holds."""
    kept, skipped = [], []
    for t in new:
        trial = [*open_trades, *kept, t]
        gross, net = exposure(trial)
        if len(trial) > MAX_OPEN:
            skipped.append({"name": t["id"], "reason": f"over {MAX_OPEN} open trades"})
        elif gross > MAX_GROSS:
            skipped.append({"name": t["id"], "reason": f"gross would be ${gross:,.0f}, over ${MAX_GROSS:,.0f}"})
        elif (big := [s for s, v in net.items() if abs(v) > MAX_SYMBOL and s in {l['symbol'] for l in t['legs']}]):
            skipped.append({"name": t["id"], "reason": f"net exposure to {', '.join(big)} over ${MAX_SYMBOL:,.0f}"})
        else:
            kept.append(t)
    return kept, skipped


def spread_return(held: list[dict], prices: dict[str, float]) -> float | None:
    """The trade's return on one side's notional from its entry fills to `prices`; None if a price is missing.
    `held` is the filled legs: {"symbol", "side", "shares", "entry_price"}."""
    pnl = 0.0
    for leg in held:
        px = prices.get(leg["symbol"])
        if px is None:
            return None
        sign = 1 if leg["side"] == "long" else -1
        pnl += sign * leg["shares"] * (px - leg["entry_price"])
    return pnl


def exit_reason(trade: dict, session: int, held: list[dict], prices: dict[str, float]) -> str | None:
    """Why this trade exits at today's close, or None to keep holding. `session` is 1 on the entry day."""
    filled = {leg["symbol"] for leg in held}
    if filled != {leg["symbol"] for leg in trade["legs"]}:
        missing = sorted({leg["symbol"] for leg in trade["legs"]} - filled)
        return f"broken: {', '.join(missing)} never filled"
    stop = trade.get("stop") or {}
    for rule in stop.get("rules", []):
        px = prices.get(rule["symbol"])
        side = "below" if "below" in rule else "above"
        if px is not None and ((px < rule["below"]) if side == "below" else (px > rule["above"])):
            return f"stopped: {rule['symbol']} at {px:,.2f} at 3:45 PM ET, {side} {rule[side]:,.2f}"
    if "spread_pct" in stop:
        pnl = spread_return(held, prices)
        if pnl is not None and pnl / trade["notional"] * 100 <= stop["spread_pct"]:
            return f"stopped: down {abs(pnl / trade['notional'] * 100):.1f}% at 3:45 PM ET, past the {abs(stop['spread_pct']):g}% stop"
    if session >= trade["horizon_days"]:
        return f"time limit: session {session} of {trade['horizon_days']}"
    return None
