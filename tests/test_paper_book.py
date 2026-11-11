"""The paper book's jobs against a stand-in broker and a temporary journal. No network, no gateway.

    uv run python -m unittest discover -s tests
"""

import asyncio
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from factor_rotation.paper import book, rules

DAY = date(2026, 11, 9)       # a Monday
NEXT = date(2026, 11, 10)


class Broker:
    """Records what it is asked to send. `fill` decides which sent orders fill (default: all, at 100)."""

    def __init__(self, fills=(), positions=None, orders=(), fill=lambda o: True, fail_place=False):
        self._fills, self._positions, self._orders = list(fills), dict(positions or {}), list(orders)
        self.fill, self.fail_place, self.sent, self.connections = fill, fail_place, [], 0

    async def __aenter__(self):
        self.connections += 1
        return self

    async def __aexit__(self, *exc):
        return None

    async def place(self, orders):
        if self.fail_place:
            raise ConnectionError("the gateway went away")
        self.sent += orders
        for o in orders:
            if self.fill(o):
                side = "BOT" if o["action"] == "BUY" else "SLD"
                self._fills.append(fill(o["trade_id"], o["symbol"], side, o["shares"], n=len(self._fills)))
                self._positions[o["symbol"]] = self._positions.get(o["symbol"], 0) + (o["shares"] if side == "BOT" else -o["shares"])
        return [{**o, "order_id": i, "status": "PreSubmitted", "note": ""} for i, o in enumerate(orders)]

    async def fills(self):
        return list(self._fills)

    async def orders(self):
        return list(self._orders)

    async def positions(self):
        return {s: v for s, v in self._positions.items() if v}

    async def net_liquidation(self):
        return 1_000_000.0


def fill(trade_id, symbol, side, shares, price=100.0, n=0):
    return {"order_ref": f"{trade_id}|{symbol}", "symbol": symbol, "side": side, "shares": float(shares), "price": price,
            "commission": 1.0, "time": None, "exec_id": f"{trade_id}-{symbol}-{side}-{n}"}


def pair(trade_id, long="AAA", short="HHH", entry=DAY, stop=None, horizon=4):
    return {"id": trade_id, "book": "dynamic", "source": "test", "name": long, "setup": "breakout", "direction": "long", "why": "",
            "entry_date": entry.isoformat(), "horizon_days": horizon, "stop": stop if stop is not None else {"spread_pct": -5.0},
            "notional": 2500, "hedge": short,
            "legs": [{"symbol": long, "side": "long", "shares": 25, "ref_price": 100.0},
                     {"symbol": short, "side": "short", "shares": 25, "ref_price": 100.0}]}


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "paper"
        for target, value in ((book, {"ROOT": self.root, "HALT": self.root / "HALT", "sleep": lambda s: None}),
                              (book.journal, {"JOURNAL": Path(tmp.name)})):
            for name, v in value.items():
                p = mock.patch.object(target, name, v)
                p.start()
                self.addCleanup(p.stop)
        p = mock.patch.object(book.asyncio, "sleep", mock.AsyncMock())
        p.start()
        self.addCleanup(p.stop)

    def write(self, kind, session, payload):
        book._write(self.root / kind / f"{session}.json", {"session": session.isoformat(), **payload})

    def read(self, kind, session):
        return json.loads((self.root / kind / f"{session}.json").read_text())

    def state(self, trade_id):
        return next(t["state"] for t in book.trades() if t["id"] == trade_id)


class Quotes(Case):
    def test_asks_again_only_for_what_is_missing(self):
        answers = [{}, {"AAA": 10.0}, {"HHH": 20.0}]
        asked = []
        with mock.patch.object(book, "_yahoo", lambda symbols: (asked.append(list(symbols)), answers.pop(0))[1]):
            self.assertEqual(book.quotes(["AAA", "HHH"], tries=4, wait=1), {"AAA": 10.0, "HHH": 20.0})
        self.assertEqual(asked, [["AAA", "HHH"], ["AAA", "HHH"], ["HHH"]])

    def test_gives_up_after_the_last_try(self):
        with mock.patch.object(book, "_yahoo", lambda symbols: {}):
            self.assertEqual(book.quotes(["AAA"], tries=3, wait=1), {})


class Orders(unittest.TestCase):
    def test_only_dead_orders_and_gateway_messages_are_warned_about(self):
        o = {"trade_id": "T1", "action": "SELL", "shares": 5, "symbol": "AAA", "note": ""}
        quiet = [{**o, "status": s} for s in ("PreSubmitted", "Submitted", "PendingSubmit", "Filled", "dry run")]
        self.assertEqual(book._order_warnings(quiet), [])
        self.assertIn("was not accepted (Cancelled)", book._order_warnings([{**o, "status": "Cancelled"}])[0])
        self.assertIn("held while shares are located", book._order_warnings([{**o, "status": "PreSubmitted", "note": "Order held while shares are located"}])[0])


