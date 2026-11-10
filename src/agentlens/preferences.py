"""What the user has told agentlens about how they want to work.

Stored in ~/.agentlens/profile.yaml (override with AGENTLENS_HOME). Everything is
optional; with no file, agentlens still personalizes through the user's own history.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

GOALS = ("speed", "cost", "quality", "focus", "learning")
STYLE_OPTIONS = {
    "steering": ("close", "balanced", "autonomous"),
    "parallelism": ("single", "few", "many"),
    "delegation": ("minimal", "fan-out", "heavy"),
}

TEMPLATE = """\
# agentlens preferences. Everything here is optional.

# What you're optimizing for. Findings tagged with these goals are shown first.
# Options: speed, cost, quality, focus, learning
goals: []

# How you deliberately like to work. Rules that would nag you about a style you
# chose on purpose are muted. Leave a key out to let agentlens judge from your data.
style: {}
#  steering: close        # close | balanced | autonomous
#  parallelism: few       # single | few | many
#  delegation: fan-out    # minimal | fan-out | heavy

# Rule ids you never want to see (`agentlens mute <id>` adds to this list).
mute: []

# Per-rule threshold overrides: {rule-id: {metric: value}}. Values can be numbers
# or percentiles of your own history, like p90.
thresholds: {}

# Extra rule pack directories, e.g. your team's shared repo checkout.
rule_packs: []
"""


@dataclass
class Preferences:
    goals: list[str] = field(default_factory=list)
    style: dict[str, str] = field(default_factory=dict)
    mute: list[str] = field(default_factory=list)
    thresholds: dict[str, dict] = field(default_factory=dict)
    rule_packs: list[str] = field(default_factory=list)
    path: Path | None = None
    ignored: list[str] = field(default_factory=list)  # values in the file that aren't valid options


def home() -> Path:
    if os.environ.get("AGENTLENS_HOME"):
        return Path(os.environ["AGENTLENS_HOME"])
    path = Path.home() / ".agentlens"
    _carry_over(Path.home() / ".panopsis", path)
    return path


def write_whole(path: Path, text: str) -> None:
    """Write `text` to `path` in one piece: to a file beside it, then into its place. An agentlens
    ended mid-write (Stop-Process -Force, a power cut) leaves the old file, never the start of the
    new one, which reads as none at all: no goals, the default settings."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        for wait in (0.02, 0.05, 0.1, 0.2):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:  # Windows: something has it open a moment, another agentlens or a scanner
                time.sleep(wait)
    finally:
        temporary.unlink(missing_ok=True)
    path.write_text(text, encoding="utf-8")  # still held: in place, as before


_carried = False  # looked for the old folder this run
CARRIED = ".from-panopsis"  # in the folder once what panopsis kept is in it


def _carry_over(old: Path, new: Path) -> None:
    """Start from what agentlens kept while it was called panopsis: its settings, spaces, keys and
    history. Copied, so a panopsis still running keeps its own; whole, or tried again next run.

    Before that it was agentlens too, and panopsis started from a copy of ~/.agentlens it then
    left as it was. panopsis's is newer, so it takes that one's place, and the old one is kept
    beside it, in ~/.agentlens.before-panopsis."""
    global _carried
    if _carried:
        return
    _carried = True
    if (new / CARRIED).exists() or not old.is_dir():
        return
    partial = new.with_name(new.name + ".partial")
    try:
        shutil.rmtree(partial, ignore_errors=True)
        shutil.copytree(old, partial)
        (partial / CARRIED).touch()
        if new.exists():
            new.replace(new.with_name(new.name + ".before-panopsis"))
        partial.replace(new)
    except OSError:
        shutil.rmtree(partial, ignore_errors=True)


def profile_path() -> Path:
    return home() / "profile.yaml"


def load(path: Path | None = None) -> Preferences:
    path = path or profile_path()
    if not path.exists():
        return Preferences(path=path)
    return parse(yaml.safe_load(path.read_text(encoding="utf-8")) or {}, path)


