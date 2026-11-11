"""The intraday setups and exit rules, as plain functions with no file or network access.

Everything is measured against the hedge: a name's move minus beta times its hedge fund's move, in units of
the name's daily ATR. Slot i is the 15-minute bar that closes at 9:45 AM ET plus 15 minutes times i. A setup
is read at a bar's close from that bar and earlier ones only, and the trade opens at the next bar's open.

  gap_hold      opened at least GAP ATRs away from the hedge and is still pushing that way after 30 minutes,
                on heavy volume. Trade with the gap.
  gap_fade      opened that far away, then closed back through the far side of its first 30 minutes' range.
                Trade against the gap.
  range_break   no gap; the first close outside the first 30 minutes' range before noon, on heavy volume,
                with the name already leading (or trailing) its hedge on the day. Trade with the break.
  stretched     no gap; has run at least STRETCH ATRs from the open against its hedge, 15-minute RSI at an
                extreme, and the latest bar turned back. Trade against the run.
"""

from dataclasses import dataclass

import numpy as np

SLOTS = 26
RANGE_SLOTS = 2            # the opening range: the first 30 minutes
MORNING_END = 9            # the bar that closes at noon; breaks and fades after it aren't taken
LAST_SIGNAL = 19           # the bar that closes at 2:30 PM; nothing opens after the 2:30 PM bar's signal
FLAT_SLOT = 25             # every trade is out at the open of the 3:45 PM bar
GAP = 0.75                 # ATRs against the hedge at the open
HEAVY = 1.5                # volume so far today against the usual by this time of day
FADE_VOLUME = 1.0
LEAD = 0.25                # ATRs ahead of the hedge on the day for a range break
STRETCH = 1.25             # ATRs from the open against the hedge
RSI_HIGH, RSI_LOW = 75, 25
STRETCH_FROM = 4           # stretched is read from the bar that closes at 10:45 AM

# Exits, in ATRs of the name: a loss this size stops the trade, a gain this size takes it, and a stretched
# trade that has done neither after its time limit is closed.
STOP_ATR, TARGET_ATR = 0.5, 1.0
STOP_RANGE = (0.004, 0.02)
MAX_BARS = {"stretched": 8}


@dataclass(frozen=True)
class Context:
    """What is known about a name before the session opens."""
    atr_pct: float               # daily ATR as a share of price
    beta: float                  # to its hedge, on 60 sessions of daily returns
    prev_close: float
    hedge_prev_close: float
    volume_curve: np.ndarray     # usual volume from the open through each slot


def stop_pct(atr_pct: float) -> float:
    lo, hi = STOP_RANGE
    return min(hi, max(lo, STOP_ATR * atr_pct))


def features(s: dict, h: dict, rsi: np.ndarray, ctx: Context) -> dict:
    """Arrays by slot for one name's day. `s` and `h` hold the name's and the hedge's open, high, low, close
    and volume by slot, NaN for bars that haven't happened."""
    a, b = ctx.atr_pct, ctx.beta
    with np.errstate(invalid="ignore", divide="ignore"):
        gap = (s["open"][0] / ctx.prev_close - 1) - b * (h["open"][0] / ctx.hedge_prev_close - 1)
        day = (s["close"] / ctx.prev_close - 1) - b * (h["close"] / ctx.hedge_prev_close - 1)
        run = (s["close"] / s["open"][0] - 1) - b * (h["close"] / h["open"][0] - 1)
        volume = np.nancumsum(s["volume"])
        typical = (s["high"] + s["low"] + s["close"]) / 3
        vwap = np.nancumsum(typical * s["volume"]) / volume
        return {
            "gap_atr": gap / a, "day_atr": day / a, "run_atr": run / a, "rvol": volume / ctx.volume_curve,
            "vwap": vwap, "rsi": rsi,
            "range_high": np.nanmax(s["high"][:RANGE_SLOTS]), "range_low": np.nanmin(s["low"][:RANGE_SLOTS]),
        }


def _hit(setup: str, direction: int, slot: int, f: dict) -> dict:
    return {"setup": setup, "direction": "long" if direction > 0 else "short", "slot": slot,
            "numbers": {"gap_atr": round(float(f["gap_atr"]), 2), "day_atr": round(float(f["day_atr"][slot]), 2),
                        "run_atr": round(float(f["run_atr"][slot]), 2), "rvol": round(float(f["rvol"][slot]), 2),
                        "rsi": round(float(f["rsi"][slot]), 1)}}


