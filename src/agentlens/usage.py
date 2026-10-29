"""Your plan's usage limits: how much of the 5-hour session and the weekly limits is used.

Both sources are your own Claude Code; agentlens never reads or passes credentials:
- `claude -p /usage`, now and then. Claude Code asks Anthropic for your plan usage and
  prints it: no model call, no tokens, no transcript (--no-session-persistence), no hooks.
- The rate-limit events sessions agentlens runs receive after each model call, which keep
  the 5-hour window current between checks.

What /usage prints is Claude Code's own wording and can change. Lines that aren't a usage
window are kept as text and shown as they are, never treated as an error.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.text import Text

from agentlens.preferences import home
from agentlens.terminal import claude_env

WARN_AT = 0.75  # the meter turns to the warning colour here, as Claude Code's own warning does
FULL_AT = 0.9
# The probe's display name in `claude agents`, so the app can leave it off the grid.
SESSION_NAME = "agentlens usage"

_WINDOW = re.compile(
    r"^\s*Current (?P<kind>session|week)(?:\s*\((?P<scope>[^)]*)\))?\s*:\s*(?P<pct>\d+(?:\.\d+)?)%\s*used"
    r"(?:\s*[·•-]\s*resets\s+(?P<resets>.+?))?\s*$",
    re.IGNORECASE,
)
_RESET_FORMATS = ("%b %d, %I:%M%p", "%b %d, %I%p", "%b %d %I:%M%p", "%b %d %I%p", "%I:%M%p", "%I%p")


@dataclass
class Window:
    key: str  # five_hour, seven_day, seven_day_<model>: the names Claude Code's rate-limit events use
    used: float  # 0..1
    resets_at: datetime | None = None
    resets: str = ""  # as Claude Code printed it, when it couldn't be read as a time
    status: str = "allowed"  # allowed | allowed_warning | rejected
    label: str = ""

    @property
    def title(self) -> str:
        return self.label or label_for(self.key)


@dataclass
class Usage:
    """What agentlens knows about your plan usage right now."""

    windows: dict[str, Window] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)  # the rest of /usage: what's contributing to your limits
    checked: datetime | None = None  # last time /usage answered
    error: str = ""
    last_run: bool = False  # what the last launch saw, shown until this one's first check answers

    def merge(self, windows: Iterable[Window]) -> bool:
        changed = False
        for w in windows:
            old = self.windows.get(w.key)
            if old is not None and not w.label:
                w.label = old.label  # keep "Week (Fable)" from /usage when an event updates the window
            if old != w:
                self.windows[w.key] = w
                changed = True
        return changed

    def ordered(self) -> list[Window]:
        return sorted(self.windows.values(), key=lambda w: (w.key != "five_hour", w.key != "seven_day", w.key))


def cache_path() -> Path:
    return home() / "usage.json"


def save_cached(usage: Usage, path: Path | None = None) -> None:
    """Keep what /usage said, so the next launch's meter has something to show while it asks again."""
    path = path or cache_path()
    windows = [{**asdict(w), "resets_at": w.resets_at.isoformat() if w.resets_at else None}
               for w in usage.windows.values()]
    data = {"checked": usage.checked.isoformat() if usage.checked else None, "notes": usage.notes, "windows": windows}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError:
        pass  # only a convenience for the next launch


def load_cached(now: datetime, path: Path | None = None) -> Usage:
    """What the last launch's /usage said, without the windows that have reset since."""
    try:
        data = json.loads((path or cache_path()).read_text(encoding="utf-8"))
        windows = [Window(**{**w, "resets_at": datetime.fromisoformat(w["resets_at"]) if w.get("resets_at") else None})
                   for w in data["windows"]]
        checked = datetime.fromisoformat(data["checked"]) if data.get("checked") else None
        notes = [str(line) for line in data.get("notes") or []]
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return Usage()
    usage = Usage(notes=notes, checked=checked, last_run=checked is not None)
    usage.merge(w for w in windows if w.resets_at is None or w.resets_at > now)
    return usage


def label_for(key: str) -> str:
    if key == "five_hour":
        return "Session"
    if key == "seven_day":
        return "Week"
    if key.startswith("seven_day_"):
        return f"Week ({key.removeprefix('seven_day_').replace('_', ' ').title()})"
    return key.replace("_", " ").capitalize()


def _key(kind: str, scope: str) -> str:
    if kind.lower() == "session":
        return "five_hour"
    word = scope.split()[0].lower() if scope.strip() else ""
    return "seven_day" if word in ("", "all") else f"seven_day_{word}"


def parse_reset(text: str, now: datetime) -> datetime | None:
    """'Oct 25, 5:10pm (Europe/Helsinki)' -> that time, in local time like Claude Code printed it."""
    clean = re.sub(r"\s*\([^)]*\)\s*$", "", text).strip()
    local_now = now.astimezone()
    for fmt in _RESET_FORMATS:
        try:
            parsed = datetime.strptime(clean, fmt)
        except ValueError:
            continue
        if "%b" in fmt:
            parsed = parsed.replace(year=local_now.year)
        else:
            parsed = parsed.replace(year=local_now.year, month=local_now.month, day=local_now.day)
        moment = parsed.astimezone()  # naive -> this machine's time zone
        if moment < local_now - timedelta(days=180):
            moment = moment.replace(year=moment.year + 1)  # "Jan 2" read in late December
        return moment.astimezone(timezone.utc)
    return None


