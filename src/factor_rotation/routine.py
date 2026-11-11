"""The daily routine and whether each step ran, for the dashboard's Today view.

Each step is judged by what it leaves behind, not by its process: the entry job's plan, the exit check's file, a
research pass's entry in the proposals. A launchd job that ran but left nothing is shown as failed with the last
line of its log. Times are New York.
"""

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

from . import bars, journal, storage
from .config import NY

LOGS = Path.home() / "Library" / "Logs" / "factor-rotation"

# key, time, name, what, who runs it, days, minutes before a step with nothing to show is called missed
STEPS = [
    ("preopen", time(8, 17), "Pre-open pass", "Lets last night's takes stand or stands them down. Must record by 9:14 AM.", "research", "session", 57),
    ("paper-open", time(9, 15), "Paper open", "Sends the day's entries as market-on-open orders.", "job", "session", 15),
    ("paper-check", time(9, 40), "Paper check", "Pulls fills and closes half-filled pairs.", "job", "session", 15),
    ("preclose", time(14, 50), "Pre-close review", "Hold, exit or extend for each open trade. Must record by 3:44 PM.", "research", "session", 54),
    ("paper-close", time(15, 45), "Paper close", "Enforces stops and time limits; exits go as market-on-close orders.", "job", "session", 15),
    ("paper-sync", time(16, 20), "Paper sync", "Pulls the day's fills, positions and account value.", "job", "session", 20),
    ("bars", time(16, 30), "Bars", "Five years of daily prices for every name.", "job", "session", 30),
    ("nightly", time(19, 0), "Nightly job", "Brief, earnings dates, scan and screen.", "job", "session", 60),
    ("evening", time(19, 30), "Evening pass", "Judges the screen and records take or pass for the next open.", "research", "eve", 240),
]


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text()) if path.exists() else None
    except ValueError:
        return None


def _at(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s).astimezone(NY) if s else None


def _pass_at(session: date, kind: str) -> datetime | None:
    doc = _read(journal.JOURNAL / "paper" / "proposals" / f"{session}.json") or {}
    times = [_at(p["at"]) for p in doc.get("passes", []) if p.get("pass") == kind]
    return min(times) if times else None


def _evidence(key: str, day: date) -> tuple[datetime | None, str | None]:
    """When the step left its mark on `day`, and a note. (None, note) when it left something unusable."""
    paper = journal.JOURNAL / "paper"
    if key == "preopen":
        return _pass_at(day, "preopen"), None
    if key == "evening":
        return _pass_at(journal.next_trading_day(day), "evening"), None
    if key == "preclose":
        doc = _read(paper / "reviews" / f"{day}.json")
        return (_at(doc.get("recorded_at") or doc.get("at")) or datetime.combine(day, time(15, 0), NY), None) if doc else (None, None)
    if key in ("paper-open", "paper-check", "paper-close"):
        doc = _read(paper / {"paper-open": "plans", "paper-check": "checks", "paper-close": "exits"}[key] / f"{day}.json")
        if not doc:
            return None, None
        note = "; ".join(doc.get("warnings") or []) or None
        if key == "paper-open":
            note = f"{len(doc.get('trades', []))} trades, {len(doc.get('orders', []))} orders" + (f"; {note}" if note else "")
        if key == "paper-close":
            orders = doc.get("orders", 0)
            note = f"{len(doc.get('exits', []))} exits, {len(orders) if isinstance(orders, list) else orders} orders" + (f"; {note}" if note else "")
        return _at(doc.get("built_at")), note
    if key == "paper-sync":
        doc = _read(paper / "fills" / f"{day}.json")
        at = _at(doc and doc.get("pulled_at"))
        return (at, None) if at and at.time() >= time(16, 15) else (None, None)
    if key == "bars":  # the file's time only shows the latest pull, so earlier days come from the job's own log
        m = storage.modified(bars.bars_path("SPY"))
        m = m.astimezone(NY) if m else None
        if m and m.date() == day and m.time() >= time(16, 25):
            return m, None
        done = [l for l in _log_lines("bars", datetime.combine(day, time(16, 25), NY), datetime.combine(day, time(23, 59), NY)) if "bars_done" in l]
        if done:
            return datetime.strptime(done[-1][:19], "%Y-%m-%d %H:%M:%S").astimezone().astimezone(NY), None
        return None, None
    if key == "nightly":
        screen = _read(journal.JOURNAL / "screens" / f"{day}.json")
        brief = (journal.JOURNAL / "briefs" / f"{day}.json").exists()
        if not screen:
            return None, ("brief written but no screen" if brief else None)
        if not brief:  # the evening pass builds its own screen when the nightly job didn't
            return None, "No brief: the job stopped early. A screen was built later, at " + _at(screen.get("built_at")).strftime("%-I:%M %p")
        return _at(screen.get("built_at")), None
    return None, None


def _log_lines(key: str, since: datetime, until: datetime) -> list[str]:
    """A launchd job's timestamped log lines between two times. The log is in the Mac's own time zone."""
    path = LOGS / f"{key}.log"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines()[-2000:]:
        try:
            at = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").astimezone().astimezone(NY)
        except ValueError:
            continue
        if since <= at < until:
            out.append(line.strip())
    return out


def _log_tail(key: str, since: datetime) -> str | None:
    """The last line the job logged on the day it was due, from its start time on."""
    lines = _log_lines(key, since, datetime.combine(since.date() + timedelta(days=1), time(0), NY))
    return lines[-1][:240] if lines else None


def _due(days: str, day: date) -> bool:
    if days == "session":
        return journal.is_trading_day(day)
    return journal.next_trading_day(day) - day <= timedelta(days=3) and day.weekday() in (6, 0, 1, 2, 3)  # Sun-Thu


def check(day: date, now: datetime) -> list[dict]:
    """Each step's state on `day`: done, missed, failed, running, upcoming or off (not scheduled that day)."""
    out = []
    for key, at, name, what, who, days, grace in STEPS:
        due = datetime.combine(day, at, NY)
        row = {"key": key, "time": at.strftime("%-I:%M %p"), "name": name, "what": what, "who": who, "day": day.isoformat()}
        if not _due(days, day):
            out.append({**row, "state": "off", "detail": "Not scheduled on this day"})
            continue
        when, note = _evidence(key, day)
        if when:
            late = (when - due).total_seconds() / 60
            out.append({**row, "state": "done", "at": when.isoformat(), "late_min": round(late) if late > grace else None, "detail": note})
        elif now < due:
            out.append({**row, "state": "upcoming", "detail": note})
        elif now < due + timedelta(minutes=grace):
            out.append({**row, "state": "running", "detail": note or "Due; nothing recorded yet"})
        else:
            tail = _log_tail(key, due) if who == "job" else None
            failed = bool(tail or note)
            out.append({**row, "state": "failed" if failed else "missed",
                        "detail": note or (f"Ran but left nothing. Last log line: {tail}" if tail else "Nothing recorded")})
    return out


def rows(now: datetime | None = None) -> list[dict]:
    """The routine for the last session before today and for today, one `routine` row per step and day."""
    now = now or datetime.now(NY)
    today = now.date()
    before = journal.previous_trading_day(today)
    return [{"section": "routine", **r} for d in (before, today) for r in check(d, now)]
