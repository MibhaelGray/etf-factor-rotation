"""How much research each candidate got, measured from the pass's own transcripts.

The evening pass hands its candidates to research agents. Over the first four nights (2026-11-09 to 11-12) an
agent took four or five names at once and each name got about six web searches and two pages read: real, but
set by the workload, and nothing held it there. Since 2026-11-14 the skill gives each name its own agent with a
bare brief ("research it very deeply; I want to understand the story") and leaves the rest to the agent, and this
module checks that every name got one and that it looked: a rule nobody measures is a hope. The counts are a floor
for looking. They say nothing about whether the story is right; three agents to a name with a fixed fact sheet
each were tried the same day and dropped for producing lists, not stories.

`fr paper depth` reads what Claude Code wrote for the session it is run in (CLAUDE_CODE_SESSION_ID): the model and
effort the pass itself is on, and for each agent it started, the names it was given, the model it ran on, and how
many searches it made and pages it opened. It can't be talked round: the numbers come from the tool calls, not
from what an agent says it did. The pass runs it after the research is in and before judging, sends another agent
after any name that comes back thin, and `fr paper propose` keeps the night's measurement in
journal/paper/research/<session>.json, so depth can later be set against results.

An agent says which name is its own with a first line `NAMES: YETI` (a basket is one name). An agent with no such
line (the market's stories, a challenger) isn't measured against the floors. A second agent on a name adds to it.

It reports and never refuses: a pass on the wrong model or with thin research still records its verdicts, since a
missed night costs more than a flagged one. Claude Code's transcript layout is not ours to rely on, so anything
unreadable is reported as "not measured" and nothing else breaks.
"""

import json
import os
import re
from datetime import date, datetime
from pathlib import Path

from ..config import NY
from . import proposals

JUDGE_MODEL, JUDGE_EFFORT = "claude-fable-5-2", "high"      # the pass itself: it judges and ranks
AGENT_MODEL, AGENT_EFFORT = "claude-opus-5-5", "medium"     # its research agents: they gather facts
MAX_NAMES = 2          # names to one agent; one is the rule, two when they share a story
MIN_SEARCHES = 6       # web searches a name, over its agents
MIN_READS = 4          # pages opened and read a name, over its agents (the first four nights averaged two)
NAMES_LINE = re.compile(r"^\s*NAMES:\s*(.+?)\s*$", re.M)


def folder() -> Path:
    """Where Claude Code keeps this project's session transcripts."""
    if os.environ.get("FR_TRANSCRIPTS"):
        return Path(os.environ["FR_TRANSCRIPTS"])
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(Path.cwd().resolve()))


def _lines(path: Path):
    with path.open() as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _text(content) -> str:
    return content if isinstance(content, str) else " ".join(b.get("text", "") for b in content or [] if isinstance(b, dict))


def _model(path: Path) -> str | None:
    """The model that wrote most of a transcript's turns."""
    seen: dict[str, int] = {}
    for x in _lines(path):
        m = (x.get("message") or {}).get("model") if x.get("type") == "assistant" else None
        if m and not m.startswith("<"):
            seen[m] = seen.get(m, 0) + 1
    return max(seen, key=seen.get) if seen else None


def _agent(path: Path) -> dict:
    meta_path = path.with_name(path.name.replace(".jsonl", ".meta.json"))
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    prompt, searches, reads, other, models = None, 0, 0, 0, {}
    for x in _lines(path):
        m = x.get("message") or {}
        if x.get("type") == "user" and prompt is None:
            prompt = _text(m.get("content"))
        if x.get("type") != "assistant":
            continue
        if m.get("model") and not m["model"].startswith("<"):
            models[m["model"]] = models.get(m["model"], 0) + 1
        for b in m.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                name = b.get("name", "").lower()
                if "search" in name and "tool" not in name:
                    searches += 1
                elif "fetch" in name:
                    reads += 1
                else:
                    other += 1
    found = NAMES_LINE.search("\n".join((prompt or "").splitlines()[:5]))
    return {"what": meta.get("description") or (prompt or "")[:60], "names": [n.strip() for n in found.group(1).split(",") if n.strip()] if found else [],
            "model": max(models, key=models.get) if models else None, "effort": meta.get("effort"),
            "searches": searches, "reads": reads, "other_tools": other}


