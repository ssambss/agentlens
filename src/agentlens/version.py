"""Which agentlens this is: 0.1.76 is the 76th commit. The last part counts the commits, so a copy
that's fallen behind has a lower number than the latest, with nothing to bump by hand. BASE, the
first two parts, changes by hand, for a change big enough to say so.

Run from a checkout, git counts them. A copy installed from one (uv tool install .) has no git
beside it: the build wrote its version into agentlens/_build.py (hatch_build.py), and into the
package's own, which `uv tool list` shows.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

BASE = "0.1"  # hatch_build.py reads it from here


def from_git(root: Path) -> str | None:
    """The version of the checkout at `root`, from how many commits it has; None without git there."""
    if not (root / ".git").exists():  # a checkout, or a worktree of one, where .git is a file
        return None
    try:
        # Without a console: in Windows, git would otherwise share agentlens's, and could change its modes.
        found = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=root, capture_output=True, text=True,
                               timeout=5, stdin=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    count = found.stdout.strip()
    return f"{BASE}.{count}" if found.returncode == 0 and count.isdigit() else None


@lru_cache(maxsize=1)
def number() -> str:
    """This agentlens's version: 0.1.76. Just BASE if nothing says how many commits it has."""
    found = from_git(Path(__file__).resolve().parents[2])
    if found:
        return found
    try:
        from agentlens import _build  # type: ignore[attr-defined]
    except ImportError:
        return BASE
    return str(getattr(_build, "VERSION", "") or BASE)


def describe() -> str:
    """What --version says: `agentlens 0.1.76`."""
    return f"agentlens {number()}"
