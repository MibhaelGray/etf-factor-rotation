import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from factor_rotation import earnings, universe
from factor_rotation.config import NY


class Tiers(unittest.TestCase):
    """The wider swing tier reaches the screen, bars and hedges, and nothing else."""

    def test_the_wide_tier_reaches_the_screen_and_the_bars(self):
        self.assertGreater(len(universe.INDEX_STOCKS), 500)
        self.assertTrue(set(universe.INDEX_STOCKS) <= set(universe.screen_universe()))
        self.assertTrue(set(universe.INDEX_STOCKS) <= set(universe.bar_universe()))
        self.assertFalse(set(universe.INDEX_STOCKS) & set(universe.CONSTITUENTS))

    def test_option_chains_stay_on_the_core_funds(self):
        self.assertFalse(set(universe.MORE_INDUSTRIES) & set(universe.option_universe()))
        self.assertFalse(set(universe.INDEX_STOCKS) & set(universe.option_universe()))
        self.assertTrue(set(universe.MORE_INDUSTRIES) <= set(universe.ETFS))

    def test_every_factor_pair_leg_is_a_fund_the_brief_loads(self):
        for p in universe.FACTOR_PAIRS:
            self.assertIn(p.long, universe.idea_universe(), p.name)
            self.assertIn(p.short, universe.idea_universe(), p.name)


class EarningsRotation(unittest.TestCase):
    """Yahoo refuses after about 500 quick calls, so the wide tier is pulled a slice a night."""

    def setUp(self):
        self.core = ["AAA", "BBB"]
        self.wide = [f"W{i:03d}" for i in range(400)]
        for name, value in (("INDEX_STOCKS", self.wide), ("_core", lambda: set(self.core))):
            p = mock.patch.object(earnings, name, value)
            p.start()
            self.addCleanup(p.stop)

    def due(self, kept, pulled, today=date(2026, 11, 12)):
        return earnings._due(self.core + self.wide, kept, pulled, today)

    def test_core_every_night_and_the_wide_tier_capped(self):
        todo = self.due({}, {})
        self.assertEqual(todo[:2], self.core)
        self.assertEqual(len(todo), 2 + earnings.WIDE_PER_NIGHT)

    def test_a_report_near_today_jumps_the_queue_and_the_stalest_come_next(self):
        kept = {s: [{"date": "2026-12-20"}] for s in self.wide}
        pulled = {s: "2026-11-11" for s in self.wide}
        pulled["W399"] = "2026-11-01"                                  # pulled longest ago
        kept["W398"] = [{"date": "2026-11-18"}]                        # reports inside ten days
        todo = self.due(kept, pulled)
        self.assertEqual(todo[2:4], ["W398", "W399"])

    def test_a_name_that_fails_keeps_its_dates_and_names_not_pulled_are_kept(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "earnings.json"
        old = {s: [{"date": "2026-12-20"}] for s in [*self.core, *self.wide]}
        path.write_text(json.dumps({"names": old, "pulled": {s: "2026-11-11" for s in old}}))
        rows = [{"date": "2027-02-01"}]
        with mock.patch.object(earnings, "PATH", path), mock.patch.object(earnings, "symbols", lambda: self.core + self.wide), \
                mock.patch.object(earnings, "_dates", lambda s: None if s == "BBB" else rows):
            out = earnings.pull(sleep=lambda s: None, now=datetime(2026, 11, 12, 19, 0, tzinfo=NY))
        doc = json.loads(path.read_text())
        self.assertEqual(out["failed"], ["BBB"])
        self.assertEqual(doc["names"]["BBB"], old["BBB"])              # the last good dates stay
        self.assertEqual(doc["names"]["AAA"], rows)
        self.assertEqual(len(doc["names"]), 402)                       # names not pulled tonight are kept
        self.assertEqual(sum(v.startswith("2026-11-12") for v in doc["pulled"].values()), 1 + earnings.WIDE_PER_NIGHT)


if __name__ == "__main__":
    unittest.main()
