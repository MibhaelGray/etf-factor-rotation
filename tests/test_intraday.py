"""The intraday setups and exits, on made-up 15-minute bars."""

import unittest

import numpy as np
import pandas as pd

from datetime import datetime

from factor_rotation.intraday import bars15, live, model, replay, signals

N = signals.SLOTS


def day(closes, volume=1000.0, first_open=None):
    """A day's bars from closes: each bar opens at the last close and its high and low are its open and close."""
    c = np.asarray(closes, float)
    o = np.concatenate([[c[0] if first_open is None else first_open], c[:-1]])
    return {"open": o, "high": np.maximum(o, c), "low": np.minimum(o, c), "close": c, "volume": np.full(len(c), float(volume))}


def flat(price=100.0):
    return day(np.full(N, price))


def ctx(prev=100.0, usual=1000.0):
    return signals.Context(atr_pct=0.02, beta=1.0, prev_close=prev, hedge_prev_close=100.0,
                           volume_curve=np.cumsum(np.full(N, usual)))


RSI = np.full(N, 50.0)


class Setups(unittest.TestCase):
    def test_range_break_needs_heavy_volume(self):
        closes = np.full(N, 100.0)
        closes[3:] = 101.0
        hit = signals.first_hit(day(closes, 2000), flat(), RSI, ctx())
        self.assertEqual((hit["setup"], hit["direction"], hit["slot"]), ("range_break", "long", 3))
        self.assertIsNone(signals.first_hit(day(closes, 1000), flat(), RSI, ctx()))

    def test_a_move_the_hedge_made_too_is_not_a_break(self):
        closes = np.full(N, 100.0)
        closes[3:] = 101.0
        self.assertIsNone(signals.first_hit(day(closes, 2000), day(closes), RSI, ctx()))

    def test_gap_hold_and_gap_fade(self):
        up = np.full(N, 103.0)
        up[1:] = 104.0
        hit = signals.first_hit(day(up, 2000, first_open=102.0), flat(), RSI, ctx())
        self.assertEqual((hit["setup"], hit["direction"], hit["slot"]), ("gap_hold", "long", 1))
        back = np.full(N, 102.0)
        back[4:] = 100.5
        hit = signals.first_hit(day(back, 1000, first_open=102.0), flat(), RSI, ctx())
        self.assertEqual((hit["setup"], hit["direction"], hit["slot"]), ("gap_fade", "short", 4))

    def test_stretched_waits_for_the_turn(self):
        closes = 100 + np.minimum(np.arange(N), 12) * 0.3      # +3.6% by 12:45 PM on normal volume, then flat
        closes[14:] = closes[13] - 0.4
        rsi = np.full(N, 85.0)
        hit = signals.first_hit(day(closes), flat(), rsi, ctx())
        self.assertEqual((hit["setup"], hit["direction"], hit["slot"]), ("stretched", "short", 14))

    def test_later_bars_never_change_an_earlier_signal(self):
        rng = np.random.default_rng(7)
        for _ in range(200):
            s = day(100 * np.cumprod(1 + rng.normal(0, 0.004, N)), rng.uniform(500, 3000), first_open=100 + rng.normal(0, 1.5))
            h = day(100 * np.cumprod(1 + rng.normal(0, 0.001, N)))
            rsi = rng.uniform(10, 90, N)
            hit = signals.first_hit(s, h, rsi, ctx())
            if not hit:
                continue
            cut = hit["slot"] + 1
            s2 = {k: np.concatenate([v[:cut], np.full(N - cut, np.nan)]) for k, v in s.items()}
            h2 = {k: np.concatenate([v[:cut], np.full(N - cut, np.nan)]) for k, v in h.items()}
            again = signals.first_hit(s2, h2, rsi, ctx())
            self.assertEqual(again, hit)


class Exits(unittest.TestCase):
    HIT = {"setup": "range_break", "direction": "long", "slot": 3}

    def test_a_stop_is_seen_at_a_close_and_filled_at_the_next_open(self):
        closes = np.full(N, 100.0)
        closes[6] = 98.5           # 1% is the stop at a 2% ATR
        closes[7:] = 98.0
        out = signals.score(self.HIT, day(closes), flat(), 0.02)
        self.assertEqual((out["reason"], out["exit_slot"]), ("stop", 7))
        self.assertAlmostEqual(out["result"], -0.015)

    def test_target_and_flat(self):
        closes = np.full(N, 100.0)
        closes[8:] = 102.5
        out = signals.score(self.HIT, day(closes), flat(), 0.02)
        self.assertEqual((out["reason"], out["exit_slot"]), ("target", 9))
        out = signals.score(self.HIT, flat(), flat(), 0.02)
        self.assertEqual((out["reason"], out["exit_slot"]), ("flat", signals.FLAT_SLOT))

    def test_stretched_has_a_time_limit_and_a_short_gains_when_the_name_falls(self):
        closes = np.full(N, 100.0)
        closes[11:] = 99.8
        out = signals.score({"setup": "stretched", "direction": "short", "slot": 3}, day(closes), flat(), 0.02)
        self.assertEqual((out["reason"], out["bars"]), ("time", 8))
        self.assertAlmostEqual(out["result"], 0.002)


