"""The Today view's routine check: a step is done only when it left its record."""

import json
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path
from unittest import mock

from factor_rotation import journal, routine
from factor_rotation.config import NY

DAY = date(2026, 11, 10)  # a Tuesday


def at(h, m=0, d=DAY):
    return datetime.combine(d, time(h, m), NY)


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for p in (mock.patch.object(journal, "JOURNAL", self.root), mock.patch.object(routine, "LOGS", self.root / "logs"),
                  mock.patch.object(routine.storage, "modified", return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def write(self, rel, doc):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc))

    def state(self, key, now, day=DAY):
        return next(r for r in routine.check(day, now) if r["key"] == key)

    def test_a_plan_file_means_the_entry_job_ran(self):
        self.write(f"paper/plans/{DAY}.json", {"built_at": at(9, 15).isoformat(), "trades": [{}], "orders": [{}, {}]})
        r = self.state("paper-open", at(10))
        self.assertEqual(r["state"], "done")
        self.assertEqual(r["detail"], "1 trades, 2 orders")

    def test_before_its_time_a_step_is_upcoming_then_due_then_missed(self):
        self.assertEqual(self.state("paper-close", at(15))["state"], "upcoming")
        self.assertEqual(self.state("paper-close", at(15, 50))["state"], "running")
        self.assertEqual(self.state("paper-close", at(17))["state"], "missed")

    def test_a_job_that_logged_but_left_nothing_failed(self):
        (self.root / "logs").mkdir()
        local = at(15, 46).astimezone()  # the log is in the Mac's own time zone
        (self.root / "logs" / "paper-close.log").write_text(f"{local:%Y-%m-%d %H:%M:%S},1 ERROR boom\n")
        r = self.state("paper-close", at(17))
        self.assertEqual(r["state"], "failed")
        self.assertIn("boom", r["detail"])

    def test_a_log_from_another_day_is_not_counted(self):
        (self.root / "logs").mkdir()
        (self.root / "logs" / "paper-close.log").write_text(f"{at(15, 46, date(2026, 11, 11)).astimezone():%Y-%m-%d %H:%M:%S},1 ERROR boom\n")
        self.assertEqual(self.state("paper-close", at(17))["state"], "missed")

    def test_a_screen_without_a_brief_is_a_failed_nightly_job(self):
        self.write(f"screens/{DAY}.json", {"built_at": at(21, 20).isoformat()})
        r = self.state("nightly", at(22, 30))
        self.assertEqual(r["state"], "failed")
        self.assertIn("No brief", r["detail"])

    def test_the_evening_pass_is_found_in_the_next_sessions_proposals(self):
        self.write("paper/proposals/2026-11-11.json", {"passes": [{"pass": "evening", "at": at(20).isoformat()}]})
        self.assertEqual(self.state("evening", at(23))["state"], "done")

    def test_a_manual_entry_is_not_a_pre_open_pass(self):
        self.write(f"paper/proposals/{DAY}.json", {"passes": [{"pass": "manual", "at": at(12).isoformat()}]})
        self.assertEqual(self.state("preopen", at(12, 30))["state"], "missed")

    def test_a_holiday_has_nothing_scheduled(self):
        holiday = date(2026, 11, 26)
        self.assertEqual(self.state("paper-open", at(12, d=holiday), holiday)["state"], "off")


if __name__ == "__main__":
    unittest.main()
