import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

from . import bars, drawdown, journal
from .analytics.compute import compute_exposures, latest_snapshot
from .config import NY
from .ib import options
from .universe import CORE_ETFS, OPTION_INDEXES, OPTION_STOCKS, bar_universe, option_universe


def _universe(group: str, symbols: str | None) -> list[str]:
    if symbols:
        return [s.strip().upper() for s in symbols.split(",")]
    return {"etfs": list(OPTION_INDEXES) + CORE_ETFS, "stocks": OPTION_STOCKS, "all": option_universe()}[group]


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
                   help="only names outside the standard universe: the paper book's legs and the basket watchlist")

    c = sub.add_parser("exposures", help="recompute exposures for a stored snapshot")
    c.add_argument("--date", default=None)
    c.add_argument("--snapshot", default=None)

    c = sub.add_parser("brief", help="build the nightly brief from stored data")
    c.add_argument("--as-of", default=None)

    sub.add_parser("trading-day", help="exit 0 if today is an NYSE trading day, else 1")
    c = sub.add_parser("market", help="write tonight's market rows for the dashboard from the brief")
    c.add_argument("--as-of", default=None)
    c.add_argument("--out", type=Path, default=Path("market.json"))

    c = sub.add_parser("serve", help="serve the local dashboard with a working Refresh button")
    c.add_argument("--port", type=int, default=8765)

    sub.add_parser("earnings", help="refresh the earnings dates for the radar, from Yahoo Finance")
    sub.add_parser("gaps", help="rebuild the table of what stocks did after earnings gaps")
    sub.add_parser("scan", help="list tonight's stock and basket candidates")
    sub.add_parser("screen", help="check every name, basket and factor pair against the setups; write the shortlist")

    c = sub.add_parser("paper", help="paper-trading jobs on the IBKR paper account (see paper/__init__.py)")
    c.add_argument("--execute", action="store_true",
                   help="a job that sends orders only looks when it is run by hand: it prints what it would send "
                        "and sends nothing. This makes it count, as the scheduled job does")
    c.add_argument("job", choices=["open", "early", "check", "midday", "close", "sync", "status", "report", "halt", "resume",
                                   "propose", "review", "midday-check", "postmortem", "learn", "hedges", "side", "holds", "depth"])
    c.add_argument("args", nargs="*", help="propose, review, midday-check, postmortem: the draft JSON file; hedges: the symbols on one side; "
                                           "side: `report` to score the side facts instead of pulling them")
    c.add_argument("--flatten", action="store_true", help="close: exit every open trade at today's close")
    c.add_argument("--reason", default="", help="halt: why, kept in the HALT file")
    c.add_argument("--book", choices=["swing", "intraday"],
                   help="resume: lift that book's 10%% pause (drawdown.py) instead of the HALT file")
    c = sub.add_parser("pass", help="run one of the swing book's research passes as a background job (see passes.py)")
    c.add_argument("name", choices=["pre-close"])
    c.add_argument("--shadow", action="store_true", help="work on a copy of the journal, so the app's pass stays the one that counts")
    c = sub.add_parser("quotes", help="current prices from Yahoo for stocks, ETFs, indexes (VIX, TNX) and futures (ES=F, CL=F)")
    c.add_argument("names", nargs="+")
    c = sub.add_parser("intraday", help="the intraday book: 15-minute bars and the replay of its setups (see intraday/__init__.py)")
    c.add_argument("job", choices=["bars", "replay", "tick", "ext", "report"])
    c.add_argument("--execute", action="store_true",
                   help="a tick run by hand only looks: it writes nothing and sends no orders. This makes it count, "
                        "as the scheduled job does. The scheduled jobs set FR_SCHEDULED=1 and need no flag")
    c = sub.add_parser("dashboard", help="build a local copy of the dashboard (dashboard/local.html) and open it")
    c.add_argument("--as-of", default=None, help="brief date for tonight's market rows (default: the latest brief)")
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
        from .paper import book
        watch = [m["symbol"] for b in basket_library()["baskets"] for m in b["members"]]
        extra = sorted({s for s in [*watch, *book.symbols_held()] if s not in set(bar_universe())})
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
    elif a.cmd == "trading-day":
        sys.exit(0 if journal.is_trading_day(_today()) else 1)
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
    elif a.cmd == "pass":
        from . import passes
        out = passes.run(a.name, a.shadow)
        print(f"pass {a.name}{' (shadow)' if a.shadow else ''}: {'recorded ' + out['recorded'] if out.get('recorded') else 'NOTHING RECORDED'}"
              f"{'; ' + out['error'] if out.get('error') else ''} in {out.get('minutes', 0)} min, {out.get('agents') or 0} agents, {len(out.get('refused') or [])} refused")
        for r in out.get("refused") or []:
            print(f"  refused {r}")
        print(out.get("message") or "")
        sys.exit(0 if out["ok"] else 1)
    elif a.cmd == "quotes":
        from . import quotes
        print(quotes.text(quotes.fetch(a.names)))
    elif a.cmd == "intraday":
        from .intraday import bars15, replay
        if a.job == "bars":
            print(json.dumps(bars15.collect(replay.universe())))
        elif a.job == "tick":
            from .intraday import live
            dry = _looking(a)
            out = live.tick(dry=dry)
            print(f"intraday tick {out['date']}{' (a look: nothing written or sent)' if dry else ''}: "
                  + (out.get("skipped") or f"bar closed {out['bar_closed']}, {len(out['new'])} new, {out['open']} open, {out['total']} today"))
            for x in out.get("new", []):
                v = x["verdict"]
                print(f"  {x['symbol']:<6} vs {x['hedge']:<5} {x['setup']:<12} {x['direction']:<5} {v['decision']:<10} {v.get('why') or v.get('error') or ''}")
            for x in out.get("paper", []):
                r = x.get("result")
                print(f"  paper {x['id']:<16} {x['state']:<9} {x.get('exit_reason') or ''}" + (f"  {r['dollars']:+.2f} ({r['bp']:+.1f} bp)" if r else ""))
            _notify("intraday", out.get("warnings", []))
        elif a.job == "ext":
            from .intraday import extended
            dry = _looking(a)
            out = extended.tick(dry=dry)
            print(f"intraday ext {out.get('night', '')}{' (a look: nothing written or sent)' if dry else ''}: "
                  + (out.get("skipped") or f"{out['session']}, {out['quoted']} prices from {out['prices']}, "
                                           f"{len(out['new'])} new"))
            for x in out.get("new", []):
                v = x["verdict"]
                print(f"  {x['symbol']:<6} vs {x['hedge']:<5} {x['direction']:<5} {x['numbers']['move_atr']:+.1f} ATR  {v['decision']:<10} {v.get('why') or v.get('error') or ''}")
            for x in out.get("paper", []):
                r = x.get("result")
                print(f"  paper {x['id']:<24} {x['state']:<9} {x.get('exit_reason') or ''}" + (f"  {r['dollars']:+.2f} ({r['bp']:+.1f} bp)" if r else ""))
            _notify("intraday ext", out.get("warnings", []))
        elif a.job == "report":
            from .intraday import extended, live
            print(json.dumps({"day": live.report(), "extended_hours": extended.report()}, indent=1))
        else:
            path, payload = replay.write()
            print(path)
            print(replay.text(payload))
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
        info = localdash.build(out, date.fromisoformat(a.as_of) if a.as_of else None)
        print(f"{info['out']}  market_rows={info['market_rows']} ({info['market']})")
        if not a.no_open:
            webbrowser.open(out.resolve().as_uri())
    elif a.cmd == "paper":
        _paper(a)


