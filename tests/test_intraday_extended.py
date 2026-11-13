"""The intraday book outside regular hours, on made-up prices and a stand-in broker. No network, no gateway.

    uv run python -m unittest discover -s tests
"""

import asyncio
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from factor_rotation.config import NY
from factor_rotation.intraday import extended, orders
from factor_rotation.paper.broker import DryRun, PaperSettings
from test_paper_book import Broker


def at(day, h, m):
    return datetime(2026, 11, day, h, m, tzinfo=NY)


CTX = {"AAA": {"hedge": "HHH", "beta": 1.0, "atr_pct": 0.02, "close": 50.0, "hedge_close": 100.0},
       "BBB": {"hedge": "HHH", "beta": 1.0, "atr_pct": 0.02, "close": 20.0, "hedge_close": 100.0}}


class Sessions(unittest.TestCase):
    def test_which_session_is_trading(self):
        self.assertEqual(extended.session_at(at(11, 17, 0)), "after-hours")     # Wednesday
        self.assertEqual(extended.session_at(at(11, 21, 0)), "overnight")
        self.assertEqual(extended.session_at(at(12, 2, 0)), "overnight")
        self.assertIsNone(extended.session_at(at(12, 3, 55)))
        self.assertEqual(extended.session_at(at(12, 5, 0)), "pre-market")
        self.assertEqual(extended.session_at(at(12, 9, 31)), "open")
        self.assertIsNone(extended.session_at(at(12, 11, 0)))
        self.assertIsNone(extended.session_at(at(13, 21, 0)))                 # Friday night
        self.assertEqual(extended.session_at(at(15, 21, 0)), "overnight")     # Sunday night

    def test_a_night_is_named_for_the_session_it_leads_into_and_measured_from_the_last_close(self):
        self.assertEqual(extended.night_of(at(11, 21, 0)), date(2026, 11, 12))
        self.assertEqual(extended.night_of(at(12, 2, 0)), date(2026, 11, 12))
        self.assertEqual(extended.night_of(at(15, 21, 0)), date(2026, 11, 16))  # Sunday night -> Monday
        self.assertEqual(extended.last_close_day(at(12, 2, 0)), date(2026, 11, 11))
        self.assertEqual(extended.last_close_day(at(15, 21, 0)), date(2026, 11, 13))

    def test_overnight_deadlines_fall_after_midnight(self):
        self.assertEqual(extended.flat_by("overnight", at(11, 21, 0)), at(12, 3, 35))
        self.assertTrue(extended.signals_open("overnight", at(11, 23, 0)))
        self.assertTrue(extended.signals_open("overnight", at(12, 2, 30)))
        self.assertFalse(extended.signals_open("overnight", at(12, 3, 16)))
        self.assertFalse(extended.signals_open("after-hours", at(11, 19, 31)))
        self.assertFalse(extended.signals_open("open", at(12, 9, 31)))


class Signals(unittest.TestCase):
    def test_a_name_a_full_atr_ahead_of_its_hedge_since_the_close(self):
        now = at(11, 17, 0)
        px = {"AAA": 51.5, "BBB": 20.2, "HHH": 100.5}            # AAA +3% vs +0.5%: 1.25 ATR; BBB +1% vs +0.5%: 0.25
        hits = extended._signals(CTX, px, set(), set(), "after-hours", now, now)
        self.assertEqual([(x["symbol"], x["direction"], x["numbers"]["move_atr"]) for x in hits], [("AAA", "long", 1.25)])
        self.assertEqual(hits[0]["plan"]["flat_by"], "7:45 PM (after-hours)")
        self.assertEqual(extended._signals(CTX, {**px, "AAA": 48.5}, set(), set(), "after-hours", now, now)[0]["direction"], "short")
        self.assertEqual(extended._signals(CTX, px, {"AAA"}, set(), "after-hours", now, now), [])   # once a night
        self.assertEqual(extended._signals(CTX, px, set(), {"AAA"}, "after-hours", now, now), [])   # held by the swing book
        with mock.patch.object(extended, "ETFS", {"AAA"}):
            self.assertEqual(extended._signals(CTX, px, set(), set(), "after-hours", now, now), [])  # funds are hedges only

    def test_the_model_follows_each_signal_to_its_stop_target_or_flat_time(self):
        now = at(11, 17, 0)
        sig = extended._signals(CTX, {"AAA": 51.5, "HHH": 100.5}, set(), set(), "after-hours", now, now)[0]
        extended.advance(sig, {"AAA": 51.6, "HHH": 100.5}, at(11, 17, 15))
        self.assertNotIn("exit", sig)
        extended.advance(sig, {"AAA": 52.6, "HHH": 100.5}, at(11, 17, 30))      # +2.1%, past the 2% target (2x the 1% stop)
        self.assertEqual(sig["exit"]["reason"], "target")
        late = extended._signals(CTX, {"AAA": 51.5, "HHH": 100.5}, set(), set(), "after-hours", now, now)[0]
        extended.advance(late, {"AAA": 51.5, "HHH": 100.5}, at(11, 19, 46))
        self.assertEqual(late["exit"]["reason"], "flat")


