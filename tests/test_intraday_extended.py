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

    def trade(self, doc, sess, px, now, b, books=None, missing=None):
        """The gateway quotes every priced name at its price, bid and ask alike, unless `books` says otherwise."""
        books = books if books is not None else {s: (p, p) for s, p in px.items()}
        return asyncio.run(extended.trade(doc, sess, px, now, lambda: None, lambda: b, books, missing))

    def test_a_take_is_not_sent_without_the_gateways_quote_on_both_legs(self):
        now, b = at(12, 5, 0), Broker()
        doc = {"signals": [self.take(now, "pre-market")]}
        px = {"AAA": 51.5, "HHH": 100.5}                                   # Yahoo's prices: enough to see the signal
        w = self.trade(doc, "pre-market", px, now, b, books={"AAA": (51.4, 51.6)},
                       missing={"HHH": "the live IBKR login is in use somewhere else (IBKR error 10197)"})
        self.assertEqual(b.sent, [])
        p = doc["signals"][0]["paper"]
        self.assertEqual(p["state"], "not sent")
        self.assertIn("no quote for HHH from the gateway because the live IBKR login is in use", p["why"])
        self.assertIn("wasn't sent", w[0])
        self.trade(doc, "pre-market", px, at(12, 5, 15), b)                # quotes are back: it is not tried again
        self.assertEqual(b.sent, [])

    def test_an_exit_without_a_quote_waits_and_goes_at_the_market_at_the_open(self):
        now, b = at(12, 8, 0), Broker()
        doc = {"signals": [self.take(now, "pre-market")]}
        px = {"AAA": 51.5, "HHH": 100.5}
        self.trade(doc, "pre-market", px, now, b)
        self.trade(doc, "pre-market", px, at(12, 8, 15), b)
        doc["signals"][0]["exit"] = {"reason": "stop"}
        w = self.trade(doc, "pre-market", px, at(12, 8, 30), b, books={"AAA": (51.4, 51.6)}, missing={"HHH": "the gateway sent a bid and no ask"})
        self.assertEqual([(o["symbol"], o["action"]) for o in b.sent[2:]], [("AAA", "SELL")])
        self.assertIn("the exit of 99 HHH waits, with no quote for it from the gateway because the gateway sent a bid and no ask", w[0])
        self.trade(doc, "open", {}, at(12, 9, 31), b)
        self.assertEqual((b.sent[-1]["symbol"], b.sent[-1]["action"], b.sent[-1]["kind"]), ("HHH", "BUY", "MKT"))

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


class QuoteReasons(unittest.TestCase):
    def test_a_missing_quote_comes_with_the_gateways_reason(self):
        from types import SimpleNamespace as N
        from factor_rotation.intraday import extended as x
        nan = float("nan")
        self.assertEqual(x._book(N(bid=99.9, ask=100.1)), (99.9, 100.1))
        self.assertEqual(x._book(N(bid=100.0, ask=100.0)), (100.0, 100.0))    # a locked book is still a quote
        for bid, ask in ((nan, nan), (-1.0, 100.1), (99.9, -1.0), (100.2, 100.1), (None, None)):
            self.assertIsNone(x._book(N(bid=bid, ask=ask)))
        self.assertIn("live IBKR login is in use somewhere else", x._why_no_book(N(bid=nan, ask=nan), (10197, "No market data during competing live session")))
        self.assertIn("(IBKR error 10197)", x._why_no_book(N(bid=nan, ask=nan), (10197, "x")))
        self.assertIn("no market data subscription", x._why_no_book(N(bid=nan, ask=nan), (10168, "x")))
        self.assertEqual(x._why_no_book(N(bid=nan, ask=nan), (999, "Something new")), "Something new (IBKR error 999)")
        self.assertEqual(x._why_no_book(N(bid=99.9, ask=-1.0), None), "the gateway sent a bid and no ask")
        self.assertIn("no bid or ask within 8 seconds and gave no reason", x._why_no_book(N(bid=nan, ask=nan), None))

    def test_quotes_are_asked_for_again_unless_the_gateway_refused(self):
        from types import SimpleNamespace as N
        from factor_rotation.intraday import extended as x
        nan = float("nan")
        asked = []

        class IB:
            def reqMktData(self, c, *a):
                asked.append(c.symbol)
                n = asked.count(c.symbol)
                if c.symbol == "LATE":                                     # its book shows up on the second ask
                    return N(contract=c, bid=10.0 if n > 1 else nan, ask=10.1 if n > 1 else nan, last=nan)
                if c.symbol == "REFUSED":
                    m.errors[c.conId] = (10197, "No market data during competing live session")
                return N(contract=c, bid=nan, ask=nan, last=nan)

            def cancelMktData(self, c):
                pass

        m = x.IBMarket.__new__(x.IBMarket)
        m.ib, m.errors = IB(), {}

        async def contracts(symbols, overnight):
            return {s: N(symbol=s, conId=i + 1) for i, s in enumerate(symbols)}
        m._contracts = contracts
        with mock.patch.object(x.asyncio, "sleep", mock.AsyncMock()):
            asyncio.run(m.quotes(["LATE", "REFUSED", "EMPTY"], False, tries=3, need_books=True))
        self.assertEqual(m.books, {"LATE": (10.0, 10.1)})
        self.assertEqual((asked.count("LATE"), asked.count("REFUSED"), asked.count("EMPTY")), (2, 1, 3))
        self.assertIn("live IBKR login", m.missing["REFUSED"])
        self.assertIn("gave no reason", m.missing["EMPTY"])
