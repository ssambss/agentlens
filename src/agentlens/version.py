"""Which agentlens this is, from how many commits it has: 0.1.76 is the 76th, so a copy that's fallen
behind has a lower number than the latest, with nothing to bump by hand. The last part goes up to 99
and the middle one to 9: the 100th commit is 0.2.0, the 900th 1.0.0 rather than 0.10.0.

Run from a checkout, git counts them. A copy installed from one (uv tool install .) has no git
beside it: the build wrote its version into agentlens/_build.py (hatch_build.py), and into the
package's own, which `uv tool list` shows.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

START = 100  # 0.1.0: the count starts at 0.1
FALLBACK = "0.1"  # when nothing says how many commits there are; hatch_build.py reads it from here


def from_count(count: int) -> str:
    """The version of the `count`th commit: 0.1.99, then 0.2.0; 0.9.99, then 1.0.0; 1.9.99, then 2.0.0."""
    total = START + count
    return f"{total // 1000}.{total // 100 % 10}.{total % 100}"


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
    return from_count(int(count)) if found.returncode == 0 and count.isdigit() else None


@lru_cache(maxsize=1)
def number() -> str:
    """This agentlens's version: 0.1.76. Just FALLBACK if nothing says how many commits it has."""
    found = from_git(Path(__file__).resolve().parents[2])
    if found:
        return found
    try:
        from agentlens import _build  # type: ignore[attr-defined]
    except ImportError:
        return FALLBACK
    return str(getattr(_build, "VERSION", "") or FALLBACK)


def describe() -> str:
    """What --version says: `agentlens 0.1.76`."""
    return f"agentlens {number()}"
