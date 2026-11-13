import tempfile
import unittest
from pathlib import Path
from unittest import mock

from factor_rotation import drawdown, portfolio
from factor_rotation.paper import book


class Pause(unittest.TestCase):
    """The 10% pause: a book down 10% of its capital from its high opens nothing new until resumed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.value = {"swing": 900_000.0, "intraday": 100_000.0}
        self.history = {"swing": [], "intraday": []}
        for target, name, v in ((drawdown.journal, "JOURNAL", self.root),
                                (portfolio, "accounts", lambda: {"books": [{"book": b, "value": x} for b, x in self.value.items()]}),
                                (portfolio, "fills", lambda b: [b] if self.history[b] else []),
                                (book, "pnl_by_session", lambda fs: self.history[fs[0]])):
            p = mock.patch.object(target, name, v)
            p.start()
            self.addCleanup(p.stop)

    def hist(self, b, *pnls):
        self.history[b] = [{"date": f"2026-11-{10 + i:02d}", "pnl": p} for i, p in enumerate(pnls)]

    def test_limits_are_ten_percent_of_each_books_capital(self):
        self.assertEqual(drawdown.status("swing")["limit"], 90_000)
        self.assertEqual(drawdown.status("intraday")["limit"], 10_000)

    def test_a_small_loss_doesnt_pause(self):
        self.value["swing"] = 850_000.0
        self.assertIsNone(drawdown.paused("swing"))
        self.assertFalse(drawdown.pause_path("swing").exists())

    def test_a_ten_percent_loss_pauses_and_the_pause_sticks(self):
        self.value["intraday"] = 89_999.0
        why = drawdown.paused("intraday")
        self.assertIn("intraday book is down $10,001", why)
        self.assertTrue(drawdown.pause_path("intraday").exists())
        self.value["intraday"] = 100_000.0                         # recovered, still paused until resumed
        self.assertEqual(drawdown.paused("intraday"), why)
        self.assertIsNone(drawdown.paused("swing"))                # the other book trades on

    def test_the_drop_counts_from_the_high_not_from_the_capital(self):
        self.hist("swing", 0.0, 60_000.0, 20_000.0)                 # up $60,000 at one close
        self.value["swing"] = 869_000.0                             # $91,000 below that high, $31,000 below capital
        s = drawdown.status("swing")
        self.assertEqual((s["peak"], s["pause_at"]), (960_000.0, 870_000.0))
        self.assertIsNotNone(drawdown.paused("swing"))

    def test_resume_lifts_the_pause_and_starts_the_high_again(self):
        self.value["intraday"] = 88_000.0
        self.assertIsNotNone(drawdown.paused("intraday"))
        drawdown.resume("intraday")
        self.assertIsNone(drawdown.paused("intraday"))              # $88,000 is the new high
        self.assertEqual(drawdown.status("intraday")["pause_at"], 78_000.0)
        self.value["intraday"] = 77_500.0
        self.assertIsNotNone(drawdown.paused("intraday"))

    def test_a_failed_check_doesnt_stop_trading(self):
        with mock.patch.object(portfolio, "accounts", side_effect=RuntimeError("no sync")):
            self.assertIsNone(drawdown.paused("swing"))


if __name__ == "__main__":
    unittest.main()