def measure(session_id: str | None = None, candidates: list[str] | None = None) -> dict:
    """The pass's judge and agents, each researched name's searches and pages, and what falls short. `candidates`
    are the names that should each have a research agent (tonight's screen); None looks them up."""
    session_id = session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
    main = folder() / f"{session_id}.jsonl" if session_id else None
    if not main or not main.exists():
        return {"measured": False, "why": "no Claude Code session to read" if not session_id else f"no transcript for session {session_id} in {folder()}"}
    agents = [_agent(p) for p in sorted((folder() / session_id / "subagents").glob("agent-*.jsonl"))]
    # The effort level is only known for the session this is run in; another session's transcript doesn't hold it.
    judge = {"model": _model(main), "effort": os.environ.get("CLAUDE_EFFORT") if session_id == os.environ.get("CLAUDE_CODE_SESSION_ID") else None}
    flags = []
    if judge["model"] and judge["model"] != JUDGE_MODEL:
        flags.append(f"this pass is running on {judge['model']}, not {JUDGE_MODEL}: the owner has to pick the model in the app, a session can't change its own")
    if judge["effort"] and judge["effort"] != JUDGE_EFFORT:
        flags.append(f"this pass is at {judge['effort']} effort, not {JUDGE_EFFORT}")
    names: dict[str, dict] = {}
    for a in agents:
        if not a["names"]:
            continue
        if a["model"] and a["model"] != AGENT_MODEL:
            flags.append(f"the agent for {', '.join(a['names'])} ran on {a['model']}, not {AGENT_MODEL}")
        for n in a["names"]:
            r = names.setdefault(n, {"searches": 0.0, "reads": 0.0, "agents": 0, "shared_with": 0})
            r["searches"] += a["searches"] / len(a["names"])      # an agent's work can't be told apart by name: share it evenly
            r["reads"] += a["reads"] / len(a["names"])
            r["agents"] += 1
            r["shared_with"] = max(r["shared_with"], len(a["names"]) - 1)
    if candidates is None:
        screen = proposals.screen_for(proposals.next_entry_session(datetime.now(NY)))
        candidates = [c["name"] for c in screen["candidates"]] if screen else []
    key = {n.lower(): n for n in names}
    for c in candidates:
        if c.lower() not in key:
            names[c] = {"searches": 0.0, "reads": 0.0, "agents": 0, "shared_with": 0}
    thin = {}
    for n, r in names.items():
        r["searches"], r["reads"] = round(r["searches"], 1), round(r["reads"], 1)
        why = []
        if not r["agents"]:
            why.append("no research agent named it")
        else:
            if r["shared_with"] >= MAX_NAMES:
                why.append(f"its agent had {r['shared_with'] + 1} names; {MAX_NAMES} at most")
            if r["reads"] < MIN_READS:
                why.append(f"{r['reads']:g} pages read, the floor is {MIN_READS}")
            if r["searches"] < MIN_SEARCHES:
                why.append(f"{r['searches']:g} searches, the floor is {MIN_SEARCHES}")
        if why:
            thin[n] = "; ".join(why)
    return {"measured": True, "session_id": session_id, "judge": judge, "agents": agents, "names": names, "thin": thin, "flags": flags,
            "floors": {"max_names_per_agent": MAX_NAMES, "min_searches": MIN_SEARCHES, "min_reads": MIN_READS}}


def report(m: dict | None = None) -> str:
    m = measure() if m is None else m
    if not m["measured"]:
        return f"Research depth: not measured ({m['why']})."
    named = [a for a in m["agents"] if a["names"]]
    lines = [f"Research depth: this pass on {m['judge']['model'] or 'an unknown model'} at {m['judge']['effort'] or 'unknown'} effort; "
             f"{len(named)} research agents, {len(m['agents']) - len(named)} other agents.",
             f"Floors a name, for looking and not for the story: its own agent, {MIN_SEARCHES} searches, {MIN_READS} pages read; "
             f"{MAX_NAMES} names to an agent at most.", ""]
    for n, r in sorted(m["names"].items()):
        lines.append(f"  {n[:28]:<28} {r['agents']} agents {r['searches']:>5g} searches {r['reads']:>5g} pages   " + (f"! {m['thin'][n]}" if n in m["thin"] else "ok"))
    lines += [""] + [f"  ! {f}" for f in m["flags"]]
    if m["thin"]:
        lines += [f"Thin: {', '.join(sorted(m['thin']))}.",
                  "Send one more agent for each of these, one name to an agent with its NAMES: line, then run this again. "
                  "After that second round, judge with what you have and say in the research note which names stayed thin."]
    elif m["names"]:
        lines.append("Every name is at or over the floors.")
    return "\n".join(lines)


def keep(session: date, m: dict | None = None) -> Path | None:
    """Keep the night's measurement beside its proposals. None when there was nothing to measure."""
    m = measure() if m is None else m
    if not m["measured"]:
        return None
    path = proposals.root() / "research" / f"{session}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = json.loads(path.read_text()) if path.exists() else {"session": session.isoformat(), "passes": []}
    doc["passes"] = [p for p in doc["passes"] if p["session_id"] != m["session_id"]] + [{"at": datetime.now(NY).isoformat(timespec="seconds"), **m}]
    path.write_text(json.dumps(doc, indent=1) + "\n")
    return path
