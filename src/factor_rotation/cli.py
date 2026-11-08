import argparse
import asyncio
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from . import bars, journal
from .analytics.compute import compute_exposures, latest_snapshot
from .config import NY
from .ib import options
from .universe import ETFS, OPTION_INDEXES, OPTION_STOCKS, bar_universe, option_universe


def _universe(group: str, symbols: str | None) -> list[str]:
    if symbols:
        return [s.strip().upper() for s in symbols.split(",")]
    return {"etfs": list(OPTION_INDEXES) + ETFS, "stocks": OPTION_STOCKS, "all": option_universe()}[group]


def _today() -> date:
    return datetime.now(NY).date()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stderr)
    logging.getLogger("ib_async").setLevel(logging.ERROR)
    # ib_async logs every API error itself; session.py already logs the ones that matter, so this keeps
    # thousands of "no security definition" lines for unlisted built options out of the job logs.
    logging.getLogger("ib_async.wrapper").setLevel(logging.CRITICAL)
    p = argparse.ArgumentParser(prog="fr")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("options", help="snapshot option chains, then compute exposures")
    c.add_argument("--group", choices=["etfs", "stocks", "all"], default="all")
    c.add_argument("--symbols", help="comma-separated override, e.g. SPY,QQQ")

    c = sub.add_parser("bars", help="pull daily adjusted bars")
    c.add_argument("--duration", default="5y")
    c.add_argument("--symbols")
    c.add_argument("--pending", action="store_true",
                   help="only names outside the standard universe: legs of unscored ideas and the basket watchlist")

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

    c = sub.add_parser("morning", help="record the morning book: 1-2 new ideas from the overnight session (before 09:30)")
    c.add_argument("draft", type=Path)

    c = sub.add_parser("intraday", help="store today's 15-minute bars for the session's idea legs")
    c.add_argument("--date", default=None)

    c = sub.add_parser("market", help="write tonight's market rows for the dashboard from the brief")
    c.add_argument("--as-of", default=None)
    c.add_argument("--out", type=Path, default=Path("market.json"))

    c = sub.add_parser("serve", help="serve the local dashboard with a working Refresh button")
    c.add_argument("--port", type=int, default=8765)

    sub.add_parser("earnings", help="refresh the earnings dates for the radar, from Yahoo Finance")
    sub.add_parser("gaps", help="rebuild the table of what stocks did after earnings gaps")
    sub.add_parser("scan", help="list tonight's stock and basket candidates")
    sub.add_parser("screen", help="check every name, basket and factor pair against the setups; write the shortlist")

    c = sub.add_parser("ledger", help="write every idea and its result as JSON rows for the dashboard")
    c.add_argument("--marks", type=Path, help="intraday marks JSON for ideas not yet scored")
    c.add_argument("--out", type=Path, default=Path("ledger.json"))

    c = sub.add_parser("paper", help="paper-trading jobs on the IBKR paper account (see paper/__init__.py)")
    c.add_argument("job", choices=["open", "close", "sync", "status", "report", "halt", "resume"])
    c.add_argument("--flatten", action="store_true", help="close: exit every open trade at today's close")
    c.add_argument("--reason", default="", help="halt: why, kept in the HALT file")
    c = sub.add_parser("dashboard", help="build a local copy of the dashboard (dashboard/local.html) and open it")
    c.add_argument("--as-of", default=None, help="brief date for tonight's market rows (default: the latest brief)")
    c.add_argument("--marks", type=Path, help="intraday marks JSON for ideas not yet scored")
    c.add_argument("--out", type=Path, default=None)
    c.add_argument("--no-open", action="store_true", help="build without opening it in the browser")

    a = p.parse_args(argv)
    if a.cmd == "options":
        if not journal.is_trading_day(_today()):
            logging.info("market_closed date=%s", _today())
            return
        stats = asyncio.run(options.collect(_universe(a.group, a.symbols)))
        compute_exposures(stats["date"], stats["snapshot_id"])
        print(json.dumps(stats))
    elif a.cmd == "bars":
        from .market import basket_library
        watch = [m["symbol"] for b in basket_library()["baskets"] for m in b["members"]]
        extra = sorted({s for s in [*journal.pending_legs(), *watch] if s not in set(bar_universe())})
        if a.pending:
            if not extra:
                print(json.dumps({"symbols": 0, "failed": []}))
                return
            print(json.dumps(bars.collect(extra, "2y")))
            return
        symbols = [s.strip().upper() for s in a.symbols.split(",")] if a.symbols else bar_universe() + extra
        print(json.dumps(bars.collect(symbols + ([] if a.symbols else list(bars.INDEXES)), a.duration)))
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
    elif a.cmd == "morning":
        print(journal.record_morning(a.draft))
    elif a.cmd == "intraday":
        day = date.fromisoformat(a.date) if a.date else _today()
        symbols = journal.intraday_symbols(day)
        if not symbols:
            logging.info("no_ideas date=%s", day)
            return
        print(journal.write_intraday(day, bars.collect_intraday(symbols, day)))
    elif a.cmd == "serve":
        from . import serve
        serve.run(a.port)
    elif a.cmd == "gaps":
        from .analytics import earnings_gaps
        df = earnings_gaps.events()
        rows = earnings_gaps.table(df)
        earnings_gaps.PATH.write_text(json.dumps({"events": len(df), "from": str(df["session"].min().date()),
                                                  "to": str(df["session"].max().date()), "rows": rows}, indent=1) + "\n")
        print(f"{earnings_gaps.PATH}  events={len(df)}")
    elif a.cmd == "scan":
        from . import scan
        path, payload = scan.write()
        print(f"{path}  tonight={len(payload['reports']['tonight'])} traded={len(payload['reports']['traded_today'])} "
              f"settled={payload['bars_settled']}")
    elif a.cmd == "screen":
        from . import screen
        path, payload = screen.write()
        print(path)
        print(screen.summary(payload))
    elif a.cmd == "earnings":
        from . import earnings
        print(json.dumps(earnings.pull()))
    elif a.cmd == "market":
        from . import market
        as_of = date.fromisoformat(a.as_of) if a.as_of else _today()
        rows = market.build(as_of)
        a.out.write_text(json.dumps(rows, separators=(",", ":"), default=journal._json_default) + "\n")
        counts = {}
        for r in rows:
            counts[r["section"]] = counts.get(r["section"], 0) + 1
        print(f"{a.out}  " + " ".join(f"{k}={v}" for k, v in counts.items()))
    elif a.cmd == "dashboard":
        import webbrowser

        from . import localdash
        out = a.out or localdash.DEFAULT_OUT
        info = localdash.build(out, date.fromisoformat(a.as_of) if a.as_of else None,
                               json.loads(a.marks.read_text()) if a.marks else None)
        print(f"{info['out']}  ideas={info['ideas']} market_rows={info['market_rows']} ({info['market']})")
        if not a.no_open:
            webbrowser.open(out.resolve().as_uri())
    elif a.cmd == "paper":
        _paper(a)
    elif a.cmd == "ledger":
        rows = journal.ledger(json.loads(a.marks.read_text()) if a.marks else None)
        a.out.write_text(json.dumps(rows, indent=1) + "\n")
        print(f"{a.out}  ideas={len(rows)} " + " ".join(
            f"{s.lower()}={sum(r['status'] == s for r in rows)}" for s in ("Final", "Intraday", "Open")))


