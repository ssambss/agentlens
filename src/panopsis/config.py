"""App settings and spaces, stored in ~/.panopsis/config.yaml (PANOPSIS_HOME overrides).

A space groups sessions that belong together. Every session lands in one:
- the space you moved it to, if you did,
- otherwise the first user-defined space that matches it by folder or title words,
- otherwise an automatic space for its repository, so worktrees of one repo stay together.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from panopsis import keys as keys_mod
from panopsis.preferences import home

PANE_KEY = re.compile(r"d-[0-9a-f]{10}")  # DrivenSession.key: a pane's own id, not Claude Code's

HEADER = """\
# panopsis settings. Most of these change from inside the app (press , for settings), but
# you can edit them here too. Spaces group sessions that belong together; a session stays in
# the space you moved it to (its id under `sessions`), else joins the first space whose paths
# or title words match it, otherwise it lands in an automatic space named after its repository.
#
# colors: any CSS colour (#ff8800, orange) or a theme variable ($accent, $success...).
#   selected: the border of the session you're on
#   question, waiting, background, working, failed, other: pane borders by state
#   you, claude, tool, error, notice: the conversation; card: behind your messages
# usage_minutes: how often the usage meter (top right) asks your Claude Code's /usage how much
#   of your plan's limits is used. No model call; 0 turns the meter off.
# history_days: how far back History (H) goes, by when a session was last active; 0 is all of it.
# history_keep_days: how long panopsis keeps what it found in a session after Claude Code has
#   deleted its transcript (30 days after it was last active, by default); 0 keeps it always.
# keys: the keys you changed, by what they do (? lists them; Enter on one changes it), in
#   Textual's names: r, R, ctrl+w, f5, question_mark. A comma between two gives an action both.
# findings_and_advice: the Report (R), Profile (P), Rules (L) and Advice (A) screens and their
#   commands. Experimental, so off unless you turn it on.
"""

# How far back History goes, as the settings screen offers it: days, and what that is.
HISTORY_WINDOWS = {1: "Past day", 7: "Past week", 30: "Past month", 90: "Past 3 months", 0: "Everything"}
# How long a session stays in History once Claude Code has deleted it, counted from when it was last active.
HISTORY_KEEP = {30: "30 days, as long as Claude Code keeps it", 90: "3 months", 365: "A year", 0: "Always"}

# Theme variables by default, so the colours follow the theme until you pick your own.
DEFAULT_COLORS = {
    "selected": "$foreground",
    "question": "$warning",
    "waiting": "$warning",
    "background": "$accent",
    "working": "$success",
    "failed": "$error",
    "other": "$panel-lighten-2",
    "you": "$warning",
    "claude": "$primary",
    "tool": "$foreground-darken-2",
    "error": "$error",
    "notice": "$accent",
    "card": "$panel",
}


@dataclass(frozen=True)
class Look:
    """Deep colours on near-black, which come as a theme and as a colour preset of the same name.
    A pane's state shows in how strong its colour is: brightest when it needs you, then working,
    then in the background, and dim when idle. Failures and errors take a contrasting colour."""

    name: str
    about: str
    colors: dict[str, str]
    background: str  # the theme's: behind everything
    surface: str  # behind dialogs and lists
    panel: str  # bars and cards

    @property
    def label(self) -> str:
        return f"{self.name}: {self.about}"


# Each one's borders reach 3:1 against both its own background and textual-dark's, and its text 4.5:1.
LOOKS = {
    "violet": Look("Violet", "violet in strengths", {
        "selected": "#F3EEFF", "question": "#F07BE0", "waiting": "#F07BE0", "background": "#7B5CE0",
        "working": "#A98BFF", "failed": "#FF6B81", "other": "#4A3D68", "you": "#F07BE0", "claude": "#C4AEFF",
        "tool": "#9C91B8", "error": "#FF6B81", "notice": "#D9A8FF", "card": "#261C3A",
    }, background="#0E0B15", surface="#161122", panel="#2A2040"),
    "oxblood": Look("Oxblood", "dark reds", {
        "selected": "#FFE6E6", "question": "#FF6B7F", "waiting": "#FF6B7F", "background": "#B03A4C",
        "working": "#D24A45", "failed": "#FFB347", "other": "#55303A", "you": "#FF8A80", "claude": "#EE8A92",
        "tool": "#B09597", "error": "#FFB347", "notice": "#F4A9B5", "card": "#2E1419",
    }, background="#120A0C", surface="#1B0F12", panel="#33171D"),
    "plum-night": Look("Plum night", "plum, purple and navy", {
        "selected": "#F1F1F1", "question": "#EE86B8", "waiting": "#EE86B8", "background": "#7290D6",
        "working": "#AF81E4", "failed": "#FF8A80", "other": "#45344F", "you": "#EE86B8", "claude": "#C3A2EE",
        "tool": "#AAAAAA", "error": "#FF8A80", "notice": "#8FAAE6", "card": "#2C1A30",
    }, background="#0F0F0F", surface="#171419", panel="#2B1D33"),
    "ember": Look("Ember", "copper and wine", {
        "selected": "#FFEDE2", "question": "#FFB768", "waiting": "#FFB768", "background": "#B94E66",
        "working": "#E5814F", "failed": "#FF5F5F", "other": "#553238", "you": "#FFB768", "claude": "#F0A285",
        "tool": "#ACA09A", "error": "#FF5F5F", "notice": "#D59AC6", "card": "#31191A",
    }, background="#110C0B", surface="#1A1210", panel="#351D1B"),
    "deep-sea": Look("Deep sea", "teal and navy", {
        "selected": "#E2F7FA", "question": "#6BE9F7", "waiting": "#6BE9F7", "background": "#5476D6",
        "working": "#3FA6CC", "failed": "#FF7B7B", "other": "#28404F", "you": "#6BE9F7", "claude": "#8DC2EA",
        "tool": "#90A6B0", "error": "#FF7B7B", "notice": "#97A8FF", "card": "#112A33",
    }, background="#0A0F12", surface="#10181C", panel="#1B2E38"),
    "aurora": Look("Aurora", "green, blue and orchid", {
        "selected": "#E9FFF6", "question": "#F5A3FF", "waiting": "#F5A3FF", "background": "#6E8DFF",
        "working": "#3BB980", "failed": "#FF6F6F", "other": "#2D3F45", "you": "#F5A3FF", "claude": "#67E6D3",
        "tool": "#95AAA2", "error": "#FF6F6F", "notice": "#B09AFC", "card": "#162D28",
    }, background="#0A100F", surface="#101917", panel="#1C2F2B"),
    "forest": Look("Forest", "greens and gold", {
        "selected": "#EDFDF2", "question": "#E9C75F", "waiting": "#E9C75F", "background": "#4FA5A7",
        "working": "#62C27D", "failed": "#FF6E5E", "other": "#2E3E34", "you": "#E9C75F", "claude": "#86D19D",
        "tool": "#97A59C", "error": "#FF6E5E", "notice": "#78ADDE", "card": "#17281D",
    }, background="#0B100C", surface="#121A14", panel="#1E3024"),
}

# The colour presets the settings screen offers: an id, what it's called, and its colours.
# Theme variables follow light and dark themes; fixed colours are picked to read on dark ones.
COLOR_PRESETS = {
    "theme": ("The theme's colours", DEFAULT_COLORS),
    **{key: (look.label, look.colors) for key, look in LOOKS.items()},
    "calm": ("Calm: only sessions that need you are coloured", {
        **DEFAULT_COLORS, "background": "$panel-lighten-2", "working": "$panel-lighten-2",
    }),
    "colorblind": ("Colour-blind safe (Okabe-Ito)", {
        **DEFAULT_COLORS, "question": "#E69F00", "waiting": "#E69F00", "background": "#009E73",
        "working": "#56B4E9", "failed": "#D55E00", "you": "#E69F00", "claude": "#56B4E9",
        "error": "#D55E00", "notice": "#CC79A7",
    }),
    "vivid": ("Vivid, for dark themes", {
        **DEFAULT_COLORS, "selected": "#FFFFFF", "question": "#FFB000", "waiting": "#FFB000",
        "background": "#00B7FF", "working": "#00E676", "failed": "#FF5252", "you": "#FFB000",
        "claude": "#40C4FF", "error": "#FF5252", "notice": "#E040FB",
    }),
}


def color_preset(colors: dict[str, str]) -> str | None:
    """The preset these colours are, or None for colours of your own."""
    return next((key for key, (_, preset) in COLOR_PRESETS.items() if preset == colors), None)


@dataclass
class Space:
    name: str
    paths: list[str] = field(default_factory=list)  # sessions working under these folders
    titles: list[str] = field(default_factory=list)  # or whose title contains one of these words
    sessions: list[str] = field(default_factory=list)  # or these session ids (picked in the app)

    def picked(self, session_id: str) -> bool:
        return any(session_id.startswith(s) for s in self.sessions if s)

    def matches(self, session_id: str, cwd: str, title: str) -> bool:
        if self.picked(session_id):
            return True
        folder = _norm(cwd)
        if any(folder == _norm(p) or folder.startswith(_norm(p) + "/") for p in self.paths if p):
            return True
        lowered = title.lower()
        return any(word.lower() in lowered for word in self.titles if word)


@dataclass
class Config:
    theme: str = "textual-dark"
    colors: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_COLORS))
    columns: int | None = None  # None: chosen from the number of panes
    sidebar: bool = True
    show_tools: bool = True
    show_times: bool = True
    refresh_seconds: float = 2.0
    usage_minutes: float = 5.0  # how often the usage meter asks your Claude Code (/usage); 0 turns it off
    # How far back History goes, in days: reading every transcript whole took long. 0: all of it.
    history_days: int = 7
    history_keep_days: int = 365  # after Claude Code deletes a session's transcript; 0: always
    last_space: str | None = None
    last_folder: str | None = None  # where the last new session was started
    default_mode: str = "default"  # permission mode for new sessions
    cli_path: str | None = None  # Claude Code to run; empty uses `claude` on PATH
    # Where files and folders open: one of editor.EDITORS (code, zed, jetbrains, nvim...), or your own
    # command with {file} and {line} in it. Empty: $VISUAL or $EDITOR, else the first one installed.
    editor: str | None = None
    editor_terminal: bool = False  # your own command runs in this terminal, like vim
    keys: dict[str, str] = field(default_factory=dict)  # the keys you changed, by keys.ACTIONS id
    findings_and_advice: bool = False  # experimental: Report, Profile, Rules and Advice
    spaces: list[Space] = field(default_factory=list)
    # Sessions panopsis runs, restored (paused) on the next launch until you end or detach them.
    driven_sessions: list[dict] = field(default_factory=list)
    hidden_sessions: list[str] = field(default_factory=list)  # panes you closed that belong to VS Code etc.
    path: Path | None = None

    def space_for(self, session_id: str, cwd: str, title: str) -> str:
        # A session you moved stays where you put it, whatever another space's folders or words say.
        picked = next((space.name for space in self.spaces if space.picked(session_id)), None)
        if picked:
            return picked
        for space in self.spaces:
            if space.matches(session_id, cwd, title):
                return space.name
        return repo_name(cwd)

    def remember_driven(self, session_id: str, cwd: str, title: str, mode: str) -> None:
        entry = {"session_id": session_id, "cwd": cwd, "title": title, "mode": mode}
        for i, saved in enumerate(self.driven_sessions):
            if saved.get("session_id") == session_id:
                self.driven_sessions[i] = entry  # in place: panes come back in this order
                return
        self.driven_sessions.append(entry)

    def retitle_driven(self, session_id: str, title: str) -> bool:
        """Give a saved session its new title. False if it isn't saved, or already has that title."""
        saved = next((d for d in self.driven_sessions if d.get("session_id") == session_id), None)
        if saved is None or saved.get("title") == title:
            return False
        saved["title"] = title
        return True

    def forget_driven(self, session_id: str | None) -> None:
        self.driven_sessions = [d for d in self.driven_sessions if d.get("session_id") != session_id]

    def assign(self, session_id: str, space_name: str) -> None:
        """Put one session in a space by hand, taking it out of any other space's picks."""
        self.unassign(session_id)
        target = next((s for s in self.spaces if s.name == space_name), None)
        if target is None:
            target = Space(space_name)
            self.spaces.append(target)
        target.sessions.append(session_id)

    def unassign(self, session_id: str) -> str | None:
        """Take a session out of the space it was put in by hand. The space it was in, if any."""
        picked = next((s.name for s in self.spaces if session_id in s.sessions), None)
        for space in self.spaces:
            space.sessions = [s for s in space.sessions if s != session_id]
        return picked


