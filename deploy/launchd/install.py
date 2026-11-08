"""Install the collector's schedule on a Mac as launchd agents.

The schedule is written in New York time and converted to this Mac's time zone, since launchd only
knows local time. Each job starts deploy/run.sh with /bin/bash, and the script keeps the Mac awake until
it finishes. Keep the repo outside ~/Documents, which launchd jobs can't read without Full Disk Access.
Logs go to ~/Library/Logs/factor-rotation/<job>.log.

    python3 deploy/launchd/install.py              # install or reinstall
    python3 deploy/launchd/install.py --dry-run    # print the plists, change nothing
    python3 deploy/launchd/install.py --uninstall
"""

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
REPO = Path(__file__).resolve().parents[2]
AGENTS = Path.home() / "Library" / "LaunchAgents"
LOGS = Path.home() / "Library" / "Logs" / "factor-rotation"
LABEL = "com.factor-rotation.{}"

# job -> times on NYSE weekdays, New York time
SCHEDULE = {
    "options-all": ["10:00", "15:30"],
    "options-etfs": ["11:00", "12:00", "13:00", "14:00"],
    "bars": ["16:30"],
    "nightly": ["19:00"],
    # Paper trading: entries into the opening auction, exits into the closing auction, then the fills.
    # Market-on-open orders must be in by 9:28 AM and market-on-close by 3:50 PM.
    "paper-open": ["09:22"],
    "paper-close": ["15:45"],
    "paper-sync": ["16:20"],
}


def local_zone() -> ZoneInfo:
    link = os.path.realpath("/etc/localtime")
    return ZoneInfo(link.split("zoneinfo/")[-1]) if "zoneinfo/" in link else ZoneInfo("America/New_York")


def calendar(times: list[str], tz: ZoneInfo, week: date) -> list[dict]:
    """launchd StartCalendarInterval entries (Weekday 0 = Sunday) for Monday-Friday at each New York time."""
    entries = []
    monday = week - timedelta(days=week.weekday())
    for offset in range(5):
        for t in times:
            hour, minute = map(int, t.split(":"))
            local = datetime.combine(monday + timedelta(days=offset), datetime.min.time(), NY).replace(
                hour=hour, minute=minute).astimezone(tz)
            entries.append({"Weekday": (local.weekday() + 1) % 7, "Hour": local.hour, "Minute": local.minute})
    return entries


def plist(job: str, entries: list[dict], path_env: str) -> dict:
    return {
        "Label": LABEL.format(job),
        "ProgramArguments": ["/bin/bash", str(REPO / "deploy" / "run.sh"), job],
        "WorkingDirectory": str(REPO),
        "EnvironmentVariables": {"PATH": path_env},
        "StartCalendarInterval": entries,
        "StandardOutPath": str(LOGS / f"{job}.log"),
        "StandardErrorPath": str(LOGS / f"{job}.log"),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--uninstall", action="store_true")
    a = p.parse_args()
    domain = f"gui/{os.getuid()}"

    if a.uninstall:
        for job in SCHEDULE:
            path = AGENTS / f"{LABEL.format(job)}.plist"
            subprocess.run(["launchctl", "bootout", domain, str(path)], capture_output=True)
            path.unlink(missing_ok=True)
            print(f"removed {path.name}")
        return

    tz = local_zone()
    winter, summer = calendar(["10:00"], tz, date(2027, 1, 11)), calendar(["10:00"], tz, date(2027, 7, 12))
    if winter != summer:
        sys.exit(f"{tz.key} doesn't shift for daylight saving with New York; set the Mac to a US time zone first")
    uv = shutil.which("uv")
    if not uv:
        sys.exit("uv is not on PATH; install it first")
    path_env = ":".join(dict.fromkeys([str(Path(uv).parent), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
                                       "/usr/sbin", "/sbin"]))
    LOGS.mkdir(parents=True, exist_ok=True)
    print(f"time zone {tz.key}; jobs scheduled in New York time")
    for job, times in SCHEDULE.items():
        doc = plist(job, calendar(times, tz, date.today()), path_env)
        path = AGENTS / f"{doc['Label']}.plist"
        when = ", ".join(f"{e['Hour']:02d}:{e['Minute']:02d}" for e in doc["StartCalendarInterval"][: len(times)])
        if a.dry_run:
            print(f"\n{path}  (local {when}, Mon-Fri)\n{plistlib.dumps(doc).decode()}")
            continue
        AGENTS.mkdir(parents=True, exist_ok=True)
        subprocess.run(["launchctl", "bootout", domain, str(path)], capture_output=True)
        path.write_bytes(plistlib.dumps(doc))
        subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
        print(f"installed {doc['Label']}: local {when}, Mon-Fri ({', '.join(times)} New York)")


if __name__ == "__main__":
    main()
