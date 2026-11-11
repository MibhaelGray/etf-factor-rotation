"""The dashboard's Refresh: the cached calendar, the quick bar pull, and what it may never write."""

import asyncio
import json
import tempfile
import time
import unittest
from concurrent.futures import Future
from datetime import date
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal

from factor_rotation import bars, calendars, journal, market, screen
from factor_rotation.paper import broker
from factor_rotation.paper.broker import IBPaper


class Calendar(unittest.TestCase):
    """The paper jobs ask this calendar whether today is a session, so it has to agree with the library exactly."""

    def same(self, start, end):
        ours, theirs = calendars.NYSE.valid_days(start, end), mcal.get_calendar("XNYS").valid_days(start, end)
        self.assertEqual(list(ours), list(theirs), (start, end))

    def test_ranges_match_the_library(self):
        for start, end in [("2019-01-01", "2026-12-31"), ("2026-11-26", "2026-11-26"), ("2026-11-27", "2026-11-27"),
                           ("2026-11-07", "2026-11-08"), ("2025-12-24", "2026-01-02"), ("2018-01-01", "2018-01-02")]:
            self.same(date.fromisoformat(start), date.fromisoformat(end))

    def test_any_date_type_and_outside_the_cache(self):
        self.same("2026-11-02", "2026-11-13")
        self.same(pd.Timestamp("2026-11-02"), pd.Timestamp("2026-11-13"))
        self.same(date(2017, 12, 20), date(2018, 1, 10))
        self.same(date(2026, 11, 1), date(2035, 1, 31))

    def test_a_timestamp_late_in_the_day_still_counts_that_day(self):
        self.same(pd.Timestamp("2026-11-09 15:45"), pd.Timestamp("2026-11-10 23:00"))

    def test_journal_helpers(self):
        self.assertTrue(journal.is_trading_day(date(2026, 11, 10)))
        self.assertFalse(journal.is_trading_day(date(2026, 11, 26)))  # Thanksgiving
        self.assertEqual(journal.next_trading_day(date(2026, 11, 25)), date(2026, 11, 27))


def frame(days, closes, symbol="TEST"):
    closes = np.asarray(closes, float)
    return pd.DataFrame({"symbol": symbol, "date": pd.to_datetime(days), "open": closes, "high": closes, "low": closes,
                         "close": closes, "volume": 1e6})


DAYS = list(pd.bdate_range("2026-10-01", periods=30))


class Merge(unittest.TestCase):
    def setUp(self):
        self.stored = frame(DAYS[:-1], 100 + np.arange(29) * 0.1)

    def recent(self, last=120.0, shift=1.0):
        days = DAYS[-5:]
        closes = [*(self.stored.set_index("date").loc[days[:-1], "close"] * shift), last * shift]
        return frame(days, closes)

    def test_adds_todays_bar_and_keeps_the_history(self):
        merged = bars._merge_recent(self.recent(103.0), self.stored)
        self.assertEqual(len(merged), 30)
        self.assertEqual(merged["date"].iloc[-1], DAYS[-1])
        self.assertAlmostEqual(merged["close"].iloc[-1], 103.0)
        self.assertEqual(merged["date"].duplicated().sum(), 0)
        self.assertTrue(merged["date"].is_monotonic_increasing)

    def test_a_part_finished_day_is_replaced_not_duplicated(self):
        stored = frame(DAYS, 100 + np.arange(30) * 0.1)
        recent = frame(DAYS[-5:], [*stored["close"].iloc[-5:-1], 103.5])
        merged = bars._merge_recent(recent, stored)
        self.assertEqual(len(merged), 30)
        self.assertAlmostEqual(merged["close"].iloc[-1], 103.5)

    def test_a_dividend_that_rebased_old_prices_needs_the_full_history(self):
        self.assertIsNone(bars._merge_recent(self.recent(103.0, shift=0.99), self.stored))

    def test_rounding_is_not_an_adjustment(self):
        self.assertIsNotNone(bars._merge_recent(self.recent(103.0, shift=1.0005), self.stored))

    def test_no_days_in_common(self):
        self.assertIsNone(bars._merge_recent(frame([DAYS[-1]], [103.0]), self.stored))

    def test_a_pull_older_than_the_store_is_refused(self):
        self.assertIsNone(bars._merge_recent(frame(DAYS[:5], self.stored["close"].iloc[:5]), self.stored))

    def test_a_suspect_move_goes_to_the_full_pull(self):
        self.assertIsNone(bars._merge_recent(self.recent(160.0), self.stored))

    def test_an_index_can_jump_without_being_suspect(self):
        jump = self.recent(160.0).assign(symbol="VIX")
        self.assertIsNotNone(bars._merge_recent(jump, self.stored))