def parse_usage_text(text: str, now: datetime) -> tuple[list[Window], list[str]]:
    """The windows /usage printed, and every other line as it was."""
    windows: list[Window] = []
    notes: list[str] = []
    for line in text.splitlines():
        match = _WINDOW.match(line)
        if not match:
            notes.append(line.rstrip())
            continue
        scope = (match["scope"] or "").strip()
        resets = (match["resets"] or "").strip()
        used = min(1.0, float(match["pct"]) / 100)
        label = "Session" if match["kind"].lower() == "session" else (f"Week ({scope})" if scope else "Week")
        windows.append(Window(_key(match["kind"], scope), used, parse_reset(resets, now) if resets else None,
                              re.sub(r"\s*\([^)]*\)\s*$", "", resets), "rejected" if used >= 1 else "allowed",
                              label))
    while notes and not notes[0].strip():
        notes.pop(0)
    while notes and not notes[-1].strip():
        notes.pop()
    return windows, notes


def from_rate_limit(info) -> list[Window]:
    """Windows from a rate-limit event (claude_agent_sdk.RateLimitInfo) a session received."""
    raw = getattr(info, "raw", None) or {}
    unified = raw.get("unifiedWindows") if isinstance(raw, dict) else None
    windows: list[Window] = []
    if isinstance(unified, dict):
        for key, w in unified.items():
            if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
                windows.append(Window(str(key), min(1.0, float(w["utilization"])), _stamp(w.get("resetsAt")),
                                      status=str(w.get("status") or "allowed")))
    elif getattr(info, "utilization", None) is not None and getattr(info, "rate_limit_type", None):
        windows.append(Window(str(info.rate_limit_type), min(1.0, float(info.utilization)), _stamp(info.resets_at),
                              status=str(getattr(info, "status", "") or "allowed")))
    return windows


def _stamp(value) -> datetime | None:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return None


def probe_command(claude: str) -> list[str]:
    return [claude, "-p", "/usage", "-n", SESSION_NAME, "--output-format", "json", "--no-session-persistence",
            "--strict-mcp-config", "--settings", json.dumps({"disableAllHooks": True})]


def run_probe(claude: str | None, timeout: float = 60) -> str:
    """What your Claude Code's /usage prints. Raises RuntimeError with a reason to show."""
    if not claude:
        raise RuntimeError("`claude` CLI not found on PATH")
    try:
        result = subprocess.run(probe_command(claude), cwd=Path.home(), capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                                env=claude_env())
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(str(e)) from e
    try:
        data = json.loads(result.stdout)
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get("is_error"):
        reason = (data or {}).get("result") if isinstance(data, dict) else None
        raise RuntimeError(str(reason or result.stderr.strip() or f"claude exited with {result.returncode}"))
    return str(data.get("result") or "")


# Rendering --------------------------------------------------------------------------------

BLOCKS = " ▏▎▍▌▋▊▉█"


def bar(fraction: float, width: int) -> tuple[str, str]:
    """The filled and empty parts of a meter, with eighth blocks so small amounts still show."""
    eighths = round(max(0.0, min(1.0, fraction)) * width * 8)
    full, part = divmod(eighths, 8)
    filled = "█" * full + (BLOCKS[part] if part else "")
    return filled, "░" * (width - len(filled))


def level_color(w: Window, colors: tuple[str, str, str]) -> str:
    normal, warning, error = colors
    if w.status == "rejected" or w.used >= FULL_AT:
        return error
    if w.status == "allowed_warning" or w.used >= WARN_AT:
        return warning
    return normal


def resets_text(w: Window, now: datetime, short: bool = False) -> str:
    if w.resets_at is None:
        return w.resets
    left = w.resets_at - now
    if left <= timedelta(0):
        return "now"
    local = w.resets_at.astimezone()
    if left < timedelta(hours=24):
        hours, minutes = divmod(int(left.total_seconds() // 60), 60)
        span = f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"
        return f"in {span}" if short else f"in {span} ({local:%H:%M})"
    return f"{local:%a %H:%M}" if left < timedelta(days=6) else f"{local:%b} {local.day}"


def meter(usage: Usage, now: datetime, colors: tuple[str, str, str], width: int = 8) -> Text:
    """One line for the top of the wall: `Session ████▌░░░ 57% in 2h 10m   Week ▍░░░░░░░ 5% Mon 08:00`."""
    text = Text(no_wrap=True, overflow="ellipsis")
    stale = usage.last_run or (usage.checked is not None and now - usage.checked > timedelta(minutes=20))
    for i, w in enumerate(usage.ordered()):
        if i:
            text.append("   ")
        color = level_color(w, colors)
        filled, empty = bar(w.used, width)
        text.append(f"{w.title} ", style="dim" if stale else "")
        text.append(filled, style=f"dim {color}" if stale else color)
        text.append(empty, style="dim")
        pct = "limit reached" if w.status == "rejected" else f"{w.used:.0%}"
        text.append(f" {pct}", style=f"bold {color}" if w.used >= WARN_AT and not stale else "dim" if stale else "bold")
        when = resets_text(w, now, short=True)
        if when:
            text.append(f" {when}", style="dim")
    return text
