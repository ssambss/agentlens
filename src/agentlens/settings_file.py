"""Your settings in one file, to set up agentlens on another PC as you have it here.

The file has how agentlens looks and behaves and your editor (config.PORTABLE), and your goals and
style (preferences.PORTABLE). Nothing else about the PC it was made on: not its spaces, which are
its folders, its Claude Code, or the sessions it restores and hides.
Importing it puts those in place of the other PC's own and leaves the rest of its settings as they are.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from agentlens import config as config_mod, preferences
from agentlens.config import Config

VERSION = 1
HEADER = """\
# agentlens settings, to set agentlens up on another PC as it is on this one: how it looks, its keys,
# how far back History goes and how long it keeps sessions, the usage meter, the mode for new
# sessions, your editor, and your goals and style. Nothing else about this PC: not your spaces,
# Claude Code or sessions. On the other PC, Import on the settings screen (,), or
# `agentlens import <this file>`.
"""
# What each one is called when an import says what it changed, as the settings screen calls them.
NAMES = {
    "theme": "theme", "colors": "colours", "columns": "columns", "sidebar": "spaces sidebar",
    "show_tools": "tool calls", "show_times": "timestamps", "refresh_seconds": "refresh interval",
    "usage_minutes": "usage meter", "history_days": "how far back History goes",
    "history_keep_days": "how long sessions are kept", "default_mode": "mode for new sessions", "editor": "editor",
    "editor_terminal": "whether your editor runs in the terminal", "keys": "keys",
    "findings_and_advice": "findings and advice", "autopilot": "autopilot", "coordinator": "the coordinator",
    "goals": "goals",
    "style": "style", "mute": "muted rules",
    "thresholds": "rule thresholds",
}


class SettingsFileError(ValueError):
    """A file that isn't settings agentlens exported, or can't be read."""


@dataclass
class Exported:
    settings: dict[str, object]  # by config.PORTABLE name, checked as the settings file is
    preferences: dict  # as the file has them; preferences.take checks them

    def changes(self, config: Config) -> dict[str, object]:
        """The settings that are different here."""
        return {name: value for name, value in self.settings.items() if value != getattr(config, name)}


NAME = "agentlens-settings.yaml"


def default_path() -> Path:
    return Path.home() / NAME


def in_folder(path: Path) -> Path:
    """Where the file is: a folder means the file of the usual name in it."""
    return path / NAME if path.is_dir() else path


def replaceable(path: Path) -> bool:
    """Whether exporting may write here without asking: nothing's there yet, or settings exported before."""
    if not path.exists():
        return True
    try:
        read(path)
    except SettingsFileError:
        return False
    return True


def export(config: Config, path: Path) -> Path:
    prefs = preferences.load()
    data = {
        "version": VERSION,
        "settings": {name: getattr(config, name) for name in config_mod.PORTABLE},
        "preferences": {key: getattr(prefs, key) for key in preferences.PORTABLE},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def read(path: Path) -> Exported:
    try:
        # utf-8-sig: Notepad and PowerShell can save it with a byte order mark.
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise SettingsFileError(f"No file at {path}") from None
    except (OSError, UnicodeError, yaml.YAMLError) as e:
        raise SettingsFileError(f"Couldn't read {path}: {e}") from None
    data = data if isinstance(data, dict) else {}
    settings = data.get("settings") if isinstance(data.get("settings"), dict) else None
    prefs = data.get("preferences") if isinstance(data.get("preferences"), dict) else None
    if settings is None and prefs is None:
        raise SettingsFileError(f"{path} isn't a settings file agentlens exported")
    # Only what may come from another PC, even from a file with more in it. In the screen's order, the theme first.
    given = {name: settings[name] for name in config_mod.PORTABLE if name in (settings or {})}
    try:
        parsed = config_mod.parse(given)
    except (TypeError, ValueError) as e:
        raise SettingsFileError(f"{path} has a setting agentlens can't read: {e}") from None
    return Exported({name: getattr(parsed, name) for name in given}, prefs or {})


def named(changed: list[str]) -> str:
    """What an import changed, in words: theme, colours and keys."""
    names = [NAMES.get(name, name) for name in changed]
    return ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else "".join(names)