def take(pid="P01", name="AAA", long=("AAA",), short=("HHH",), conviction=3, horizon=4, stop=-5.0, preopen=None, decision="take"):
    return {"id": f"{DAY}-{pid}", "session": DAY.isoformat(), "pass": "evening", "name": name, "kind": "stock", "setup": "breakout",
            "direction": "long", "source": "screen 2026-11-06 #1", "signal": {"why": "broke out"}, "decision": decision,
            "conviction": conviction, "reason": "the reason", "wrong_if": "closes back under the breakout", "reviewed": True,
            "reasoning": None, "hedge_why": "its sector", "through_earnings": None, "stats": None, "preopen": preopen,
            "long": list(long), "short": list(short), "plan": {"horizon_days": horizon, "stop": {"spread_pct": stop}}}


class Open(Case):
    def run_open(self, live, broker, props=(take(),)):
        if props is not None:
            book._write(self.root / "proposals" / f"{DAY}.json", {"session": DAY.isoformat(), "screen": "2026-11-06", "passes": [], "proposals": list(props)})
        with mock.patch.object(book, "quotes", lambda symbols, *a: {s: live[s] for s in symbols if s in live}), \
                mock.patch.object(book, "_closes", lambda symbols: {s: 50.0 for s in symbols}):
            return asyncio.run(book.open_session(DAY, broker))

    def test_trades_the_take_with_its_own_plan_at_the_larger_size(self):
        plan = self.run_open({"AAA": 50.0, "HHH": 100.0}, Broker(), [take(horizon=7, stop=-6.5), take("P02", "BBB", ("BBB",), decision="pass")])
        (t,) = plan["trades"]
        self.assertEqual((t["proposal_id"], t["horizon_days"], t["stop"], t["notional"], t["book"]), (f"{DAY}-P01", 7, {"spread_pct": -6.5}, 10_000, "dynamic"))
        self.assertEqual([(leg["symbol"], leg["side"], leg["shares"]) for leg in t["legs"]], [("AAA", "long", 200), ("HHH", "short", 100)])
        self.assertEqual((plan["proposals"], plan["warnings"]), ({"take": 1, "pass": 1}, []))

    def test_takes_nothing_and_says_so_when_no_proposals_were_recorded(self):
        plan = self.run_open({}, Broker(), None)
        self.assertEqual(plan["trades"], [])
        self.assertIn("No research proposals", plan["warnings"][0])

    def test_no_warning_when_the_pass_took_nothing(self):
        plan = self.run_open({}, Broker(), [take(decision="pass")])
        self.assertEqual((plan["trades"], plan["warnings"]), ([], []))

    def test_says_when_every_trade_was_sized_on_the_last_close(self):
        plan = self.run_open({}, Broker())
        self.assertEqual(plan["prices"], {"quoted": 0, "last_close": ["AAA", "HHH"]})
        self.assertIn("price pull failed", plan["warnings"][0])
        self.assertEqual(self.read("plans", DAY)["warnings"], plan["warnings"])

    def test_names_the_legs_without_a_quote(self):
        plan = self.run_open({"AAA": 51.0}, Broker())
        self.assertEqual(plan["prices"], {"quoted": 1, "last_close": ["HHH"]})
        self.assertIn("HHH", plan["warnings"][0])
        with self.assertRaises(FileExistsError):
            self.run_open({}, Broker())

    def test_a_job_that_dies_while_sending_leaves_the_plan_so_a_rerun_cannot_send_twice(self):
        with self.assertRaises(ConnectionError):
            self.run_open({"AAA": 51.0, "HHH": 49.0}, Broker(fail_place=True))
        plan = self.read("plans", DAY)
        self.assertEqual((len(plan["trades"]), plan["orders"]), (1, []))
        with self.assertRaises(FileExistsError):
            self.run_open({}, Broker())


class Takes(unittest.TestCase):
    prices = {s: 100.0 for s in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "XLY")}

    def test_strongest_conviction_first_up_to_the_cap(self):
        names = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
        props = [take(f"P{i}", n, (n,), ("XLY",), conviction=c) for i, (n, c) in enumerate(zip(names, [3, 5, 3, 4, 3, 3]), 1)]
        trades, skipped = rules.proposal_trades(props, str(DAY), self.prices, set())
        self.assertEqual([t["name"] for t in trades], ["BBB", "DDD", "AAA", "CCC", "EEE"])
        self.assertEqual([t["id"][-3:] for t in trades], ["A01", "A02", "A03", "A04", "A05"])
        self.assertIn("came first", skipped[0]["reason"])

    def test_skips_a_stand_down_a_pass_and_a_name_already_held(self):
        props = [take("P1", preopen={"decision": "stand_down", "reason": "gapped 2 ATR"}), take("P2", "BBB", ("BBB",), ("XLY",), decision="pass"),
                 take("P3", "CCC", ("CCC",), ("XLY",)), take("P4", "DDD", ("DDD",), ("XLY",))]
        trades, skipped = rules.proposal_trades(props, str(DAY), self.prices, {"CCC"})
        self.assertEqual([t["name"] for t in trades], ["DDD"])        # XLY is a shared hedge, not a clash
        self.assertEqual(sorted(k["reason"].split(":")[0] for k in skipped), ["already holds CCC", "stood down before the open"])