class CollectRecent(unittest.TestCase):
    def setUp(self):
        bars._read_cache.clear()

    def download(self, symbols):
        days = DAYS[-5:]
        pieces = {s: pd.DataFrame({c: closes for c in ("Open", "High", "Low", "Close", "Volume")}, index=days)
                  for s, closes in symbols.items()}
        return pd.concat(pieces, axis=1)

    def test_merges_what_lines_up_and_pulls_the_rest_in_full(self):
        stored = {s: frame(DAYS[:-1], 100 + np.arange(29) * 0.1, s) for s in ("AAA", "BBB")}
        reads = {bars.bars_path(s): df for s, df in stored.items()}
        written = {}
        closes = {s: [*df["close"].iloc[-4:], 103.0] for s, df in stored.items()}
        closes["BBB"] = [c * 1.05 for c in closes["BBB"]]
        with mock.patch("yfinance.download", return_value=self.download(closes)) as down, \
                mock.patch.object(bars.storage, "stamp", side_effect=lambda p: 1 if p in reads else None), \
                mock.patch.object(bars.storage, "read", side_effect=lambda p: reads[p]), \
                mock.patch.object(bars.storage, "write", side_effect=lambda df, p: written.__setitem__(p, df)), \
                mock.patch.object(bars, "collect", return_value={"failed": [], "suspect": []}) as full:
            out = bars.collect_recent(["AAA", "BBB"])
        self.assertEqual(down.call_count, 1)
        self.assertEqual(down.call_args.kwargs["period"], "5d")
        full.assert_called_once_with(["BBB"])
        self.assertEqual(out["full"], ["BBB"])
        self.assertEqual(list(written), [bars.bars_path("AAA")])
        self.assertEqual(len(written[bars.bars_path("AAA")]), 30)


    def test_a_name_yahoo_dropped_is_asked_for_again_before_a_full_pull(self):
        df = frame(DAYS[:-1], 100 + np.arange(29) * 0.1, "AAA")
        path = bars.bars_path("AAA")
        closes = {"AAA": [*df["close"].iloc[-4:], 103.0]}
        ok = self.download({"BBB": [1.0] * 5})
        with mock.patch("yfinance.download", side_effect=[ok, self.download(closes)]) as down, \
                mock.patch.object(bars.storage, "stamp", side_effect=lambda p: 1 if p == path else None), \
                mock.patch.object(bars.storage, "read", return_value=df), \
                mock.patch.object(bars.storage, "write") as write, \
                mock.patch.object(bars, "collect", return_value={"failed": [], "suspect": []}) as full:
            out = bars.collect_recent(["AAA"])
        self.assertEqual(down.call_count, 2)
        self.assertEqual(down.call_args_list[1].args[0], ["AAA"])
        full.assert_not_called()
        self.assertEqual(out["full"], [])
        write.assert_called_once()


def quote(symbol="AAA", price=103.0, day="2026-11-10", **over):
    stamp = pd.Timestamp(f"{day} 11:00", tz="America/New_York").timestamp()
    return {"symbol": symbol, "regularMarketOpen": 102.0, "regularMarketDayHigh": 104.0, "regularMarketDayLow": 101.0,
            "regularMarketPrice": price, "regularMarketVolume": 5e5, "regularMarketTime": int(stamp), **over}


