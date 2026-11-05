"""The zoom view's subagent map: which subagents a session ran, who launched which, and
which ran at the same time. Deliberately not what they did; that's in their transcripts.

Subagents whose lifetimes overlap form a wave. Each wave gets its own time axis, so a
session with hours between waves still shows each wave's shape: a fan-out launched at
once, one agent after another, the main thread working alongside them or waiting.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from rich.console import Console, ConsoleOptions, RenderResult
from rich.text import Text

from agentlens.feed import Palette
from agentlens.live import LiveSubagent, ago
from agentlens.model import MODEL, TOOL, Span

WORKING_KINDS = (MODEL, TOOL)  # waiting on its own subagents, or to be messaged again, is idle here
DEFAULT_TYPES = {"general-purpose", "unknown"}  # not worth a label on every row
DURATION_WIDTH = 8
RUNNING_STYLE = "green"
QUIET_STYLE = "yellow"


class SubagentMap:
    """A Rich renderable: newest wave first, each a small tree of agents with a timeline bar."""

    def __init__(self, agents: list[LiveSubagent], main_spans: list[Span], now: datetime, palette: Palette) -> None:
        self.agents = agents
        self.main_spans = main_spans
        self.now = now
        self.palette = palette

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        if not self.agents:
            yield Text("No subagents in this session.", style="dim")
            return
        width = options.max_width
        groups = waves(self.agents, self.now)
        head = Text("Subagents", style="bold")
        counts = [(sum(1 for a in self.agents if _status(a) == s), s) for s in ("running", "finished", "quiet")]
        head.append("  " + " · ".join(f"{n} {s}" for n, s in counts if n), style="dim")
        if len(groups) > 1:
            head.append(f" · {len(groups)} waves", style="dim")
        yield head
        for wave in reversed(groups):
            yield Text("")
            yield from self._wave(wave, width)
        yield Text("")
        yield Text("● running  ✓ finished  ○ quiet 10m+", style="dim", no_wrap=True, overflow="ellipsis")
        lanes = "━ working  ─ idle"
        if any(_known_from(a) > _start(a) for a in self.agents):
            lanes += "  ┄ not read (long transcript)"
        yield Text(lanes, style="dim", no_wrap=True, overflow="ellipsis")

    def _wave(self, wave: list[LiveSubagent], width: int) -> RenderResult:
        t0 = min(_start(a) for a in wave)
        t1 = max(_end(a, self.now) for a in wave)
        bar_width = max(10, min(30, width // 4))  # names matter more than resolution
        label_width = max(10, width - bar_width - DURATION_WIDTH - 1)
        yield Text(_wave_title(wave, t0, t1, self.now), style="bold dim", no_wrap=True, overflow="ellipsis")

        # The main thread's lane, when the transcript tail reaches back this far.
        if self.main_spans and self.main_spans[0].start <= t1:
            alive = (max(self.main_spans[0].start, t0), t1)  # an open session's main thread is there until now
            working = [(s.start, s.end) for s in self.main_spans if s.kind in WORKING_KINDS]
            row = Text("main thread".ljust(label_width + 1), style="dim", no_wrap=True, overflow="crop")
            row.append(_bar(bar_width, t0, t1, alive, working), style=self.palette.tool)
            yield row

        for depth, agent in tree_order(wave):
            style = _style(agent, self.palette)
            label = Text("  " * depth + ("└ " if depth else ""), style="dim", no_wrap=True, overflow="crop")
            label.append(f"{_GLYPH[_status(agent)]} ", style=style)
            label.append(agent.description)
            kind = " · ".join(k for k in ("" if agent.agent_type in DEFAULT_TYPES else agent.agent_type,
                                          "" if agent.background else "blocking") if k)
            if kind and label.cell_len + len(kind) + 3 <= label_width:
                label.append(f" · {kind}", style="dim")
            label.truncate(label_width, overflow="ellipsis", pad=True)
            row = label + Text(" ")
            end = _end(agent, self.now)
            working = [(s.start, s.end) for s in agent.spans if s.kind in WORKING_KINDS]
            if agent.running:
                working.append((agent.last_active, self.now))  # busy right now, in a tool or the model
            row.append(_bar(bar_width, t0, t1, (_start(agent), end), working, _known_from(agent)), style=style)
            row.append(ago(end - _start(agent)).rjust(DURATION_WIDTH), style="dim")
            yield row


def waves(agents: list[LiveSubagent], now: datetime) -> list[list[LiveSubagent]]:
    """Subagents grouped by overlapping lifetimes, oldest wave first, each in start order."""
    groups: list[list[LiveSubagent]] = []
    group_end: datetime | None = None
    for agent in sorted(agents, key=_start):
        if groups and group_end is not None and _start(agent) <= group_end:
            groups[-1].append(agent)
            group_end = max(group_end, _end(agent, now))
        else:
            groups.append([agent])
            group_end = _end(agent, now)
    return groups


def tree_order(wave: list[LiveSubagent]) -> list[tuple[int, LiveSubagent]]:
    """The wave as (depth, agent) rows: agents the main thread launched, each followed by its own."""
    ids = {a.id for a in wave}
    children: dict[str, list[LiveSubagent]] = defaultdict(list)
    for agent in wave:
        children[agent.parent if agent.parent in ids else "main"].append(agent)
    rows: list[tuple[int, LiveSubagent]] = []
    seen: set[str] = set()

    def walk(key: str, depth: int) -> None:
        for agent in children.get(key, []):
            if agent.id not in seen:
                seen.add(agent.id)
                rows.append((depth, agent))
                walk(agent.id, depth + 1)

    walk("main", 0)
    return rows


def _bar(width: int, t0: datetime, t1: datetime, alive: tuple[datetime, datetime],
         working: list[tuple[datetime, datetime]], known_from: datetime | None = None) -> str:
    """One lane: ━ where it was mostly working, ─ where it existed but waited, blank elsewhere,
    and ┄ before `known_from`, the part of a long transcript the live view didn't read."""
    total = max((t1 - t0).total_seconds(), 1e-6)
    step = total / width

    def offsets(a: datetime, b: datetime) -> tuple[float, float]:
        return max(0.0, (a - t0).total_seconds()), min(total, (b - t0).total_seconds())

    work = [0.0] * width
    for a, b in working:
        lo, hi = offsets(a, b)
        if hi <= lo:
            continue
        for i in range(int(lo / step), min(width - 1, int(hi / step)) + 1):
            work[i] += max(0.0, min(hi, (i + 1) * step) - max(lo, i * step))
    lo, hi = offsets(*alive)
    known = offsets(known_from, known_from)[0] if known_from else lo
    cells = []
    for i in range(width):
        present = min(hi, (i + 1) * step) - max(lo, i * step)
        unread = min(known, (i + 1) * step) - max(lo, i * step)
        if present <= 0:
            cells.append(" ")
        elif unread >= present / 2:
            cells.append("┄")
        else:
            cells.append("━" if work[i] >= present / 2 else "─")
    if lo <= hi and all(c == " " for c in cells):  # too short to fill any cell: still show where it was
        cells[min(width - 1, int(lo / step))] = "━"
    return "".join(cells)


