"""Which agentlens this is: its version, and the commit it was made from with that commit's date, so a
copy that's months old says so. The version number alone doesn't change from one commit to the next.

Run from a checkout, git says which commit. A copy installed from one (uv tool install .) has no git
beside it: the build wrote the commit into agentlens/_build.py (hatch_build.py).
"""

from __future__ import annotations

import subprocess
from datetime import date
from functools import lru_cache
from pathlib import Path

from agentlens import __version__


@lru_cache(maxsize=1)
def commit() -> tuple[str, str] | None:
    """The commit this code is from, short, and its date (YYYY-MM-DD); None if that isn't known."""
    root = Path(__file__).resolve().parents[2]
    if (root / ".git").exists():  # a checkout, or a worktree of one, where .git is a file
        try:
            # Without a console: in Windows, git would otherwise share agentlens's, and could change its modes.
            found = subprocess.run(["git", "log", "-1", "--format=%h %cs"], cwd=root, capture_output=True,
                                   text=True, timeout=5, stdin=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            parts = found.stdout.split()
            if found.returncode == 0 and len(parts) == 2:
                return parts[0], parts[1]
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        from agentlens import _build  # type: ignore[attr-defined]
    except ImportError:
        return None
    return str(getattr(_build, "COMMIT", "")), str(getattr(_build, "DATE", ""))


def age(when: str, today: date | None = None) -> str:
    """How long ago a date (YYYY-MM-DD) was, roughly: today, yesterday, 3 days, 2 weeks, 4 months ago."""
    try:
        days = ((today or date.today()) - date.fromisoformat(when)).days
    except ValueError:
        return ""
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    for size, unit in ((365, "year"), (30, "month"), (7, "week"), (1, "day")):
        if days >= size:
            count = days // size
            return f"{count} {unit}{'s' * (count > 1)} ago"
    return ""


def about(today: date | None = None) -> str:
    """`0.1.0 · 40c5fc9 from 2026-10-20, 3 weeks ago`, or less, as far as it's known: what the app's
    title bar says after its name."""
    text = __version__
    found = commit()
    if found and found[0]:
        text += f" · {found[0]}"
        if found[1]:
            when = age(found[1], today)
            text += f" from {found[1]}" + (f", {when}" if when else "")
    return text


def describe(today: date | None = None) -> str:
    """`agentlens 0.1.0 · 40c5fc9 from 2026-10-20, 3 weeks ago`: what --version says."""
    return f"agentlens {about(today)}"