class QuoteBar(unittest.TestCase):
    def test_a_full_quote(self):
        bar = bars._quote_bar("AAA", quote())
        self.assertEqual(bar["date"], pd.Timestamp("2026-11-10"))
        self.assertEqual((bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]), (102.0, 104.0, 101.0, 103.0, 5e5))

    def test_the_time_is_read_in_new_york(self):
        stamp = pd.Timestamp("2026-11-10 19:30", tz="America/New_York").timestamp()
        self.assertEqual(bars._quote_bar("AAA", quote(regularMarketTime=int(stamp)))["date"], pd.Timestamp("2026-11-10"))

    def test_the_range_holds_the_open_and_the_price(self):
        bar = bars._quote_bar("AAA", quote(regularMarketOpen=105.0, regularMarketDayLow=103.5))
        self.assertEqual((bar["high"], bar["low"]), (105.0, 103.0))

    def test_incomplete_quotes_are_refused(self):
        self.assertIsNone(bars._quote_bar("AAA", quote(regularMarketOpen=None)))
        self.assertIsNone(bars._quote_bar("AAA", quote(regularMarketPrice=0)))

    def test_an_index_without_volume_is_fine(self):
        q = quote("^VIX")
        del q["regularMarketVolume"]
        self.assertEqual(bars._quote_bar("VIX", q)["volume"], 0.0)

    def test_scaled_symbols_are_scaled(self):
        with mock.patch.dict(bars.SCALE, {"AAA": 0.1}):
            self.assertAlmostEqual(bars._quote_bar("AAA", quote())["close"], 10.3)


class MergeQuote(unittest.TestCase):
    TODAY = date(2026, 11, 10)

    def store(self, last):
        days = pd.bdate_range(end=last, periods=5)
        df = frame(days, [100.0, 101.0, 102.0, 101.5, 102.5], "AAA")
        df["date"] = df["date"].astype("datetime64[ms]")
        return df

    def bar(self, **kw):
        return bars._quote_bar("AAA", quote(**kw))

    def test_adds_todays_bar_after_yesterdays(self):
        stored = self.store("2026-11-09")
        merged = bars._merge_quote(self.bar(), stored, self.TODAY)
        self.assertEqual(len(merged), 6)
        self.assertEqual(merged["date"].iloc[-1], pd.Timestamp("2026-11-10"))
        self.assertEqual(merged.dtypes.to_dict(), stored.dtypes.to_dict())

    def test_replaces_a_part_finished_today(self):
        merged = bars._merge_quote(self.bar(), self.store("2026-11-10"), self.TODAY)
        self.assertEqual(len(merged), 5)
        self.assertEqual(merged["close"].iloc[-1], 103.0)

    def test_before_the_open_nothing_changes(self):
        stored = self.store("2026-11-09")
        self.assertIs(bars._merge_quote(self.bar(day="2026-11-09"), stored, self.TODAY), stored)

    def test_a_name_that_has_not_traded_today_is_left_alone(self):
        stored = self.store("2026-11-10")
        self.assertIs(bars._merge_quote(self.bar(day="2026-11-09"), stored, self.TODAY), stored)

    def test_a_store_more_than_a_session_behind_needs_the_short_pull(self):
        self.assertIsNone(bars._merge_quote(self.bar(), self.store("2026-11-06"), self.TODAY))
        self.assertIsNone(bars._merge_quote(self.bar(day="2026-11-09"), self.store("2026-11-06"), self.TODAY))

    def test_monday_follows_friday(self):
        merged = bars._merge_quote(self.bar(day="2026-11-09"), self.store("2026-11-06"), date(2026, 11, 9))
        self.assertEqual(len(merged), 6)

    def test_a_split_sized_move_needs_the_short_pull(self):
        self.assertIsNone(bars._merge_quote(self.bar(price=51.0), self.store("2026-11-09"), self.TODAY))

    def test_an_index_can_jump(self):
        stored = self.store("2026-11-09").assign(symbol="VIX")
        bar = {**self.bar(price=160.0), "symbol": "VIX"}
        self.assertEqual(len(bars._merge_quote(bar, stored, self.TODAY)), 6)


class CollectQuotes(unittest.TestCase):
    def setUp(self):
        bars._read_cache.clear()
        self.stored = frame(DAYS[:-1], 100 + np.arange(29) * 0.1, "AAA")
        self.reads = {bars.bars_path("AAA"): self.stored}

    def run_it(self, quotes):
        with mock.patch.object(bars, "_quotes", return_value=quotes), \
                mock.patch.object(bars.storage, "stamp", side_effect=lambda p: 1 if p in self.reads else None), \
                mock.patch.object(bars.storage, "read", side_effect=lambda p: self.reads[p]), \
                mock.patch.object(bars.storage, "write") as write, \
                mock.patch.object(bars, "datetime", wraps=bars.datetime) as clock, \
                mock.patch.object(bars, "collect_recent", return_value={"full": [], "failed": [], "suspect": []}) as recent:
            clock.now.return_value = pd.Timestamp(DAYS[-1]).to_pydatetime()
            out = bars.collect_quotes(["AAA"])
        return out, write, recent

    def test_a_good_quote_is_stored_with_no_pull(self):
        day = DAYS[-1].date().isoformat()
        out, write, recent = self.run_it({"AAA": quote(price=103.0, day=day)})
        write.assert_called_once()
        recent.assert_not_called()
        self.assertEqual(out["behind"], [])

    def test_when_the_quote_call_fails_every_name_takes_the_short_pull(self):
        out, write, recent = self.run_it({})
        write.assert_not_called()
        recent.assert_called_once_with(["AAA"])
        self.assertEqual(out["behind"], ["AAA"])


