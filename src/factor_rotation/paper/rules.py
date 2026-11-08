"""The paper books' rules, as plain functions with no broker or file access, so they can be tested alone.

A trade is a dict:
    {"id", "book", "source", "name", "setup", "direction", "why", "entry_date", "horizon_days",
     "stop": {"spread_pct": -3.0} and/or {"rules": [...]}, "skip_if_open": [...],
     "notional": 10000, "legs": [{"symbol", "side": "long" | "short", "shares", "ref_price"}]}

Each side of a trade gets `notional` dollars: a pair is $10k long and $10k short, an outright trade $10k on
one side, and a basket side splits its $10k evenly across its members. Results are quoted in bp of one
side's notional, which is the same scale as an idea's score (long return minus short return).
"""

import math

# Sizing and caps. Paper money, but the caps keep a bug from building a silly book.
NOTIONAL = 10_000          # dollars per side
MAX_NEW_DYNAMIC = 10       # screen trades opened per session
MAX_OPEN = 60              # open trades across both books
MAX_GROSS = 900_000        # dollars, long plus short, across both books
MAX_SYMBOL = 30_000        # dollars of net exposure to one symbol across both books

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


def dynamic_trades(screen: dict, entry_date: str, prices: dict[str, float], held: set[str]) -> tuple[list[dict], list[dict]]:
    """The screen's best candidates as trades, best first, up to MAX_NEW_DYNAMIC. Skips a candidate whose
    stock, basket members or factor funds are already in an open dynamic trade or one taken tonight (`held`
    is those core symbols), a stock reporting inside its time limit, and anything it can't size. Returns
    (trades, skipped with reasons)."""
    trades, skipped, used = [], [], set(held)
    for c in screen.get("candidates", []):
        if len(trades) >= MAX_NEW_DYNAMIC:
            break
        horizon = HORIZON.get(c["setup"])
        if horizon is None:
            skipped.append({"name": c["name"], "reason": f"no default plan for {c['setup']}"})
            continue
        sides = _sides(c)
        if sides is None:
            skipped.append({"name": c["name"], "reason": "fewer than 3 basket members left after dropping reporters"})
            continue
        longs, shorts = sides
        # What the trade is about: the stock, the basket's members or both funds of a factor pair. A shared
        # hedge ETF is fine (MAX_SYMBOL caps it); the same story twice is not.
        symbols = {*longs, *shorts} - ({c.get("hedge")} if c["kind"] != "factor" and len({*longs, *shorts}) > 1 else set())
        if c["kind"] == "stock" and c.get("next_earnings"):
            away = c["next_earnings"].get("sessions_away")
            if away is not None and away <= horizon:
                skipped.append({"name": c["name"], "reason": f"reports in {away} sessions, inside the {horizon}-session limit"})
                continue
        clash = symbols & used
        if clash:
            skipped.append({"name": c["name"], "reason": f"already holds {', '.join(sorted(clash))}"})
            continue
        legs = size_legs(longs, shorts, prices)
        if legs is None:
            skipped.append({"name": c["name"], "reason": "no price, or a share costs more than its side's notional"})
            continue
        n = c.get("numbers") or {}
        daily = n.get("atr_pct") if c["kind"] == "stock" else n.get("sd_20d")
        trades.append({
            "id": f"{entry_date}-D{len(trades) + 1:02d}", "book": "dynamic", "source": f"screen {screen['as_of']} #{screen['candidates'].index(c) + 1}",
            "name": c["name"], "setup": c["setup"], "direction": c["direction"], "why": c.get("why", ""),
            "entry_date": entry_date, "horizon_days": horizon, "stop": {"spread_pct": _stop_pct(daily)},
            "skip_if_open": [], "notional": NOTIONAL, "legs": legs,
            "hedge": c.get("hedge") if c["kind"] != "factor" else None,
        })
        used |= symbols
    return trades, skipped


def selective_trades(ideas: list[dict], entry_date: str, prices: dict[str, float], checks: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """The recorded ideas for the session as trades, with their own plan. Ideas the pre-open check stood
    down are skipped, as are ideas recorded before plans existed (they get a one-session limit, no stop)."""
    trades, skipped = [], []
    for idea in ideas:
        if (checks.get(idea["id"]) or {}).get("decision") == "stand_down":
            skipped.append({"name": idea["id"], "reason": "stood down at the pre-open check"})
            continue
        side = lambda k: list(idea[k]) if isinstance(idea.get(k), list) else ([idea[k]] if idea.get(k) else [])
        legs = size_legs(side("long"), side("short"), prices)
        if legs is None:
            skipped.append({"name": idea["id"], "reason": "no price, or a share costs more than its side's notional"})
            continue
        label = " / ".join(filter(None, [idea.get("long_label") or ", ".join(side("long")), idea.get("short_label") or ", ".join(side("short"))]))
        trades.append({
            "id": f"{entry_date}-S-{idea['id']}", "book": "selective", "source": f"idea {idea['id']}",
            "name": label, "setup": idea.get("setup", "other"), "direction": "pair" if side("long") and side("short") else ("long" if side("long") else "short"),
            "why": idea.get("thesis", ""), "entry_date": entry_date, "horizon_days": idea.get("horizon_days", 1),
            "stop": idea.get("stop") or {}, "skip_if_open": idea.get("skip_if_open", []), "notional": NOTIONAL, "legs": legs,
        })
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
    """Keep new trades in order while every cap holds; selective trades go first."""
    kept, skipped = [], []
    ordered = sorted(new, key=lambda t: t["book"] != "selective")
    for t in ordered:
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


def skip_at_open(trade: dict, quotes: dict[str, float]) -> str | None:
    """A skip_if_open rule met by the pre-market quote. Orders go into the opening auction, so this is the
    last price the job can see; with no quote the trade goes in."""
    for rule in trade.get("skip_if_open", []):
        px = quotes.get(rule["symbol"])
        if px is None:
            continue
        side = "below" if "below" in rule else "above"
        if (px < rule["below"]) if side == "below" else (px > rule["above"]):
            return f"{rule['symbol']} at {px:,.2f} before the open, {side} {rule[side]:,.2f}"
    return None


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