def config_path() -> Path:
    return home() / "config.yaml"


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        return Config(path=path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults = Config()
    spaces = [
        Space(
            name=str(s.get("name", "space")),
            paths=[str(p) for p in s.get("paths") or []],
            titles=[str(t) for t in s.get("titles") or []],
            # Moving a session run here once saved its pane's key, new every launch, not its id.
            sessions=[str(i) for i in s.get("sessions") or [] if not PANE_KEY.fullmatch(str(i))],
        )
        for s in data.get("spaces") or []
        if isinstance(s, dict)
    ]
    columns = data.get("columns")
    colors = data.get("colors") if isinstance(data.get("colors"), dict) else {}
    keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
    return Config(
        theme=str(data.get("theme", defaults.theme)),
        colors={**DEFAULT_COLORS, **{k: str(v) for k, v in colors.items() if k in DEFAULT_COLORS and v}},
        columns=int(columns) if isinstance(columns, int) and columns > 0 else None,
        sidebar=bool(data.get("sidebar", defaults.sidebar)),
        show_tools=bool(data.get("show_tools", defaults.show_tools)),
        show_times=bool(data.get("show_times", defaults.show_times)),
        refresh_seconds=float(data.get("refresh_seconds", defaults.refresh_seconds)),
        usage_minutes=_minutes(data.get("usage_minutes"), defaults.usage_minutes),
        history_days=_days(data.get("history_days"), defaults.history_days),
        history_keep_days=_days(data.get("history_keep_days"), defaults.history_keep_days),
        last_space=data.get("last_space"),
        last_folder=data.get("last_folder"),
        default_mode=str(data.get("default_mode", defaults.default_mode)),
        cli_path=data.get("cli_path") or None,
        editor=str(data["editor"]) if data.get("editor") else None,
        editor_terminal=bool(data.get("editor_terminal", defaults.editor_terminal)),
        keys=keys_mod.changed({}, {str(k): keys_mod.normalize(str(v)) for k, v in keys.items() if v}),
        findings_and_advice=bool(data.get("findings_and_advice", defaults.findings_and_advice)),
        spaces=spaces,
        driven_sessions=[
            {k: str(d.get(k) or "") for k in ("session_id", "cwd", "title", "mode")}
            for d in data.get("driven_sessions") or []
            if isinstance(d, dict) and d.get("session_id") and d.get("cwd")
        ],
        hidden_sessions=[str(s) for s in data.get("hidden_sessions") or []],
        path=path,
    )


def _minutes(value, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return 0.0 if value <= 0 else max(1.0, float(value))  # at most once a minute


def _days(value, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(0, int(value))


def save(config: Config) -> Path:
    path = config.path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(config)
    data.pop("path")
    path.write_text(HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


@lru_cache(maxsize=512)
def repo_root(cwd: str) -> str:
    """The repository a folder belongs to. Claude Code worktrees live under
    <repo>/.claude/worktrees/<name>, so they map back to <repo>."""
    path = Path(cwd.replace("\\", "/"))
    parts = path.parts
    for i in range(len(parts) - 1):
        if parts[i] == ".claude" and parts[i + 1] == "worktrees":
            return str(Path(*parts[:i]))
    for candidate in (path, *path.parents):
        try:
            if (candidate / ".git").exists():
                return str(candidate)
        except OSError:
            break
    return str(path)


def repo_name(cwd: str) -> str:
    if not cwd:
        return "other"
    return Path(repo_root(cwd)).name or cwd


def _norm(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").lower()