class CommissionWait(unittest.TestCase):
    def broker(self, reports):
        """A broker whose one fill gets its commission report after `reports` looks, or never when None."""
        from types import SimpleNamespace as N
        looks = {"n": 0}

        def fills():
            looks["n"] += 1
            ready = reports is not None and looks["n"] > reports
            return [N(execution=N(acctNumber="DU1", orderRef="r", side="BOT", shares=1.0, price=10.0, execId="e"),
                      contract=N(symbol="AAA"), time=None,
                      commissionReport=N(execId="e" if ready else "", commission=1.0 if ready else 0.0))]

        async def executions():
            return []

        p = IBPaper.__new__(IBPaper)
        p.s, p.ib = N(account="DU1"), N(fills=fills, reqExecutionsAsync=executions)
        return p, looks

    def test_does_not_wait_when_reports_are_in(self):
        p, _ = self.broker(0)
        started = time.perf_counter()
        out = asyncio.run(p.fills())
        self.assertLess(time.perf_counter() - started, 0.04)
        self.assertEqual(out[0]["commission"], 1.0)

    def test_waits_for_a_late_report(self):
        p, _ = self.broker(3)
        self.assertEqual(asyncio.run(p.fills())[0]["commission"], 1.0)

    def test_gives_up_on_a_report_that_never_comes(self):
        p, looks = self.broker(None)
        with mock.patch.object(broker, "COMMISSION_WAITS", 3):
            out = asyncio.run(p.fills())
        self.assertEqual(out[0]["commission"], 0.0)
        self.assertEqual(looks["n"], 4)

    def test_gives_up_on_a_gateway_that_never_answers(self):
        p, _ = self.broker(0)

        async def never():
            await asyncio.sleep(60)

        p.ib.reqExecutionsAsync = never
        with mock.patch.object(broker, "REQUEST_TIMEOUT", 0.05), self.assertRaises(TimeoutError):
            asyncio.run(p.fills())


class ReadCache(unittest.TestCase):
    def setUp(self):
        bars._read_cache.clear()

    def test_a_rewritten_file_is_read_again(self):
        with mock.patch.object(bars.storage, "stamp", side_effect=[1, 1, 2]), \
                mock.patch.object(bars.storage, "read", side_effect=["first", "second"]) as read:
            self.assertEqual([bars._read("p"), bars._read("p"), bars._read("p")], ["first", "first", "second"])
        self.assertEqual(read.call_count, 2)

    def test_a_missing_file(self):
        with mock.patch.object(bars.storage, "stamp", return_value=None):
            self.assertIsNone(bars._read("p"))


