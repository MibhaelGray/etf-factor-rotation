"""The side facts: recorded beside the night's verdicts, kept from the passes, scored night by night.
A stand-in for Yahoo and a temporary journal. No network.

    uv run python -m unittest discover -s tests
"""

import json
from datetime import datetime

from factor_rotation.config import NY
from factor_rotation.paper import learn, side

from test_paper_book import DAY, SCREEN, SUNDAY_EVENING, Research, frame, verdict

NAMES = ["AAA", "BBB", "CCC", "DDD"]
PLAN = {"horizon_days": 2, "stop": {"spread_pct": -8}}
LATER = datetime(2026, 11, 8, 21, 30, tzinfo=NY)


class Side(Research):
    def setUp(self):
        super().setUp()
        self.asked = []
        self.short = {"AAA": 0.02, "BBB": 0.05, "CCC": 0.10, "DDD": 0.20}

    def night(self, direction="long"):
        screen = {**SCREEN, "candidates": [{**SCREEN["candidates"][0], "name": n, "direction": direction} for n in NAMES]}
        (self.root.parent / "screens" / "2026-11-06.json").write_text(json.dumps(screen))
        self.propose(verdict(plan=PLAN), *[{"candidate": n, "decision": "pass", "conviction": 2, "reason": "no story", "plan": PLAN} for n in NAMES[1:]])

    def yahoo(self, symbol, now):
        self.asked.append(symbol)
        value = self.short.get(symbol)
        return {"at": now.isoformat(), "facts": {"short_pct_float": value} if value is not None else {}, "raw": {}}

    def record(self, now=LATER, **kw):
        return side.record(now, puller=self.yahoo, pause=0, **kw)

    def test_each_idea_s_stock_is_pulled_once_and_its_hedge_never(self):
        self.assertIn("no verdicts recorded", self.record()["note"])
        self.night()
        self.assertEqual(self.record(dry=True)["todo"], NAMES)                # a look pulls and writes nothing
        self.assertEqual((self.asked, side.load(DAY)), ([], None))
        out = self.record()
        self.assertEqual((out["pulled"], out["failed"], self.asked), (NAMES, [], NAMES))   # HHH, the hedge, isn't asked for
        self.assertEqual(side.load(DAY)["symbols"]["CCC"]["facts"], {"short_pct_float": 0.10})
        again = self.record()
        self.assertEqual((again["pulled"], again["have"], self.asked, len(side.load(DAY)["runs"])), ([], 4, NAMES, 1))

    def test_a_name_yahoo_had_nothing_for_is_tried_again_and_nothing_is_pulled_after_the_open(self):
        self.night()
        del self.short["DDD"]
        self.assertEqual(self.record()["failed"], ["DDD"])
        self.short["DDD"] = 0.2
        self.assertEqual(self.record()["pulled"], ["DDD"])
        (self.root / "side" / f"{DAY}.json").unlink()
        opened = self.record(datetime(DAY.year, DAY.month, DAY.day, 9, 30, tzinfo=NY))
        self.assertEqual(opened["pulled"], [])
        self.assertIsNone(side.load(DAY))

    def test_a_fact_is_scored_against_how_the_stock_then_did_night_by_night(self):
        self.night()
        self.record()
        self.assertEqual(side.table()["facts"]["short_pct_float"]["nights"], 0)      # nothing has finished
        # short interest 2%, 5%, 10%, 20% and the stocks finish +6%, +4%, -2%, -3% against a flat hedge
        self.bars = frame({"AAA": [103.0, 106.0], "BBB": [102.0, 104.0], "CCC": [99.0, 98.0], "DDD": [99.0, 97.0], "HHH": [100.0, 100.0]})
        t = side.table()
        self.assertEqual((t["recorded_nights"], t["scored_nights"]), (1, 1))
        self.assertEqual(t["facts"]["short_pct_float"], {"nights": 1, "ideas": 4, "ic": -1.0, "t": None})
        self.assertEqual(t["facts"]["rating_mean"]["nights"], 0)                     # a fact nobody had
        self.assertIn("short_pct_float        -1.00 over 1 night, 4 ideas", side.report())

    def test_a_short_idea_is_scored_on_its_stock_not_on_its_result(self):
        self.night("short")
        self.record()
        # the same stocks and the same moves: the shorts of the heavily shorted names made money, and the fact still
        # reads as "more short interest, worse stock"
        self.bars = frame({"AAA": [103.0, 106.0], "BBB": [102.0, 104.0], "CCC": [99.0, 98.0], "DDD": [99.0, 97.0], "HHH": [100.0, 100.0]})
        self.assertEqual(side.table()["facts"]["short_pct_float"]["ic"], -1.0)

    def test_the_passes_brief_says_nothing_of_the_side_facts(self):
        self.night()
        self.record()
        self.bars = frame({"AAA": [103.0, 106.0], "BBB": [102.0, 104.0], "CCC": [99.0, 98.0], "DDD": [99.0, 97.0], "HHH": [100.0, 100.0]})
        brief = learn.brief().lower()
        for word in ("side fact", "short_pct", "short interest", "insider"):
            self.assertNotIn(word, brief)
