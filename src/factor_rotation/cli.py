import argparse
import asyncio
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from . import journal
from .analytics.compute import compute_exposures, latest_snapshot
from .config import NY
from .ib import bars, options
from .universe import CONSTITUENTS, ETFS, bar_universe


def _universe(group: str, symbols: str | None) -> list[str]:
    if symbols:
        return [s.strip().upper() for s in symbols.split(",")]
    return {"etfs": ETFS, "stocks": CONSTITUENTS, "all": ETFS + CONSTITUENTS}[group]


def _today() -> date:
    return datetime.now(NY).date()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stderr)
    logging.getLogger("ib_async").setLevel(logging.ERROR)
    p = argparse.ArgumentParser(prog="fr")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("options", help="snapshot option chains, then compute exposures")
    c.add_argument("--group", choices=["etfs", "stocks", "all"], default="all")
    c.add_argument("--symbols", help="comma-separated override, e.g. SPY,QQQ")

    c = sub.add_parser("bars", help="pull daily adjusted bars")
    c.add_argument("--duration", default="5 Y")
    c.add_argument("--symbols")

    c = sub.add_parser("exposures", help="recompute exposures for a stored snapshot")
    c.add_argument("--date", default=None)
    c.add_argument("--snapshot", default=None)

    c = sub.add_parser("brief", help="build the nightly brief from stored data")
    c.add_argument("--as-of", default=None)

    c = sub.add_parser("record", help="record tonight's ideas from a draft JSON")
    c.add_argument("draft", type=Path)

    sub.add_parser("trading-day", help="exit 0 if today is an NYSE trading day, else 1")
    sub.add_parser("score", help="score every idea whose trade date has bars")
    sub.add_parser("scorecard", help="print the running scorecard")

    c = sub.add_parser("check", help="record the pre-open check from a draft JSON (before 09:30)")
    c.add_argument("draft", type=Path)

    c = sub.add_parser("intraday", help="store today's 15-minute bars for the session's idea legs")
    c.add_argument("--date", default=None)

    c = sub.add_parser("ledger", help="write every idea and its result as JSON rows for the dashboard")
    c.add_argument("--marks", type=Path, help="intraday marks JSON for ideas not yet scored")
    c.add_argument("--out", type=Path, default=Path("ledger.json"))

    a = p.parse_args(argv)
    if a.cmd == "options":
        if not journal.is_trading_day(_today()):
            logging.info("market_closed date=%s", _today())
            return
        stats = asyncio.run(options.collect(_universe(a.group, a.symbols)))
        compute_exposures(stats["date"], stats["snapshot_id"])
        print(json.dumps(stats))
    elif a.cmd == "bars":
        symbols = [s.strip().upper() for s in a.symbols.split(",")] if a.symbols else bar_universe()
        print(json.dumps(asyncio.run(bars.collect(symbols, a.duration))))
    elif a.cmd == "exposures":
        day = a.date or _today().isoformat()
        snapshot = a.snapshot or latest_snapshot(day)
        df = compute_exposures(day, snapshot)
        print(df.drop(columns=["top_unusual"], errors="ignore").to_string(index=False))
    elif a.cmd == "brief":
        as_of = date.fromisoformat(a.as_of) if a.as_of else _today()
        brief = journal.build_brief(as_of)
        print(f"journal/briefs/{as_of}.json  pairs={len(brief['factor_pairs'])} options={len(brief['options'])} "
              f"bars_through={brief['bars_through']} snapshot={brief['options_snapshot']}")
    elif a.cmd == "record":
        print(journal.record_ideas(a.draft))
    elif a.cmd == "trading-day":
        sys.exit(0 if journal.is_trading_day(_today()) else 1)
    elif a.cmd == "score":
        for path in journal.score():
            print(path)
    elif a.cmd == "scorecard":
        print(journal.scorecard_report())
    elif a.cmd == "check":
        print(journal.record_check(a.draft))
    elif a.cmd == "intraday":
        day = date.fromisoformat(a.date) if a.date else _today()
        symbols = journal.intraday_symbols(day)
        if not symbols:
            logging.info("no_ideas date=%s", day)
            return
        print(journal.write_intraday(day, asyncio.run(bars.collect_intraday(symbols, day))))
    elif a.cmd == "ledger":
        rows = journal.ledger(json.loads(a.marks.read_text()) if a.marks else None)
        a.out.write_text(json.dumps(rows, indent=1) + "\n")
        print(f"{a.out}  ideas={len(rows)} " + " ".join(
            f"{s.lower()}={sum(r['status'] == s for r in rows)}" for s in ("Final", "Intraday", "Open")))