class Spreads(unittest.TestCase):
    def test_a_book_wider_than_a_quarter_of_the_atr_gives_no_signal(self):
        now = at(11, 21, 0)
        px = {"AAA": 51.5, "HHH": 100.5}
        narrow = {"AAA": (51.49, 51.51), "HHH": (100.49, 100.51)}          # 0.04%, inside 0.25 x 2% ATR
        self.assertEqual(len(extended._signals(CTX, px, set(), set(), "overnight", now, now, narrow)), 1)
        wide = {**narrow, "AAA": (49.5, 53.5)}                             # 7.8% wide, as LIN was overnight
        self.assertEqual(extended._signals(CTX, px, set(), set(), "overnight", now, now, wide), [])
        self.assertEqual(extended._signals(CTX, px, set(), set(), "overnight", now, now,
                                           {**narrow, "HHH": (97.0, 104.0)}), [])   # a wide hedge counts too
        self.assertEqual(len(extended._signals(CTX, px, set(), set(), "overnight", now, now, {})), 1)  # no quote, no test

    def test_an_exit_crosses_a_narrow_book_and_rests_in_a_wide_one(self):
        sig = {"id": "X", "numbers": {"daily_atr_pct": 2.0}}
        narrow = extended._order(sig, "AAA", "BUY", 3, "overnight", 50.0, at(11, 21, 0), (49.99, 50.01), exiting=True)
        self.assertEqual(narrow["limit"], 50.26)                           # the ask, plus the slip
        wide = extended._order(sig, "AAA", "BUY", 3, "overnight", 50.0, at(11, 21, 0), (48.0, 52.0), exiting=True)
        self.assertEqual(wide["limit"], 50.25)                             # the mid, and the 9:31 AM order finishes it