class Plumbing(unittest.TestCase):
    def test_grid_and_merge(self):
        when = pd.to_datetime(["2026-11-09 09:30", "2026-11-09 15:45", "2026-11-10 09:45"])
        df = pd.DataFrame({"symbol": "T", "when": when, "open": [1.0, 2, 3], "high": [1.0, 2, 3], "low": [1.0, 2, 3],
                           "close": [1.0, 2, 3], "volume": [5.0, 6, 7]})
        g = bars15.grid(df, pd.DatetimeIndex(["2026-11-09", "2026-11-10"]))
        self.assertEqual((g["close"][0, 0], g["close"][0, 25], g["close"][1, 1]), (1, 2, 3))
        self.assertTrue(np.isnan(g["close"][1, 0]))
        new = df.iloc[1:].assign(close=[9.0, 9.0])
        self.assertEqual(bars15.merge(new, df)["close"].tolist(), [1.0, 9.0, 9.0])

    def test_hedges_follow_the_daily_rule(self):
        rng = np.random.default_rng(3)
        spy, xlk = rng.normal(0, 0.01, 60), rng.normal(0, 0.01, 60)
        r = pd.DataFrame({"SPY": spy, "XLK": xlk, "AAA": 1.5 * xlk + rng.normal(0, 0.002, 60), "ZZZ": rng.normal(0, 0.01, 60)})
        out = replay.pick_hedges(r, {})
        self.assertEqual(out["AAA"][0], "XLK")
        self.assertAlmostEqual(out["AAA"][1], 1.5, delta=0.1)
        self.assertEqual((out["ZZZ"][0], out["XLK"][0]), ("SPY", "SPY"))
        self.assertEqual(replay.slot_time(1), "10:00 AM")
        self.assertEqual(replay.slot_time(19), "2:30 PM")


class Live(unittest.TestCase):
    def test_last_slot(self):
        self.assertEqual(live.last_slot(datetime(2026, 11, 10, 10, 1)), 1)     # the 10:00 AM bar
        self.assertEqual(live.last_slot(datetime(2026, 11, 10, 15, 46)), 24)   # the 3:45 PM bar
        self.assertEqual(live.last_slot(datetime(2026, 11, 10, 9, 40)), -1)

    def test_known_hides_what_has_not_happened(self):
        k = live.known(day(np.arange(100.0, 100 + N)), 5)
        self.assertEqual(k["close"][5], 105)
        self.assertTrue(np.isnan(k["close"][6:]).all() and np.isnan(k["volume"][6:]).all())
        self.assertEqual(k["open"][6], 105)                                    # the next bar's open is the fill
        self.assertTrue(np.isnan(k["open"][7:]).all())

    def test_a_signal_fills_then_exits_as_bars_arrive(self):
        closes = np.full(N, 100.0)
        closes[6] = 98.5
        closes[7:] = 98.0
        s, h = day(closes), flat()
        sig = {"setup": "range_break", "direction": "long", "slot": 3, "atr_pct": 0.02}
        live._advance(sig, live.known(s, 3), live.known(h, 3))
        self.assertEqual(sig["entry"]["at"], "10:30 AM")
        self.assertNotIn("exit", sig)
        live._advance(sig, live.known(s, 5), live.known(h, 5))
        self.assertNotIn("exit", sig)
        live._advance(sig, live.known(s, 6), live.known(h, 6))
        self.assertEqual((sig["exit"]["reason"], sig["exit"]["at"], sig["exit"]["result_bp"]), ("stop", "11:15 AM", -150.0))

    def test_verdicts_are_read_strictly(self):
        good = model._parse('Sure: {"decision": "take", "conviction": 9, "catalyst": "x", "why": "y", "against": "z"}')
        self.assertEqual((good["decision"], good["conviction"]), ("take", 5))
        self.assertIsNone(model._parse('{"decision": "buy"}'))
        self.assertIsNone(model._parse("no json here"))


if __name__ == "__main__":
    unittest.main()
