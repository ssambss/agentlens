"""Live view of every open Claude Code session, including the interactive VS Code and
terminal sessions that Agent View (`claude agents`) leaves out until they're backgrounded.

Which sessions are open comes from `claude agents --json`, which is documented and
lists interactive and background sessions. What each one is doing comes from the
tail of its transcript.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from agentlens import timings
from agentlens.adapters.claude_transcripts import (
    DEFAULT_ROOT,
    DELEGATING_TOOLS,
    HANDBACK_TOOLS,
    load_tail,
    worktree_name,
)
from agentlens.advise import SESSION_NAME as ADVISE_SESSION_NAME
from agentlens.curate import SESSION_NAME as CURATE_SESSION_NAME
from agentlens.model import ACTIVE_KINDS, AgentRun, Event, FileChange, Session, Span
from agentlens.terminal import claude_env
from agentlens.usage import SESSION_NAME as USAGE_SESSION_NAME

TAIL_BYTES = 4 * 1024 * 1024
# The first look at the wall's sessions: enough of each transcript for its pane's state and latest
# lines, and a fifth of the time to read. The full tail follows straight after.
QUICK_TAIL_BYTES = 1024 * 1024
SUBAGENT_RECENT = timedelta(minutes=10)  # a subagent with no activity for this long isn't running

# Live states, most urgent first.
QUESTION = "question"
APPROVAL = "approve plan"
WAITING = "waiting on you"
BACKGROUND = "waiting on agents"
WORKING = "working"
OTHER = "other"
URGENCY = {QUESTION: 0, APPROVAL: 0, WAITING: 1, BACKGROUND: 2, WORKING: 3, OTHER: 4}
STATE_STYLE = {
    QUESTION: "bold black on yellow",
    APPROVAL: "bold black on yellow",
    WAITING: "bold yellow",
    BACKGROUND: "cyan",
    WORKING: "green",
    OTHER: "dim",
}
BUSY_STATUSES = {"busy", "working", "running"}
IDLE_STATUSES = {"idle", "waiting", "needs_input", "needs-input"}


@dataclass
class LiveSubagent:
    id: str
    description: str
    last_tool: str | None
    last_active: datetime
    started: datetime | None = None
    agent_type: str = ""
    background: bool = False
    parent: str = "main"  # who launched it: "main", or another subagent's id
    running: bool = False
    finished: bool = False  # handed back its report, or its foreground call returned
    spans: list[Span] = field(default_factory=list)


@dataclass
class LiveSession:
    name: str
    session_id: str
    project: str
    title: str
    kind: str  # interactive or background, as `claude agents` reports it
    state: str
    since: datetime | None
    detail: str
    subagents: list[LiveSubagent] = field(default_factory=list)  # still running
    past_subagents: list[LiveSubagent] = field(default_factory=list)  # finished or stalled
    main_spans: list[Span] = field(default_factory=list)  # what the main thread spent its time on, for the map
    feed: list[Event] = field(default_factory=list)
    started: datetime | None = None
    worktree: str = ""  # the Claude Code worktree it works in ("ui-latency"), if any
    cwd: str = ""
    job_id: str = ""  # the short id `claude agents` lists for a background session; stop and attach take it
    changes: list[FileChange] = field(default_factory=list)  # files it changed, most recent first

    @property
    def label(self) -> str:
        """What messages call it: its title, else Claude Code's name for it."""
        return self.title or self.name


def open_sessions() -> list[dict]:
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("`claude` CLI not found on PATH")
    # Not the console's input: on Windows, a claude sharing it puts back the mode it found when it
    # exits, and the first listing can still be running when the app takes the console over.
    result = subprocess.run(
        [claude, "agents", "--json"], capture_output=True, text=True, encoding="utf-8", timeout=30,
        stdin=subprocess.DEVNULL, env=claude_env()
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"`claude agents --json` exited with {result.returncode}")
    data = json.loads(result.stdout or "[]")
    # agentlens's own advice, curator and usage runs are claude processes too, but not the user's sessions.
    own = {ADVISE_SESSION_NAME, CURATE_SESSION_NAME, USAGE_SESSION_NAME}
    return [e for e in data if isinstance(e, dict) and e.get("sessionId") and e.get("name") not in own]


class EarlyListing:
    """`open_sessions`, with the first listing already under way while the app loads. Starting
    `claude` can take seconds where security software checks every program that starts, and
    until the listing is in, the wall has nothing to show."""

    def __init__(self) -> None:
        self._first: list[dict] | Exception = []
        self._thread: threading.Thread | None = threading.Thread(target=self._list_first, daemon=True)
        self._thread.start()

    def _list_first(self) -> None:
        started = time.perf_counter()
        try:
            self._first = open_sessions()
        except Exception as e:  # noqa: BLE001 - raised where the listing is used, as open_sessions would
            self._first = e
        timings.mark(f"`claude agents --json` answered, after {time.perf_counter() - started:.2f}s")

    def __call__(self) -> list[dict]:
        thread, self._thread = self._thread, None
        if thread is None:
            return open_sessions()
        thread.join()
        if isinstance(self._first, Exception):
            raise self._first
        return self._first