class Check(Case):
    def test_closes_the_filled_side_of_a_broken_pair_at_the_market(self):
        self.write("plans", DAY, {"trades": [pair("T1"), pair("T2", "BBB", "HHH")], "orders": [{"status": "PreSubmitted"}]})
        b = Broker(fills=[fill("T1", "HHH", "SLD", 25), fill("T2", "BBB", "BOT", 25), fill("T2", "HHH", "SLD", 25, n=1)],
                   positions={"HHH": -50, "BBB": 25},
                   orders=[{"trade_id": "T1", "symbol": "AAA", "action": "BUY", "shares": 25, "status": "Cancelled"}])
        out = asyncio.run(book.check_session(DAY, b))
        self.assertEqual(b.sent, [{"trade_id": "T1", "symbol": "HHH", "action": "BUY", "shares": 25, "kind": "MKT"}])
        self.assertEqual([e["trade_id"] for e in out["exits"]], ["T1"])
        self.assertIn("AAA (cancelled) never filled", out["warnings"][0])
        self.assertEqual((self.state("T1"), self.state("T2")), ("closed", "open"))
        self.assertEqual(book.reconcile(asyncio.run(b.positions())), [])
        self.assertEqual(b.connections, 1)
        self.assertIsNone(next(r for r in self.rows() if r["id"] == "T1")["model_bps"])
        self.assertEqual(book.alerts(), out["warnings"])
        with self.assertRaises(FileExistsError):
            asyncio.run(book.check_session(DAY, b))

    def rows(self):
        import pandas as pd
        with mock.patch.object(book.bars, "load", lambda symbols: pd.DataFrame()), \
                mock.patch.object(book.storage, "modified", lambda p: None):
            return book.rows()

    def test_a_trade_with_no_fills_is_reported_and_nothing_is_sent(self):
        self.write("plans", DAY, {"trades": [pair("T1")], "orders": [{"status": "PreSubmitted"}]})
        b = Broker()
        out = asyncio.run(book.check_session(DAY, b))
        self.assertEqual((b.sent, [u["trade_id"] for u in out["unfilled"]], out["exits"]), ([], ["T1"], []))
        self.assertIn("wasn't taken", out["warnings"][0])

    def test_an_order_still_working_is_left_alone(self):
        self.write("plans", DAY, {"trades": [pair("T1")], "orders": [{"status": "PreSubmitted"}]})
        b = Broker(fills=[fill("T1", "HHH", "SLD", 25)], positions={"HHH": -25},
                   orders=[{"trade_id": "T1", "symbol": "AAA", "action": "BUY", "shares": 25, "status": "Submitted"}])
        out = asyncio.run(book.check_session(DAY, b))
        self.assertEqual((b.sent, out["exits"], [w["trade_id"] for w in out["waiting"]]), ([], [], ["T1"]))

    def test_nothing_to_do_without_a_plan(self):
        out = asyncio.run(book.check_session(DAY, Broker()))
        self.assertIn("no entries", out["note"])
        self.assertFalse((self.root / "checks").exists())