def _paper(a) -> None:
    from .paper import book
    from .paper.broker import IBPaper, PaperSettings

    today = _today()
    if a.job in ("open", "close", "sync") and not journal.is_trading_day(today):
        logging.info("market_closed date=%s", today)
        return
    if a.job == "open":
        plan = asyncio.run(book.open_session(today))
        tag = " (dry run: set FR_PAPER_SEND=1 to send)" if plan["dry_run"] else ""
        print(f"paper open {today}{tag}: {len(plan['trades'])} trades, {len(plan['orders'])} orders, {len(plan['skipped'])} skipped"
              + (f"; HALTED: {plan['halted']}" if plan.get("halted") else ""))
        for t in plan["trades"]:
            print(f"  {t['id']:<28} {t['book']:<9} {t['name'][:34]:<34} {t['setup']:<20} {t['horizon_days']}d stop {journal.stop_text(t) or '-'}")
        for o in plan["orders"]:
            if o["status"] not in ("PreSubmitted", "Submitted", "dry run"):
                print(f"  ! {o['trade_id']} {o['action']} {o['shares']} {o['symbol']}: {o['status']} {o.get('note', '')}")
        for k in plan["skipped"]:
            print(f"  skipped {k['name']}: {k['reason']}")
    elif a.job == "close":
        out = asyncio.run(book.close_session(today, flatten=a.flatten))
        tag = " (dry run)" if out["dry_run"] else ""
        print(f"paper close {today}{tag}: {len(out['exits'])} exits, {len(out['orders'])} orders")
        for e in out["exits"]:
            print(f"  {e['trade_id']}: {e['reason']}")
        for o in out["orders"]:
            if o["status"] not in ("PreSubmitted", "Submitted", "dry run"):
                print(f"  ! {o['trade_id']} {o['action']} {o['shares']} {o['symbol']}: {o['status']} {o.get('note', '')}")
    elif a.job == "sync":
        out = asyncio.run(book.sync(today))
        print(f"paper sync {today}: {len(out['fills'])} fills, {len(out['positions'])} positions, net liquidation {out['net_liquidation']}")
        for m in out["mismatches"]:
            print(f"  ! {m}")
    elif a.job == "status":
        async def status():
            async with IBPaper(readonly=True) as b:
                return b.s.account, await b.net_liquidation(), await b.positions()
        account, nav, positions = asyncio.run(status())
        print(f"paper account {account} on port {PaperSettings().port}: net liquidation {nav}, {len(positions)} positions")
        print(f"orders {'ON (FR_PAPER_SEND=1)' if PaperSettings().send else 'off: dry runs only'}; "
              f"{'HALTED: ' + book.HALT.read_text().strip() if book.HALT.exists() else 'not halted'}")
        for m in book.reconcile(positions):
            print(f"  ! {m}")
    elif a.job == "report":
        print(book.summary_text())
        for r in book.rows():
            print(f"  {r['id']:<28} {r['state']:<8} {r['name'][:30]:<30} {r['bps'] if r['bps'] is not None else '-':>8} bp  {r['reason'] or ''}")
    elif a.job == "halt":
        book.HALT.parent.mkdir(parents=True, exist_ok=True)
        book.HALT.write_text((a.reason or "halted by hand") + "\n")
        print(f"{book.HALT} written: no new paper trades until fr paper resume")
    elif a.job == "resume":
        book.HALT.unlink(missing_ok=True)
        print("paper trading resumed")
