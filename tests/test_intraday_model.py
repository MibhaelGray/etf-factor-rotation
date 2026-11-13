import unittest

from factor_rotation.intraday import model


def sig(symbol):
    return {"symbol": symbol, "hedge": "SPY", "beta": 1.0, "setup": "range_break", "direction": "long",
            "signal_at": "10:30 AM", "numbers": {}, "plan": {}}


class JudgeAll(unittest.TestCase):
    def test_each_signal_gets_its_own_headlines_and_verdict(self):
        sigs = [sig("AAA"), sig("BBB")]
        model.judge_all(sigs, decide=lambda s, news: {"decision": "take", "why": f"{s['symbol']} {news[0]}"},
                        news=lambda symbol: [f"news on {symbol}"])
        self.assertEqual([s["verdict"]["why"] for s in sigs], ["AAA news on AAA", "BBB news on BBB"])
        self.assertEqual(sigs[1]["headlines"], ["news on BBB"])

    def test_one_failure_leaves_the_others_judged(self):
        def decide(s, news):
            if s["symbol"] == "AAA":
                raise RuntimeError("boom")
            return {"decision": "pass"}
        sigs = [sig("AAA"), sig("BBB")]
        model.judge_all(sigs, decide=decide, news=lambda symbol: [])
        self.assertEqual(sigs[0]["verdict"]["decision"], "undecided")
        self.assertEqual(sigs[1]["verdict"]["decision"], "pass")

    def test_nothing_to_judge(self):
        model.judge_all([], decide=None, news=None)

    def test_parse_keeps_the_research(self):
        v = model._parse('Done.\n{"decision": "take", "conviction": 9, "catalyst": "c", "why": "w", "against": "a", '
                         '"coming": "n", "sources": ["s1", "s2", "s3", "s4", "s5", "s6"]}')
        self.assertEqual((v["conviction"], v["coming"], len(v["sources"])), (5, "n", 5))


if __name__ == "__main__":
    unittest.main()