class NothingSaved(unittest.TestCase):
    """The research passes read the saved brief and screen, so a mid-day refresh must leave them alone."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for folder in ("briefs", "screens"):
            (self.root / folder).mkdir()
        (self.root / "briefs" / "2026-11-06.json").write_text(json.dumps({"as_of": "2026-11-06"}))
        (self.root / "screens" / "2026-11-09.json").write_text(json.dumps({"as_of": "2026-11-09", "built_at": "x", "candidates": []}))
        p = mock.patch.object(journal, "JOURNAL", self.root)
        p.start()
        self.addCleanup(p.stop)

    def files(self):
        return {str(f): f.read_bytes() for f in self.root.rglob("*") if f.is_file()}

    def build(self, newest, as_of=None):
        brief = {"as_of": str(newest), "built_at": "t", "bars_through": str(newest), "factor_pairs": []}
        with mock.patch.object(market, "_newest_bar", return_value=newest), \
                mock.patch.object(market, "_brief", return_value=brief) as saved, \
                mock.patch.object(journal, "build_brief", return_value=brief) as built, \
                mock.patch.object(screen, "build", return_value={"as_of": str(newest), "built_at": "t", "candidates": []}) as live, \
                mock.patch.object(market, "_live_pairs", side_effect=lambda b, d: b), \
                mock.patch.object(market, "latest_snapshot", return_value=None), \
                mock.patch.object(market, "_factors", return_value=[]), mock.patch.object(market, "_regime", return_value=[]), \
                mock.patch.object(market, "_gamma", return_value=[]), mock.patch.object(market, "_flows", return_value=[]), \
                mock.patch.object(market, "_baskets", return_value=[]), mock.patch.object(market, "_earnings", return_value=[]), \
                mock.patch.object(market, "_latest_session", return_value=newest), \
                mock.patch.object(market, "_paper", return_value=[]):
            rows = market.build(as_of)
        return rows, saved, built, live

    def test_newer_bars_build_an_unsaved_brief_and_screen(self):
        before = self.files()
        rows, saved, built, live = self.build(date(2026, 11, 10))
        built.assert_called_once_with(date(2026, 11, 10), write=False)
        live.assert_called_once()
        saved.assert_not_called()
        self.assertEqual(rows[0]["as_of"], "2026-11-10")
        self.assertEqual(self.files(), before)

    def test_bars_no_newer_than_the_saved_brief_read_it(self):
        (self.root / "screens" / "2026-11-09.json").unlink()
        (self.root / "screens" / "2026-11-06.json").write_text(json.dumps({"as_of": "2026-11-06", "built_at": "x", "candidates": []}))
        _, saved, built, live = self.build(date(2026, 11, 6))
        built.assert_not_called()
        live.assert_not_called()
        saved.assert_called_once()

    def test_an_explicit_date_reads_that_nights_brief(self):
        _, saved, built, live = self.build(date(2026, 11, 10), as_of=date(2026, 11, 6))
        built.assert_not_called()
        saved.assert_called_once_with(date(2026, 11, 6))

    def test_build_brief_can_skip_the_write(self):
        empty = pd.DataFrame({"date": pd.to_datetime([]), "symbol": []})
        with mock.patch.object(journal, "load", return_value=empty), \
                mock.patch.object(journal, "symbol_technicals", return_value=pd.DataFrame({"symbol": []})), \
                mock.patch.object(journal, "factor_pair_technicals", return_value=pd.DataFrame()), \
                mock.patch.object(journal, "latest_snapshot", return_value=None):
            journal.build_brief(date(2026, 11, 10), write=False)
            self.assertFalse((self.root / "briefs" / "2026-11-10.json").exists())
            journal.build_brief(date(2026, 11, 10))
            self.assertTrue((self.root / "briefs" / "2026-11-10.json").exists())


class Workers(unittest.TestCase):
    def test_no_pool_runs_here(self):
        self.assertIsNone(market._start(None, str))
        self.assertEqual(market._finish(None, lambda: "here"), "here")

    def test_a_pool_that_will_not_start_runs_here(self):
        broken = mock.Mock()
        broken.submit.side_effect = RuntimeError("broken")
        self.assertIsNone(market._start(broken, str))

    def test_a_worker_result_is_used(self):
        job = Future()
        job.set_result("worker")
        self.assertEqual(market._finish(job, lambda: "here"), "worker")

    def test_a_failed_worker_falls_back(self):
        job = Future()
        job.set_exception(RuntimeError("died"))
        self.assertEqual(market._finish(job, lambda: "here"), "here")


class Fit(unittest.TestCase):
    def test_matches_pandas(self):
        rng = np.random.default_rng(7)
        a = pd.Series(rng.normal(size=60))
        b = a * 0.8 + pd.Series(rng.normal(size=60))
        a[[3, 9]] = np.nan
        b[20] = np.nan
        both = a.notna() & b.notna()
        fit = screen._fit(a, b)
        self.assertEqual(fit["corr_60d"], round(float(a[both].corr(b[both])), 2))
        self.assertEqual(fit["beta_60d"], round(float(a[both].cov(b[both]) / b[both].var()), 2))

    def test_too_little_or_flat(self):
        self.assertIsNone(screen._fit(pd.Series(np.arange(20.0)), pd.Series(np.arange(20.0))))
        self.assertIsNone(screen._fit(pd.Series(np.arange(40.0)), pd.Series(np.ones(40))))


if __name__ == "__main__":
    unittest.main()
