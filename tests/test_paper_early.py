"""The swing book's entries outside regular hours, against a stand-in broker and a temporary journal.

    uv run python -m unittest discover -s tests
"""

import asyncio
import unittest
from datetime import datetime
from unittest import mock

from factor_rotation.config import NY
from factor_rotation.paper import book, early
from factor_rotation.paper.broker import GuardError, check_order
from test_paper_book import DAY, Broker, Case, take

SUNDAY_NIGHT = datetime(2026, 11, 8, 20, 30, tzinfo=NY)    # the night before DAY, a Monday
PREMARKET = datetime(2026, 11, 9, 8, 50, tzinfo=NY)


class Venue(Case):
    def test_each_session_and_the_gaps_between(self):
        at = lambda d, h, m: datetime(2026, 11, d, h, m, tzinfo=NY)  # noqa: E731
        self.assertEqual(early.venue(at(10, 17, 0)), "extended")    # Tuesday after-hours
        self.assertEqual(early.venue(at(10, 21, 0)), "overnight")
        self.assertEqual(early.venue(at(11, 2, 0)), "overnight")
        self.assertIsNone(early.venue(at(11, 3, 55)))                # IBKR's overnight ends at 3:50 AM
        self.assertEqual(early.venue(at(11, 4, 5)), "extended")      # pre-market
        self.assertIsNone(early.venue(at(11, 9, 12)))                # too close to the open
        self.assertIsNone(early.venue(at(11, 12, 0)))                # the regular session isn't this job's
        self.assertIsNone(early.venue(at(13, 21, 0)))                # Friday night: no overnight session
        self.assertEqual(early.venue(at(8, 20, 30)), "overnight")    # Sunday night is


class Guards(Case):
    def test_a_limit_order_needs_a_price_and_a_session(self):
        o = {"trade_id": "T", "symbol": "AAA", "action": "BUY", "shares": 5, "kind": "LMT"}
        with self.assertRaises(GuardError):
            check_order({**o, "venue": "overnight"})
        with self.assertRaises(GuardError):
            check_order({**o, "limit": 10.0, "venue": "regular"})
        check_order({**o, "limit": 10.0, "venue": "overnight"})


