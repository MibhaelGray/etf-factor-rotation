"""The Hormuz view's numbers: last week against the base year, and the scaled flow estimates."""

import unittest

from factor_rotation import hormuz


def days(base_cap, recent_cap):
    rows = [[f"2025-{m:02d}-15", 50, 90, base_cap, base_cap] for m in range(1, 13)]
    return rows + [[f"2026-03-{d:02d}", 1, 4, recent_cap, recent_cap] for d in range(1, 15)]


class Summary(unittest.TestCase):
    def test_flows_scale_with_tanker_capacity(self):
        s = hormuz.summary(days(1000.0, 100.0))
        self.assertAlmostEqual(s["share"], 0.1)
        self.assertAlmostEqual(s["oil_mbd"], hormuz.EIA["oil_mbd"] * 0.1)
        self.assertAlmostEqual(s["lng_bcfd"], hormuz.EIA["lng_bcfd"] * 0.1, places=2)
        self.assertEqual(s["tankers_7d"], 1)

    def test_normal_traffic_has_no_closure_date(self):
        self.assertIsNone(hormuz.summary(days(1000.0, 1000.0))["below_quarter_since"])

    def test_a_collapse_is_dated_after_the_base_year(self):
        self.assertTrue(hormuz.summary(days(1000.0, 10.0))["below_quarter_since"].startswith("2026-03"))

    def test_no_base_year_gives_no_estimate(self):
        s = hormuz.summary([["2026-03-01", 1, 2, 10, 10]])
        self.assertIsNone(s["share"])
        self.assertIsNone(s["oil_mbd"])


if __name__ == "__main__":
    unittest.main()


class Live(unittest.TestCase):
    def test_ship_types_sort_into_oil_gas_and_other(self):
        self.assertEqual(hormuz._kind("Crude Oil Tanker"), "oil")
        self.assertEqual(hormuz._kind("LNG Tanker"), "gas")
        self.assertEqual(hormuz._kind("Bunkering Tanker"), "other")
        self.assertEqual(hormuz._kind("Bulk Carrier"), "other")

    def test_crossings_leave_out_ships_sitting_in_the_strait(self):
        from unittest import mock
        replies = {
            "crossings/daily/by_cat": [{"day": "2026-11-10", "direction": "inbound", "ship_category": "Crude Oil Tanker", "count": 2},
                                       {"day": "2026-11-10", "direction": "in_strait", "ship_category": "Crude Oil Tanker", "count": 9},
                                       {"day": "2026-11-10", "direction": "outbound", "ship_category": "LNG Tanker", "count": 1}],
            "crossings/by_type": [{"direction": "inbound", "ship_category": "Tanker", "count": 3}],
            "summary": {"last_poll": "2026-11-10T18:00:00+00:00", "total_crossings": 4, "inbound": 3, "outbound": 1},
            "oil_export/daily": {"days": [{"day": "2026-11-09", "total_barrels": 100}], "avg_bbl_per_day": 100},
        }
        saved = {}
        with mock.patch.object(hormuz, "_get", side_effect=lambda url, p: replies[url.split("/api/")[1]]), \
                mock.patch.object(hormuz, "_save", side_effect=lambda n, doc: saved.__setitem__(n, doc)):
            hormuz.pull_live()
        day = saved["live"]["days"][0]
        self.assertEqual((day["in"], day["out"], day["oil"], day["gas"], day["other"]), (2, 1, 2, 1, 0))
        self.assertEqual(saved["live"]["last24"]["oil"], 3)