class Close(Case):
    def run_close(self, day, broker, prices):
        with mock.patch.object(book, "quotes", lambda symbols, *a: {s: prices[s] for s in symbols if s in prices}):
            return asyncio.run(book.close_session(day, broker=broker))

    def held(self, trade_id="T1", **kw):
        self.write("plans", DAY, {"trades": [pair(trade_id, **kw)], "orders": [{"status": "PreSubmitted"}]})
        return Broker(fills=[fill(trade_id, "AAA", "BOT", 25), fill(trade_id, "HHH", "SLD", 25)], positions={"AAA": 25, "HHH": -25})

    def test_a_stop_rule_on_a_symbol_outside_the_trade_is_priced_and_fires(self):
        b = self.held(stop={"rules": [{"symbol": "SPY", "below": 700}]})
        out = self.run_close(DAY, b, {"AAA": 100.0, "HHH": 100.0, "SPY": 690.0})
        self.assertIn("stopped: SPY", out["exits"][0]["reason"])
        self.assertEqual({o["kind"] for o in b.sent}, {"MOC"})
        self.assertEqual(b.connections, 1)

    def test_says_when_a_stop_could_not_be_checked(self):
        b = self.held()
        out = self.run_close(DAY, b, {"AAA": 100.0})
        self.assertEqual((out["exits"], out["stops_not_checked"], b.sent), ([], ["T1"], []))
        self.assertIn("No price for HHH", out["warnings"][0])
        self.assertEqual(self.read("exits", DAY)["warnings"], out["warnings"])

    def test_holds_quietly_when_priced_and_inside_the_stop(self):
        out = self.run_close(DAY, self.held(), {"AAA": 100.0, "HHH": 100.0})
        self.assertEqual((out["exits"], out["stops_not_checked"], out["warnings"]), ([], [], []))

    def test_an_exit_that_never_filled_is_sent_again(self):
        b = self.held()
        b.fill = lambda o: False
        self.run_close(DAY, b, {"AAA": 80.0, "HHH": 100.0})          # stopped; the exits never fill
        self.assertEqual(self.state("T1"), "closing")
        b.sent, b.fill = [], lambda o: True
        out = self.run_close(NEXT, b, {})
        self.assertEqual([r["trade_id"] for r in out["retried"]], ["T1"])
        self.assertEqual(sorted((o["symbol"], o["action"], o["shares"]) for o in b.sent), [("AAA", "SELL", 25), ("HHH", "BUY", 25)])
        asyncio.run(book.sync(NEXT, b))
        self.assertEqual(self.state("T1"), "closed")

    def test_an_exit_whose_fills_were_missed_is_not_sent_twice(self):
        b = self.held()
        b.fill = lambda o: False
        self.run_close(DAY, b, {"AAA": 80.0, "HHH": 100.0})
        b.sent, b._positions = [], {}                                  # the exits did fill; only the record of them was lost
        out = self.run_close(NEXT, b, {})
        self.assertEqual((b.sent, out["retried"]), ([], []))
        self.assertIn("wasn't sent again", out["warnings"][0])


# ---- The research gate ------------------------------------------------------------------------------------
from datetime import datetime  # noqa: E402

import pandas as pd  # noqa: E402

from factor_rotation.config import NY  # noqa: E402
from factor_rotation.paper import learn, proposals  # noqa: E402

SUNDAY_EVENING = datetime(2026, 11, 8, 19, 30, tzinfo=NY)
SCREEN = {"as_of": "2026-11-06", "next_session": DAY.isoformat(), "candidates": [
    {"name": "AAA", "kind": "stock", "setup": "breakout", "direction": "long", "hedge": "HHH", "why": "broke out on 2x volume",
     "chart": "uptrend", "numbers": {"atr_pct": 0.02}, "next_earnings": None, "score": 3.1},
    {"name": "BBB", "kind": "stock", "setup": "turn", "direction": "short", "hedge": "HHH", "why": "turned down",
     "chart": "stretched", "numbers": {"atr_pct": 0.04}, "next_earnings": None, "score": 2.4}]}
REASONING = {"what_changed": "a", "story": "b", "chart": "c", "coming": "d", "case_against": "e"}


def verdict(**kw):
    return {"candidate": "AAA", "decision": "take", "conviction": 4, "reason": "guidance raised and the breakout held",
            "reasoning": REASONING, "wrong_if": "closes back under 95", "hedge_why": "its sector fund",
            "plan": {"horizon_days": 4, "stop": {"spread_pct": -5}}, **kw}


def frame(closes: dict[str, list[float]], opens: dict[str, float] | None = None) -> pd.DataFrame:
    """Daily bars from DAY: each symbol opens at 100 (or `opens`) on DAY, then closes as listed, with SPY flat."""
    days = book.journal.sessions_from(DAY, max(len(v) for v in closes.values()))
    rows = []
    for sym, cs in {"SPY": [100.0] * len(days), **closes}.items():
        for d, c in zip(days, cs):
            rows.append({"symbol": sym, "date": pd.Timestamp(d), "open": (opens or {}).get(sym, 100.0) if d == DAY else c, "high": c, "low": c, "close": c, "volume": 1e6})
    return pd.DataFrame(rows)