def at_slot(i: int, s: dict, f: dict) -> dict | None:
    """The setup that fires at slot i's close, or None."""
    c, o = s["close"], s["open"]
    if i < 1 or i > LAST_SIGNAL or np.isnan(c[i]) or np.isnan(f["rvol"][i]):
        return None
    gap = f["gap_atr"]
    gapped = abs(gap) >= GAP
    side = 1 if gap > 0 else -1
    if gapped and i == RANGE_SLOTS - 1:
        if side * (c[i] - o[0]) > 0 and side * f["run_atr"][i] > 0 and f["rvol"][i] >= HEAVY:
            return _hit("gap_hold", side, i, f)
    if i < RANGE_SLOTS:
        return None
    edge_far = f["range_low"] if side > 0 else f["range_high"]
    if gapped and i <= MORNING_END:
        crossed = side * (c[i] - edge_far) < 0 and not side * (c[i - 1] - edge_far) < 0
        if crossed and side * (c[i] - f["vwap"][i]) < 0 and f["rvol"][i] >= FADE_VOLUME:
            return _hit("gap_fade", -side, i, f)
    if not gapped and i <= MORNING_END:
        for d, edge in ((1, f["range_high"]), (-1, f["range_low"])):
            first = d * (c[i] - edge) > 0 and not (d * (c[RANGE_SLOTS:i] - edge) > 0).any()
            if first and f["rvol"][i] >= HEAVY and d * f["day_atr"][i] >= LEAD and d * (c[i] - f["vwap"][i]) > 0:
                return _hit("range_break", d, i, f)
    if not gapped and i >= STRETCH_FROM:
        run, r = f["run_atr"][i], f["rsi"][i]
        if run >= STRETCH and r >= RSI_HIGH and c[i] < o[i] and c[i] < c[i - 1]:
            return _hit("stretched", -1, i, f)
        if run <= -STRETCH and r <= RSI_LOW and c[i] > o[i] and c[i] > c[i - 1]:
            return _hit("stretched", 1, i, f)
    return None


def first_hit(s: dict, h: dict, rsi: np.ndarray, ctx: Context) -> dict | None:
    """The first setup to fire in a name's day. One trade a name a day."""
    f = features(s, h, rsi, ctx)
    for i in range(1, LAST_SIGNAL + 1):
        if hit := at_slot(i, s, f):
            return hit
    return None


def exit_reason(result: float, bars_held: int, slot: int, setup: str, stop: float, target: float) -> str | None:
    """Why a trade showing `result` at slot's close should be closed at the next open, or None to keep it."""
    if result <= -stop:
        return "stop"
    if result >= target:
        return "target"
    if bars_held >= MAX_BARS.get(setup, SLOTS):
        return "time"
    if slot + 1 >= FLAT_SLOT:
        return "flat"
    return None


def score(hit: dict, s: dict, h: dict, atr_pct: float, stop_atr: float | None = STOP_ATR,
          target_atr: float | None = TARGET_ATR) -> dict | None:
    """A hit traded by the rules: in at the next bar's open, each close checked, out at the open after the
    check that ends it. The result is the name's return minus the hedge's, as a share of one side, before
    costs. With no stop and no target it is simply held to the 3:45 PM bar."""
    e = hit["slot"] + 1
    side = 1 if hit["direction"] == "long" else -1
    if e >= FLAT_SLOT or np.isnan(s["open"][e]) or np.isnan(h["open"][e]):
        return None
    stop = stop_pct(atr_pct) if stop_atr is not None else np.inf
    target = target_atr / STOP_ATR * stop_pct(atr_pct) if target_atr is not None else np.inf
    setup = hit["setup"] if stop_atr is not None else ""

    def spread(sp: float, hp: float) -> float:
        return side * ((sp / s["open"][e] - 1) - (hp / h["open"][e] - 1))

    for j in range(e, FLAT_SLOT):
        if np.isnan(s["close"][j]) or np.isnan(h["close"][j]):
            continue
        why = exit_reason(spread(s["close"][j], h["close"][j]), j - e + 1, j, setup, stop, target)
        if why and not (np.isnan(s["open"][j + 1]) or np.isnan(h["open"][j + 1])):
            return {"result": spread(s["open"][j + 1], h["open"][j + 1]), "exit_slot": j + 1, "reason": why,
                    "bars": j + 1 - e}
    return None
