"""Tool-agnostic data model. Adapters produce these; everything else consumes them.

Keeping this layer independent of any one agent tool's file format is what lets a
new data source (hook event log, OpenTelemetry, another harness) slot in without
touching metrics, rules or reports.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

# What a stretch of wall-clock time was spent on. Each span is classified by the
# record that ends it: a human prompt ends a wait on you, a tool result ends a tool
# run, and so on.
MODEL = "model"  # model thinking / generating
TOOL = "tool"  # a tool call executing
DELEGATED = "delegated"  # main thread blocked on a foreground subagent
WAITING_ON_YOU = "waiting_on_you"  # turn ended, question asked, or permission prompt open
WAITING_ON_BACKGROUND = "waiting_on_background"  # turn ended, waiting on a background task or agent
DORMANT = "dormant"  # subagent idle until its parent messaged it again
AWAY = "away"  # waiting on you or on background work for longer than the away threshold, or closed

ACTIVE_KINDS = (MODEL, TOOL, DELEGATED)


@dataclass
class Span:
    kind: str
    start: datetime
    end: datetime

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


@dataclass
class Event:
    """One line of a session's activity feed, for the live views."""

    at: datetime
    kind: str  # you | claude | tool | error | notice | compact
    text: str  # one line, for panes
    detail: str = ""  # fuller text, for the zoomed view
    tag: str = ""  # e.g. "queued": a message you sent while Claude was working


@dataclass
class FileChange:
    """A file an agent changed with its edit tools. Changes made through shell commands
    (sed, a script, git checkout) don't show up here."""

    path: str  # as the tool was given it
    at: datetime  # the latest change
    line: int | None = None  # where the latest change starts, when the tool reported it
    edits: int = 1


@dataclass
class AgentRun:
    """One thread of agent work: a session's main thread, or one subagent."""

    id: str
    agent_type: str
    description: str
    background: bool
    start: datetime
    end: datetime
    spans: list[Span] = field(default_factory=list)
    tool_calls: Counter = field(default_factory=Counter)
    files_read: set[str] = field(default_factory=set)
    files_changed: dict[str, FileChange] = field(default_factory=dict)  # by normalized path
    output_tokens: int = 0
    models: set[str] = field(default_factory=set)
    final_chars: int = 0  # size of what it reported back: all hand-backs, else its last text
    launched_by: str | None = None  # tool_use id of the call that spawned this run
    agent_calls: dict[str, bool] = field(default_factory=dict)  # subagents it launched: tool_use id -> call returned
    last_tool: str | None = None
    pending_tools: list[tuple[str, datetime]] = field(default_factory=list)  # (tool, called at), no result yet

    def minutes(self, *kinds: str) -> float:
        return sum(s.minutes for s in self.spans if s.kind in kinds)


@dataclass
class Session:
    id: str
    project: str
    title: str
    entrypoint: str
    version: str
    main: AgentRun
    subagents: list[AgentRun] = field(default_factory=list)
    human_prompts: list[datetime] = field(default_factory=list)
    skills: Counter = field(default_factory=Counter)
    compactions: int = 0
    cwd: str = ""
    worktree: str = ""  # the Claude Code worktree it works in now ("ui-latency"), if any
    compaction_pre_tokens: list[int] = field(default_factory=list)  # context size when each one fired
    manual_compactions: int = 0  # of `compactions`, the ones you ran with /compact
    permission_modes: Counter = field(default_factory=Counter)  # your prompts, by the permission mode they went in
    commands: Counter = field(default_factory=Counter)  # slash commands you typed: "/model"

    @property
    def start(self) -> datetime:
        return self.main.start

    @property
    def end(self) -> datetime:
        return max([self.main.end, *(s.end for s in self.subagents)])