def _wave_title(wave: list[LiveSubagent], t0: datetime, t1: datetime, now: datetime) -> str:
    local, today = t0.astimezone(), now.astimezone()
    when = f"{local:%H:%M}" if local.date() == today.date() else f"{local:%b %d %H:%M}"
    top = [a for a in wave if a.parent not in {b.id for b in wave}]
    peak = _peak_overlap(top, now)
    if len(top) == 1:
        shape = "1 subagent"
    elif peak == len(top):
        shape = f"{len(top)} in parallel"
    else:
        shape = f"{len(top)} subagents, up to {peak} in parallel"
    nested = len(wave) - len(top)
    if nested:
        shape += f", {nested} nested"
    so_far = " so far" if any(a.running for a in wave) else ""
    return f"{when} · {shape} · {ago(t1 - t0)}{so_far}"


def _peak_overlap(agents: list[LiveSubagent], now: datetime) -> int:
    edges = sorted([(_start(a), 1) for a in agents] + [(_end(a, now), -1) for a in agents],
                   key=lambda e: (e[0], -e[1]))  # one starting as another ends counts as overlapping
    peak = current = 0
    for _, delta in edges:
        current += delta
        peak = max(peak, current)
    return peak


_GLYPH = {"running": "●", "finished": "✓", "quiet": "○"}


def _status(agent: LiveSubagent) -> str:
    return "running" if agent.running else "finished" if agent.finished else "quiet"


def _style(agent: LiveSubagent, palette: Palette) -> str:
    return {"running": RUNNING_STYLE, "finished": palette.claude, "quiet": QUIET_STYLE}[_status(agent)]


def _known_from(agent: LiveSubagent) -> datetime:
    return agent.spans[0].start if agent.spans else _start(agent)


def _start(agent: LiveSubagent) -> datetime:
    return agent.started or agent.last_active


def _end(agent: LiveSubagent, now: datetime) -> datetime:
    return now if agent.running else agent.last_active