class Paper(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for target, name, value in ((orders, "HALT", Path(tmp.name) / "HALT"), (extended.asyncio, "sleep", mock.AsyncMock()),
                                    (extended.drawdown, "paused", lambda b: None)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def take(self, now, sess="after-hours"):
        sig = extended._signals(CTX, {"AAA": 51.5, "HHH": 100.5}, set(), set(), sess, now, now)[0]
        sig["verdict"] = {"decision": "take"}
        return sig

    def trade(self, doc, sess, px, now, b):
        return asyncio.run(extended.trade(doc, sess, px, now, lambda: None, lambda: b))

    def test_a_take_goes_in_as_limit_orders_and_comes_out_at_its_exit(self):
        now, b = at(11, 17, 0), Broker()
        doc = {"signals": [self.take(now)]}
        px = {"AAA": 51.5, "HHH": 100.5}
        self.trade(doc, "after-hours", px, now, b)
        self.assertEqual([(o["symbol"], o["action"], o["shares"], o["kind"], o["limit"], o["venue"]) for o in b.sent],
                         [("AAA", "BUY", 194, "LMT", 51.76, "extended"), ("HHH", "SELL", 99, "LMT", 100.0, "extended")])
        self.trade(doc, "after-hours", px, at(11, 17, 15), b)
        self.assertEqual(doc["signals"][0]["paper"]["state"], "open")
        doc["signals"][0]["exit"] = {"reason": "target"}
        self.trade(doc, "after-hours", px, at(11, 17, 30), b)
        self.assertEqual([(o["symbol"], o["action"], o["shares"]) for o in b.sent[2:]], [("AAA", "SELL", 194), ("HHH", "BUY", 99)])
        self.trade(doc, "after-hours", px, at(11, 17, 45), b)
        p = doc["signals"][0]["paper"]
        self.assertEqual((p["state"], p["exit_reason"]), ("closed", "target"))
        self.assertEqual(p["result"]["commission"], 4.0)

    def test_overnight_orders_go_to_that_venue_and_pre_market_ones_end_before_the_open(self):
        b = Broker()
        doc = {"signals": [self.take(at(11, 21, 0), "overnight")]}
        self.trade(doc, "overnight", {"AAA": 51.5, "HHH": 100.5}, at(11, 21, 0), b)
        self.assertEqual({o["venue"] for o in b.sent}, {"overnight"})
        b2 = Broker()
        doc = {"signals": [self.take(at(12, 5, 0), "pre-market")]}
        self.trade(doc, "pre-market", {"AAA": 51.5, "HHH": 100.5}, at(12, 5, 0), b2)
        self.assertEqual(b2.sent[0]["good_till"], "20261112 09:25:00 US/Eastern")

    def test_an_unfilled_entry_is_dropped_and_a_one_sided_one_is_closed(self):
        now = at(11, 17, 0)
        b = Broker(fill=lambda o: False)
        doc = {"signals": [self.take(now)]}
        self.trade(doc, "after-hours", {"AAA": 51.5, "HHH": 100.5}, now, b)
        self.trade(doc, "after-hours", {"AAA": 51.5, "HHH": 100.5}, at(11, 17, 15), b)
        self.assertEqual(doc["signals"][0]["paper"]["state"], "unfilled")
        b = Broker(fill=lambda o: o["symbol"] == "AAA")
        doc = {"signals": [self.take(now)]}
        self.trade(doc, "after-hours", {"AAA": 51.5, "HHH": 100.5}, now, b)
        self.trade(doc, "after-hours", {"AAA": 51.5, "HHH": 100.5}, at(11, 17, 15), b)
        self.assertEqual(doc["signals"][0]["paper"]["exit_reason"], "broken: one side never filled")
        self.assertEqual([(o["symbol"], o["action"]) for o in b.sent[2:]], [("AAA", "SELL")])

    def test_an_exit_that_does_not_fill_is_repriced_and_goes_at_the_market_at_the_open(self):
        now = at(12, 8, 0)
        b = Broker()
        doc = {"signals": [self.take(now, "pre-market")]}
        self.trade(doc, "pre-market", {"AAA": 51.5, "HHH": 100.5}, now, b)
        b.fill = lambda o: False
        self.trade(doc, "pre-market", {"AAA": 51.5, "HHH": 100.5}, at(12, 9, 16), b)   # past 9:20? not yet: flat at 9:20
        self.trade(doc, "pre-market", {"AAA": 51.0, "HHH": 100.0}, at(12, 9, 21), b)
        self.assertEqual(doc["signals"][0]["paper"]["state"], "exiting")
        self.assertEqual(b.sent[-1]["kind"], "LMT")
        b.fill = lambda o: True
        self.trade(doc, "open", {}, at(12, 9, 31), b)
        self.assertEqual({o["kind"] for o in b.sent[-2:]}, {"MKT"})
        self.trade(doc, "open", {}, at(12, 9, 32), b)
        self.assertEqual(doc["signals"][0]["paper"]["state"], "closed")

    def test_a_dry_run_sends_nothing(self):
        now = at(11, 17, 0)
        doc = {"signals": [self.take(now)]}
        self.trade(doc, "after-hours", {"AAA": 51.5, "HHH": 100.5}, now, DryRun(PaperSettings(send=False)))
        self.assertEqual(doc["signals"][0]["paper"]["state"], "dry run")


if __name__ == "__main__":
    unittest.main()


class EntriesCross(unittest.TestCase):
    def test_an_entry_crosses_a_narrow_book_and_rests_at_a_wide_one(self):
        from datetime import datetime
        from factor_rotation.intraday import extended as x
        sig = {"id": "X-1", "numbers": {"daily_atr_pct": 4.0}}
        now = datetime(2026, 11, 12, 0, 5)
        narrow = x._order(sig, "ABC", "SELL", 5, "overnight", 100.0, now, (99.9, 100.1))
        self.assertEqual(narrow["limit"], round(99.9 * (1 - x.LIMIT_SLIP), 2))
        wide = x._order(sig, "ABC", "BUY", 5, "overnight", 100.0, now, (90.0, 110.0))
        self.assertEqual(wide["limit"], round(100.0 * (1 + x.LIMIT_SLIP), 2))
        # The rule is the 1% spread, not the name's ATR: 0.9% wide crosses, 1.1% rests at the mid.
        ok = x._order(sig, "ABC", "BUY", 5, "overnight", 100.0, now, (99.55, 100.45))
        self.assertEqual(ok["limit"], round(100.45 * (1 + x.LIMIT_SLIP), 2))
        not_ok = x._order(sig, "ABC", "BUY", 5, "overnight", 100.0, now, (99.45, 100.55))
        self.assertEqual(not_ok["limit"], round(100.0 * (1 + x.LIMIT_SLIP), 2))
