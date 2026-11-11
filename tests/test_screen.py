"""The screen's chart measures: how tight the base was before a breakout, and which way the trend runs."""

import unittest

import numpy as np
import pandas as pd

from factor_rotation import screen


def bars(closes, ranges, volumes=None):
    """Daily bars from closes and each day's high-low range as a share of the close."""
    closes = np.asarray(closes, float)
    ranges = np.asarray(ranges, float)
    dates = pd.bdate_range("2025-01-02", periods=len(closes))
    volume = np.full(len(closes), 1e6) if volumes is None else np.asarray(volumes, float)
    return pd.DataFrame({"date": dates, "symbol": "TEST", "open": closes, "high": closes * (1 + ranges / 2),
                         "low": closes * (1 - ranges / 2), "close": closes, "volume": volume})


def hits(df):
    f = screen._features(df)
    return f, screen._symbol_hits("TEST", f, "SPY", pd.Series(dtype=float), 0.0, {}, None)


class Base(unittest.TestCase):
    def breakout(self, quiet: float, drift: float):
        """300 sessions drifting, the last 10 before today with `quiet` times the usual range, then a
        4% day to a new high on triple volume."""
        n = 300
        closes = 100 * (1 + drift) ** np.arange(n)
        closes[-1] = closes[-2] * 1.04
        if drift < 0:  # a new 20-day high inside a decline needs a bigger day
            closes[-1] = closes[-21:-1].max() * 1.02
        ranges = np.full(n, 0.03)
        ranges[-11:-1] = 0.03 * quiet
        volumes = np.full(n, 1e6)
        volumes[-1] = 3e6
        return hits(bars(closes, ranges, volumes))

    def test_tight_base_in_an_uptrend(self):
        f, found = self.breakout(quiet=0.5, drift=0.001)
        self.assertLess(f["tightness"], screen.TIGHT_BASE)
        self.assertEqual(f["trend"], "up")
        hit = next(h for h in found if h["setup"] == "breakout")
        self.assertEqual(hit["adjust"], {"tight_base": screen.ADJUST["tight_base"]})
        self.assertIn("a tight base", hit["why"])
        self.assertNotIn("against this trade", hit["why"])
        plain = {**hit, "adjust": {}}
        self.assertAlmostEqual(screen._score(hit) - screen._score(plain), screen.ADJUST["tight_base"])

    def test_today_is_left_out_of_the_base(self):
        f, _ = self.breakout(quiet=1.0, drift=0.001)
        self.assertAlmostEqual(f["tightness"], 1.0, places=2)

    def test_loose_base_earns_nothing(self):
        f, found = self.breakout(quiet=1.6, drift=0.001)
        self.assertGreater(f["tightness"], screen.LOOSE_BASE)
        hit = next(h for h in found if h["setup"] == "breakout")
        self.assertEqual(hit["adjust"], {})
        self.assertIn("a loose base", hit["why"])

    def test_breakout_in_a_downtrend_is_marked_down(self):
        f, found = self.breakout(quiet=1.0, drift=-0.001)
        self.assertEqual(f["trend"], "down")
        hit = next(h for h in found if h["setup"] == "breakout")
        self.assertEqual(hit["adjust"], {"breakout_in_downtrend": screen.ADJUST["breakout_in_downtrend"]})
        self.assertIn("against this trade", hit["why"])

    def test_breakdown_in_an_uptrend_says_so_but_keeps_its_score(self):
        n = 300
        closes = 100 * 1.001 ** np.arange(n)
        closes[-1] = closes[-21:-1].min() * 0.98
        volumes = np.full(n, 1e6)
        volumes[-1] = 3e6
        f, found = hits(bars(closes, np.full(n, 0.03), volumes))
        self.assertEqual(f["trend"], "up")
        hit = next(h for h in found if h["setup"] == "breakdown")
        self.assertEqual(hit["adjust"], {})
        self.assertIn("against this trade", hit["why"])

    def test_other_setups_are_not_adjusted(self):
        _, found = self.breakout(quiet=0.5, drift=0.001)
        for h in found:
            if h["setup"] not in screen.BASE_SETUPS:
                self.assertEqual(h["adjust"], {})

    def test_short_history_has_no_measures(self):
        n = 60
        closes = 100 * 1.001 ** np.arange(n)
        closes[-1] = closes[-2] * 1.04
        volumes = np.full(n, 1e6)
        volumes[-1] = 3e6
        f, found = hits(bars(closes, np.full(n, 0.03), volumes))
        self.assertIsNone(f["tightness"])
        self.assertIsNone(f["trend"])
        hit = next(h for h in found if h["setup"] == "breakout")
        self.assertEqual(hit["adjust"], {})
        verdict, nums = screen._verdict(f)
        self.assertNotIn("the 50 before", verdict)
        self.assertIsNone(nums["trend"])

    def test_verdict_names_the_trend_and_the_base(self):
        f, _ = self.breakout(quiet=0.5, drift=0.001)
        verdict, nums = screen._verdict(f)
        self.assertIn("50-day above the 200-day and rising (trend up)", verdict)
        self.assertIn("x the 50 before", verdict)
        self.assertNotIn("base", verdict)
        self.assertEqual(nums["trend"], "up")
        self.assertLess(nums["tightness"], screen.TIGHT_BASE)

    def test_spread_tightness(self):
        rng = np.random.default_rng(7)
        spread = pd.Series(rng.normal(0, 0.01, 260))
        spread.iloc[-11:-1] *= 0.4
        _, nums = screen._spread_hits("Basket against XLK", spread, "basket_stretched", "basket_big_move")
        self.assertLess(nums["tightness"], 0.6)


