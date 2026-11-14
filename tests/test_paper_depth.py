"""The research-depth check, on made-up Claude Code transcripts. No network.

    uv run python -m unittest discover -s tests
"""

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from factor_rotation.paper import depth

SESSION = "abc-123"


def turn(model, *tools):
    return {"type": "assistant", "message": {"model": model, "content": [{"type": "tool_use", "name": t} for t in tools]}}


class Depth(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        p = mock.patch.dict(os.environ, {"FR_TRANSCRIPTS": tmp.name, "CLAUDE_CODE_SESSION_ID": SESSION, "CLAUDE_EFFORT": "high"})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(depth.proposals, "root", lambda: self.dir / "paper")
        p.start()
        self.addCleanup(p.stop)
        self.main(depth.JUDGE_MODEL)

    def main(self, model):
        (self.dir / f"{SESSION}.jsonl").write_text(json.dumps({"type": "user", "message": {"content": "run the evening pass"}}) + "\n"
                                                   + "not json\n" + json.dumps(turn(model, "Bash", "Agent")) + "\n")

    def agent(self, key, prompt, searches, reads, model=depth.AGENT_MODEL, what="Research"):
        d = self.dir / SESSION / "subagents"
        d.mkdir(parents=True, exist_ok=True)
        rows = [{"type": "user", "message": {"content": [{"type": "text", "text": prompt}]}},
                turn(model, "ToolSearch", *["WebSearch"] * searches, *["WebFetch"] * reads)]
        (d / f"agent-{key}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        (d / f"agent-{key}.meta.json").write_text(json.dumps({"description": what, "effort": "medium"}))

    def test_a_name_with_its_own_agent_and_enough_reading_passes(self):
        self.agent("a", "NAMES: YETI\nWhat is the story?", searches=9, reads=5)
        self.agent("m", "What is the market arguing about this week?", searches=12, reads=6, what="Market stories")   # no NAMES line: not measured
        m = depth.measure(candidates=["YETI"])
        self.assertEqual((m["names"], m["thin"], m["flags"]), ({"YETI": {"searches": 9, "reads": 5, "agents": 1, "shared_with": 0}}, {}, []))
        self.assertEqual((m["judge"], len(m["agents"])), ({"model": depth.JUDGE_MODEL, "effort": "high"}, 2))
        self.assertIn("Every name is at or over the floors.", depth.report(m))

    def test_thin_research_a_crowded_agent_and_a_candidate_nobody_researched_are_named(self):
        self.agent("a", "NAMES: AAA, BBB, CCC\nthe story please", searches=30, reads=15)       # plenty of work, three names
        self.agent("b", "NAMES: DDD", searches=7, reads=2)
        m = depth.measure(candidates=["AAA", "BBB", "CCC", "DDD", "EEE"])
        self.assertEqual(m["names"]["AAA"], {"searches": 10, "reads": 5, "agents": 1, "shared_with": 2})
        self.assertEqual(m["thin"], {"AAA": "its agent had 3 names; 2 at most", "BBB": "its agent had 3 names; 2 at most",
                                     "CCC": "its agent had 3 names; 2 at most", "DDD": "2 pages read, the floor is 4",
                                     "EEE": "no research agent named it"})
        self.assertIn("Thin: AAA, BBB, CCC, DDD, EEE.", depth.report(m))
        self.agent("c", "NAMES: DDD\nHere is the first write-up; what is wrong or missing?", searches=3, reads=4)   # a second agent counts on top
        again = depth.measure(candidates=["DDD"])
        self.assertEqual((again["names"]["DDD"]["agents"], "DDD" in again["thin"]), (2, False))

    def test_the_wrong_model_is_flagged_for_the_pass_and_for_an_agent(self):
        self.main("claude-opus-5-5")
        self.agent("a", "NAMES: YETI", searches=9, reads=5, model="claude-fable-5-2")
        with mock.patch.dict(os.environ, {"CLAUDE_EFFORT": "medium"}):
            flags = depth.measure(candidates=[])["flags"]
        self.assertEqual(len(flags), 3)
        self.assertIn("this pass is running on claude-opus-5-5, not claude-fable-5-2", flags[0])
        self.assertIn("medium effort, not high", flags[1])
        self.assertIn("the agent for YETI ran on claude-fable-5-2, not claude-opus-5-5", flags[2])

    def test_the_measurement_is_kept_once_a_session_and_a_missing_transcript_breaks_nothing(self):
        self.agent("a", "NAMES: YETI", searches=9, reads=5)
        day = date(2026, 11, 16)
        depth.keep(day, depth.measure(candidates=["YETI"]))
        path = depth.keep(day, depth.measure(candidates=["YETI"]))
        doc = json.loads(path.read_text())
        self.assertEqual((len(doc["passes"]), doc["passes"][0]["names"]["YETI"]["reads"]), (1, 5))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "gone"}):
            m = depth.measure(candidates=[])
            self.assertEqual((m["measured"], depth.keep(day, m)), (False, None))
            self.assertIn("not measured", depth.report(m))


if __name__ == "__main__":
    unittest.main()