Tail = tuple[Session | None, dict[str, list[Event]]]


class TranscriptTails:
    """Session tails and activity feeds keyed by transcript; re-read only when a file changes."""

    def __init__(self, root: Path = DEFAULT_ROOT, tail_bytes: int = TAIL_BYTES):
        self.root = root
        self.tail_bytes = tail_bytes
        self._cache: dict[Path, tuple[tuple, Tail]] = {}
        # A working session's transcript changes on every refresh; its subagents, mostly, don't.
        self._subagents: dict[Path, tuple] = {}

    def get(self, session_id: str) -> Tail:
        path = next(iter(self.root.glob(f"*/{session_id}.jsonl")), None)
        if path is None:
            return None, {}
        subagent_files = sorted((path.parent / session_id / "subagents").glob("*.jsonl"))
        key = (_stat(path), tuple(_stat(p) for p in subagent_files))
        cached = self._cache.get(path)
        if cached and cached[0] == key:
            return cached[1]
        tail = load_tail(path, self.tail_bytes, subagent_cache=self._subagents)
        self._cache[path] = (key, tail)
        return tail


def derive(
    entry: dict,
    session: Session | None,
    events: dict[str, list[Event]] | None = None,
    *,
    now: datetime,
) -> LiveSession:
    status = str(entry.get("status", "")).lower()
    events = events or {}
    started = entry.get("startedAt")
    live = LiveSession(
        name=entry.get("name") or entry["sessionId"][:8],
        session_id=entry["sessionId"],
        project=session.project if session else Path(str(entry.get("cwd", "")).replace("\\", "/")).name,
        title=session.title if session and session.title != "(untitled)" else "",
        kind=entry.get("kind", ""),
        state=OTHER,
        since=None,
        detail=status,
        feed=events.get("main", []),
        started=datetime.fromtimestamp(started / 1000, timezone.utc) if isinstance(started, (int, float)) else None,
        cwd=str(entry.get("cwd", "")),
        job_id=str(entry.get("id") or ""),
        worktree=session.worktree if session else worktree_name(str(entry.get("cwd", ""))),
    )
    if session is None:
        live.since = live.started
        if status in IDLE_STATUSES:
            live.state, live.detail = WAITING, "new session"
        elif status in BUSY_STATUSES:
            live.state, live.detail = WORKING, "starting"
        return live

    main = session.main
    live.subagents, live.past_subagents = subagent_lists(session, now)
    live.main_spans = main.spans
    live.changes = file_changes(session)
    turn_start = max(session.human_prompts) if session.human_prompts else None
    pending = [(name, t) for name, t in main.pending_tools if turn_start is None or t >= turn_start]
    pending_names = [name for name, _ in pending]

    # A question or plan blocks the turn whatever the reported status is.
    if "AskUserQuestion" in pending_names:
        live.state, live.detail = QUESTION, "asked you a question"
        live.since = _pending_since(pending, "AskUserQuestion")
    elif "ExitPlanMode" in pending_names:
        live.state, live.detail = APPROVAL, "plan waiting for your approval"
        live.since = _pending_since(pending, "ExitPlanMode")
    elif status in BUSY_STATUSES:
        live.state = WORKING
        live.since = _streak_start(main)
        if pending:
            name, called = pending[-1]
            live.detail = (
                "blocked on a subagent" if name in DELEGATING_TOOLS else f"running {name} {ago(now - called)}"
            )
        else:
            live.detail = "thinking"
    elif status in IDLE_STATUSES:
        live.since = main.end
        if live.subagents:
            live.state = BACKGROUND
            live.detail = f"{len(live.subagents)} subagent{'s' if len(live.subagents) != 1 else ''} still working"
        else:
            live.state, live.detail = WAITING, "turn finished"
    return live


def subagent_lists(session: Session | None, now: datetime) -> tuple[list[LiveSubagent], list[LiveSubagent]]:
    """The subagents still running, and the finished or stalled ones; newest first."""
    if session is None:
        return [], []
    launcher = {call: run for run in [session.main, *session.subagents] for call in run.agent_calls}
    running: list[LiveSubagent] = []
    past: list[LiveSubagent] = []
    for run in sorted(session.subagents, key=lambda r: r.end, reverse=True):
        # The launching call is missing when it's older than the tail read; then main launched it.
        parent = launcher.get(run.launched_by or "")
        returned = parent is not None and parent.agent_calls[run.launched_by or ""]
        sub = LiveSubagent(
            run.id, run.description or run.id, run.last_tool, run.end, started=run.start, agent_type=run.agent_type,
            background=run.background, parent="main" if parent is None or parent is session.main else parent.id,
            finished=_handed_back(run) or (not run.background and returned), spans=run.spans,
        )
        sub.running = not sub.finished and now - run.end < SUBAGENT_RECENT
        (running if sub.running else past).append(sub)
    return running, past