def returns(n=60, seed=1, **mix):
    """Daily returns for funds built from shared drivers: name=(weight on market, weight on tech, own noise)."""
    rng = np.random.default_rng(seed)
    market, tech = rng.normal(0, 0.01, n), rng.normal(0, 0.01, n)
    return pd.DataFrame({name: m * market + k * tech + rng.normal(0, own, n) for name, (m, k, own) in mix.items()})


class Hedge(unittest.TestCase):
    def setUp(self):
        self.r = returns(SPY=(1, 0, 0.001), XLK=(1, 1, 0.001), XLI=(1, -0.5, 0.004), SMH=(1, 1.4, 0.004),
                         CHIP=(1, 1.5, 0.006), LONER=(0, 0, 0.02), GOLDISH=(0.05, 0, 0.01))

    def test_a_stock_gets_the_fund_it_moves_with(self):
        hedge, fit = screen._pick_hedge("CHIP", self.r, None)
        self.assertIn(hedge, ("SMH", "XLK"))
        self.assertGreater(fit["corr_60d"], 0.8)
        self.assertFalse(fit["weak"])
        self.assertIn("best match", fit["chosen"])

    def test_the_basket_hedge_stays_when_it_is_nearly_as_good(self):
        best, _ = screen._pick_hedge("CHIP", self.r, None)
        other = "XLK" if best == "SMH" else "SMH"
        gap = abs(self.r["CHIP"].corr(self.r["SMH"]) - self.r["CHIP"].corr(self.r["XLK"]))
        hedge, fit = screen._pick_hedge("CHIP", self.r, other)
        if gap <= screen.KEEP_BASKET_WITHIN:
            self.assertEqual((hedge, fit["chosen"]), (other, "its basket's hedge"))
        else:
            self.assertEqual(hedge, best)

    def test_a_poor_basket_hedge_is_replaced(self):
        hedge, fit = screen._pick_hedge("CHIP", self.r, "XLI")
        self.assertNotEqual(hedge, "XLI")
        self.assertFalse(fit["weak"])

    def test_nothing_moves_with_it(self):
        hedge, fit = screen._pick_hedge("LONER", self.r, None)
        self.assertEqual(hedge, "SPY")
        self.assertTrue(fit["weak"])
        self.assertIn("SPY by default", fit["chosen"])
        self.assertIn("close to an outright position", screen._fit_text(hedge, fit))

    def test_a_fund_is_set_against_the_market_and_flagged_when_unrelated(self):
        r = self.r.rename(columns={"GOLDISH": "USO"})
        hedge, fit = screen._pick_hedge("USO", r, None)
        self.assertEqual(hedge, "SPY")
        self.assertTrue(fit["weak"])
        hedge, fit = screen._pick_hedge("XLK", r, None)
        self.assertEqual(hedge, "SPY")
        self.assertFalse(fit["weak"])

    def test_short_history_keeps_the_default(self):
        hedge, fit = screen._pick_hedge("CHIP", self.r.iloc[-20:], "XLI")
        self.assertEqual((hedge, fit), ("XLI", None))
        self.assertIsNone(screen._fit_text(hedge, fit))


def cand(name, setup, score, kind="stock", direction="long"):
    return {"name": name, "kind": kind, "setup": setup, "direction": direction, "score": score, "why": f"{name} did something"}


