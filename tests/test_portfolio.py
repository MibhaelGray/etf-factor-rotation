"""The portfolio view's positions from fills. No network.

    uv run python -m unittest discover -s tests
"""

import unittest
from datetime import date, datetime
from unittest import mock

from factor_rotation import portfolio
from factor_rotation.config import NY


def fill(symbol, side, shares, price, day=10, commission=1.0, hour=9, session=None):
    return {"symbol": symbol, "side": side, "shares": float(shares), "price": price, "commission": commission,
            "at": datetime(2026, 11, day, hour, 30, tzinfo=NY), "session": session or date(2026, 11, day)}


class Held(unittest.TestCase):
    def test_adds_average_in_and_sells_realize_against_the_average(self):
        p = portfolio._held([fill("AAA", "BOT", 100, 10.0), fill("AAA", "BOT", 100, 12.0, 11), fill("AAA", "SLD", 50, 15.0, 12)])["AAA"]
        self.assertEqual((p["shares"], p["cost"], p["realized"], p["commission"]), (150, 11.0, 200.0, 3.0))
        self.assertEqual(p["since"].day, 10)

    def test_a_short_gains_when_it_is_bought_back_lower_and_a_closed_position_holds_nothing(self):
        p = portfolio._held([fill("HHH", "SLD", 100, 50.0), fill("HHH", "BOT", 100, 48.0, 11)])["HHH"]
        self.assertEqual((p["shares"], p["realized"]), (0, 200.0))

    def test_trading_through_zero_starts_a_new_position_at_the_new_price(self):
        p = portfolio._held([fill("AAA", "BOT", 100, 10.0), fill("AAA", "SLD", 150, 12.0, 11)])["AAA"]
        self.assertEqual((p["shares"], p["cost"], p["realized"], p["since"].day), (-50, 12.0, 200.0, 11))



class Marks(unittest.TestCase):
    def test_a_fill_overnight_is_not_marked_at_the_close_before_it(self):
        import pandas as pd
        # Bought at 11:30 PM on 11-11 for the 11-12 session; the latest close is 11-11's, before the entry.
        f = fill("SMH", "BOT", 20, 493.0, day=11, hour=23, session=date(2026, 11, 12))
        bars_ = pd.DataFrame({"symbol": ["SMH", "SMH"], "date": pd.to_datetime(["2026-11-10", "2026-11-11"]), "close": [480.0, 507.0]})
        with mock.patch.object(portfolio, "fills", lambda b: [f] if b == "swing" else []), \
             mock.patch.object(portfolio.bars, "load", lambda s: bars_), \
             mock.patch.object(portfolio.book, "ROOT", portfolio.book.ROOT / "no-such-dir"):
            [row] = portfolio.positions()
        self.assertEqual((row["last"], row["pnl"], row["day_pnl"]), (493.0, 0.0, 0.0))


class Accounts(unittest.TestCase):
    def accounts(self, sync, swing, intraday, pos=(), first=None):
        books = {"swing": swing, "intraday": intraday}
        with mock.patch.object(portfolio, "fills", lambda b: books[b]), mock.patch.object(portfolio, "_last_sync", lambda: sync), \
                mock.patch.object(portfolio, "_first_sync", lambda: first if first is not None else sync):
            return portfolio.accounts(list(pos))

    def test_each_book_is_its_capital_plus_its_own_pnl_and_the_rest_is_unassigned(self):
        swing = [fill("AAA", "BOT", 100, 10.0), fill("AAA", "SLD", 50, 12.0, 11)]       # +100 realized, 50 open
        intraday = [fill("HHH", "SLD", 10, 50.0)]
        a = self.accounts({"net_liquidation": 1_026_000.0, "marks": {"AAA": 11.0, "HHH": 49.0}}, swing, intraday)
        s, i = a["books"]
        self.assertEqual((s["realized"], s["open"], s["commission"], s["value"]), (100.0, 50.0, 2.0, 900_148.0))
        self.assertEqual((i["open"], i["value"]), (10.0, 100_009.0))
        self.assertEqual(a["unassigned"], 1_026_000.0 - 900_148.0 - 100_009.0)

    def test_returns_on_the_whole_account_are_against_its_value_when_the_books_began(self):
        a = self.accounts({"net_liquidation": 1_010_000.0, "marks": {}}, [fill("AAA", "BOT", 100, 10.0, commission=0.0),
                          fill("AAA", "SLD", 100, 110.0, 11, commission=0.0)], [],
                          first={"net_liquidation": 1_000_000.0, "session": "2026-11-09"})
        self.assertEqual((a["books"][0]["account_pct"], a["books"][0]["return_pct"]), (1.0, 1.111))
        self.assertEqual((a["start_value"], a["pnl"], a["account_pct"]), (1_000_000.0, 10_000.0, 1.0))

    def test_a_symbol_ibkr_gave_no_price_for_is_marked_at_the_close(self):
        a = self.accounts({"net_liquidation": 1e6, "marks": {}}, [fill("AAA", "BOT", 100, 10.0)], [],
                          [{"book": "swing", "symbol": "AAA", "last": 10.5}])
        self.assertEqual((a["books"][0]["open"], a["books"][0]["marked_at_close"]), (50.0, ["AAA"]))

    def test_no_sync_yet_leaves_the_account_and_unassigned_blank(self):
        a = self.accounts({}, [], [])
        self.assertEqual((a["net_liquidation"], a["unassigned"], a["books"][0]["value"]), (None, None, 900_000.0))


class Deployed(unittest.TestCase):
    def test_gross_at_cost_is_averaged_over_time_since_the_first_fill(self):
        # $1,000 long from hour 9 to 11, then $1,000 long plus $500 short until hour 13: 4 hours in all.
        fs = [fill("AAA", "BOT", 10, 100.0, hour=9), fill("HHH", "SLD", 10, 50.0, hour=11)]
        d = portfolio.deployed(fs, now=datetime(2026, 11, 10, 13, 30, tzinfo=NY))
        self.assertEqual((d["now"], d["peak"], d["average"]), (1500.0, 1500.0, 1250.0))

    def test_a_flat_book_counts_its_idle_time(self):
        fs = [fill("AAA", "BOT", 10, 100.0, hour=9), fill("AAA", "SLD", 10, 101.0, hour=10)]
        d = portfolio.deployed(fs, now=datetime(2026, 11, 10, 13, 30, tzinfo=NY))
        self.assertEqual((d["now"], d["peak"], d["average"]), (0.0, 1000.0, 250.0))

    def test_no_fills_deploys_nothing(self):
        self.assertEqual(portfolio.deployed([])["average"], 0.0)


if __name__ == "__main__":
    unittest.main()
