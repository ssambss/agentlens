"""Which Claude Code features someone's sessions use, so advice can suggest ones they haven't tried.

Read from the sessions themselves: tool calls, the permission mode prompts went in, the slash
commands typed, the kinds of subagents. What a session can't show (CLAUDE.md, hooks, custom
subagents) comes from the setup files instead, in work.py.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from panopsis.model import AgentRun, Session

FORK = "fork"  # the agent type of a forked subagent, as the subagent-handoff-overlap rule assumes too
MODEL_DATE = re.compile(r"-\d{8}$")  # claude-haiku-4-5-20251001 -> claude-haiku-4-5


def tools(session: Session) -> Counter:
    """Tool calls across the main thread and every subagent."""
    total = Counter(session.main.tool_calls)
    for run in session.subagents:
        total.update(run.tool_calls)
    return total


# Each feature, and how a session shows it was used.
FEATURES: list[tuple[str, Callable[[Session], bool]]] = [
    ("plan mode", lambda s: s.permission_modes["plan"] > 0 or tools(s)["ExitPlanMode"] > 0),
    ("subagents", lambda s: any(run.agent_type != FORK for run in s.subagents)),
    ("forked subagents", lambda s: any(run.agent_type == FORK for run in s.subagents)),
    ("background subagents", lambda s: any(run.background for run in s.subagents)),
    ("worktrees", lambda s: bool(s.worktree) or tools(s)["EnterWorktree"] > 0),
    ("skills", lambda s: bool(s.skills)),
    ("MCP tools", lambda s: any(name.startswith("mcp__") for name in tools(s))),
    ("web search or fetch", lambda s: tools(s)["WebSearch"] + tools(s)["WebFetch"] > 0),
    ("/compact when you choose", lambda s: s.manual_compactions > 0 or s.commands["/compact"] > 0),
    ("/context", lambda s: s.commands["/context"] > 0),
]


@dataclass
class Usage:
    sessions: int
    features: dict[str, int]  # feature -> sessions that used it
    permission_modes: Counter = field(default_factory=Counter)  # your prompts, by permission mode
    commands: Counter = field(default_factory=Counter)  # slash commands typed
    agent_types: Counter = field(default_factory=Counter)  # subagents, by type
    agent_models: dict[str, Counter] = field(default_factory=dict)  # subagent type -> runs by model
    main_models: Counter = field(default_factory=Counter)  # sessions by the main conversation's model
    mcp_servers: Counter = field(default_factory=Counter)  # MCP tool calls, by server

    def never_used(self) -> list[str]:
        return [name for name, used in self.features.items() if not used]


def build(sessions: list[Session]) -> Usage:
    usage = Usage(len(sessions), {name: sum(1 for s in sessions if used(s)) for name, used in FEATURES})
    for s in sessions:
        usage.permission_modes.update(s.permission_modes)
        usage.commands.update(s.commands)
        usage.agent_types.update(run.agent_type for run in s.subagents)
        usage.main_models.update(_models(s.main))
        for run in s.subagents:
            usage.agent_models.setdefault(run.agent_type, Counter()).update(_models(run))
        usage.mcp_servers.update({name.split("__")[1]: n for name, n in tools(s).items()
                                  if name.startswith("mcp__") and name.count("__") >= 2})
    return usage


def describe(usage: Usage) -> list[str]:
    """Lines for the advice prompt."""
    lines = [f"- {name}: " + (f"{used} of {usage.sessions} sessions" if used else "never")
             for name, used in usage.features.items()]
    for label, counts in (("prompts by permission mode", usage.permission_modes),
                          ("slash commands typed", usage.commands),
                          ("main conversation's model, by sessions", usage.main_models)):
        if counts:
            lines.append(f"- {label}: " + ", ".join(f"{name} {n}" for name, n in counts.most_common(8)))
    if usage.agent_types:
        # The model each type ran on, so advice reads it rather than guessing from the type's name.
        lines.append("- subagents by type, with the model they ran on: " + ", ".join(
            f"{name} {n} ({_ran_on(usage.agent_models.get(name, Counter()))})"
            for name, n in usage.agent_types.most_common(8)))
    if usage.mcp_servers:
        lines.append("- MCP servers called: " + ", ".join(f"{name} {n}" for name, n in usage.mcp_servers.most_common(8)))
    return lines


def _models(run: AgentRun) -> set[str]:
    return {MODEL_DATE.sub("", m) for m in run.models}  # the adapter already leaves out "<synthetic>"


def _ran_on(models: Counter) -> str:
    if not models:
        return "model not recorded"
    (top, n), total = models.most_common(1)[0], sum(models.values())
    return top if n == total else f"mostly {top}, {n} of {total}"
