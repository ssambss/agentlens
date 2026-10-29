"""Where startup time goes, for a machine where panopsis is slow to show your sessions.

Start it with PANOPSIS_TIMINGS=1 set, and when you quit, it prints when each step finished.
"""

from __future__ import annotations

import os
import time

import panopsis

ENABLED = bool(os.environ.get("PANOPSIS_TIMINGS"))
_marks: list[tuple[float, str]] = []


def mark(what: str, at: float | None = None) -> None:
    """A step finished: now, or at a time.perf_counter() gave. Safe from any thread."""
    if ENABLED:
        _marks.append(((time.perf_counter() if at is None else at) - panopsis.STARTED, what))


def report() -> str:
    lines = ["panopsis startup, in seconds since its code began loading:"]
    lines += [f"{seconds:7.2f}  {what}" for seconds, what in sorted(_marks)]
    return "\n".join(lines)