def file_changes(session: Session | None) -> list[FileChange]:
    """The files a session changed, its own edits and its subagents' together, most recent first.
    Live views read only the end of a transcript, so a long session's early edits can be missing."""
    merged: dict[str, FileChange] = {}
    for run in [session.main, *session.subagents] if session else []:
        for key, change in run.files_changed.items():
            seen = merged.get(key)
            if seen is None or change.at >= seen.at:
                merged[key] = replace(change, edits=change.edits + (seen.edits if seen else 0))
            else:
                seen.edits += change.edits
    return sorted(merged.values(), key=lambda c: c.at, reverse=True)


def snapshot(tails: TranscriptTails, now: datetime | None = None, project: str | None = None) -> list[LiveSession]:
    now = now or datetime.now(timezone.utc)
    sessions = [derive(e, *tails.get(e["sessionId"]), now=now) for e in open_sessions()]
    if project:
        sessions = [s for s in sessions if project.lower() in s.project.lower()]
    return sort_sessions(sessions, now)


def sort_sessions(sessions: list[LiveSession], now: datetime) -> list[LiveSession]:
    # Most urgent state first; within a state, whoever has waited longest.
    return sorted(sessions, key=lambda s: (URGENCY[s.state], s.since or now))


def render(sessions: list[LiveSession], now: datetime) -> Group:
    table = Table(header_style="bold", expand=True, show_edge=False, pad_edge=False)
    table.add_column("state", no_wrap=True)
    table.add_column("for", justify="right", no_wrap=True)
    table.add_column("session", no_wrap=True)
    table.add_column("project", no_wrap=True)
    table.add_column("title", ratio=1, overflow="ellipsis", no_wrap=True)
    table.add_column("now", no_wrap=True)
    for s in sessions:
        table.add_row(
            Text(f" {s.state} ", style=STATE_STYLE[s.state]),
            ago(now - s.since) if s.since else "",
            s.name + (" [bg]" if s.kind == "background" else ""),
            s.project,
            s.title,
            s.detail,
        )
        for sub in s.subagents:
            last = f"last {sub.last_tool} " if sub.last_tool else "active "
            table.add_row("", "", Text(f"  └ {sub.description}", style="dim"), "", "",
                          Text(f"{last}{ago(now - sub.last_active)} ago", style="dim"))
    counts = {state: sum(1 for s in sessions if s.state == state) for state in URGENCY}
    needs_you = counts[QUESTION] + counts[APPROVAL] + counts[WAITING]
    footer = (
        f"{needs_you} need you · {counts[WORKING]} working · {counts[BACKGROUND]} waiting on agents"
        f"   {now.astimezone():%H:%M:%S}"
    )
    if not sessions:
        return Group(Text("No open Claude Code sessions.", style="dim"), Text(footer, style="dim"))
    return Group(table, Text(footer, style="dim"))


def pane_title(s: LiveSession, space: str) -> str:
    """What a pane leads with: Claude Code's title for the session, else its first prompt. A
    leading project name is dropped, since the pane shows its space: in the agentlens space,
    "Agentlens usage visualization" reads "Usage visualization"."""
    title = s.title or next((e.text for e in s.feed if e.kind == "you"), "")
    return _without_prefix(title, space or s.project) or "(untitled)"


def pane_place(s: LiveSession, space: str) -> str:
    """Where a session works, for its pane's border: its worktree, if it has one, and its space."""
    return " · ".join(dict.fromkeys(p for p in (s.worktree, space or s.project) if p))


def _without_prefix(title: str, name: str) -> str:
    words, target = title.split(), _letters(name)
    for n in range(1, min(len(words), 4)):  # "Agentlens", "Agent lens", "Recipe box"; never every word
        prefix = _letters(" ".join(words[:n]))
        if not prefix or not target.startswith(prefix):
            break
        if prefix == target:
            rest = " ".join(words[n:]).lstrip("-–—:·|/ ")
            if rest.split(" ")[0].islower():  # "usage" becomes "Usage"; "iOS" stays as it is
                rest = rest[:1].upper() + rest[1:]
            return rest or title
    return title


def _letters(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


def print_snapshot(console: Console, project: str | None = None, root: Path = DEFAULT_ROOT) -> None:
    now = datetime.now(timezone.utc)
    console.print(render(snapshot(TranscriptTails(root), now, project), now))


def _handed_back(run: AgentRun) -> bool:
    return run.last_tool in HANDBACK_TOOLS and not run.pending_tools


def _streak_start(run: AgentRun) -> datetime:
    """When the current stretch of uninterrupted agent work began."""
    start = run.end
    for span in reversed(run.spans):
        if span.kind not in ACTIVE_KINDS:
            break
        start = span.start
    return start


def _pending_since(pending: list[tuple[str, datetime]], name: str) -> datetime:
    return next(t for n, t in pending if n == name)


def _stat(path: Path) -> tuple:
    try:
        st = path.stat()
    except OSError:
        return (str(path), None, None)
    return (str(path), st.st_size, st.st_mtime_ns)


def ago(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