class Shortlist(unittest.TestCase):
    def setUp(self):
        self.r = returns(SPY=(1, 0, 0.001), XLK=(1, 1, 0.001), QQQ=(1, 0.9, 0.001), XLE=(0.2, -1, 0.01), MTUM=(1, 1.1, 0.001))

    def test_names_in_the_book_are_left_out(self):
        ranked = [cand("ABNB", "earnings_gap_held", 3.6), cand("CAT", "turn", 2.7)]
        top, rest, held = screen._shortlist(ranked, self.r, {"ABNB": "the book holds it (2026-11-09-D01)"})
        self.assertEqual([c["name"] for c in top], ["CAT"])
        self.assertEqual([(c["name"], c["in_book"]) for c in held], [("ABNB", "the book holds it (2026-11-09-D01)")])

    def test_a_basket_is_kept_even_when_named_like_a_holding(self):
        ranked = [{**cand("Refiners", "basket_breakout", 3.0, kind="basket")}]
        top, _, held = screen._shortlist(ranked, self.r, {"Refiners": "the book holds it"})
        self.assertEqual((len(top), held), (1, []))

    def test_funds_that_move_together_are_one_idea(self):
        ranked = [cand("MTUM", "turn", 2.7, "etf"), cand("XLK", "turn", 2.6, "etf"), cand("QQQ", "turn", 2.5, "etf"),
                  cand("XLE", "turn", 2.4, "etf"), cand("XLK", "stretched", 1.2, "etf", "short")]
        top, _, _ = screen._shortlist(ranked, self.r, {})
        self.assertEqual([(c["name"], c["setup"]) for c in top], [("MTUM", "turn"), ("XLE", "turn"), ("XLK", "stretched")])
        self.assertEqual(top[0]["moved_with"], ["XLK", "QQQ"])
        self.assertIn("XLK, QQQ gave the same signal", top[0]["why"])

    def test_stocks_are_never_folded(self):
        r = returns(SPY=(1, 0, 0.001), MPC=(1, 1, 0.0005), VLO=(1, 1, 0.0005))
        top, _, _ = screen._shortlist([cand("MPC", "turn", 2.6), cand("VLO", "turn", 2.5)], r, {})
        self.assertEqual(len(top), 2)

    def test_no_setup_takes_more_than_its_share(self):
        ranked = [cand(f"T{i}", "turn", 3 - i / 100) for i in range(12)] + [cand(f"S{i}", "stretched", 1 - i / 100) for i in range(3)]
        top, rest, _ = screen._shortlist(ranked, self.r, {})
        self.assertEqual(sum(c["setup"] == "turn" for c in top), screen.MAX_PER_SETUP)
        self.assertEqual(sum(c["setup"] == "stretched" for c in top), 3)
        self.assertEqual([c["name"] for c in rest[:2]], ["T5", "T6"])

    def test_the_list_stops_at_twenty(self):
        ranked = [cand(f"N{i}", s, 3 - i / 100) for i, s in enumerate(s for s in list(screen.WEIGHTS) for _ in range(3))]
        top, rest, _ = screen._shortlist(ranked, self.r, {})
        self.assertEqual(len(top), screen.TOP)
        self.assertEqual(len(top) + len(rest), len(ranked))


class BookNames(unittest.TestCase):
    def test_open_and_recently_closed_names(self):
        from datetime import date
        from pathlib import Path
        from unittest import mock

        from factor_rotation.paper import book

        leg = lambda sym, side: {"symbol": sym, "side": side}  # noqa: E731
        trades = [
            {"id": "A1", "state": "open", "hedge": "XLY", "legs": [leg("ABNB", "long"), leg("XLY", "short")], "exit": None},
            {"id": "A2", "state": "closed", "hedge": "XLI", "legs": [leg("XLI", "long"), leg("RDW", "short")],
             "exit": {"session": "2026-11-09", "reason": "stopped"}},
            {"id": "A3", "state": "closed", "hedge": "SPY", "legs": [leg("OLD", "long"), leg("SPY", "short")],
             "exit": {"session": "2026-11-02", "reason": "time limit"}},
            {"id": "A4", "state": "unfilled", "hedge": "SPY", "legs": [leg("NOPE", "long"), leg("SPY", "short")], "exit": None},
            {"id": "A5", "state": "closing", "hedge": None, "legs": [leg("MTUM", "long"), leg("USMV", "short")], "exit": {"session": "2026-11-09", "reason": "time limit"}},
        ]
        sessions = [date(2026, 11, d) for d in (2, 3, 4, 5, 6, 9, 10, 11)]
        with mock.patch.object(book, "trades", return_value=trades), mock.patch.object(book, "ROOT", Path(__file__).parent):
            names = screen._book_names(sessions, date(2026, 11, 9))
        self.assertEqual(sorted(names), ["ABNB", "MTUM", "RDW", "USMV"])
        self.assertIn("holds it", names["ABNB"])
        self.assertIn("closed it on 2026-11-09", names["RDW"])


if __name__ == "__main__":
    unittest.main()