class Early(Case):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(book, "_closes", lambda symbols: {s: 50.0 for s in symbols})
        p.start()
        self.addCleanup(p.stop)

    def props(self, *props):
        book._write(self.root / "proposals" / f"{DAY}.json", {"session": DAY.isoformat(), "screen": "2026-11-06", "passes": [], "proposals": list(props)})

    def run_early(self, now, broker, live=None, books=None, missing=None):
        """`live` is Yahoo's price for sizing. The gateway quotes each of those names at that price, bid and ask
        alike, unless `books` says otherwise; `missing` is the gateway's reason for a name it doesn't quote."""
        live = live if live is not None else {"AAA": 50.0, "HHH": 100.0}
        books = books if books is not None else {s: (p, p) for s, p in live.items()}

        async def gateway(symbols, overnight):
            return {s: books[s] for s in symbols if s in books}, {s: w for s, w in (missing or {}).items() if s in symbols}
        with mock.patch.object(book, "quotes", lambda symbols, *a: {s: live[s] for s in symbols if s in live}), \
                mock.patch.object(book, "PaperSettings", lambda: mock.Mock(send=True)):
            return asyncio.run(early.run(now, broker, quotes=gateway))

    def run_open(self, broker, live=None):
        live = live or {"AAA": 50.0, "HHH": 100.0}
        with mock.patch.object(book, "quotes", lambda symbols, *a: {s: live[s] for s in symbols if s in live}):
            return asyncio.run(book.open_session(DAY, broker))

    def test_a_take_goes_in_overnight_as_limit_orders_half_a_percent_through(self):
        self.props(take())
        b = Broker()
        r = self.run_early(SUNDAY_NIGHT, b)
        self.assertEqual((r["venue"], r["new"]), ("overnight", [f"{DAY}-A01"]))
        self.assertEqual([(o["symbol"], o["action"], o["shares"], o["kind"], o["limit"], o["venue"]) for o in b.sent],
                         [("AAA", "BUY", 200, "LMT", 50.25, "overnight"), ("HHH", "SELL", 100, "LMT", 99.5, "overnight")])
        self.assertNotIn("good_till", b.sent[0])
        saved = early.load(DAY)
        self.assertEqual([t["id"] for t in saved["trades"]], [f"{DAY}-A01"])
        self.assertEqual(len(saved["runs"]), 1)

    def test_a_later_run_reprices_only_what_is_left_and_premarket_orders_end_before_the_open(self):
        self.props(take())
        b = Broker(fill=lambda o: o["symbol"] == "AAA")
        self.run_early(SUNDAY_NIGHT, b)
        r = self.run_early(PREMARKET, b, {"AAA": 51.0, "HHH": 101.0})
        self.assertEqual(r["cancelled"], 1)
        self.assertEqual([(o["symbol"], o["shares"], o["limit"], o["good_till"]) for o in r["orders"]],
                         [("HHH", 100, 100.5, "20261109 09:25:00 US/Eastern")])
        self.assertEqual(r["new"], [])                                  # sized once, at the first run

    def test_the_open_sends_only_what_did_not_fill_early(self):
        self.props(take())
        b = Broker(fill=lambda o: o["symbol"] == "AAA" or o["kind"] == "MOO")
        self.run_early(SUNDAY_NIGHT, b)
        plan = self.run_open(b)
        self.assertEqual([t["id"] for t in plan["trades"]], [f"{DAY}-A01"])
        self.assertEqual([(o["symbol"], o["action"], o["shares"], o["kind"]) for o in plan["orders"]], [("HHH", "SELL", 100, "MOO")])
        self.assertEqual(plan["early"], [{"trade_id": f"{DAY}-A01", "filled": {"AAA": 200, "HHH": 0}}])
        self.assertEqual(self.state(f"{DAY}-A01"), "open")

    def test_a_take_stood_down_after_filling_early_closes_at_the_open(self):
        self.props(take())
        b = Broker()
        self.run_early(SUNDAY_NIGHT, b)
        self.props(take(preopen={"decision": "stand_down", "reason": "gapped 1.5 ATR"}))
        r = self.run_early(PREMARKET, b)
        self.assertEqual((r["stood_down"], r["orders"]), ([f"{DAY}-A01"], []))
        plan = self.run_open(b)
        self.assertEqual([(o["symbol"], o["action"], o["shares"]) for o in plan["orders"]], [("AAA", "SELL", 200), ("HHH", "BUY", 100)])
        self.assertIn("stood down after filling early", plan["exits"][0]["reason"])
        self.assertEqual(self.state(f"{DAY}-A01"), "closing")
        asyncio.run(book.sync(DAY, b))                                  # the fills of the exit at the open
        self.assertEqual(self.state(f"{DAY}-A01"), "closed")

    def test_a_stand_down_that_never_filled_is_simply_skipped(self):
        self.props(take())
        b = Broker(fill=lambda o: False)
        self.run_early(SUNDAY_NIGHT, b)
        self.props(take(preopen={"decision": "stand_down", "reason": "news"}))
        plan = self.run_open(b)
        self.assertEqual((plan["trades"], plan["orders"]), ([], []))
        self.assertIn("stood down", plan["skipped"][0]["reason"])

    def test_a_take_added_at_the_pre_open_pass_gets_the_next_id(self):
        self.props(take())
        b = Broker()
        self.run_early(SUNDAY_NIGHT, b)
        self.props(take(), take("P02", "BBB", ("BBB",), ("XLY",)))
        r = self.run_early(PREMARKET, b, {"AAA": 50.0, "HHH": 100.0, "BBB": 20.0, "XLY": 200.0})
        self.assertEqual(r["new"], [f"{DAY}-A02"])
        self.assertEqual([o["symbol"] for o in r["orders"]], ["BBB", "XLY"])

    def test_nothing_goes_out_between_sessions_when_halted_or_after_the_open(self):
        self.props(take())
        b = Broker()
        self.assertIn("between sessions", self.run_early(datetime(2026, 11, 9, 9, 12, tzinfo=NY), b)["note"])
        (self.root / "HALT").write_text("testing")
        self.assertIn("halted", self.run_early(SUNDAY_NIGHT, b)["note"])
        self.assertEqual(b.sent, [])

    def test_a_paused_swing_book_enters_nothing_early_or_at_the_open(self):
        self.props(take())
        (self.root / "PAUSE-swing").write_text("the swing book is down $91,000")
        b = Broker()
        self.assertIn("paused: the swing book is down", self.run_early(SUNDAY_NIGHT, b)["note"])
        with mock.patch.object(book, "quotes", lambda symbols, *a: {s: 50.0 for s in symbols}):
            plan = asyncio.run(book.open_session(DAY, broker=b))
        self.assertIn("down $91,000", plan["halted"])
        self.assertEqual((b.sent, plan["orders"]), ([], []))