class Research(Case):
    def setUp(self):
        super().setUp()
        (self.root.parent / "screens").mkdir()
        (self.root.parent / "screens" / "2026-11-06.json").write_text(json.dumps(SCREEN))
        self.reports = {}
        for target, name, value in ((proposals.earnings, "load", lambda: {"names": self.reports}),
                                    (proposals.bars, "load", lambda symbols: self.bars),
                                    (book.journal, "_bars_settled", lambda d: True)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.bars = pd.DataFrame()

    def propose(self, *verdicts, kind="evening", now=SUNDAY_EVENING, checks=()):
        return proposals.record({"session": DAY.isoformat(), "pass": kind, "proposals": list(verdicts), "checks": list(checks)}, now)


class Proposals(Research):
    def test_a_take_keeps_the_screen_numbers_and_an_unjudged_candidate_is_recorded_as_not_reviewed(self):
        _, doc, notes = self.propose(verdict())
        a, b = doc["proposals"]
        self.assertEqual((a["id"], a["source"], a["signal"]["why"], a["long"], a["short"], a["plan"]),
                         (f"{DAY}-P01", "screen 2026-11-06 #1", "broke out on 2x volume", ["AAA"], ["HHH"], {"horizon_days": 4, "stop": {"spread_pct": -5.0}}))
        self.assertEqual((b["decision"], b["reviewed"], b["long"], b["short"], b["plan"]),
                         ("pass", False, ["HHH"], ["BBB"], {"horizon_days": 3, "stop": {"spread_pct": -6.0}}))
        self.assertIn("BBB", notes[0])

    def test_a_name_the_screen_left_out_because_the_book_holds_it_is_refused_with_the_reason(self):
        screen = {**SCREEN, "in_book": [{"name": "HELD", "kind": "stock", "setup": "breakout", "direction": "long", "score": 3.0,
                                         "why": "broke out", "in_book": "the book holds it (2026-11-09-A01)"}]}
        (self.root.parent / "screens" / "2026-11-06.json").write_text(json.dumps(screen))
        with self.assertRaisesRegex(ValueError, "left out of the screen.*the book holds it"):
            self.propose(verdict(candidate="HELD"))

    def test_the_screens_hedge_fit_is_kept_with_the_verdict(self):
        screen = json.loads(json.dumps(SCREEN))
        screen["candidates"][0].update(hedge_fit={"corr_60d": 0.7, "beta_60d": 1.1, "chosen": "the best match of 24 funds", "weak": False},
                                       moved_with=["CCC"], adjust={"tight_base": 0.15})
        (self.root.parent / "screens" / "2026-11-06.json").write_text(json.dumps(screen))
        _, doc, _ = self.propose(verdict())
        signal = doc["proposals"][0]["signal"]
        self.assertEqual((signal["hedge_fit"]["corr_60d"], signal["moved_with"], signal["adjust"]), (0.7, ["CCC"], {"tight_base": 0.15}))

    def test_a_pass_needs_only_a_verdict_and_gets_the_mechanical_plan(self):
        _, doc, notes = self.propose(verdict(), {"candidate": "BBB", "decision": "pass", "conviction": 2, "reason": "no story behind the move"})
        self.assertEqual((doc["proposals"][1]["reviewed"], doc["proposals"][1]["plan"]["horizon_days"], notes), (True, 3, []))

    def test_what_a_take_must_carry(self):
        for bad, message in ((verdict(conviction=2), "conviction 3 or more"), (verdict(reasoning={"story": "x"}), "reasoning"),
                             (verdict(wrong_if=""), "wrong_if"), (verdict(hedge_why=None), "hedge_why"),
                             (verdict(plan={"horizon_days": 11, "stop": {"spread_pct": -5}}), "1-10 sessions"),
                             (verdict(plan={"horizon_days": 4, "stop": {"spread_pct": -9}}), "between -1 and -8"),
                             (verdict(plan={"horizon_days": 4, "stop": {"spread_pct": -5, "rules": [{"symbol": "ZZZ", "below": 5}]}}), "a leg's symbol"),
                             (verdict(candidate="ZZZ"), "not a candidate"), (verdict(short=["AAA"], long=["AAA"]), "both sides"),
                             (verdict(long=["VIX"]), "index")):
            with self.assertRaisesRegex(ValueError, message):
                self.propose(bad)
        self.assertFalse((self.root / "proposals").exists())

    def test_a_take_through_earnings_must_say_why(self):
        self.reports = {"AAA": [{"date": "2026-11-10", "session": "after_close"}]}     # trades on session 3
        with self.assertRaisesRegex(ValueError, "AAA reports 2026-11-10 after close, which trades on session 3"):
            self.propose(verdict())
        self.propose(verdict(plan={"horizon_days": 2, "stop": {"spread_pct": -5}}))     # out before it
        (self.root / "proposals" / f"{DAY}.json").unlink()
        _, doc, _ = self.propose(verdict(through_earnings="the report is the point"))
        self.assertEqual(doc["proposals"][0]["through_earnings"], "the report is the point")

    def test_the_pass_can_choose_its_own_hedge_and_record_its_own_idea(self):
        own = verdict(candidate=None, name="Refiners", setup="catalyst", direction="long", long=["CCC", "DDD", "EEE"], short=["XLE"])
        _, doc, _ = self.propose(verdict(hedge="XLY"), own, {"candidate": "BBB", "decision": "take", "conviction": 3, "reason": "r", "reasoning": REASONING,
                                                             "wrong_if": "w", "hedge": None, "plan": {"horizon_days": 2, "stop": {"spread_pct": -4}}})
        self.assertEqual([(p["name"], p["source"], p["long"], p["short"]) for p in doc["proposals"]],
                         [("AAA", "screen 2026-11-06 #1", ["AAA"], ["XLY"]), ("Refiners", "own", ["CCC", "DDD", "EEE"], ["XLE"]),
                          ("BBB", "screen 2026-11-06 #2", [], ["BBB"])])

    def test_only_for_the_next_entries_and_never_rewritten(self):
        with self.assertRaisesRegex(ValueError, "next entries, 2026-11-09"):
            proposals.record({"session": "2026-11-10", "pass": "evening", "proposals": [verdict()]}, SUNDAY_EVENING)
        with self.assertRaisesRegex(ValueError, "next entries, 2026-11-10"):
            self.propose(verdict(), now=datetime(2026, 11, 9, 9, 14, tzinfo=NY))
        self.propose(verdict())
        with self.assertRaisesRegex(ValueError, "already has a verdict"):
            self.propose(verdict(), kind="preopen", now=datetime(2026, 11, 9, 8, 40, tzinfo=NY))

    def test_the_pre_open_pass_stands_a_take_down_and_gives_a_late_verdict(self):
        self.propose(verdict())
        morning = datetime(2026, 11, 9, 8, 40, tzinfo=NY)
        with self.assertRaisesRegex(ValueError, "no take with that id"):
            self.propose(kind="preopen", now=morning, checks=[{"id": f"{DAY}-P02", "decision": "stand_down", "reason": "x"}])
        _, doc, _ = self.propose({"candidate": "BBB", "decision": "pass", "conviction": 1, "reason": "gapped back up overnight"}, kind="preopen", now=morning,
                                 checks=[{"id": f"{DAY}-P01", "decision": "stand_down", "reason": "gapped 2 ATR, the move is done"}])
        a, b = doc["proposals"]
        self.assertEqual((a["preopen"]["decision"], b["id"], b["reviewed"], b["pass"]), ("stand_down", f"{DAY}-P02", True, "preopen"))
        trades, skipped = rules.proposal_trades(doc["proposals"], str(DAY), {"AAA": 100.0, "HHH": 100.0}, set())
        self.assertEqual((trades, skipped[0]["reason"]), ([], "stood down before the open: gapped 2 ATR, the move is done"))


class Review(Research):
    """One automatic trade, long 100 AAA / short 100 HHH from 100, opened on DAY with a 2-session limit."""

    def setUp(self):
        super().setUp()
        t = {**pair("T1", horizon=2), "notional": 10_000, "proposal_id": f"{DAY}-P01", "conviction": 4, "wrong_if": "x"}
        t["legs"] = [{**leg, "shares": 100} for leg in t["legs"]]
        self.write("plans", DAY, {"trades": [t], "orders": [{"status": "PreSubmitted"}]})
        self.broker = Broker(fills=[fill("T1", "AAA", "BOT", 100), fill("T1", "HHH", "SLD", 100)], positions={"AAA": 100, "HHH": -100})
        asyncio.run(book.sync(DAY, self.broker))

    def review(self, session, hour=15, **r):
        return learn.record_review({"session": session.isoformat(), "reviews": [{"trade_id": "T1", "thesis": "intact", "reason": "why", **r}]},
                                   datetime(session.year, session.month, session.day, hour, 10, tzinfo=NY))

    def close(self, day, prices):
        with mock.patch.object(book, "quotes", lambda symbols, *a: {s: prices[s] for s in symbols if s in prices}):
            return asyncio.run(book.close_session(day, broker=self.broker))

    def test_an_early_exit_closes_the_trade_at_the_close(self):
        self.review(DAY, action="exit", thesis="broken", reason="guidance was withdrawn at noon")
        out = self.close(DAY, {"AAA": 99.0, "HHH": 100.0})
        self.assertEqual(out["exits"][0]["reason"], "review: guidance was withdrawn at noon")
        self.assertEqual(sorted((o["symbol"], o["action"], o["kind"]) for o in self.broker.sent), [("AAA", "SELL", "MOC"), ("HHH", "BUY", "MOC")])

    def test_a_hold_changes_nothing_and_a_stop_still_fires(self):
        self.review(DAY, action="hold")
        out = self.close(DAY, {"AAA": 94.0, "HHH": 100.0})
        self.assertIn("stopped: down 6.0%", out["exits"][0]["reason"])

    def test_the_review_must_cover_every_open_trade_in_time_and_only_extend_on_the_last_session(self):
        with self.assertRaisesRegex(ValueError, "missing: T1"):
            learn.record_review({"session": DAY.isoformat(), "reviews": []}, datetime(2026, 11, 9, 15, 10, tzinfo=NY))
        with self.assertRaisesRegex(ValueError, "3:44 PM"):
            self.review(DAY, hour=16, action="hold")
        with self.assertRaisesRegex(ValueError, "last session"):
            self.review(DAY, action="extend", sessions=2)
        with self.assertRaisesRegex(ValueError, "1-3 sessions"):
            self.review(NEXT, action="extend", sessions=4)

    def test_a_winner_on_its_last_session_is_extended_once(self):
        self.close(DAY, {"AAA": 101.0, "HHH": 100.0})
        self.review(NEXT, action="extend", sessions=2, reason="the second leg of the story lands Thursday")
        out = self.close(NEXT, {"AAA": 103.0, "HHH": 100.0})
        self.assertEqual((out["exits"], out["extended"]), ([], [{"trade_id": "T1", "sessions": 2, "reason": "the second leg of the story lands Thursday"}]))
        t = book.open_trades()[0]
        self.assertEqual((t["horizon_days"], t["planned_horizon_days"], t["extended"]), (4, 2, True))
        third, fourth = book.journal.sessions_from(DAY, 4)[2:]
        self.assertEqual(self.close(third, {"AAA": 103.0, "HHH": 100.0})["exits"], [])
        with self.assertRaisesRegex(ValueError, "already been extended"):
            self.review(fourth, action="extend", sessions=1)
        self.assertIn("time limit: session 4 of 4", self.close(fourth, {"AAA": 103.0, "HHH": 100.0})["exits"][0]["reason"])

    def test_a_loser_is_not_extended(self):
        self.close(DAY, {"AAA": 100.0, "HHH": 100.0})
        self.review(NEXT, action="extend", sessions=2)
        out = self.close(NEXT, {"AAA": 99.0, "HHH": 100.0})
        self.assertEqual(out["extended"], [])
        self.assertIn("time limit: session 2 of 2; the extension asked for was refused: it isn't in profit", out["exits"][0]["reason"])


class Learn(Research):
    def test_takes_and_passes_are_scored_the_same_way_and_set_side_by_side(self):
        self.propose(verdict(plan={"horizon_days": 2, "stop": {"spread_pct": -5}}),
                     {"candidate": "BBB", "decision": "pass", "conviction": 2, "reason": "no story"})
        self.bars = frame({"AAA": [101.0, 103.0, 110.0], "BBB": [102.0, 108.0, 90.0], "HHH": [100.0, 100.0, 100.0]})
        rows = {r["name"]: r for r in learn.outcomes()}
        self.assertEqual((rows["AAA"]["verdict"], rows["AAA"]["plan_status"], rows["AAA"]["plan_bps"]), ("take", "time", 300.0))
        # the pass, as the screen would have traded it: long HHH / short BBB, stopped at the second close, down 8%
        self.assertEqual((rows["BBB"]["verdict"], rows["BBB"]["plan_status"], rows["BBB"]["plan_bps"]), ("pass", "stopped", -800.0))
        t = learn.tables()
        self.assertEqual((t["by_verdict"]["take"], t["by_verdict"]["pass"]["mean"], t["by_conviction"][4]["n"]),
                         ({"n": 1, "mean": 300.0, "median": 300.0, "winners": 1.0}, -800.0, 1))
        self.assertIn("take", learn.brief())

    def test_an_unfinished_plan_has_no_result_yet(self):
        self.propose(verdict())
        self.bars = frame({"AAA": [101.0], "HHH": [100.0]})
        (row, _) = learn.outcomes()
        self.assertEqual((row["plan_status"], row["plan_bps"], row["plan_so_far_bps"]), ("open", None, 100.0))
        self.assertEqual(learn.tables()["finished"], 0)

    def test_a_post_mortem_tag_has_to_agree_with_the_result(self):
        t = {**pair("T1", horizon=1), "proposal_id": f"{DAY}-P01"}
        self.write("plans", DAY, {"trades": [t], "orders": [{"status": "PreSubmitted"}]})
        b = Broker(fills=[fill("T1", "AAA", "BOT", 25), fill("T1", "HHH", "SLD", 25)], positions={"AAA": 25, "HHH": -25})
        with mock.patch.object(book, "quotes", lambda symbols, *a: {"AAA": 100.0, "HHH": 100.0}):
            asyncio.run(book.close_session(DAY, broker=b))                       # the time limit; exits fill at 100, so it loses its commissions
        asyncio.run(book.sync(DAY, b))
        self.assertEqual([r["id"] for r in learn.needs_postmortem()], ["T1"])
        m = {"trade_id": "T1", "what_happened": "went nowhere", "lesson": "a breakout with no follow-through by the first close is done"}
        with self.assertRaisesRegex(ValueError, "lost 16 bp, so its tag is one of: thesis_broke, noise"):
            learn.record_postmortems({"postmortems": [{**m, "tag": "right_reason"}]})
        learn.record_postmortems({"postmortems": [{**m, "tag": "noise"}]})
        self.assertEqual((learn.needs_postmortem(), learn.postmortems()["T1"]["tag"]), ([], "noise"))
        with self.assertRaisesRegex(ValueError, "already has a post-mortem"):
            learn.record_postmortems({"postmortems": [{**m, "tag": "noise"}]})


class Pnl(Case):
    def setUp(self):
        super().setUp()
        self.write("plans", DAY, {"trades": [pair("T1")]})
        self.bars = frame({"AAA": [104.0, 106.0], "HHH": [101.0, 102.0]})
        for target, name, value in ((book.bars, "load", lambda symbols: self.bars), (book.journal, "_bars_settled", lambda d: True)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def fills(self, session, *rows):
        """Each row is (symbol, side, price, UTC time or None)."""
        made = [{**fill("T1", s, side, 25, price=px, n=f"{session}{i}"), "time": at} for i, (s, side, px, at) in enumerate(rows)]
        self.write("fills", session, {"fills": made, "positions": {}, "net_liquidation": 1e6})

    def enter(self):
        self.fills(DAY, ("AAA", "BOT", 100.0, "2026-11-09T14:30:02+00:00"), ("HHH", "SLD", 100.0, "2026-11-09T14:30:02+00:00"))

    def test_nothing_filled_is_no_line(self):
        self.assertEqual(book.pnl_by_session(), [])
        self.assertEqual(book.pnl_intraday(), {"points": [], "missing": []})

    def test_starts_from_zero_and_marks_open_positions_at_each_close_after_commissions(self):
        self.enter()
        rows = book.pnl_by_session()
        self.assertEqual([r["date"] for r in rows], ["2026-11-06", "2026-11-09", "2026-11-10"])
        # long +4 and short -1 on 25 shares, less $2 of commissions; then long +6 and short -2
        self.assertEqual([r["pnl"] for r in rows], [0.0, 73.0, 98.0])

    def test_the_last_point_ties_to_the_trade_table(self):
        self.enter()
        self.assertEqual(book.pnl_by_session()[-1]["pnl"], sum(r["pnl"] for r in book.rows()))

    def test_a_closed_trade_stays_at_its_realized_result(self):
        self.enter()
        self.fills(NEXT, ("AAA", "SLD", 105.0, "2026-11-10T21:00:01+00:00"), ("HHH", "BOT", 102.0, "2026-11-10T21:00:01+00:00"))
        self.bars = frame({"AAA": [104.0, 106.0, 90.0], "HHH": [101.0, 102.0, 130.0]})
        days = book.journal.sessions_from(DAY, 3)
        rows = {r["date"]: r["pnl"] for r in book.pnl_by_session()}
        # +5 and -2 on 25 shares, less $4 of commissions; the later bars don't touch it
        self.assertEqual(rows[NEXT.isoformat()], 71.0)
        if days[2] <= datetime.now(NY).date():
            self.assertEqual(rows[days[2].isoformat()], 71.0)

    def test_a_position_entered_after_the_last_bar_sits_at_its_fill(self):
        self.bars = frame({"AAA": [104.0], "HHH": [101.0]})
        self.fills(NEXT, ("AAA", "BOT", 100.0, "2026-11-10T14:30:02+00:00"), ("HHH", "SLD", 100.0, "2026-11-10T14:30:02+00:00"))
        self.write("plans", DAY, {"trades": [pair("T1", entry=NEXT)]})
        rows = book.pnl_by_session()
        self.assertEqual((rows[-1]["date"], rows[-1]["pnl"]), (NEXT.isoformat(), -2.0))
        self.assertEqual(book.rows()[0]["pnl"], -2.0)

    def bar_series(self, values):
        """15-minute closes from 3:30 PM ET on DAY, one bar each."""
        idx = pd.date_range("2026-11-09 15:30", periods=len(values), freq="15min", tz=NY)
        return pd.Series(values, index=idx)

    def test_intraday_marks_each_bar_and_counts_the_closing_auction_in_the_last_one(self):
        self.enter()
        self.fills(NEXT, ("AAA", "SLD", 110.0, "2026-11-09T21:00:03+00:00"), ("HHH", "BOT", 100.0, "2026-11-09T21:00:03+00:00"))
        closes = {"SPY": self.bar_series([1.0, 1.0]), "AAA": self.bar_series([103.0, 109.0]), "HHH": self.bar_series([100.0, 100.0])}
        with mock.patch.object(book.bars, "intraday_closes", lambda symbols, sessions: closes):
            out = book.pnl_intraday()
        self.assertEqual(out["missing"], [])
        self.assertEqual([p["pnl"] for p in out["points"]], [73.0, 246.0])
        self.assertEqual(out["points"][0]["session"], "2026-11-09")

    def test_a_name_with_no_bars_is_reported_and_marked_at_its_fill(self):
        self.enter()
        closes = {"SPY": self.bar_series([1.0]), "AAA": self.bar_series([103.0])}
        with mock.patch.object(book.bars, "intraday_closes", lambda symbols, sessions: closes):
            out = book.pnl_intraday()
        self.assertEqual((out["missing"], out["points"][0]["pnl"]), (["HHH"], 73.0))


if __name__ == "__main__":
    unittest.main()