def _looking(a) -> bool:
    """True when this run only looks. A tick started by deploy/run.sh is a scheduled one and counts; a tick run
    by hand writes nothing and sends nothing unless --execute says so."""
    return not (os.environ.get("FR_SCHEDULED") == "1" or getattr(a, "execute", False))


def _dry_tag(dry: bool, looking: bool) -> str:
    if looking:
        return " (a look: nothing sent; add --execute to trade)"
    return " (dry run: set FR_PAPER_SEND=1 to send)" if dry else ""


def _notify(job: str, warnings: list[str]) -> None:
    """Print a job's warnings and, on a Mac, raise a notification, so a failure isn't found days later in a
    log. Best effort: a notification that can't be shown changes nothing."""
    for w in warnings:
        print(f"  ! {w}")
    if not warnings or sys.platform != "darwin":
        return
    import subprocess

    text = warnings[0] if len(warnings) == 1 else f"{len(warnings)} warnings. First: {warnings[0]}"
    script = 'on run argv\ndisplay notification (item 1 of argv) with title (item 2 of argv)\nend run'
    try:
        subprocess.run(["osascript", "-e", script, text[:240], f"Paper {job}"], capture_output=True, timeout=10)
    except Exception:
        pass


def _research(a) -> None:
    """The research pass's commands. They only read and write the journal; none of them reaches the gateway."""
    from .paper import learn, proposals

    def draft() -> dict:
        if len(a.args) != 1:
            sys.exit(f"usage: fr paper {a.job} <draft.json>")
        return json.loads(Path(a.args[0]).read_text())

    try:
        if a.job == "propose":
            path, doc, notes = proposals.record(draft())
            takes = [p for p in doc["proposals"] if p["decision"] == "take"]
            print(f"{path}: {len(doc['proposals'])} verdicts for {doc['session']}, {len(takes)} takes "
                  f"(the entry job trades up to {proposals.rules.MAX_NEW}, strongest conviction first, then by rank); "
                  f"{sum(bool(p.get('rank')) for p in doc['proposals'])} ranked")
            for p in takes:
                st = p.get("stats") or {}
                legs = " / ".join(filter(None, [p["long"] and "long " + ", ".join(p["long"]), p["short"] and "short " + ", ".join(p["short"])]))
                print(f"  {p['id']}  {p['name'][:24]:<24} conviction {p['conviction']}  {p['plan']['horizon_days']}d, stop {abs(p['plan']['stop']['spread_pct']):g}%  {legs}"
                      + (f"  (hedge correlation {st['corr_60d']:+.2f}, beta {st['beta_60d']:.2f})" if st else "")
                      + (f"  {p['preopen']['decision'].upper().replace('_', ' ')}: {p['preopen']['reason']}" if p.get("preopen") else ""))
            for n in notes:
                print(f"  note: {n}")
            try:  # the night's research depth, kept beside the verdicts (paper/depth.py); it never stops a pass recording
                from .paper import depth
                m = depth.measure()
                if m["measured"] and depth.keep(date.fromisoformat(doc["session"]), m):
                    for f in m["flags"]:
                        print(f"  note: {f}")
                    if m["thin"]:
                        print(f"  note: research was thin for {', '.join(sorted(m['thin']))} (uv run fr paper depth)")
            except Exception as e:  # noqa: BLE001
                print(f"  note: research depth not kept ({type(e).__name__}: {e})")
        elif a.job == "review":
            path = learn.record_review(draft())
            for r in json.loads(path.read_text())["reviews"]:
                print(f"  {r['trade_id']}: {r['action']}" + (f" {r['sessions']} sessions" if r["action"] == "extend" else "") + f" ({r['thesis']}): {r['reason']}")
            print(f"{path}: the 3:45 PM ET exit check will read it")
        elif a.job == "midday-check":
            path = learn.record_midday(draft())
            for r in json.loads(path.read_text())["reviews"]:
                print(f"  {r['trade_id']}: {r['action']} ({r['thesis']}): {r['reason']}")
            print(f"{path}: the next midday job (1:15 PM and 2:15 PM ET) closes the exits at the market")
        elif a.job == "postmortem":
            print(learn.record_postmortems(draft()))
        elif a.job == "learn":
            print(learn.brief())
        elif a.job == "hedges":
            if not a.args:
                sys.exit("usage: fr paper hedges SYMBOL [SYMBOL ...]  (several symbols are one equal-weight side)")
            print(f"{', '.join(a.args)} against each ETF, daily returns over 60 sessions:")
            for r in learn.hedges(a.args):
                print(f"  {r['hedge']:<6} correlation {r['corr_60d']:+.2f}  beta {r['beta_60d']:.2f}")
    except (ValueError, FileNotFoundError, FileExistsError, KeyError) as e:
        sys.exit(f"fr paper {a.job} refused: {e}")