class NoQuoteNoOrder(Early):
    """Nothing is sent without the gateway's own bid and ask. Yahoo's price sizes a trade and never prices an order."""

    def test_a_pair_with_an_unquoted_leg_waits_whole_and_says_why(self):
        self.props(take())
        b = Broker()
        r = self.run_early(PREMARKET, b, books={"AAA": (49.9, 50.1)},
                           missing={"HHH": "the live IBKR login is in use somewhere else (IBKR error 10197)"})
        self.assertEqual((b.sent, r["orders"]), ([], []))
        self.assertIn("no quote for HHH from the gateway because the live IBKR login is in use", r["warnings"][0])
        self.assertIn("the whole trade waits", r["warnings"][0])
        self.assertEqual(list(r["no_quote"]), ["HHH"])
        self.assertEqual(early.load(DAY)["runs"][-1]["no_quote"], r["no_quote"])     # kept in the journal

    def test_with_no_quotes_at_all_nothing_goes_out_even_though_yahoo_has_prices(self):
        self.props(take())
        b = Broker()
        r = self.run_early(PREMARKET, b, live={"AAA": 50.0, "HHH": 100.0}, books={})
        self.assertEqual(b.sent, [])
        self.assertIn("the gateway gave no bid and ask", r["warnings"][0])
        plan = self.run_open(b)                                                       # the open takes the whole trade
        self.assertEqual(sorted((o["symbol"], o["kind"]) for o in plan["orders"]), [("AAA", "MOO"), ("HHH", "MOO")])

    def test_once_a_trade_is_partly_in_only_the_unquoted_leg_waits(self):
        self.props(take())
        b = Broker(fill=lambda o: o["symbol"] == "AAA")
        self.run_early(SUNDAY_NIGHT, b)                                               # AAA fills, HHH doesn't
        r = self.run_early(PREMARKET, b, books={"AAA": (50.0, 50.0)}, missing={"HHH": "the gateway sent a bid and no ask"})
        self.assertEqual(r["orders"], [])
        self.assertIn("that leg waits", r["warnings"][0])

    def test_a_gateway_that_cannot_be_reached_gives_its_reason_for_every_name(self):
        class Down:
            def __init__(self, **k):
                pass

            async def __aenter__(self):
                raise ConnectionRefusedError("nothing is listening on 4002")

            async def __aexit__(self, *exc):
                pass
        from factor_rotation.intraday import extended
        with mock.patch.object(extended, "IBMarket", Down):
            books, missing = asyncio.run(early.gateway_books(["AAA", "HHH"], False))
        self.assertEqual(books, {})
        self.assertIn("ConnectionRefusedError: nothing is listening on 4002", missing["AAA"])
        self.assertEqual(set(missing), {"AAA", "HHH"})


