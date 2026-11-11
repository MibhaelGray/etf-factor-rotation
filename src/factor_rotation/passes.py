"""The swing book's research passes run as background jobs, outside the Claude app: `fr pass pre-close`.

Each pass is Claude Code run headless on the prompt in deploy/passes/<name>.md, signed in with the token in
.env (FR_CLAUDE_TOKEN, from `claude setup-token`). The run can read files, search the web, spawn research
agents and run only the commands listed here; everything else is refused, whatever the Mac's own Claude
settings allow. Drafts go in data/pass-drafts/.

`--shadow` is the trial mode: the pass works on a copy of the journal in data/shadow-journal and records
there, so the pass scheduled in the Claude app stays the one that counts while the two are compared.
"""

import json
import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from . import journal
from .config import NY
from .intraday.model import binary

log = logging.getLogger(__name__)

MODEL, EFFORT = "claude-fable-5-2", "medium"
TIMEOUT = 40 * 60
PROMPTS = Path("deploy/passes")
DRAFTS = Path("data/pass-drafts")
SHADOW = Path("data/shadow-journal")
LOGS = Path("data/pass-logs")
READING = ["Read", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "Skill",
           f"Write({DRAFTS}/**)", f"Edit({DRAFTS}/**)", "Bash(uv run fr trading-day)", "Bash(uv run fr paper learn)",
           "Bash(uv run fr quotes:*)"]
# What each pass may run on top of the reading tools, and the journal file it has to leave behind.
PASSES = {
    "pre-close": {"allow": [f"Bash(uv run fr paper review {DRAFTS}/review.json)"], "leaves": "paper/reviews/{day}.json"},
}


def _shadow_journal(leaves: str) -> Path:
    """A fresh copy of the journal without today's record of this pass, so the trial run starts where the real
    one did."""
    if SHADOW.exists():
        shutil.rmtree(SHADOW)
    shutil.copytree(journal.JOURNAL, SHADOW)
    (SHADOW / leaves).unlink(missing_ok=True)
    return SHADOW


def run(name: str, shadow: bool = False) -> dict:
    spec = PASSES[name]
    now = datetime.now(NY)
    exe, token = binary(), os.environ.get("FR_CLAUDE_TOKEN")
    if not exe or not token:
        return {"pass": name, "ok": False, "error": "Claude Code not found" if not exe else "FR_CLAUDE_TOKEN is not set in .env"}
    leaves = spec["leaves"].format(day=now.date())
    root = _shadow_journal(leaves) if shadow else journal.JOURNAL
    before = (root / leaves).stat().st_mtime if (root / leaves).exists() else None
    DRAFTS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": token, "FR_JOURNAL_DIR": str(root.resolve()), **({"FR_PAPER_SEND": "0"} if shadow else {})}
    cmd = [exe, "-p", "--model", MODEL, "--effort", EFFORT, "--output-format", "json", "--permission-mode", "dontAsk",
           "--allowedTools", *READING, *spec["allow"]]
    try:
        done = subprocess.run(cmd, input=(PROMPTS / f"{name}.md").read_text(), capture_output=True, text=True, timeout=TIMEOUT, env=env)
        reply = json.loads(done.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return {"pass": name, "ok": False, "shadow": shadow, "error": repr(e)[:300]}
    after = (root / leaves).stat().st_mtime if (root / leaves).exists() else None
    out = {"pass": name, "shadow": shadow, "started": now.isoformat(timespec="seconds"),
           "minutes": round((datetime.now(NY) - now).total_seconds() / 60, 1),
           "ok": not reply.get("is_error") and after is not None and after != before, "recorded": str(root / leaves) if after and after != before else None,
           "turns": reply.get("num_turns"), "agents": (reply.get("subagent_stats") or {}).get("spawned"),
           "refused": [f"{d.get('tool_name')}: {str(d.get('tool_input'))[:120]}" for d in reply.get("permission_denials", [])],
           "message": reply.get("result")}
    (LOGS / f"{now.date()}-{name}{'-shadow' if shadow else ''}.json").write_text(json.dumps(out, indent=1) + "\n")
    return out
