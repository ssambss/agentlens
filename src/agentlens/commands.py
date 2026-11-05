"""Claude Code's slash commands, for the menu that opens when you type / in a session's box.

The list comes from the Claude Code a session runs (built-in commands, your skills, your
plugins' commands), with their descriptions and argument hints, so the menu shows what that
session really has. Until a session has started, the last list seen stands in; it's kept in
~/.agentlens/claude-commands.json between launches.

A few commands mean something different here than in a terminal, so agentlens handles them:
/usage opens the usage view, /exit closes the session. A few others are left out because
they don't work in a session Claude Code runs headless (/fast) or change a terminal's UI.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from agentlens.preferences import home


@dataclass(frozen=True)
class Command:
    name: str
    description: str = ""
    hint: str = ""  # argument hint, as Claude Code gives it: "<model>", "[on|off]", "[name]"
    aliases: tuple[str, ...] = ()
    builtin: bool = False
    local: bool = False  # agentlens handles it instead of sending it to Claude Code

    @property
    def needs_argument(self) -> bool:
        """Claude Code marks a required argument <like this>; [this] is optional."""
        return self.hint.startswith("<") and "optional" not in self.hint.lower()


@dataclass(frozen=True)
class Choice:
    value: str
    label: str = ""
    description: str = ""


@dataclass(frozen=True)
class Suggestion:
    command: Command
    choice: Choice | None = None

    @property
    def text(self) -> str:
        """What the box holds once this is picked."""
        return f"/{self.command.name} {self.choice.value}" if self.choice else f"/{self.command.name}"


LOCAL = (
    Command("usage", "Your plan's session and weekly limits, and what's using them (agentlens's usage view)",
            aliases=("cost", "stats"), builtin=True, local=True),
    Command("exit", "Close this session; the conversation stays in History", aliases=("quit",), builtin=True,
            local=True),
)
# Terminal-only, or not available in a session Claude Code runs for the Agent SDK.
HIDDEN = {"fast", "focus", "color", "heapdump", "workflow-launch-exec"}


@dataclass
class Catalog:
    commands: list[Command] = field(default_factory=list)
    models: list[Choice] = field(default_factory=list)
    styles: list[Choice] = field(default_factory=list)

    @classmethod
    def from_server_info(cls, info: dict | None) -> Catalog:
        """From ClaudeSDKClient.get_server_info(): Claude Code's own list for this session."""
        info = info if isinstance(info, dict) else {}
        commands: list[Command] = []
        for c in info.get("commands") or []:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            name, description = str(c["name"]).lstrip("/"), str(c.get("description") or "")
            if name in HIDDEN or name.startswith("_") or description.startswith(("(removed)", "Renamed to")):
                continue
            commands.append(Command(name, description, str(c.get("argumentHint") or ""),
                                    tuple(str(a) for a in c.get("aliases") or []), bool(c.get("builtin"))))
        models = [Choice(str(m["value"]), str(m.get("displayName") or ""), str(m.get("description") or ""))
                  for m in info.get("models") or [] if isinstance(m, dict) and m.get("value")]
        styles = [Choice(str(s)) for s in info.get("available_output_styles") or [] if isinstance(s, str)]
        return cls(commands, models, styles)

    def with_local(self) -> Catalog:
        local = {c.name for c in LOCAL}
        return Catalog([*LOCAL, *(c for c in self.commands if c.name not in local)], self.models, self.styles)

    def find(self, name: str) -> Command | None:
        name = name.lower()
        return next((c for c in self.commands if c.name.lower() == name or name in c.aliases), None)

    def choices(self, command: Command) -> list[Choice]:
        """The values an argument can take, when Claude Code says: models, output styles, or an
        alternation in the hint like <low|medium|high>."""
        if command.name == "model" and self.models:
            return self.models
        if command.name == "output-style" and self.styles:
            return self.styles
        hint = command.hint.strip()
        if len(hint) < 3 or hint[0] not in "<[" or hint[-1] not in ">]" or "|" not in hint:
            return []
        # The plain words: "<low|high|ultracode [on|off]>" offers low and high.
        return [Choice(w) for w in (w.strip() for w in hint[1:-1].split("|")) if re.fullmatch(r"[\w.\-]+", w)]

    def suggest(self, text: str) -> list[Suggestion]:
        """What the menu shows for the box's text: commands while you type the name, then the
        argument's choices once there's a space after it."""
        if not text.startswith("/") or "\n" in text:
            return []
        name, space, arg = text[1:].partition(" ")
        if space:
            command = self.find(name)
            if command is None:
                return []
            needle = arg.strip().lower()
            choices = self.choices(command)
            ranked = sorted(
                (c for c in choices if not needle or needle in c.value.lower() or needle in c.label.lower()),
                key=lambda c: (not c.value.lower().startswith(needle), choices.index(c)))
            return [Suggestion(command, c) for c in ranked]
        query = name.lower()
        scored = [(score, c) for c in self.commands if (score := _score(c, query)) is not None]
        scored.sort(key=lambda sc: (sc[0], not sc[1].builtin, sc[1].name))
        return [Suggestion(c) for _, c in scored]

    # Kept between launches, so the menu works before a session has started ---------------

    def to_dict(self) -> dict:
        return {
            "commands": [{"name": c.name, "description": c.description, "argumentHint": c.hint,
                          "aliases": list(c.aliases), "builtin": c.builtin} for c in self.commands],
            "models": [{"value": m.value, "displayName": m.label, "description": m.description} for m in self.models],
            "available_output_styles": [s.value for s in self.styles],
        }


def local_command(text: str) -> Command | None:
    """The command agentlens handles itself that this message is, if any (/usage, /exit...)."""
    text = text.strip()
    if not text.startswith("/"):
        return None
    return Catalog(list(LOCAL)).find(text[1:].split(" ", 1)[0])


def _score(c: Command, query: str) -> int | None:
    """Lower is better; None leaves it out. Name first, then aliases, then words in the name
    (plugin:skill, kebab-case), then anywhere in the name, then the description."""
    if not query:
        return 0
    name = c.name.lower()
    if name == query or query in c.aliases:
        return 0
    if name.startswith(query):
        return 1
    if any(a.startswith(query) for a in c.aliases):
        return 2
    if any(part.startswith(query) for part in re.split(r"[:\-_]", name)):
        return 3
    if query in name:
        return 4
    if len(query) >= 3 and query in c.description.lower():
        return 5
    return None


def cache_path() -> Path:
    return home() / "claude-commands.json"


def load_cached(path: Path | None = None) -> Catalog:
    try:
        return Catalog.from_server_info(json.loads((path or cache_path()).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return Catalog()


def save_cached(catalog: Catalog, path: Path | None = None) -> None:
    path = path or cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(catalog.to_dict(), indent=1), encoding="utf-8")
    except OSError:
        pass  # only a convenience for the next launch