class InTheBook(Early):
    """A trade is in the book from its first order, whether or not the 9:15 AM entry job runs."""

    def run_close(self, day, b):
        with mock.patch.object(book, "quotes", lambda symbols, *a: {s: 100.0 for s in symbols}):
            return asyncio.run(book.close_session(day, broker=b))

    def test_a_trade_filled_early_is_open_with_no_entry_job(self):
        self.props(take())
        self.run_early(SUNDAY_NIGHT, Broker())
        self.assertEqual([t["id"] for t in book.open_trades()], [f"{DAY}-A01"])
        self.assertEqual(self.state(f"{DAY}-A01"), "open")

    def test_its_stop_is_checked_at_the_close_with_no_entry_job(self):
        self.props(take(stop=-1.0))
        b = Broker()
        self.run_early(SUNDAY_NIGHT, b)
        with mock.patch.object(book, "quotes", lambda symbols, *a: {"AAA": 90.0, "HHH": 100.0}):
            r = asyncio.run(book.close_session(DAY, broker=b))
        self.assertEqual([e["trade_id"] for e in r["exits"]], [f"{DAY}-A01"])

    def test_a_close_before_its_session_leaves_it_alone(self):
        self.props(take(horizon=1))
        b = Broker()
        self.run_early(datetime(2026, 11, 6, 20, 30, tzinfo=NY), b)   # Friday night: between sessions
        self.run_early(SUNDAY_NIGHT, b)
        t = book.open_trades()[0]
        self.assertEqual(book._session(t, DAY.replace(day=6)), 0)
        self.assertEqual(self.run_close(DAY.replace(day=6), b)["exits"], [])

    def test_a_stand_down_after_filling_closes_at_the_close_if_the_open_job_missed_it(self):
        self.props(take())
        b = Broker()
        self.run_early(SUNDAY_NIGHT, b)
        self.props(take(preopen={"decision": "stand_down", "reason": "gapped 1.5 ATR"}))
        self.run_early(PREMARKET, b)
        r = self.run_close(DAY, b)
        self.assertIn("stood down after filling early", r["exits"][0]["reason"])

    def test_a_stand_down_that_never_filled_is_never_in_the_book(self):
        self.props(take())
        b = Broker(fill=lambda o: False)
        self.run_early(SUNDAY_NIGHT, b)
        self.props(take(preopen={"decision": "stand_down", "reason": "news"}))
        self.run_early(PREMARKET, b)
        self.assertEqual(book.trades(), [])

    def test_the_open_job_does_not_count_an_early_trade_twice(self):
        self.props(take())
        b = Broker(fill=lambda o: o["symbol"] == "AAA" or o["kind"] == "MOO")
        self.run_early(SUNDAY_NIGHT, b)
        plan = self.run_open(b)
        self.assertEqual([t["id"] for t in book.trades()], [f"{DAY}-A01"])
        self.assertEqual(plan["skipped"], [])


class WideSpreads(Early):
    def run_with_books(self, books_):
        return self.run_early(SUNDAY_NIGHT, Broker(), books=books_)

    def test_both_legs_cross_up_to_two_percent(self):
        self.props(take())
        r = self.run_with_books({"AAA": (49.8, 50.2), "HHH": (99.0, 101.0)})   # 0.8% wide, 2.0% wide
        self.assertEqual([(o["symbol"], o["limit"]) for o in r["orders"]], [("AAA", 50.45), ("HHH", 98.5)])
        self.assertEqual((r["orders"][0]["bid"], r["orders"][0]["ask"], r["orders"][0]["spread_pct"]), (49.8, 50.2, 0.8))

    def test_a_pair_with_a_wide_leg_waits_whole_until_something_fills(self):
        self.props(take())
        r = self.run_with_books({"AAA": (49.8, 50.2), "HHH": (98.5, 101.0)})   # HHH 2.5% wide
        self.assertEqual(r["orders"], [])
        self.assertIn("HHH 2.5%", r["warnings"][0])
        self.assertIn("the whole trade waits", r["warnings"][0])


class Crossing(unittest.TestCase):
    def test_buy_at_the_ask_sell_at_the_bid(self):
        from factor_rotation.paper import early
        self.assertEqual(early.crossing("BUY", (99.0, 100.0)), 100.0)
        self.assertEqual(early.crossing("SELL", (99.0, 100.0)), 99.0)
        self.assertIsNone(early.crossing("SELL", None))                 # no book, no price, no order
        self.assertEqual(early.limit_price("SELL", 99.0), 98.5)