def _paper(a) -> None:
    from .paper import book
    from .paper.broker import IBPaper, PaperSettings

    if a.job in ("propose", "review", "midday-check", "postmortem", "learn", "hedges"):
        return _research(a)
    if a.job == "depth":
        # How much research each candidate got, from this session's transcripts (paper/depth.py). `args`: a session id.
        from .paper import depth
        return print(depth.report(depth.measure(a.args[0] if a.args else None)))
    if a.job == "holds":
        # Every idea scored at every hold from 1 to 10 sessions (paper/holds.py). Journal and bars only.
        from .paper import holds
        return print(holds.report())
    if a.job == "side":
        # Facts recorded beside the verdicts and kept from the research passes (paper/side.py). No gateway.
        from .paper import side
        if a.args == ["report"]:
            return print(side.report())
        r = side.record(dry=_looking(a))
        print(f"paper side {r['session']}: " + (r.get("note") or f"{len(r['pulled'])} names pulled, {len(r['failed'])} failed, {r['have']} already had"))
        if r["failed"]:
            print("  ! nothing from Yahoo for " + ", ".join(r["failed"]) + "; the next run tries again")
        return
    today = _today()
    if a.job in ("open", "check", "midday", "close", "sync") and not journal.is_trading_day(today):
        logging.info("market_closed date=%s", today)
        return
    # A job that starts late (the Mac woke from sleep, say) must not send orders after the auction cutoffs:
    # market-on-open by 9:28 AM ET, market-on-close by 3:50 PM ET. Missed entries are simply not taken;
    # missed exits go out the next session, since a trade past its time limit exits at the next check.
    now = datetime.now(NY).strftime("%H:%M")
    # The post-open check closes half-filled trades at the market, so it runs only while the market is open
    # and before the 3:45 PM exits, which deal with whatever is left.
    window = {"open": ("09:00", "09:27"), "check": ("09:33", "15:29"), "midday": ("10:00", "15:29"),
              "close": ("15:30", "15:49")}.get(a.job)
    if window and not (window[0] <= now <= window[1]) and PaperSettings().send:
        print(f"paper {a.job}: {now} ET is outside {window[0]}-{window[1]} ET, so nothing was sent")
        sys.exit(1)
    looking = _looking(a) if a.job in ("early", "open", "check", "midday", "close") else False
    if a.job == "early":
        from .paper import early
        r = asyncio.run(early.run(dry=looking))
        tag = _dry_tag(r["dry_run"], looking)
        print(f"paper early {r['at']}{tag}: " + (r.get("note") or f"{r['venue']} session, {len(r['new'])} new takes, "
                                                 f"{r['cancelled']} cancelled, {len(r['orders'])} limit orders"))
        for o in r["orders"]:
            print(f"  {o['trade_id']:<16} {o['action']:<4} {o['shares']:>6} {o['symbol']:<6} limit {o['limit']:.2f}  {o.get('status', '')}")
        for k in r["skipped"]:
            print(f"  skipped {k['name']}: {k['reason']}")
        _notify("early", r["warnings"])
    elif a.job == "open":
        plan = asyncio.run(book.open_session(today, dry=looking))
        tag = _dry_tag(plan["dry_run"], looking)
        print(f"paper open {today}{tag}: {len(plan['trades'])} trades, {len(plan['orders'])} orders, {len(plan['skipped'])} skipped"
              + (f"; HALTED: {plan['halted']}" if plan.get("halted") else ""))
        for t in plan["trades"]:
            print(f"  {t['id']:<28} {t['book']:<9} {t['name'][:34]:<34} {t['setup']:<20} {t['horizon_days']}d stop {journal.stop_text(t) or '-'}")
        for k in plan["skipped"]:
            print(f"  skipped {k['name']}: {k['reason']}")
        _notify("open", plan.get("warnings", []))
    elif a.job == "check":
        out = asyncio.run(book.check_session(today, dry=looking))
        print(f"paper check {today}: " + (out.get("note") or f"{len(out['unfilled'])} trades never filled, {len(out['exits'])} broken trades "
                                          f"closed with {len(out['orders'])} orders, {len(out['waiting'])} still waiting"))
        _notify("check", out["warnings"])
    elif a.job == "midday":
        out = asyncio.run(book.midday_session(today, dry=looking))
        tag = _dry_tag(out["dry_run"], looking)
        print(f"paper midday {today}{tag}: " + (out.get("note") or f"{len(out['exits'])} exits, {len(out['orders'])} market orders"))
        for e in out["exits"]:
            print(f"  {e['trade_id']}: {e['reason']}")
        _notify("midday", out["warnings"])
    elif a.job == "close":
        out = asyncio.run(book.close_session(today, flatten=a.flatten, dry=looking))
        tag = _dry_tag(out["dry_run"], looking)
        print(f"paper close {today}{tag}: {len(out['exits'])} exits, {len(out['orders'])} orders")
        for e in [*out["exits"], *out["retried"]]:
            print(f"  {e['trade_id']}: {e['reason']}")
        _notify("close", out["warnings"])
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
        for name in ("swing", "intraday"):
            d = drawdown.status(name)
            print(f"  {name} book ${d['value']:,.0f}, high ${d['peak']:,.0f}, pauses at ${d['pause_at']:,.0f}"
                  + (f"; PAUSED: {d['paused']}" if d["paused"] else ""))
        for m in book.reconcile(positions):
            print(f"  ! {m}")
    elif a.job == "report":
        print(book.summary_text())
        for w in book.alerts():
            print(f"  ! {w}")
        for r in book.rows():
            print(f"  {r['id']:<28} {r['state']:<8} {r['name'][:30]:<30} {r['bps'] if r['bps'] is not None else '-':>8} bp  {r['reason'] or ''}")
    elif a.job == "halt":
        book.HALT.parent.mkdir(parents=True, exist_ok=True)
        book.HALT.write_text((a.reason or "halted by hand") + "\n")
        print(f"{book.HALT} written: no new paper trades until fr paper resume")
    elif a.job == "resume" and a.book:
        d = drawdown.resume(a.book)
        print(f"{a.book} book resumed at ${d['value']:,.0f}; it pauses again at ${d['value'] - d['limit']:,.0f}")
    elif a.job == "resume":
        book.HALT.unlink(missing_ok=True)
        print("paper trading resumed")