def parse(data: dict, path: Path | None = None) -> Preferences:
    """Preferences as the file has them, checked: values that aren't options are left out, and listed."""
    goals = data.get("goals") if isinstance(data.get("goals"), list) else []
    style = data.get("style") if isinstance(data.get("style"), dict) else {}
    mute = data.get("mute") if isinstance(data.get("mute"), list) else []
    thresholds = data.get("thresholds") if isinstance(data.get("thresholds"), dict) else {}
    ignored = [f"goal {g!r} (options: {', '.join(GOALS)})" for g in goals if g not in GOALS]
    for key, value in style.items():
        if key not in STYLE_OPTIONS:
            ignored.append(f"style {key!r} (options: {', '.join(STYLE_OPTIONS)})")
        elif value not in STYLE_OPTIONS[key]:
            ignored.append(f"{key} {value!r} (options: {', '.join(STYLE_OPTIONS[key])})")
    return Preferences(
        goals=[g for g in goals if g in GOALS],
        style={k: v for k, v in style.items() if v in STYLE_OPTIONS.get(k, ())},
        mute=[str(m) for m in mute if m],
        thresholds={str(k): v for k, v in thresholds.items() if isinstance(v, dict)},  # {rule-id: {metric: value}}
        rule_packs=list(data.get("rule_packs") or []),
        path=path,
        ignored=ignored,
    )


# The preferences that go to another PC (settings_file): not the rule packs, which are its folders.
PORTABLE = ("goals", "style", "mute", "thresholds")


def take(data: dict, path: Path | None = None) -> list[str]:
    """Put an exported file's goals, style, mutes and thresholds in place of yours, leaving the rest of the
    file as written. The ones that changed; with none, there's no file to make."""
    given = parse(data)
    current = load(path)
    changed = [key for key in PORTABLE if key in data and getattr(given, key) != getattr(current, key)]
    if changed:
        path = ensure_file(path)
        for key in changed:
            _write_entry(path, key, _flow(getattr(given, key)))
    return changed


def ensure_file(path: Path | None = None) -> Path:
    path = path or profile_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TEMPLATE, encoding="utf-8")
    return path


def remove_mute(rule_id: str, path: Path | None = None) -> Path:
    path = ensure_file(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    _write_mutes(path, [m for m in data.get("mute") or [] if m != rule_id])
    return path


def add_mute(rule_id: str, path: Path | None = None) -> Path:
    """Append a rule id to the mute list, preserving the rest of the file as written."""
    path = ensure_file(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    mutes = list(data.get("mute") or [])
    if rule_id not in mutes:
        _write_mutes(path, mutes + [rule_id])
    return path


def set_goals(goals: list[str], path: Path | None = None) -> Path:
    path = ensure_file(path)
    _write_entry(path, "goals", "[" + ", ".join(g for g in GOALS if g in goals) + "]")
    return path


def set_style(key: str, value: str | None, path: Path | None = None) -> Path:
    """Set one deliberate style (steering: close), or None to let agentlens judge it from your data."""
    path = ensure_file(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    style = {k: v for k, v in (data.get("style") or {}).items() if k != key}
    if value:
        style[key] = value
    _write_entry(path, "style", _flow(style))
    return path


def _write_mutes(path: Path, mutes: list[str]) -> None:
    _write_entry(path, "mute", "[" + ", ".join(mutes) + "]")


def _flow(value: object) -> str:
    """A value as one line of YAML, for _write_entry."""
    return yaml.safe_dump(value, default_flow_style=True, width=1000, allow_unicode=True).strip()


def _write_entry(path: Path, key: str, value: str) -> None:
    """Rewrite one top-level entry as a single flow-style line, leaving every other line as written."""
    entry = f"{key}: {value}"
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.startswith(f"{key}:"):
            lines[i] = entry
            # Drop the rest of the old value, now folded into the line above: block items, indented
            # or not ("mute:\n- a" is what yaml.dump writes), and the tail of a multi-line flow list.
            while i + 1 < len(lines) and (lines[i + 1][:1].isspace() or lines[i + 1].startswith("-")):
                del lines[i + 1]
            break
    else:
        lines.append(entry)
    write_whole(path, "\n".join(lines) + "\n")
