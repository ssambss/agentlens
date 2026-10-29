"""What panopsis is doing in the background, for the line at the top of the wall.

Each job says what it's doing and how far it got: so many of so many, or for a paced one that
can't count (asking Claude for your plan usage), against how long it took last time. Those times
are kept in ~/.panopsis/durations.json, and only fill the bar: no time is ever shown.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

from rich.text import Text

from panopsis.preferences import home

SHOW_AFTER = 0.5  # seconds; a quicker job never shows, so the line doesn't flicker on every refresh
BAR_CELLS = 10


def durations_path() -> Path:
    return home() / "durations.json"


@dataclass
class Job:
    what: str  # "reading your history"
    started: float = field(default_factory=time.monotonic)
    done: int = 0
    total: int = 0  # 0: it can't count
    steps: list[str] = field(default_factory=list)  # what it has been on: "reading code.claude.com/docs/en/hooks"
    note: str = ""  # why it takes longer this time
    usual: float | None = None  # seconds a paced job took last time
    paced: bool = False

    @property
    def step(self) -> str:
        return self.steps[-1] if self.steps else ""

    def fraction(self, now: float) -> float | None:
        """How far along it is, 0-1, or None where there's nothing to tell it by."""
        if self.total:
            return min(1.0, self.done / self.total)
        if self.usual:
            # Two thirds of the way by last time's, and slowing after, so it never looks
            # done, or stuck, while it's still going.
            return 1 - math.exp(-(now - self.started) / self.usual)
        return None


class Jobs:
    """The jobs under way, by key. Only the UI thread changes them: workers go through call_from_thread."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or durations_path()
        self.running: dict[str, Job] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._usual = {str(k): float(v) for k, v in data.items()}
        except (OSError, ValueError, TypeError, AttributeError):
            self._usual = {}

    def start(self, key: str, what: str, total: int = 0, paced: bool = False) -> Job:
        """A paced job's bar fills against how long it took last time."""
        job = self.running[key] = Job(what, total=total, usual=self._usual.get(key) if paced else None, paced=paced)
        return job

    def get(self, key: str) -> Job | None:
        return self.running.get(key)

    def finish(self, key: str) -> None:
        job = self.running.pop(key, None)
        if job is None or not job.paced:
            return
        self._usual[key] = round(time.monotonic() - job.started, 2)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._usual, indent=1), encoding="utf-8")
        except OSError:
            pass  # only fills the next run's bar

    def shown(self, now: float | None = None) -> list[Job]:
        now = time.monotonic() if now is None else now
        return [job for job in self.running.values() if now - job.started >= SHOW_AFTER]


def bar(fraction: float, cells: int = BAR_CELLS) -> str:
    filled = min(cells, int(fraction * cells + 0.5))
    return "█" * filled + "░" * (cells - filled)


def line(jobs: list[Job], spinner: str, now: float, color: str) -> Text:
    """`✻ reading your history ███░░░░░░░ 312 of 1072  +1`: the longest-running job, and how many more."""
    if not jobs:
        return Text()
    job = jobs[0]
    what = f"{job.what}: {job.step}" if job.step else job.what
    text = Text.assemble((spinner + " ", f"bold {color}"), what, no_wrap=True, overflow="ellipsis")
    fraction = job.fraction(now)
    if fraction is not None:
        text.append(" " + bar(fraction), style=color)
    if job.total:
        text.append(f" {job.done} of {job.total}", style="dim")
    if len(jobs) > 1:
        text.append(f"  +{len(jobs) - 1}", style="dim")
    return text
