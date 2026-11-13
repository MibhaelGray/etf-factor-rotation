"""The intraday book's paper orders against a stand-in broker. No network, no gateway.

    uv run python -m unittest discover -s tests
"""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from factor_rotation.intraday import orders
from factor_rotation.paper.broker import DryRun, PaperSettings
from test_paper_book import Broker

SLOT = 3


def sig(decision="take", direction="long", symbol="AAA", hedge="HHH", price=50.0, hedge_price=100.0):
    return {"id": f"I-1111-{symbol}", "symbol": symbol, "hedge": hedge, "setup": "range_break", "direction": direction,
            "slot": SLOT, "numbers": {"price": price}, "hedge_price": hedge_price, "verdict": {"decision": decision}}


def run(doc, slot, b, saves=None):
    saves = saves if saves is not None else []
    with mock.patch.object(orders, "SETTLE", 0):
        return orders.step(doc, slot, lambda: saves.append([o.get("paper", {}).get("state") for o in doc["signals"]]), lambda: b)


class Orders(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.halt = Path(tmp.name) / "HALT"
        p = mock.patch.object(orders, "HALT", self.halt)
        p.start()
        self.addCleanup(p.stop)
        self.paused = mock.patch.object(orders.drawdown, "paused", return_value=None).start()   # not the real journal's
        self.addCleanup(mock.patch.stopall)

    def test_a_take_goes_out_as_two_market_orders_ten_thousand_a_side(self):
        doc, b, saves = {"signals": [sig(), sig("pass", symbol="BBB")]}, Broker(), []
        self.assertEqual(run(doc, SLOT, b, saves), [])
        self.assertEqual([(o["symbol"], o["action"], o["shares"], o["kind"]) for o in b.sent],
                         [("AAA", "BUY", 200, "MKT"), ("HHH", "SELL", 100, "MKT")])
        self.assertTrue(all(o["trade_id"] == "I-1111-AAA" for o in b.sent))
        self.assertEqual(saves, [["sending", None]])            # on disk before anything was sent
        p = doc["signals"][0]["paper"]
        self.assertEqual(p["state"], "entered")
        self.assertEqual(len(p["fills"]), 2)
        self.assertNotIn("paper", doc["signals"][1])            # passes never trade

    def test_a_short_sells_the_name_and_buys_the_hedge(self):
        doc, b = {"signals": [sig(direction="short")]}, Broker()
        run(doc, SLOT, b)
        self.assertEqual([(o["symbol"], o["action"]) for o in b.sent], [("HHH", "BUY"), ("AAA", "SELL")])

    def test_the_model_exit_closes_it_and_the_result_is_after_commissions(self):
        doc, b = {"signals": [sig()]}, Broker()
        run(doc, SLOT, b)
        run(doc, SLOT + 1, b)
        self.assertEqual(len(b.sent), 2)                         # nothing to do while the model holds
        doc["signals"][0]["exit"] = {"reason": "target"}
        run(doc, SLOT + 2, b)
        self.assertEqual([(o["symbol"], o["action"], o["shares"]) for o in b.sent[2:]], [("AAA", "SELL", 200), ("HHH", "BUY", 100)])
        p = doc["signals"][0]["paper"]
        self.assertEqual((p["state"], p["exit_reason"]), ("closed", "target"))
        self.assertEqual(p["result"], {"dollars": -4.0, "commission": 4.0, "bp": -4.0})   # all fills at 100, $1 each
        run(doc, SLOT + 3, b)
        self.assertEqual(len(b.sent), 4)                         # closed stays closed

    def test_flat_by_the_345_pm_bar_whatever_the_model_says(self):
        doc, b = {"signals": [sig()]}, Broker()
        run(doc, SLOT, b)
        run(doc, orders.FLAT_CHECK - 1, b)
        self.assertEqual(len(b.sent), 2)
        run(doc, orders.FLAT_CHECK, b)
        self.assertEqual(doc["signals"][0]["paper"]["exit_reason"], "flat by 3:45 PM")
        self.assertEqual(doc["signals"][0]["paper"]["state"], "closed")

    def test_a_pair_with_a_dead_leg_is_closed_at_the_next_tick(self):
        doc, b = {"signals": [sig()]}, Broker(fill=lambda o: o["symbol"] != "HHH")
        run(doc, SLOT, b)
        run(doc, SLOT + 1, b)
        self.assertEqual([(o["symbol"], o["action"], o["shares"]) for o in b.sent[2:]], [("AAA", "SELL", 200)])
        self.assertEqual(doc["signals"][0]["paper"]["exit_reason"], "broken: HHH never filled")

    def test_nothing_filled_means_not_taken(self):
        doc, b = {"signals": [sig()]}, Broker(fill=lambda o: False)
        run(doc, SLOT, b)
        w = run(doc, SLOT + 1, b)
        self.assertEqual(doc["signals"][0]["paper"]["state"], "unfilled")
        self.assertIn("neither leg", w[0])
        self.assertEqual(len(b.sent), 2)

    def test_an_exit_that_did_not_fill_is_sent_again(self):
        doc = {"signals": [sig()]}
        b = Broker()
        run(doc, SLOT, b)
        b.fill = lambda o: False
        doc["signals"][0]["exit"] = {"reason": "stop"}
        run(doc, SLOT + 1, b)
        self.assertEqual(doc["signals"][0]["paper"]["state"], "exiting")
        b.fill = lambda o: True
        w = run(doc, SLOT + 2, b)
        self.assertIn("sent again", w[0])
        self.assertEqual(doc["signals"][0]["paper"]["state"], "closed")
        self.assertEqual(len(b.sent), 6)

    def test_a_dry_run_records_orders_and_sends_nothing(self):
        doc = {"signals": [sig()]}
        dry = DryRun(PaperSettings(send=False))
        run(doc, SLOT, dry)
        p = doc["signals"][0]["paper"]
        self.assertEqual((p["state"], p["entry_orders"][0]["status"]), ("dry run", "dry run"))
        doc["signals"][0]["exit"] = {"reason": "stop"}
        run(doc, SLOT + 1, dry)
        self.assertEqual((p["state"], len(p["exit_orders"]), p.get("result")), ("closed", 2, None))

    def test_a_paused_book_sends_no_entries(self):
        self.paused.return_value = "the intraday book is down $10,500"
        doc, b = {"signals": [sig()]}, Broker()
        warnings = run(doc, SLOT, b)
        self.assertEqual(b.sent, [])
        self.assertEqual(doc["signals"][0]["paper"]["state"], "not sent")
        self.assertIn("paused", warnings[0])
        self.paused.assert_called_with("intraday")

    def test_halt_stops_entries(self):
        self.halt.write_text("testing")
        doc, b = {"signals": [sig()]}, Broker()
        run(doc, SLOT, b)
        self.assertEqual(b.sent, [])
        self.assertEqual(doc["signals"][0]["paper"]["state"], "not sent")

    def test_a_gateway_that_fails_gives_up_the_entry_and_keeps_open_trades(self):
        doc, b = {"signals": [sig()]}, Broker()
        run(doc, SLOT, b)
        doc["signals"].append(sig(symbol="CCC"))
        doc["signals"][0]["exit"] = {"reason": "stop"}

        class Down:
            async def __aenter__(self):
                raise ConnectionError("no gateway")

            async def __aexit__(self, *exc):
                return None

        w = run(doc, SLOT + 1, Down())
        self.assertIn("no gateway", w[0])
        self.assertEqual(doc["signals"][1]["paper"]["state"], "not sent")
        self.assertEqual(doc["signals"][0]["paper"]["state"], "entered")   # its exit goes at the next tick
        run(doc, SLOT + 2, b)
        self.assertEqual(doc["signals"][0]["paper"]["state"], "closed")

    def test_what_the_intraday_book_holds_is_known_to_the_swing_book(self):
        doc, b = {"signals": [sig()]}, Broker()
        run(doc, SLOT, b)
        with tempfile.TemporaryDirectory() as root, mock.patch.object(orders.journal, "JOURNAL", Path(root)):
            days = Path(root) / "intraday" / "days"
            days.mkdir(parents=True)
            (days / "2026-11-11.json").write_text(json.dumps(doc))
            self.assertEqual(orders.held_now(date(2026, 11, 11)), {"AAA": 200, "HHH": -100})


if __name__ == "__main__":
    unittest.main()
