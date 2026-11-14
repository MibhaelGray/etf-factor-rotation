"""The hold-period table: every idea scored at every hold, from the proposals and the bars. No network.

    uv run python -m unittest discover -s tests
"""

import json

from factor_rotation.paper import holds, learn

from test_paper_book import SCREEN, Research, frame, verdict

NAMES = ["AAA", "BBB", "CCC", "DDD"]
PLAN = {"horizon_days": 2, "stop": {"spread_pct": -4}}
# AAA climbs, BBB rises then gives it back, CCC falls through its 4% stop and recovers, DDD only falls
CLOSES = {"AAA": [102.0, 104.0, 106.0], "BBB": [103.0, 101.0, 99.0], "CCC": [95.0, 101.0, 103.0], "DDD": [99.0, 98.0, 97.0], "HHH": [100.0, 100.0, 100.0]}


class Holds(Research):
    def night(self):
        screen = {**SCREEN, "candidates": [{**SCREEN["candidates"][0], "name": n} for n in NAMES]}
        (self.root.parent / "screens" / "2026-11-06.json").write_text(json.dumps(screen))
        self.propose(verdict(plan=PLAN), *[{"candidate": n, "decision": "pass", "conviction": 2, "reason": "no story", "plan": PLAN} for n in NAMES[1:]])

    def test_every_idea_is_scored_at_every_hold_it_has_reached_with_and_without_its_stop(self):
        self.night()
        self.assertEqual(holds.table()["so_far"]["ideas"], 0)                        # no bars yet
        self.assertIn("no idea has a settled first session yet", holds.report())
        self.bars = frame(CLOSES)
        rows = {r["name"]: r for r in holds.rows()}
        self.assertEqual((rows["AAA"]["held"], rows["AAA"]["taken"], rows["AAA"]["limit"]), ([200.0, 400.0, 600.0], True, 2))
        self.assertEqual((rows["CCC"]["held"], rows["CCC"]["stopped"]), ([-500.0, 100.0, 300.0], [-500.0, -500.0, -500.0]))
        t = holds.table()
        a = t["so_far"]
        self.assertEqual((t["nights"], a["ideas"], t["complete"]["ideas"]), (1, 4, 0))   # nobody has 10 sessions in
        self.assertEqual([a["held"]["all ideas"][h]["mean"] for h in (1, 2, 3)], [-25.0, 100.0, 125.0])
        self.assertEqual([a["stopped"]["all ideas"][h]["mean"] for h in (1, 2, 3)], [-25.0, -50.0, -75.0])
        self.assertEqual((a["held"]["takes"][3]["mean"], a["held"]["not taken"][3]["n"], a["held"]["all ideas"][4]), (600.0, 3, {"n": 0}))
        # the plan itself: two sessions with the stop, so CCC is out at -500 on the first close
        self.assertEqual(a["own_limit"]["all ideas"], {**learn._stat([400.0, 100.0, -500.0, -200.0]), "sessions": 2})
        self.assertEqual(a["by_setup"]["breakout"][1]["n"], 4)

    def test_the_edge_at_each_hold_is_worked_night_by_night(self):
        self.night()
        self.bars = frame(CLOSES)
        e = holds.table()["so_far"]["edges"]
        # ranked AAA, BBB, CCC, DDD: after one session they stand +2%, +3%, -5%, -1%; after three, +6%, -1%, +3%, -3%,
        # so one pair is out of order
        self.assertEqual((e["rank"][1], e["rank"][3]["mean"], e["rank"][4]), ({"nights": 1, "mean": 0.6, "t": None}, 0.8, {"nights": 0}))
        self.assertEqual((e["takes_less_rest"][1]["mean"], e["takes_less_rest"][3]["mean"]), (300.0, 633.333))
        text = holds.report()
        for line in ("ideas counted               4      4      4      .", "rank correlation        +0.60  +0.95  +0.80      .",
                     "No idea has all 10 sessions in yet"):
            self.assertIn(line, text)

    def test_the_passes_brief_says_nothing_of_hold_periods(self):
        self.night()
        self.bars = frame(CLOSES)
        self.assertNotIn("hold period", learn.brief().lower())
        self.assertNotIn("sessions held", learn.brief().lower())

    def test_the_pass_s_own_ideas_are_set_beside_the_screen_s(self):
        screen = {**SCREEN, "candidates": [{**SCREEN["candidates"][0], "name": n} for n in NAMES[:3]]}
        (self.root.parent / "screens" / "2026-11-06.json").write_text(json.dumps(screen))
        own = verdict(candidate=None, name="DDD before its investor day", setup="catalyst", direction="long", long=["DDD"], short=["HHH"], plan=PLAN)
        self.propose(verdict(plan=PLAN), own, *[{"candidate": n, "decision": "pass", "conviction": 2, "reason": "no story", "plan": PLAN} for n in NAMES[1:3]])
        self.bars = frame(CLOSES)
        held = holds.table()["so_far"]["held"]
        self.assertEqual((held["from the screen"][1]["n"], held["the pass's own ideas"][1], held["the pass's own ideas"][3]["mean"]),
                         (3, learn._stat([-100.0]), -300.0))
        self.assertIn("the pass's own ideas     -100   -200   -300      .", holds.report())

