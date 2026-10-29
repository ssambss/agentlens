"""panopsis: the app. A monitoring wall of every open Claude Code session, grouped into
spaces, with your history, report, advice, profile and rules one key away.

Panes keep their place (a wall where panes jump around is unreadable); state shows as
border colour. Each pane reads as a conversation: your messages, Claude's replies as
markdown, tool calls folded into one line.
"""

from __future__ import annotations

import asyncio
import gc
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

from rich.console import Group, RenderableType
from rich.style import Style
from rich.text import Text
from textual import events, on, work
from textual.color import Color, ColorParseError
from textual.app import App, ComposeResult, SuspendNotSupported, SystemCommand
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.driver import Driver
from textual.reactive import Reactive
from textual.markup import escape
from textual.notifications import Notification, Notify
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widget import Widget
from textual.widgets import DataTable, Footer, Header, Input, Label, LoadingIndicator, Markdown, OptionList, Static, Tree
from textual.widgets.option_list import Option
from textual.widgets.tree import TreeNode
from textual.worker import get_current_worker

from panopsis import activity as activity_mod, advise, analysis as analysis_mod, clipboard, commands as commands_mod
from panopsis import config as config_mod
from panopsis import editor as editor_mod, keys as keys_mod, preferences, terminal, timings, usage as usage_mod
from panopsis.adapters.claude_transcripts import (
    DEFAULT_ROOT,
    DETAIL_CHARS,
    SessionCache,
    load_tail,
    recent_transcripts,
    short_path,
    worked_in,
    worked_in_named,
)
from panopsis.analysis import Analysis
from panopsis.read import ReadSessions
from panopsis.store import SessionStore
from panopsis.driver import (
    ENDED,
    FAILED,
    PAUSED,
    PERMISSION,
    STARTING,
    DrivenSession,
    default_client_factory,
    folder_trust,
    is_home,
)
from panopsis.keys import binding as bind
from panopsis.tui_driven import SPIN_SECONDS, SPINNER, ChoiceScreen, Composer, DetachScreen, NewSessionScreen, as_live
from panopsis.tui_keys import KeysScreen
from panopsis.tui_settings import SettingsScreen
from panopsis.feed import Palette, activity_log, conversation, palette_from
from panopsis.frames import PacedScreen
from panopsis.live import (
    APPROVAL,
    BACKGROUND,
    OTHER,
    QUESTION,
    WAITING,
    WORKING,
    LiveSession,
    QUICK_TAIL_BYTES,
    TAIL_BYTES,
    TranscriptTails,
    ago,
    derive,
    file_changes,
    open_sessions,
    pane_place,
    pane_title,
    subagent_lists,
)
from panopsis.metrics import session_metrics
from panopsis.model import Event, FileChange, Session
from panopsis.report import counted, fmt_min, report_parts, session_parts, style_panel
from panopsis.subagent_map import SubagentMap
from panopsis.selectable import Selectable

ALL = "All sessions"
NEEDS_YOU = {QUESTION, APPROVAL, PERMISSION, WAITING}
STATE_CLASS = {
    QUESTION: "st-question",
    APPROVAL: "st-question",
    PERMISSION: "st-question",
    WAITING: "st-waiting",
    BACKGROUND: "st-background",
    WORKING: "st-working",
    STARTING: "st-other",
    ENDED: "st-other",
    PAUSED: "st-other",
    FAILED: "st-failed",
    OTHER: "st-other",
}
# Colours come from the settings file as CSS variables ($al-<name>); see resolve_colors.
STATE_COLOR = {
    "st-question": "$al-question",
    "st-waiting": "$al-waiting",
    "st-background": "$al-background",
    "st-working": "$al-working",
    "st-failed": "$al-failed",
    "st-other": "$al-other",
}


def _state_css() -> str:
    rules = []
    for cls, color in STATE_COLOR.items():
        border = "heavy" if cls == "st-question" else "round"
        rules.append(f"SessionPane.{cls} {{ border: {border} {color}; border-title-color: {color}; }}")
    # The pane you're on, also while you type in its box: its own colour, so it stands out
    # from the state colours around it. The title keeps the state colour.
    rules.append("SessionPane:focus-within { border: heavy $al-selected; background: $al-selected 7%; "
                 "border-title-style: bold; }")
    return "\n".join(rules)


def _parse_color(value: str, variables: dict[str, str]) -> Color | None:
    if value.startswith("$"):
        value = variables.get(value[1:], "")
    try:
        return Color.parse(value)
    except ColorParseError:
        return None


def resolve_colors(colors: dict[str, str], variables: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """The configured colours as CSS variables, plus the settings that weren't colours."""
    resolved, bad = {}, []
    for name, default in config_mod.DEFAULT_COLORS.items():
        value = colors.get(name, default)
        color = _parse_color(value, variables)
        if color is None:
            bad.append(f"{name}: {value}")
            color = _parse_color(default, variables) or Color(128, 128, 128)
        resolved[f"al-{name}"] = color.hex6
    return resolved, bad


def look_themes() -> list[Theme]:
    """panopsis's own themes, one for each look: its near-black, with its colours as the theme's,
    so the theme's colours (the default) are the look's."""
    return [
        Theme(name=key, primary=look.colors["claude"], secondary=look.colors["you"],
              accent=look.colors["background"], warning=look.colors["question"], error=look.colors["failed"],
              success=look.colors["working"], foreground=look.colors["selected"], background=look.background,
              surface=look.surface, panel=look.panel, dark=True)
        for key, look in config_mod.LOOKS.items()
    ]


BUSY = {WORKING, PERMISSION, QUESTION, APPROVAL}  # mid-turn: ending or detaching affects running work
FLUSH_SECONDS = 0.15  # a streaming reply repaints at most ~7 times a second
STREAMING_PANES = 3  # with more replies streaming at once, the panes you're not on repaint less often
# Once the wall is filled in, history is read in the background, so the report screens open at once.
# None: only when one asks for it (tests).
PRELOAD_SECONDS: float | None = 2.0
# A report screen opened on history read longer ago than this shows it at once, and reads it again quietly.
HISTORY_FRESH = timedelta(minutes=1)


def background_ids(list_sessions: Callable[[], list[dict]]) -> set[str]:
    try:
        return {e["sessionId"] for e in list_sessions() if e.get("kind") == "background"}
    except (RuntimeError, OSError, ValueError):
        return set()


def background_resume(
    claude: str | None, session_id: str, cwd: str, mode: str, instruction: str,
    list_sessions: Callable[[], list[dict]] = open_sessions, wait_seconds: float = 8.0,
) -> tuple[bool, str]:
    """Hand a conversation to Claude Code's background service (`claude --resume <id> --bg`).

    Success is judged by a background session actually appearing, not by output wording:
    on a false "failed" the caller would reopen the session here and two clients would
    write to it."""
    if not claude:
        return False, "`claude` CLI not found on PATH"
    before = background_ids(list_sessions)
    cmd = [claude, "--resume", session_id, "--bg", "--permission-mode", mode, "--", instruction]
    try:
        result = subprocess.run(cmd, cwd=cwd or None, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=120, stdin=subprocess.DEVNULL, env=terminal.claude_env())
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    deadline = time.monotonic() + wait_seconds
    while True:
        now_ids = background_ids(list_sessions)
        if session_id in now_ids or now_ids - before:
            lines = [ln for ln in output.splitlines() if ln.strip() and not ln.startswith("  claude ")]
            return True, "\n".join(lines[:3]) or "backgrounded"
        if time.monotonic() > deadline:
            return False, output or f"claude exited with {result.returncode}"
        time.sleep(0.5)


def stop_background(claude: str | None, session_id: str, name: str = "", job_id: str = "") -> tuple[bool, str]:
    if not claude:
        return False, "`claude` CLI not found on PATH"
    output = ""
    # `claude stop` matches the short job id; the full session id gets "No job matching".
    for ident in (job_id, session_id, name):
        if not ident:
            continue
        try:
            result = subprocess.run([claude, "stop", ident], capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=60, stdin=subprocess.DEVNULL, env=terminal.claude_env())
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)
        output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        if result.returncode == 0:
            return True, output
    return False, output


def visible_events(events: list[Event], show_tools: bool) -> list[Event]:
    return events if show_tools else [e for e in events if e.kind != "tool"]


def file_link(href: str, cwd: str) -> tuple[Path, int | None] | None:
    """The file and line a link points at, like the ones Claude writes in VS Code:
    `src/app.py#L42`, `D:/work/app.py:42`. None for web addresses and other schemes."""
    # A scheme (mailto:, vscode://), unless all that follows is a line number (app.py:42).
    # One letter before the colon is a drive.
    if re.match(r"[a-zA-Z][a-zA-Z0-9+.-]+:(?!\d+(?::\d+)?$)", href):
        return None
    target = unquote(href)
    line = None
    if match := re.search(r"(?:#L(\d+)(?:C\d+)?(?:-L?\d+(?:C\d+)?)?|:(\d+)(?::\d+)?)$", target):
        line = int(match.group(1) or match.group(2))
        target = target[: match.start()]
    target = target.split("#", 1)[0]
    if re.match(r"/[a-zA-Z]:", target):  # /d:/work/app.py
        target = target[1:]
    if not target:
        return None
    path = Path(target)
    return (path if path.is_absolute() or not cwd else Path(cwd) / path), line


def describe_copy(text: str) -> str:
    lines = text.count("\n") + 1
    if lines > 1:
        return f"{lines} lines"
    return f"“{text}”" if len(text) <= 60 else f"“{text[:59]}…”"


def continue_keys() -> str:
    """How History continues a past session, with your keys: `o continues it here, c in Claude Code`."""
    return f"{keys_mod.name('bring-here')} continues it here, {keys_mod.name('claude-code')} in Claude Code"


# Widgets --------------------------------------------------------------------------------


class Feed(VerticalScroll, can_focus=False):
    """A scrolling feed that follows new content unless you've scrolled up. Its text can be
    selected and copied, and its links clicked."""

    _pending: RenderableType | None = None

    def compose(self) -> ComposeResult:
        yield Selectable(classes="feed-body")  # the anchor keeps it at the end, in the same frame

    def on_mount(self) -> None:
        # Textual keeps an anchored feed at the end as it grows, in the same frame, until you
        # scroll up; scrolling back down to the end anchors it again.
        self.anchor()
        if self._pending is not None:
            self.show(self._pending)

    def show(self, renderable: RenderableType) -> None:
        try:
            body = self.query_one(".feed-body", Selectable)
        except NoMatches:
            self._pending = renderable  # a pane painted as it's mounted; shown once composed
            return
        self._pending = None
        body.show(renderable)


class SessionPane(Vertical, can_focus=True):
    def __init__(self, session_id: str) -> None:
        super().__init__(id="s-" + session_id)
        self.session_id = session_id
        self.data: LiveSession | None = None
        self.now: datetime | None = None
        self.space = ""
        self._feed_key: tuple | None = None
        # What's on screen now. Updating a widget costs a repaint, and a layout of the whole
        # screen for most, so paint() only touches what changed.
        self._shown: dict[str, object] = {}

    def _changed(self, part: str, value: object) -> bool:
        if part in self._shown and self._shown[part] == value:
            return False
        self._shown[part] = value
        return True

    def compose(self) -> ComposeResult:
        yield Static(classes="headline")
        yield Feed()
        yield Static(classes="subagents")

    def on_mount(self) -> None:
        self.paint()

    def on_click(self, event) -> None:
        if getattr(event, "chain", 1) == 2:
            self.screen.clear_selection()  # a double click selects the feed too; zooming is what you meant
            self.app.action_zoom()

    def on_focus(self, event) -> None:
        self.app.visited(self.session_id)  # type: ignore[attr-defined]

    def on_descendant_focus(self, event) -> None:
        self.app.visited(self.session_id)  # type: ignore[attr-defined]

    def show(self, data: LiveSession, now: datetime) -> None:
        self.data, self.now = data, now
        self.paint()

    def paint(self, force: bool = False) -> None:
        data, now = self.data, self.now
        if data is None or now is None:
            return
        try:
            headline = self.query_one(".headline", Static)
            subagents = self.query_one(".subagents", Static)
            feed = self.query_one(Feed)
        except NoMatches:
            return  # not composed yet; on_mount paints
        app: PanopsisApp = self.app  # type: ignore[assignment]
        state_class = STATE_CLASS[data.state if data.state not in NEEDS_YOU or app.needs_you(data) else OTHER]
        for cls in set(STATE_CLASS.values()):
            self.set_class(cls == state_class, cls)
        suffix = {"background": " · bg", "panopsis": " · here"}.get(data.kind, "")
        title = pane_place(data, self.space) + suffix
        if self._changed("title", title):
            self.border_title = title
        subtitle = f"{data.state} {ago(now - data.since)}" if data.since else data.state
        if self._changed("subtitle", subtitle):
            self.border_subtitle = subtitle
        shown_title = pane_title(data, self.space)  # depends on the space as well as the session
        if self._changed("headline", (shown_title, data.detail)):
            head = Text(shown_title, style="bold", no_wrap=True, overflow="ellipsis")
            head.append(f"\n{data.detail}", style="italic dim")
            headline.update(head, layout=False)  # fixed height: no need to lay out the screen again

        cfg = app.config
        last = data.feed[-1].at if data.feed else None
        key = (len(data.feed), last, sum(1 for e in data.feed if e.tag), app.view_mode, cfg.show_tools,
               cfg.show_times, app.palette)
        if force or key != self._feed_key:
            self._feed_key = key
            events = visible_events(data.feed, cfg.show_tools)
            if app.view_mode == "activity":
                feed.show(activity_log(events, app.palette, show_times=cfg.show_times))
            else:
                feed.show(conversation(events, app.palette, show_times=cfg.show_times))

        subagents.display = bool(data.subagents)
        rows = [(sub.description, f"last {sub.last_tool} " if sub.last_tool else "", ago(now - sub.last_active))
                for sub in data.subagents]
        previous = self._shown.get("subagents")
        if rows and self._changed("subagents", rows):
            lines = Text(no_wrap=True, overflow="ellipsis")
            for i, (description, last_tool, when) in enumerate(rows):
                lines.append(("\n" if i else "") + f"└ {description}  ", style="bold")
                lines.append(f"{last_tool}{when} ago", style="dim")
            # One line per subagent: only a different number of them changes the pane's layout.
            subagents.update(lines, layout=not isinstance(previous, list) or len(previous) != len(rows))


class DrivenPane(SessionPane):
    """A pane for a session panopsis runs itself: the feed plus a box to type into."""

    def compose(self) -> ComposeResult:
        yield Static(classes="headline")
        yield Feed()
        yield Static(classes="subagents")
        yield Composer(self.session_id)

    def paint(self, force: bool = False) -> None:
        super().paint(force)
        app: PanopsisApp = self.app  # type: ignore[assignment]
        driven = app.driven.get(self.session_id)
        if driven is not None:
            try:
                self.query_one(Composer).show(driven, app.palette)
            except NoMatches:
                pass


class SpacesTree(Tree[str]):
    """The spaces list. Its cursor is always on the space you're in, so up and down switch
    space as you go (like [ and ]); enter or right goes back to the sessions."""

    BINDINGS = [
        Binding("up", "app.space(-1, False)", "Switch space", key_display="↑↓"),
        Binding("down", "app.space(1, False)", show=False),
        Binding("enter", "app.focus_sessions", "Sessions"),
        Binding("right,escape", "app.focus_sessions", show=False),
    ]


class KeyFooter(Footer):
    """The footer, rebuilt only when the keys it shows change.

    Textual's rebuilds all its key widgets whenever focus moves, even when the keys stay the
    same (moving between panes, usually). That was a third of the time an arrow key took."""

    def __init__(self) -> None:
        super().__init__(show_command_palette=True)
        self._shown_keys: tuple | None = None
        # Show the keys from the first compose; Textual's composes an empty footer first.
        ready = getattr(Footer, "_bindings_ready", None)  # internal to Textual: skip if it's gone
        if ready is not None:
            self.set_reactive(ready, True)

    def _keys(self) -> tuple:
        return tuple((binding.key, binding.action, binding.description, enabled, tooltip)
                     for _, binding, enabled, tooltip in self.screen.active_bindings.values() if binding.show)

    def bindings_changed(self, screen: Screen) -> None:
        if not self.is_attached or screen is not self.screen:
            return
        if screen is not self.app.screen:
            return  # covered by another screen, whose keys are the ones that apply; this catches up on return
        if self._keys() != self._shown_keys:
            super().bindings_changed(screen)

    def compose(self) -> ComposeResult:
        self._shown_keys = self._keys()
        yield from super().compose()


class UsageMeter(Static):
    """Your plan's session and weekly limits, top right of the wall and of a zoomed-in session.
    Click (or U) for details."""

    def on_mount(self) -> None:
        self.paint()  # a zoom's is new each time; until the first check answers, the last launch's reading

    def paint(self) -> None:
        app: PanopsisApp = self.app  # type: ignore[assignment]
        self.update(usage_mod.meter(app.usage, datetime.now(timezone.utc), app.meter_colors))

    def on_click(self) -> None:
        self.app.action_usage()  # type: ignore[attr-defined]


class Activity(Static):
    """What panopsis is doing in the background, left of the usage meter: shown while a job
    runs, once it has for a moment (see panopsis.activity)."""

    def on_mount(self) -> None:
        self._width = 0
        self._timer = self.set_interval(SPIN_SECONDS, self.paint, pause=True)
        self.wake()

    def wake(self) -> None:
        """A job started: paint while there are any."""
        self._timer.resume()
        self.paint()

    def paint(self) -> None:
        app: PanopsisApp = self.app  # type: ignore[assignment]
        now = time.monotonic()
        if not app.jobs.running:
            self._timer.pause()
        jobs = app.jobs.shown(now)
        if self.display != bool(jobs):
            self.display = bool(jobs)
        if jobs:
            text = activity_mod.line(jobs, SPINNER[int(now / SPIN_SECONDS) % len(SPINNER)], now, app.palette.claude)
            # Twice a second or more: lay out the bar again only when the line's length changes.
            self.update(text, layout=text.cell_len != self._width)
            self._width = text.cell_len


class Sidebar(Vertical):
    def compose(self) -> ComposeResult:
        yield Label("SPACES", classes="sidebar-title")
        tree = SpacesTree("spaces", id="spaces")
        tree.show_root = False
        tree.guide_depth = 2
        yield tree


# Screens --------------------------------------------------------------------------------


class QuietFocusScreen(PacedScreen):
    """A screen that doesn't repaint all of itself whenever focus moves.

    Textual repaints the whole screen on every focus change, although the widgets whose
    look depends on focus restyle themselves anyway. On a big terminal that full repaint
    was most of the time an arrow key took on the wall."""

    focused: Reactive[Widget | None] = Reactive(None, repaint=False)


class WallScreen(QuietFocusScreen):
    """The wall of sessions: the app's first screen."""


class ZoomFeed(Feed, can_focus=True):
    """The zoomed conversation: focusable, so arrow keys and PgUp/PgDn scroll it. → moves over
    to the column beside it: the files the session changed, then its subagents."""

    BINDINGS = [Binding("right", "screen.focus_side", "Files, subagents", key_display="→")]


class SubagentScroll(VerticalScroll, can_focus=True):
    """The subagent map, under the changed files. ↑ ↓ scroll it, and ↑ from its top goes back
    up to the files."""

    BINDINGS = [Binding("left", "screen.focus_conversation", "Conversation", key_display="←")]

    def action_scroll_up(self) -> None:
        files = self.screen.query_one("#zoom-files", ChangedFiles)
        if self.scroll_y <= 0 and files.display:
            files.focus()
            files.action_last()  # coming up from below: the file just above
        else:
            super().action_scroll_up()


class ChangedFiles(OptionList):
    """The files a session changed, newest first; Enter opens one in your editor at the change.
    ↓ past the last one goes on down to the subagent map."""

    BINDINGS = [
        Binding("enter", "select", "Open in editor"),
        Binding("left", "screen.focus_conversation", "Conversation", key_display="←"),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(compact=True, **kwargs)
        self.cwd = ""
        self.changes: dict[str, FileChange] = {}

    def on_focus(self) -> None:
        if self.highlighted is None:
            self.action_first()  # so Enter opens one straight away
        self.screen.show_files_head()  # type: ignore[attr-defined]

    def on_blur(self) -> None:
        self.screen.show_files_head()  # type: ignore[attr-defined]

    def _enabled(self) -> list[int]:
        return [i for i, option in enumerate(self.options) if not option.disabled]

    def action_cursor_down(self) -> None:
        enabled = self._enabled()
        if enabled and self.highlighted == enabled[-1]:
            self.screen.action_focus_map()  # type: ignore[attr-defined]
        else:
            super().action_cursor_down()

    def action_cursor_up(self) -> None:
        enabled = self._enabled()
        if not enabled or self.highlighted != enabled[0]:
            super().action_cursor_up()
        else:
            self.scroll_home(animate=False)  # the top stays put, showing its label: the column doesn't wrap

    def show(self, changes: list[FileChange], cwd: str, since: datetime | None, show_times: bool) -> None:
        """`since` is your last message: what changed after it is listed first, under its own label."""
        current = self.highlighted_option.id if self.highlighted_option else None
        self.cwd = cwd
        self.changes = {c.path: c for c in changes}
        recent = [c for c in changes if since is not None and c.at >= since]
        earlier = changes[len(recent):]
        options: list[Option] = []
        if since is not None and recent:
            options.append(Option(Text("Since your last message", style="italic"), disabled=True))
        options += [self._option(c, show_times) for c in recent]
        if since is not None and earlier:
            options.append(Option(Text("Earlier", style="italic"), disabled=True))
        options += [self._option(c, show_times) for c in earlier]
        self.set_options(options)
        if current in self.changes:
            self.highlighted = self.get_option_index(current)

    def _option(self, change: FileChange, show_times: bool) -> Option:
        label = Text(short_path(change.path, self.cwd))
        if change.line:
            label.append(f":{change.line}", style="dim")
        extra = [f"×{change.edits}"] if change.edits > 1 else []
        if show_times:
            extra.append(f"{change.at.astimezone():%H:%M}")
        if extra:
            label.append("  " + " · ".join(extra), style="dim")
        if not Path(editor_mod.absolute(change.path, self.cwd)).exists():
            label.append("  not on disk", style="dim italic")  # deleted or moved since
        return Option(label, id=change.path)


class ZoomScreen(QuietFocusScreen):
    """One session at full size: the whole conversation in a readable column, and beside it
    the files it changed and a map of its subagents."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        bind("toggle-view", "app.toggle_view", "Conversation/activity"),
        bind("toggle-tools", "app.toggle_tools", "Tool calls"),
        bind("copy-reply", "app.copy_reply", "Copy reply"),
        bind("attach", "app.attach", "Attach (bg)"),
    ]

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id
        self._key: tuple | None = None
        self._changes_key: tuple | None = None
        self._head_text: tuple[Text, Text] | None = None
        self._subs_key: tuple | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="zoom-head"):
            with Horizontal(id="zoom-head-top"):
                yield Static(id="zoom-where")
                yield UsageMeter(id="zoom-usage")
            yield Static(id="zoom-title")
        with Horizontal(id="zoom-row"):
            yield ZoomFeed(id="zoom-main")
            with Vertical(id="zoom-subs"):
                yield Static(id="zoom-files-head")
                yield ChangedFiles(id="zoom-files")
                with SubagentScroll(id="zoom-map"):
                    yield Selectable(id="zoom-subs-body")
        if self.session_id in getattr(self.app, "driven", {}):
            yield Composer(self.session_id, id="zoom-composer")
        yield KeyFooter()

    def on_mount(self) -> None:
        # Fill in the conversation before the first frame rather than after it: one layout, no blank flash.
        session = self.app.sessions.get(self.session_id)  # type: ignore[attr-defined]
        if session is not None:
            self.show(session, datetime.now(timezone.utc))
            self.app.mark_read(session)  # type: ignore[attr-defined]
        composers = list(self.query(Composer))
        if composers:
            self.call_after_refresh(composers[0].focus_input)
        else:
            self.query_one("#zoom-main", ZoomFeed).focus()

    def show(self, data: LiveSession, now: datetime) -> None:
        try:
            where_widget = self.query_one("#zoom-where", Static)
            title_widget = self.query_one("#zoom-title", Static)
            main = self.query_one("#zoom-main", ZoomFeed)
            subs = self.query_one("#zoom-subs-body", Selectable)
        except NoMatches:
            return
        app: PanopsisApp = self.app  # type: ignore[assignment]
        driven = app.driven.get(self.session_id)
        for composer in self.query(Composer):
            if driven is not None:
                composer.show(driven, app.palette)
        shown_id = (driven.session_id or "starting") if driven else data.session_id
        since = f" for {ago(now - data.since)}" if data.since else ""
        where = Text(" · ".join(dict.fromkeys(p for p in (data.project, data.worktree, shown_id) if p)), style="dim")
        title = Text(data.title or "(untitled)", style="bold")
        title.append(f"   {data.state}{since}: {data.detail}", style="italic")
        if (where, title) != self._head_text:  # this runs on every streamed word
            self._head_text = (where, title)
            where_widget.update(where, layout=False)  # fixed size, beside the usage meter
            title_widget.update(title, layout=False)
        last = data.feed[-1].at if data.feed else None
        key = (len(data.feed), last, sum(1 for e in data.feed if e.tag), app.view_mode, app.config.show_tools,
               app.config.show_times, app.palette)
        if key != self._key:
            self._key = key
            events = visible_events(data.feed, app.config.show_tools)
            if app.view_mode == "activity":
                main.show(activity_log(events, app.palette, show_times=app.config.show_times, limit=300))
            else:
                main.show(conversation(events, app.palette, full=True, show_times=app.config.show_times,
                                       max_blocks=200))
        self._show_changes(data, app.config.show_times)
        agents = [*data.subagents, *data.past_subagents]
        running = any(a.running for a in agents)  # only then does the map move with the clock
        subs_key = (app.palette, now.replace(microsecond=0) if running else now.date(), tuple(data.main_spans),
                    tuple((a.id, a.description, a.agent_type, a.parent, a.started, a.last_active, a.running,
                           a.finished, len(a.spans)) for a in agents))
        if subs_key != self._subs_key:  # this runs on every streamed word
            self._subs_key = subs_key
            subs.show(SubagentMap(agents, data.main_spans, now, app.palette))

    def _show_changes(self, data: LiveSession, show_times: bool) -> None:
        files = self.query_one("#zoom-files", ChangedFiles)
        last_prompt = next((e.at for e in reversed(data.feed) if e.kind == "you"), None)
        key = (tuple((c.path, c.at, c.line, c.edits) for c in data.changes), last_prompt, data.cwd, show_times)
        if key == self._changes_key:
            return  # rebuilding the list on every refresh would lose your place in it
        self._changes_key = key
        files.display = bool(data.changes)
        if data.changes:
            files.show(data.changes, data.cwd, last_prompt, show_times)
        self.show_files_head()

    def show_files_head(self) -> None:
        """The list's heading, with the key that applies: → to get to it, then Enter to open one."""
        try:
            head = self.query_one("#zoom-files-head", Static)
            files = self.query_one("#zoom-files", ChangedFiles)
        except NoMatches:
            return  # the screen is closing
        if not files.display:
            head.update(Text("No file edits seen yet.", style="dim"))
            return
        # The screen's focus, not has_focus: Textual sets that after the list's own on_focus has run.
        hint = "Enter opens it in your editor" if self.focused is files else "→ to choose one"
        head.update(Text.assemble(("Changed files", "bold"), (f"   {hint}", "dim")))

    def action_focus_conversation(self) -> None:
        self.query_one("#zoom-main", ZoomFeed).focus()

    def action_focus_side(self) -> None:
        """The column beside the conversation: the files it changed, or its subagents if none."""
        files = self.query_one("#zoom-files", ChangedFiles)
        if files.display:
            files.focus()
        else:
            self.action_focus_map()

    def action_focus_map(self) -> None:
        self.query_one("#zoom-map", SubagentScroll).focus()

    @on(OptionList.OptionSelected, "#zoom-files")
    def _open_change(self, event: OptionList.OptionSelected) -> None:
        files = self.query_one("#zoom-files", ChangedFiles)
        change = files.changes.get(event.option.id or "")
        if change is not None:
            app: PanopsisApp = self.app  # type: ignore[assignment]
            app.open_in_code_editor(editor_mod.absolute(change.path, files.cwd), change.line)


class AnalysisScreen(QuietFocusScreen):
    """Base for screens built from full history: shows a loading state until it's ready."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Back"), bind("refresh", "reload", "Re-read history")]
    LOADING_TEXT = "Reading your session history…"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(classes="reading"):
            yield Static(self.LOADING_TEXT, id="loading-text")
            yield LoadingIndicator()
            yield from self.compose_body()
        yield KeyFooter()

    def compose_body(self) -> Iterable:
        yield Static(id="body", classes="reading-body")

    def on_mount(self) -> None:
        self._progress = self.set_interval(SPIN_SECONDS, self._paint_progress)  # until it's ready
        self.app.request_analysis(self._ready)  # type: ignore[attr-defined]

    def action_reload(self) -> None:
        self._loading(True)
        self.app.request_analysis(self._ready, force=True)  # type: ignore[attr-defined]

    def _loading(self, on: bool) -> None:
        for widget in self.query("#loading-text, LoadingIndicator"):
            widget.display = on
        if on:
            self._progress.resume()
        else:
            self._progress.pause()

    def _paint_progress(self) -> None:
        app: PanopsisApp = self.app  # type: ignore[assignment]
        job = app.jobs.get("history")
        if job is None:
            return
        text = Text(self.LOADING_TEXT)
        if job.total:
            fraction = job.fraction(time.monotonic()) or 0.0
            text.append(f"\n{activity_mod.bar(fraction, 30)} {job.done} of {job.total} transcripts", style="dim")
        if job.note:
            text.append(f"\n{job.note}", style="dim")
        self.query_one("#loading-text", Static).update(text)

    def _ready(self, data: Analysis) -> None:
        if not self.is_attached:  # closed while history was still loading
            return
        self._loading(False)
        self.populate(data)

    def populate(self, data: Analysis) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class ReportScreen(AnalysisScreen):
    TITLE = "Report"

    def populate(self, data: Analysis) -> None:
        body = self.query_one("#body", Static)
        if not data.sessions or data.profile is None:
            body.update("No sessions in your history yet.")
            return
        footer = (f"Why each rule matters: Rules ({keys_mod.name('rules')}). "
                  f"What to do about these: Advice ({keys_mod.name('advise')}).") if data.findings else ""
        body.update(Group(*report_parts(data.sessions, data.profile, data.findings, data.muted, data.stale,
                                        data.prefs.goals, hints=False, broken=data.broken, trend=data.trend,
                                        footer=footer)))


class ProfileScreen(AnalysisScreen):
    TITLE = "Your profile"
    BINDINGS = [bind("profile-edit", "edit", "Edit preferences")]

    def populate(self, data: Analysis) -> None:
        prefs = data.prefs
        stated = Text("\nWhat you told panopsis", style="bold")
        stated.append(f"   {prefs.path}\n", style="dim")
        if prefs.path and prefs.path.exists():
            stated.append(f"  goals: {', '.join(prefs.goals) or '-'}\n  style: {prefs.style or '-'}\n"
                          f"  muted: {', '.join(prefs.mute) or '-'}\n")
            for value in prefs.ignored:
                stated.append(f"  ignoring {value}\n", style="yellow")
        else:
            stated.append(f"  Nothing yet. Press {keys_mod.name('profile-edit')} to create your preferences file.\n")
        stated.append(
            "\nGoals rank findings (speed, cost, quality, focus, learning). A deliberate style "
            "(steering: close, for example) mutes rules that would nag you about it. Both change what "
            "panopsis tells you, not how Claude works.", style="dim")
        parts: list[RenderableType] = [style_panel(data.profile)] if data.profile else []
        self.query_one("#body", Static).update(Group(*parts, stated))

    def action_edit(self) -> None:
        self.app.edit_file(preferences.ensure_file())  # type: ignore[attr-defined]
        self.app.reevaluate_analysis(self._ready)  # type: ignore[attr-defined]


ADVICE_STEPS = 8  # of what Claude did, the latest, while it writes advice


class AdviseScreen(AnalysisScreen):
    TITLE = "Advice"
    BINDINGS = [bind("advice-new", "generate", "Get fresh advice")]
    LOADING_TEXT = "Preparing…"

    def compose_body(self) -> Iterable:
        yield Static(id="advice-meta", classes="reading-body")
        yield Markdown(id="advice", classes="reading-body")

    def populate(self, data: Analysis) -> None:
        self.data = data
        saved = advise.latest()
        meta = self.query_one("#advice-meta", Static)
        key = keys_mod.name("advice-new")
        if saved:
            meta.update(Text(f"Last advice, from {saved.at:%Y-%m-%d %H:%M}. Press {key} for fresh advice, which "
                             "follows up on this one (Claude searches the web for current practice first).", style="dim"))
            self.query_one("#advice", Markdown).update(saved.text)
        else:
            meta.update(Text(f"No advice yet. Press {key}: Claude reads your profile and findings, checks the "
                             "current Claude Code docs, and writes recommendations.", style="dim"))

    def action_generate(self) -> None:
        if not hasattr(self, "data") or self.data.profile is None:
            return
        app: PanopsisApp = self.app  # type: ignore[assignment]
        if app.jobs.get("advice") is not None:
            return  # Claude is on it already
        app.start_job("advice", "getting advice")
        self._loading(True)
        self._run_advice(self.data)

    def _ready(self, data: Analysis) -> None:
        app: PanopsisApp = self.app  # type: ignore[assignment]
        if app.jobs.get("advice") is not None:  # history read again while Claude writes: keep its steps showing
            self.data = data
            return
        super()._ready(data)

    def _paint_progress(self) -> None:
        app: PanopsisApp = self.app  # type: ignore[assignment]
        job = app.jobs.get("advice")
        if job is None:
            super()._paint_progress()
            return
        text = Text("Asking Claude for advice. It checks the current Claude Code docs first.")
        spinner = SPINNER[int(time.monotonic() / SPIN_SECONDS) % len(SPINNER)]
        shown = job.steps[-ADVICE_STEPS:]
        for i, step in enumerate(shown):
            last = i == len(shown) - 1  # what it's on now; the ones before, dimmed
            text.append(f"\n  {spinner if last else '·'} ", style=f"bold {app.palette.claude}" if last else "dim")
            text.append(step, style="" if last else "dim")
        self.query_one("#loading-text", Static).update(text)

    @work(thread=True, exclusive=True)
    def _run_advice(self, data: Analysis) -> None:
        app: PanopsisApp = self.app  # type: ignore[assignment]
        prompt = advise.build_prompt(data.profile, data.prefs, data.findings, data.rules, data.sessions,
                                     previous=advise.latest(), context=data.context)
        try:
            text = advise.run(prompt, on_step=lambda step: app.call_from_thread(app.job_step, "advice", step),
                              on_rate_limit=lambda windows: app.call_from_thread(app._rate_limit_seen, windows))
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            app.call_from_thread(app.finish_job, "advice")
            app.call_from_thread(app.notify, str(e), title="Advice failed", severity="error")
            app.call_from_thread(self._loading, False)
            return
        advise.save(text)
        app.call_from_thread(app.finish_job, "advice")
        app.call_from_thread(self._ready, data)


class RulesScreen(AnalysisScreen):
    TITLE = "Rules"
    BINDINGS = [bind("rules-mute", "toggle_mute", "Mute/unmute")]

    def compose_body(self) -> Iterable:
        yield DataTable(id="rules", cursor_type="row", zebra_stripes=True)
        yield Static(id="rule-detail", classes="reading-body")

    def populate(self, data: Analysis) -> None:
        self.data = data
        table = self.query_one("#rules", DataTable)
        if not table.columns:
            table.add_columns("rule", "applies to", "pack", "reviewed", "status", "findings")
        table.clear()
        stale = {r.id: age for r, age in data.stale}
        broken = {r.id for r, _ in data.broken}
        counts: dict[str, int] = {}
        for f in data.findings:
            counts[f.rule.id] = counts.get(f.rule.id, 0) + 1
        for rule in data.rules:
            status = []
            if rule.id in data.prefs.mute:
                status.append("muted")
            if rule.id in stale:
                status.append("possibly outdated")
            if rule.id in broken:
                status.append("unknown metric, never fires")
            table.add_row(rule.id, rule.applies_to, rule.pack, str(rule.reviewed), ", ".join(status) or "active",
                          str(counts.get(rule.id, "")), key=rule.id)
        table.focus()
        self._detail(data.rules[0].id if data.rules else None)

    @on(DataTable.RowHighlighted)
    def _highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._detail(event.row_key.value if event.row_key else None)

    def _detail(self, rule_id: str | None) -> None:
        rule = next((r for r in getattr(self, "data", None).rules if r.id == rule_id), None) if rule_id else None
        if rule is None:
            return
        text = Text(f"\n{rule.title}\n", style="bold")
        text.append(f"{rule.advice}\n", style="italic")
        text.append(f"source: {rule.source}  ·  reviewed {rule.reviewed}  ·  goals: {', '.join(rule.goals) or '-'}",
                    style="dim")
        missing = next((metrics for r, metrics in self.data.broken if r.id == rule.id), None)
        if missing:
            text.append(f"\nNever fires: no {rule.applies_to} metric called {', '.join(missing)}", style="red")
        self.query_one("#rule-detail", Static).update(text)

    def action_toggle_mute(self) -> None:
        table = self.query_one("#rules", DataTable)
        if table.row_count == 0:
            return
        rule_id = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        if rule_id in self.data.prefs.mute:
            preferences.remove_mute(rule_id)
        else:
            preferences.add_mute(rule_id)
        self.app.reevaluate_analysis(self._ready)  # type: ignore[attr-defined]


class HistoryScreen(QuietFocusScreen):
    """Past sessions, the latest started first. Reading every transcript whole took long, so only
    the sessions active in the window picked in the settings are read, and each shows as soon as
    it has been; m reaches further back. What's been read stays read until it changes."""

    TITLE = "History"
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("enter", "open", "Details"),
        bind("bring-here", "continue_here", "Continue here"),
        bind("claude-code", "continue_in_claude", "Open in Claude Code"),
        bind("history-older", "older", "Older sessions"),
        bind("refresh", "reload", "Re-read history"),
    ]
    COLUMNS = [("started", "started"), ("project", "project"), ("title", "title"), ("length", "length"),
               ("working", "working"), ("waiting on you", "waiting"), ("prompts", "prompts"),
               ("subagents", "subagents")]

    def __init__(self) -> None:
        super().__init__()
        self.days = 7  # how far back it reads: the setting's, until m reaches further
        self.sessions: dict[str, Session] = {}
        self.rows: list[dict] = []
        self._analysis: Analysis | None = None
        self._reading = 0  # which read is under way, so one that was replaced can't add its rows
        self.deleted: set[str] = set()  # sessions Claude Code has deleted, that panopsis kept

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(classes="reading"):
            yield Static(id="history-status")
            yield Input(placeholder="Filter by title or project…", id="history-filter")
            yield DataTable(id="history", cursor_type="row", zebra_stripes=True)
        yield KeyFooter()

    def on_mount(self) -> None:
        table = self.query_one("#history", DataTable)
        for label, key in self.COLUMNS:
            table.add_column(label, key=key)
        table.focus()
        self.days = self.app.config.history_days  # type: ignore[attr-defined]
        self._read_window()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        return bool(self.days) if action == "older" else True  # nothing older than all of it

    def action_older(self) -> None:
        self.days = min((d for d in config_mod.HISTORY_WINDOWS if d == 0 or d > self.days),
                        key=lambda d: d or float("inf"))
        self.refresh_bindings()
        self._read_window()

    def action_reload(self) -> None:
        self._read_window()  # what changed since; the rest comes from what was read

    def _window(self) -> str:
        if not self.days:
            return "all your history"
        return "the " + config_mod.HISTORY_WINDOWS.get(self.days, f"Past {self.days} days").lower()

    def _read_window(self) -> None:
        self._reading += 1
        self.sessions, self.rows, self._analysis, self.deleted = {}, [], None, set()
        self.query_one("#history", DataTable).clear()
        self.query_one("#history-status", Static).update(f"Reading the sessions active in {self._window()}…")
        since = datetime.now(timezone.utc) - timedelta(days=self.days) if self.days else None
        self._read(self.app, self._reading, since)

    @work(thread=True, exclusive=True, group="history")
    def _read(self, app: PanopsisApp, reading: int, since: datetime | None) -> None:
        paths = recent_transcripts(app.root, since)  # the latest written first: most likely what you want
        worker = get_current_worker()
        batch: list[Session] = []
        posted = float("-inf")  # the first at once
        for done, path in enumerate(paths, 1):
            if worker.is_cancelled:
                return
            session = app.session_cache.get(path)
            if session is not None and (since is None or session.end >= since):
                batch.append(session)
            if time.perf_counter() - posted > 0.1:  # a few times a second
                app.call_from_thread(self._add, reading, batch, f"{done} of {len(paths)}")
                batch, posted = [], time.perf_counter()
        # Then the ones only panopsis has now: Claude Code has deleted their transcripts.
        app.call_from_thread(self._add, reading, batch, None, app.session_cache.gone(app.root, since))

    def _add(self, reading: int, sessions: list[Session], progress: str | None,
             deleted: list[Session] | None = None) -> None:
        """Rows for sessions just read, and how far it got, or None once it's done."""
        if reading != self._reading or not self.is_attached:
            return  # a read that was replaced, or History was closed
        table = self.query_one("#history", DataTable)
        needle = self.query_one("#history-filter", Input).value.lower()
        self.deleted.update(s.id for s in deleted or [])
        for session in [*sessions, *(deleted or [])]:
            if session.id in self.sessions:
                continue  # a copy of a transcript already listed
            row = session_metrics(session)
            self.sessions[session.id] = session
            self.rows.append(row)
            if self._matches(row, needle):
                self._add_row(table, row, session.id in self.deleted)
        if sessions or deleted:
            self._analysis = None  # findings in the details come from the sessions read by then
            self._sort(table)
        status = self.query_one("#history-status", Static)
        if progress is not None:
            status.update(f"Reading the sessions active in {self._window()}… {progress}")
        elif self.days:
            status.update(f"{counted(len(self.rows), 'session')} active in {self._window()}. "
                          f"{keys_mod.name('history-older')} shows older ones; how far back History goes is in the "
                          f"settings ({keys_mod.name('settings')}).")
        else:
            status.update(f"{counted(len(self.rows), 'session')} in all your history.")

    @staticmethod
    def _matches(row: dict, needle: str) -> bool:
        return not needle or needle in row["title"].lower() or needle in row["project"].lower()

    @staticmethod
    def _add_row(table: DataTable, row: dict, deleted: bool = False) -> None:
        title = Text.assemble(row["title"], (" · deleted by Claude Code", "dim") if deleted else "")
        table.add_row(f"{row['start'].astimezone():%Y-%m-%d %H:%M}", row["project"], title,
                      fmt_min(row["wall_min"]), fmt_min(row["active_min"]), fmt_min(row["waiting_on_you_min"]),
                      str(row["human_prompts"]), str(row["subagents"] or ""), key=row["id"])

    def _sort(self, table: DataTable) -> None:
        """The latest started first. Rows come in as they're read; the one you're on stays yours."""
        key = self._cursor_key() if table.cursor_row > 0 else None
        table.sort("started", reverse=True)
        table.move_cursor(row=table.get_row_index(key) if key is not None else 0, animate=False)

    @on(Input.Changed, "#history-filter")
    def _filter(self, event: Input.Changed) -> None:
        table = self.query_one("#history", DataTable)
        table.clear()
        needle = event.value.lower()
        for row in self.rows:
            if self._matches(row, needle):
                self._add_row(table, row, row["id"] in self.deleted)
        self._sort(table)

    def _cursor_key(self) -> str | None:
        table = self.query_one("#history", DataTable)
        if not table.row_count:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def _selected_session(self) -> Session | None:
        """The session you're on, if it can be continued: Claude Code can't resume one it deleted."""
        key = self._cursor_key()
        if key in self.deleted:
            self.notify("Claude Code has deleted this session's transcript (by default it does 30 days after a "
                        "session was last active), so it can't continue it. panopsis kept what it found in it, "
                        "under Details.", title="Can't continue it")
            return None
        return self.sessions.get(key) if key is not None else None

    def _details(self, key: str) -> None:
        if self._analysis is None:
            self._analysis = analysis_mod.from_sessions(list(self.sessions.values()))
        self.app.push_screen(SessionDetailScreen(key, self._analysis))

    def action_continue_here(self) -> None:
        session = self._selected_session()
        if session is not None:
            self.app.pop_screen()
            self.app.open_driven(session.cwd, resume=session.id, title=session.title)  # type: ignore[attr-defined]

    def action_continue_in_claude(self) -> None:
        session = self._selected_session()
        if session is not None:
            self.app.handoff(session.id, session.cwd)  # type: ignore[attr-defined]

    @on(DataTable.RowSelected, "#history")
    def _selected(self, event: DataTable.RowSelected) -> None:
        self._details(event.row_key.value)

    def action_open(self) -> None:
        key = self._cursor_key()
        if key is not None:
            self._details(key)


class SessionDetailScreen(QuietFocusScreen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def __init__(self, session_id: str, data: Analysis) -> None:
        super().__init__()
        self.session_id, self.data = session_id, data

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(classes="reading"):
            yield Static(id="body", classes="reading-body")
        yield KeyFooter()

    def on_mount(self) -> None:
        session = self.data.session(self.session_id)
        if session is None:
            return
        rows = {r["id"]: r for r in self.data.entities["subagent"] if r["session_id"] == session.id}
        self.query_one("#body", Static).update(Group(*session_parts(session, rows, self.data.findings_for(session.id))))


class UsageScreen(PacedScreen):
    """Your plan's limits as meters, and what Claude Code says is using them (its /usage)."""

    TITLE = "Usage"
    BINDINGS = [Binding("escape", "app.pop_screen", "Back"), bind("refresh", "check", "Check now")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(classes="reading"):
            yield Static(id="body", classes="reading-body")
        yield KeyFooter()

    def on_mount(self) -> None:
        self.paint()
        app: PanopsisApp = self.app  # type: ignore[assignment]
        checked = app.usage.checked
        if checked is None or app.usage.last_run or datetime.now(timezone.utc) - checked > timedelta(minutes=1):
            app.refresh_usage()

    def action_check(self) -> None:
        self.app.refresh_usage()  # type: ignore[attr-defined]
        self.paint()

    def paint(self) -> None:
        app: PanopsisApp = self.app  # type: ignore[assignment]
        usage, now = app.usage, datetime.now(timezone.utc)
        text = Text()
        text.append("Plan usage\n", style="bold")
        if app.usage_checking:
            text.append("Asking Claude Code…\n", style="italic dim")
        windows = usage.ordered()
        width = max((len(w.title) for w in windows), default=0) + 2
        for w in windows:
            color = usage_mod.level_color(w, app.meter_colors)
            filled, empty = usage_mod.bar(w.used, 40)
            text.append(f"\n{w.title.ljust(width)}", style="bold")
            text.append(filled, style=color)
            text.append(empty, style="dim")
            text.append(f"  {'limit reached' if w.status == 'rejected' else f'{w.used:.0%} used'}",
                        style=f"bold {color}")
            when = usage_mod.resets_text(w, now)
            if when:
                text.append(f"  resets {when}", style="dim")
        if not windows and not app.usage_checking:
            text.append("\nNo plan limits to show." if usage.checked else "\nNot checked yet.", style="dim")
        if usage.error:
            text.append(f"\n\nCouldn't ask Claude Code: {usage.error}", style=app.palette.error)
        if usage.notes:
            text.append("\n\n")
            for line in usage.notes:
                heading = line.strip().endswith("?") or line.startswith("Last ")
                text.append(line + "\n", style="bold" if heading else "")
        checked = f"Checked {usage.checked.astimezone():%H:%M}. " if usage.checked else ""
        if usage.checked and usage.last_run:
            checked = f"These are from when panopsis last ran ({usage.checked.astimezone():%b %d, %H:%M}). "
        every = (f"panopsis asks again every {app.config.usage_minutes:g} min (usage_minutes in the settings "
                 "file; 0 turns the meter off)" if app.config.usage_minutes > 0 else
                 "The meter is off (usage_minutes: 0 in the settings file)")
        text.append(f"\n{checked}From your own Claude Code's /usage: no model call, nothing from your sessions "
                    f"is sent. {every}, and sessions panopsis runs update the session meter as they work. "
                    f"{keys_mod.name('refresh')} checks now.", style="dim")
        try:
            self.query_one("#body", Static).update(text)
        except NoMatches:
            pass


class MoveToSpaceScreen(PacedScreen, ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, spaces: list[str]) -> None:
        super().__init__()
        self.session_title, self.spaces = title, spaces

    def compose(self) -> ComposeResult:
        with Vertical(id="move-dialog"):
            yield Static(Text.assemble("Move ", (self.session_title or "this session", "bold"), " to a space"))
            yield OptionList(*self.spaces, id="move-options")
            yield Input(placeholder="…or type a new space name and press Enter", id="move-new")

    @on(OptionList.OptionSelected)
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.prompt))

    @on(Input.Submitted, "#move-new")
    def _typed(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


def _sessions_in(analysis: Analysis) -> list[tuple[str, datetime]]:
    """What a read of history found, to tell whether reading it again changed anything."""
    return [(s.id, s.end) for s in analysis.sessions]


# App ------------------------------------------------------------------------------------


class PanopsisApp(App):
    TITLE = "panopsis"
    AUTO_FOCUS = "SessionPane"  # start on the grid, not the sidebar
    CSS = (
        """
        #body-row { height: 1fr; }
        Sidebar { width: 30; border-right: solid $panel-lighten-1; padding: 0 1; }
        Sidebar.hidden { display: none; }
        .sidebar-title { color: $text-muted; text-style: bold; padding: 1 0 0 1; }
        #spaces { background: transparent; }
        #main { width: 1fr; }
        #summary { height: 1; padding: 0 1; background: $panel; link-color: $al-waiting; }
        /* The one link in an alert is its Open button. */
        Toast { link-background: $primary; link-color: $text; link-style: bold;
                link-background-hover: $primary-lighten-2; link-color-hover: $text; link-style-hover: bold; }
        #topbar { height: 1; background: $panel; }
        #topbar #summary { width: 1fr; }
        #usage-meter { width: auto; height: 1; padding: 0 1; }
        #activity { width: auto; max-width: 60%; height: 1; padding: 0 1; display: none; }
        #grid { layout: grid; grid-size: 2; grid-gutter: 0 1; padding: 0 1; }
        SessionPane { border: round $panel-lighten-2; padding: 0 1; height: 1fr; min-height: 8; }
        SessionPane .headline { height: 2; text-wrap: nowrap; text-overflow: ellipsis; }
        SessionPane Feed { height: 1fr; scrollbar-size-vertical: 1; }
        SessionPane .subagents { height: auto; max-height: 4; text-wrap: nowrap; text-overflow: ellipsis; }
        #empty, #looking { padding: 2 4; color: $text-muted; }
        #zoom-head { height: 2; padding: 0 2; background: $panel; }
        #zoom-head-top { height: 1; }
        #zoom-where { width: 1fr; text-wrap: nowrap; text-overflow: ellipsis; }
        #zoom-usage { width: auto; height: 1; padding-left: 2; }
        #zoom-title { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
        #zoom-main { width: 2fr; padding: 1 2; align-horizontal: center; border-right: solid $panel-lighten-1; }
        #zoom-main .feed-body { max-width: 104; }
        #zoom-subs { width: 1fr; padding: 1 2; }
        #zoom-files { max-height: 16; margin-bottom: 1; background: transparent; }
        #zoom-map { height: 1fr; scrollbar-size-vertical: 1; }
        #zoom-map:focus { background-tint: $foreground 5%; }  /* as the files list shows it has the keys */
        .reading { align-horizontal: center; padding: 1 2; }
        .reading-body { max-width: 110; }
        #loading-text { color: $text-muted; max-width: 110; }
        LoadingIndicator { height: 3; }
        #rules { max-height: 24; }  /* leaves room for the rule's details below */
        #history { height: 1fr; }
        #history-status { color: $text-muted; margin-bottom: 1; }
        #history-filter { max-width: 60; margin-bottom: 1; }
        MoveToSpaceScreen { align: center middle; }
        /* Dialogs keep their width, or the terminal's where that's narrower, so their text wraps inside. */
        #keys-dialog { width: 84; max-width: 100%; height: 90%; padding: 1 2; border: round $accent;
                       background: $surface; }
        #keys-list { height: 1fr; margin: 1 0; background: transparent; scrollbar-size-vertical: 1; }
        #keys-list > .option-list--option-disabled { color: $foreground; }  /* keys you can't change read alike */
        KeyCaptureScreen { align: center middle; }
        #capture-dialog { width: 64; max-width: 100%; height: auto; padding: 1 2; border: round $accent;
                          background: $surface; }
        #capture-dialog .capture-ask { margin-top: 1; }
        #capture-note { height: auto; margin-top: 1; }
        SettingsScreen { align: center middle; }
        #settings-dialog { width: 84; max-width: 100%; height: auto; max-height: 90%; padding: 1 2;
                           border: round $accent; background: $surface; scrollbar-size-vertical: 1; }
        #settings-dialog .settings-section { margin-top: 1; text-style: bold; }
        #settings-dialog .settings-row { height: auto; }
        #settings-dialog .settings-row Label { width: 16; color: $text-muted; }
        #settings-dialog .settings-row Select, #settings-dialog .settings-row Input { width: 1fr; }
        #settings-dialog .settings-row Checkbox { margin-right: 1; }
        #settings-dialog .settings-note { color: $text-muted; padding-left: 16; }
        #settings-buttons { height: auto; margin-top: 1; }
        #settings-buttons Button { margin-right: 2; }
        #move-dialog { width: 64; max-width: 100%; height: auto; max-height: 24; padding: 1 2; border: round $accent;
                       background: $surface; }
        #move-options { height: auto; max-height: 12; margin: 1 0; }
        .composer { height: auto; }
        .composer .request { height: auto; max-height: 20; margin-top: 1; }
        .composer-input { margin-top: 1; }
        /* While Claude works: the activity line, right above the box. */
        .composer-status { display: none; height: 1; margin-top: 1; text-wrap: nowrap; text-overflow: ellipsis; }
        .composer.working .composer-status { display: block; }
        .composer.working .composer-input { margin-top: 0; }
        .composer-images { height: 1; margin-top: 1; text-wrap: nowrap; text-overflow: ellipsis; }
        DrivenPane .request { max-height: 9; }
        .command-menu { height: auto; max-height: 10; background: $surface; }
        DrivenPane .command-menu { max-height: 7; }
        .composer-hint { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
        #zoom-row { height: 1fr; }
        #zoom-composer { padding: 0 2 1 2; max-height: 50%; }
        NewSessionScreen { align: center middle; }
        #new-dialog { width: 80; max-width: 100%; height: auto; padding: 1 2; border: round $accent;
                      background: $surface; }
        #new-dialog Input, #new-dialog Select { margin-top: 1; }
        #new-error { height: auto; }
        #new-folder-row { height: auto; }
        #new-folder-row Input { width: 1fr; }
        #new-browse { margin: 1 0 0 1; }
        FolderPickerScreen { align: center middle; }
        #folder-dialog { width: 90; max-width: 100%; height: 80%; padding: 1 2; border: round $accent;
                         background: $surface; }
        #folder-bar, #folder-buttons { height: auto; margin-top: 1; }
        #folder-bar Button, #folder-buttons Button { margin-right: 1; }
        #folder-drive { width: 14; }
        #folder-tree { height: 1fr; margin-top: 1; border: round $panel; }
        #folder-chosen { height: auto; margin-top: 1; }
        """
        + _state_css()
    )
    # The footer shows the keys for the focused session (Move to space among them), then
    # Needs you (while a session does), New, Settings, All keys and Quit, with Commands (ctrl+p)
    # at its right end; the rest are on the keys screen (?) and in the command palette.
    # Their keys are in keys.py, where you can change them (? lists them, Enter on one changes it).
    BINDINGS = [
        bind("compose", "compose", "Type"),
        bind("interrupt", "interrupt", "Stop turn"),
        bind("bring-here", "bring_here", "Bring here"),
        bind("zoom", "zoom", "Zoom"),
        bind("open-folder", "open_folder", "Editor"),
        bind("detach", "detach", "Detach"),
        bind("close", "end_session", "Close"),
        bind("move-to-space", "move_to_space", "Move to space"),
        bind("jump", "jump", "Needs you"),
        bind("new-session", "new_session", "New"),
        bind("settings", "settings", "Settings"),
        bind("show-keys", "show_keys", "All keys"),
        bind("quit", "quit", "Quit"),
        # Textual's own palette key, named for what it does; the footer puts it at its right end.
        Binding("ctrl+p", "command_palette", "Commands", show=False, priority=True,
                tooltip="Search every command by name"),
        bind("claude-code", "handoff_focused", "Claude Code", show=False),
        bind("copy-reply", "copy_reply", "Copy reply", show=False),
        Binding("e", "end_session", show=False),
        Binding("ctrl+w", "end_session", show=False),
        *[Binding(str(n), f"choose({n})", show=False) for n in range(1, 10)],
        bind("toggle-filter", "toggle_filter", "Needs you", show=False),
        bind("toggle-view", "toggle_view", "View", show=False),
        bind("report", "report", "Report", show=False),
        bind("advise", "advise", "Advise", show=False),
        bind("history", "history", "History", show=False),
        bind("profile", "profile", "Profile", show=False),
        bind("rules", "rules", "Rules", show=False),
        bind("usage", "usage", "Usage", show=False),
        bind("toggle-sidebar", "toggle_sidebar", "Sidebar", show=False),
        bind("toggle-tools", "toggle_tools", "Tool calls", show=False),
        bind("space-prev", "space(-1)", "Prev space", show=False),
        bind("space-next", "space(1)", "Next space", show=False),
        bind("columns-more", "columns(1)", "+ column", show=False),
        bind("columns-fewer", "columns(-1)", "- column", show=False),
        bind("attach", "attach", "Attach", show=False),
        bind("refresh", "refresh", "Refresh", show=False),
        bind("move-left", "move(-1, 0)", show=False),
        bind("move-right", "move(1, 0)", show=False),
        bind("move-up", "move(0, -1)", show=False),
        bind("move-down", "move(0, 1)", show=False),
    ]

    def __init__(
        self,
        provider: Callable[[], list[dict]] = open_sessions,
        root: Path = DEFAULT_ROOT,
        interval: float | None = None,
        project: str | None = None,
        config: config_mod.Config | None = None,
    ) -> None:
        super().__init__()
        for theme in look_themes():
            self.register_theme(theme)
        self.provider = provider
        self.root = root
        self.tails = TranscriptTails(root)
        self.config = config or config_mod.load()
        keys_mod.use(self.config.keys)
        self.set_keymap(self.config.keys)  # the keys you changed, over the bindings' own
        self.interval = interval or self.config.refresh_seconds
        self.project = project
        self.sessions: dict[str, LiveSession] = {}
        self.space_of: dict[str, str] = {}
        self.current_space = self.config.last_space or ALL
        self.only_needs_you = False
        self.view_mode = "conversation"
        self.palette = Palette()
        self.analysis: Analysis | None = None
        # Sessions read whole, for History and the report screens; kept between launches too.
        self.session_cache = SessionCache(SessionStore())
        self._analysis_waiters: list[Callable[[Analysis], None]] = []
        # Screens showing history read a while ago, for when it's read again: told only if it changed.
        self._stale_waiters: list[Callable[[Analysis], None]] = []
        # Until the wall is filled in the first time. Only the report screens need it, and turning
        # them on in the settings reads it then.
        self._preload_due = PRELOAD_SECONDS is not None and self.config.findings_and_advice
        self.jobs = activity_mod.Jobs()  # what it's doing in the background, for the line at the top
        self._seen_states: dict[str, str] = {}
        self._alerts: dict[str, Notification] = {}  # sessions whose alert you haven't followed yet: its toast
        self.read = ReadSessions()  # sessions needing you that you've read: they don't count until they next do
        self._needing: frozenset[str] = frozenset()  # sessions that need you, as the footer last saw them
        self._space_nodes: dict[str, TreeNode[str]] = {}
        self.driven: dict[str, DrivenSession] = {}  # sessions panopsis runs itself, by pane key
        self.driven_space: dict[str, str] = {}  # space a driven session was started in
        self._dirty: set[str] = set()
        self._monitored: list[LiveSession] = []
        # Transcript tails of the sessions panopsis runs, by session id: their subagents and changed files.
        self._driven_tails: dict[str, Session | None] = {}
        self.client_factory = default_client_factory  # tests swap in a fake Claude Code
        self._usage_timer = None  # asks /usage every usage_minutes
        self.launch_editor = editor_mod.launch  # tests swap in a recorder
        self._last_key: str | None = None
        self._restored = False
        # Your plan's limits, from /usage and sessions' rate-limit events. Until the first check answers
        # (seconds, where starting `claude` is slow), the meter shows the last launch's, dimmed.
        self.usage = (usage_mod.load_cached(datetime.now(timezone.utc)) if self.config.usage_minutes > 0
                      else usage_mod.Usage())
        self.usage_checking = False
        self._usage_timed = False  # the first check's time is in the startup timings
        self.meter_colors = ("#0178D4", "#FEA62B", "#B93C5B")  # normal, nearly full, full; set from the theme
        self.catalog = commands_mod.load_cached()  # slash commands for boxes whose session hasn't started
        self._summary_text: Text | None = None
        self._empty_text = ""  # what the wall says while it shows no panes
        self._loads = 0  # refreshes started, so a listing can be told apart from one fetched earlier
        self._listing = False  # a refresh is under way
        self._refresh_due = False  # and another came due meanwhile
        # Sessions panopsis is closing, kept off the wall: None while Claude Code shuts down, then
        # the number of refreshes started by the time it had.
        self._closing_sessions: dict[str, int | None] = {}
        self._flush_timer = None
        self._last_flush = 0.0
        self._painted: dict[str, float] = {}  # when each session panopsis runs was last repainted

    def get_default_screen(self) -> Screen:
        return WallScreen(id="_default")

    def get_driver_class(self) -> type[Driver]:
        return terminal.fit_glyphs_to_console(super().get_driver_class())

    @property
    def _background_screens(self) -> list[Screen]:
        # The screens that show through the one on top. Textual always counts the one beneath,
        # though a screen like the zoom is opaque and covers it: every word streamed into a pane
        # on the wall then repainted all of the zoom, twenty times a second with three replies
        # coming in, and kept you waiting as you typed. A screen hidden like that is brought up
        # to date when it shows again.
        stack = self._screen_stack
        if stack and stack[-1].styles.background.a == 1:
            return []
        return super()._background_screens

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body-row"):
            yield Sidebar(classes="" if self.config.sidebar else "hidden")
            with Vertical(id="main"):
                with Horizontal(id="topbar"):
                    yield Static(id="summary")
                    yield Activity(id="activity")
                    yield UsageMeter(id="usage-meter")
                with Grid(id="grid"):
                    yield Static("Looking for your open Claude Code sessions…", id="looking")
        yield KeyFooter()

    def on_mount(self) -> None:
        if self.config.theme in self.available_themes:
            self.theme = self.config.theme
        self.refresh_css(animate=False)  # apply the colours from the settings file
        self._update_palette()
        self._warn_bad_colors()
        self._warn_key_clashes()
        self.theme_changed_signal.subscribe(self, self._theme_changed)
        self.call_after_refresh(timings.mark, "wall drawn")
        self.action_refresh()
        self.set_interval(self.interval, self.action_refresh)
        self._schedule_usage()  # the first check comes with the first listing: see _load
        self.set_interval(30, self._paint_usage)  # "resets in 2h 10m" counts down
        self.set_timer(10, self._forget_old_sessions)  # once the wall is busy with more urgent things

    def on_resize(self, event: events.Resize) -> None:
        self._hide_cursor()

    def _hide_cursor(self) -> None:
        """Hide the cursor again in Windows' own console window, which shows it when resized."""
        if terminal.classic_console() and self._driver is not None:
            self._driver.write(terminal.HIDE_CURSOR)

    WALL_ONLY = {"zoom", "toggle_filter", "space", "columns", "toggle_sidebar", "move"}
    WALL_OR_ZOOM = {"move_to_space", "new_session", "handoff_focused", "open_folder", "copy_reply"}
    DRIVEN_ONLY = {"compose", "interrupt", "choose", "detach"}
    FINDINGS_AND_ADVICE = {"report", "advise", "profile", "rules"}  # experimental: the settings turn them on
    TYPED_KEY_ACTIONS = {
        "quit", "zoom", "new_session", "compose", "interrupt", "handoff_focused", "detach", "end_session",
        "bring_here", "choose", "move_to_space", "toggle_filter", "toggle_view", "report", "advise", "history",
        "profile", "rules", "toggle_sidebar", "toggle_tools", "space", "columns", "attach", "refresh", "move",
        "show_keys", "open_folder", "copy_reply", "usage", "jump", "settings",
    }

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """Hide keys from the footer (and disable them) on screens where they mean nothing."""
        if isinstance(self.focused, Input) and action in self.TYPED_KEY_ACTIONS:
            return False  # in a text box these keys type; the box's hint says to press Esc first
        if action in self.FINDINGS_AND_ADVICE:
            return self.config.findings_and_advice
        if action == "jump":  # on every screen but a dialog, while a session you're not in needs you
            here = self._focused_session(strict=True)
            elsewhere = self._needing - {here.session_id} if here else self._needing
            return bool(elsewhere) and not isinstance(self.screen, ModalScreen)
        on_wall = self.screen is self.screen_stack[0]
        in_zoom = isinstance(self.screen, ZoomScreen)
        if action in self.WALL_ONLY:
            return on_wall
        if action in self.WALL_OR_ZOOM:
            return on_wall or in_zoom
        if action in ("end_session", "bring_here", "attach"):
            focused = self._focused_session(strict=True) if (on_wall or in_zoom) else None
            if focused is None:
                return False
            if action in ("bring_here", "attach"):
                return focused.kind == "background"
            return True  # close works on every pane: ends, stops, or hides one in a terminal or an editor
        if action in self.DRIVEN_ONLY:
            driven = self._focused_driven(strict=True) if (on_wall or in_zoom) else None
            if driven is None:
                return False
            if action == "choose":
                return driven.pending is not None
            if action == "interrupt":
                return driven.state in BUSY
            return True
        return True

    # Settings ---------------------------------------------------------------------------

    def _save_config(self) -> None:
        try:
            config_mod.save(self.config)
        except OSError as e:
            self.notify(f"Couldn't save settings: {e}", severity="error")

    def get_css_variables(self) -> dict[str, str]:
        variables = super().get_css_variables()
        config = getattr(self, "config", None)  # Textual asks before __init__ has loaded it
        resolved, self._bad_colors = resolve_colors(config.colors if config else {}, variables)
        return {**variables, **resolved}

    def _warn_bad_colors(self) -> None:
        if getattr(self, "_bad_colors", None):
            self.notify("Those aren't colours, so the defaults are used: " + "; ".join(self._bad_colors),
                        title="Check colors in the settings file", severity="warning", timeout=10)

    def _warn_key_clashes(self) -> None:
        """Keys edited in the file that two actions share: Textual gives the key to only one."""
        clashes = [f"{keys_mod.show(key)} for {a.id} and {b.id}" for key, a, b in keys_mod.clashes(self.config.keys)]
        if clashes:
            self.notify("Two actions have the same key, so it does only one of them: " + "; ".join(clashes)
                        + f". Change one in the keys list ({keys_mod.name('show-keys')}).",
                        title="Check keys in the settings file", severity="warning", timeout=10)

    def _update_palette(self) -> None:
        variables = self.get_css_variables()
        self.palette = palette_from(variables, self.current_theme.dark)

        def pick(name: str, default: str) -> str:
            value = variables.get(name, "")
            return value[:7] if value.startswith("#") else default

        self.meter_colors = (pick("primary", "#0178D4"), pick("warning", "#FEA62B"), pick("error", "#B93C5B"))

    def _theme_changed(self, theme) -> None:
        self.config.theme = theme.name
        self._save_config()
        self._update_palette()
        self._repaint()

    def change_setting(self, name: str, value: object) -> None:
        """Save a setting picked in the settings screen, and show it straight away."""
        if name == "theme":
            self.theme = str(value)  # _theme_changed saves it
            return
        setattr(self.config, name, value)
        self._save_config()
        if name == "colors":
            self._apply_colors()
        elif name == "columns":
            self._layout()
        elif name == "sidebar":
            self._show_sidebar()
        elif name in ("show_tools", "show_times"):
            self._repaint()
        elif name == "history_keep_days":
            self._forget_old_sessions()
        elif name == "usage_minutes":
            self._schedule_usage()
            if self.config.usage_minutes > 0:
                self.refresh_usage()
        elif name == "keys":
            self._use_keys()
        elif name == "findings_and_advice" and value:
            self.preload_history()  # so their screens open at once

    def change_keys(self, changes: dict[str, str]) -> None:
        """Keys changed in the keys list (?), by action id: saved like a setting."""
        self.change_setting("keys", keys_mod.changed(self.config.keys, changes))

    def _use_keys(self) -> None:
        """The keys you picked, on every binding and in every hint that names one."""
        keys_mod.use(self.config.keys)
        self.set_keymap(self.config.keys)
        self._repaint()
        for screen in self.screen_stack:
            for composer in screen.query(Composer):
                composer.update_menu(composer.query_one(Input).value)  # its hint names a key

    def _apply_colors(self) -> None:
        self.refresh_css()
        self._update_palette()
        self._repaint()

    def _repaint(self) -> None:
        for pane in self.query(SessionPane):
            pane.paint(force=True)
        for screen in self.screen_stack:  # also under a dialog, like the settings screen
            if isinstance(screen, ZoomScreen) and screen.session_id in self.sessions:
                screen._key = None
                screen.show(self.sessions[screen.session_id], datetime.now(timezone.utc))

    # Live data ---------------------------------------------------------------------------

    def action_refresh(self) -> None:
        self._hide_cursor()  # also after a resize the console didn't report
        if self._listing:
            # One at a time. Where listing and reading take longer than the interval, a worker
            # thread can't be stopped, so they piled up and slowed each other down.
            self._refresh_due = True
            return
        self._listing, self._refresh_due = True, False
        self._loads += 1
        if self._loads == 1:
            self.start_job("sessions", "reading your sessions", paced=True)
        else:  # shows only where a refresh is slow
            self.start_job("refresh", "refreshing your sessions")
        self._load(self._loads)

    @work(thread=True, exclusive=True, group="live")
    def _load(self, generation: int = 0) -> None:
        try:
            self._list_and_read(generation)
        finally:
            self.call_from_thread(self._loaded)

    def _loaded(self) -> None:
        self._listing = False
        self.finish_job("sessions")
        self.finish_job("refresh")
        if self._refresh_due:  # one came due while this one ran: it starts now, with what's new
            self.action_refresh()

    def _list_and_read(self, generation: int) -> None:
        now = datetime.now(timezone.utc)
        try:
            entries = self.provider()
        except (RuntimeError, OSError, ValueError) as e:
            self.call_from_thread(self._listing_failed, str(e))
            return
        finally:
            if generation == 1 and self.config.usage_minutes > 0:
                # Not before: where starting a program is slow, a second `claude` holds up the listing.
                self.call_from_thread(self.refresh_usage)
        if generation == 1:
            # A long session's full tail takes a while to read, so the wall first shows the panes from
            # less of each transcript, then fills in the rest: older conversation, files, subagents.
            quick = TranscriptTails(self.root, QUICK_TAIL_BYTES)
            sessions, driven_tails = self._read_sessions(entries, quick, now)
            timings.mark(f"transcripts' ends read, {counted(len(entries), 'session')}")
            self.call_from_thread(self._apply, sessions, now, driven_tails, generation, first_look=True)
            if get_current_worker().is_cancelled:
                return  # a newer refresh is reading them in full already
            self.call_from_thread(self.job_step, "sessions", "filling in the rest")
        sessions, driven_tails = self._read_sessions(entries, self.tails, now)
        if generation == 1:
            timings.mark(f"transcripts read in full, {counted(len(entries), 'session')}")
        self.call_from_thread(self._apply, sessions, now, driven_tails, generation)

    def _read_sessions(self, entries: list[dict], tails: TranscriptTails,
                       now: datetime) -> tuple[list[LiveSession], dict[str, Session | None]]:
        sessions = [derive(e, *tails.get(e["sessionId"]), now=now) for e in entries]
        if self.project:
            sessions = [s for s in sessions if self.project.lower() in s.project.lower()]
        # The SDK doesn't report a session's subagents or the files it changed, so read them from its transcript.
        driven_ids = [d.session_id for d in list(self.driven.values()) if d.session_id]
        return sessions, {sid: tails.get(sid)[0] for sid in driven_ids}

    def _listing_failed(self, error: str) -> None:
        self._stop_looking()
        self.notify(error, title="Can't list sessions", severity="error")

    def _stop_looking(self) -> None:
        """The first listing is in, or failed: the wall isn't waiting for it any more."""
        for widget in self.screen_stack[0].query("#looking"):  # also under a dialog
            widget.remove()

    def _apply(self, monitored: list[LiveSession], now: datetime,
               driven_tails: dict[str, Session | None] | None = None, generation: int | None = None,
               first_look: bool = False) -> None:
        self._monitored = monitored
        if driven_tails is not None:
            self._driven_tails = driven_tails
        if generation is not None:
            self._stop_looking()
            # A listing fetched after a session finished closing no longer has it.
            self._closing_sessions = {sid: closed for sid, closed in self._closing_sessions.items()
                                      if closed is None or closed >= generation}
        if not self._restored:
            self._restored = True
            self._restore_driven(monitored)
            timings.mark(f"restored {counted(len(self.driven), 'session')} panopsis ran last time")
        self._adopt_names(monitored)
        # A session panopsis runs also shows up as an open session; keep only the driven pane.
        # One it's closing does too, until its Claude Code has exited and a fresh listing is in.
        driven_ids = {d.session_id for d in self.driven.values() if d.session_id}
        hidden = {*self.config.hidden_sessions, *self._closing_sessions}
        sessions = [s for s in monitored if s.session_id not in driven_ids and s.session_id not in hidden]
        sessions += [self._driven_live(d, now) for d in self.driven.values()]
        # Stable order: by when the session started, so panes don't jump around.
        sessions.sort(key=lambda s: (s.started or now, s.name))
        self.space_of = {s.session_id: self._space_for(s) for s in sessions}
        grid = self.query_one("#grid", Grid)
        panes = {pane.session_id: pane for pane in grid.query(SessionPane)}
        current = {s.session_id for s in sessions}
        gone = [pane for session_id, pane in panes.items() if session_id not in current]
        replacement = self._focus_past(gone)
        for pane in gone:
            pane.remove()
        for s in sessions:
            pane = panes.get(s.session_id)
            if pane is None:
                pane = DrivenPane(s.session_id) if s.session_id in self.driven else SessionPane(s.session_id)
                grid.mount(pane)
            if s.session_id == replacement:
                # At once, not with focus(), which waits: the wall coming back from a zoom gives an
                # unfocused wall's first pane focus, and that's the session beside this one.
                self.screen_stack[0].set_focus(pane)
            pane.space = self.space_of[s.session_id]
            pane.show(s, now)
            if not first_look:  # read from less of the transcript, a state can still change on the full read
                self._notify_if_needed(s)
        self.sessions = {s.session_id: s for s in sessions}
        for sid in [sid for sid in self._alerts if sid not in self.sessions]:
            self._drop_alert(sid)
        if generation is not None and not first_look:  # a fresh listing, read in full
            self.read.keep_only(self.sessions)
        self._update_sidebar()
        self._apply_visibility()
        if isinstance(self.screen, ZoomScreen) and self.screen.session_id in self.sessions:
            self.screen.show(self.sessions[self.screen.session_id], now)
        if generation == 1:
            self.call_after_refresh(timings.mark, "sessions on the wall" if first_look else "all of them filled in")
        if generation is not None and not first_look and self._preload_due:
            self._preload_due = False
            if PRELOAD_SECONDS:
                self.set_timer(PRELOAD_SECONDS, self.preload_history)
            else:
                self.call_later(self.preload_history)

    def _focus_past(self, gone: list[SessionPane]) -> str | None:
        """Before panes come off the wall, move focus from the one you're in to the pane that takes
        its place (the one before it if it was last), so you're still on the wall. Left to Textual,
        focus goes to what's before it in the focus chain: the box of the session next to it.

        A session brought here from the background stays the one you're in, in the pane panopsis
        runs for it: its key comes back, to focus once that pane is on the wall. Moving to the one
        beside it would take that one's alert as seen, and a zoom opened again would show it."""
        screen = self.screen_stack[0]  # the wall's, also while a dialog or zoom is on top
        focused = screen.focused
        leaving = next((p for p in gone if focused is not None and p in focused.ancestors_with_self), None)
        if leaving is None:
            return None
        replacement = next((d.key for d in self.driven.values() if d.session_id == leaving.session_id), None)
        if replacement is not None:
            screen.set_focus(None)
            return replacement
        panes = self._visible_panes()
        index = panes.index(leaving) if leaving in panes else len(panes)
        after = [p for p in panes[index + 1:] if p not in gone]
        before = [p for p in panes[:index] if p not in gone]
        screen.set_focus(after[0] if after else before[-1] if before else None)
        return None

    def _space_for(self, s: LiveSession) -> str:
        driven = self.driven.get(s.session_id)
        if driven is not None:
            return self.driven_space.get(s.session_id) or self.config.space_for(
                driven.session_id or s.session_id, s.cwd, s.title)
        return self.config.space_for(s.session_id, s.cwd, s.title)

    # Sessions panopsis runs ---------------------------------------------------------------

    def open_driven(self, cwd: str, *, prompt: str = "", images: list | None = None, mode: str | None = None,
                    resume: str | None = None, title: str = "", force: bool = False, start: bool = True,
                    apply: bool = True, focus: bool = True) -> DrivenSession | None:
        if resume and not force and resume in {s.session_id for s in self._monitored}:
            self.notify("That session is open somewhere else (a terminal or an editor). Close it there first, "
                        "so two clients don't write to it at once.", title="Already open", severity="warning")
            return None
        if not cwd or not Path(cwd).is_dir():
            self.notify(f"Folder not found: {cwd or '(unknown)'}", severity="error")
            return None
        driven = DrivenSession(
            cwd, resume=resume, permission_mode=mode or self.config.default_mode,
            on_change=self._driven_changed, on_rate_limit=self._rate_limit_seen, cli_path=self.config.cli_path,
            client_factory=self.client_factory,
        )
        driven.title = title
        self.driven[driven.key] = driven
        if space := self._made_space():
            self.driven_space[driven.key] = space
        if resume:
            known, past = self._past(resume)
            if known is not None:
                driven.title = known.title if known.title != "(untitled)" else title
                driven.worktree = known.worktree
            if start:
                driven.events = past
            else:
                driven.park(past)
            self._remember(driven)
        self.config.last_folder = cwd
        self._save_config()
        if start:
            self.run_worker(driven.start(prompt or None, images), group="driven", exclusive=False)
        if apply:
            self._apply(self._monitored, datetime.now(timezone.utc))
        if focus:
            self.call_after_refresh(self._focus_key, driven.key)
        return driven

    def _past(self, session_id: str) -> tuple[Session | None, list[Event]]:
        """The session and its conversation so far, from its transcript, so a resumed pane isn't
        blank and has the title Claude Code gave it."""
        path = next(iter(self.root.glob(f"*/{session_id}.jsonl")), None)
        if path is None:
            return None, []
        session, events = load_tail(path, TAIL_BYTES)
        return session, events.get("main", [])

    def _adopt_names(self, monitored: list[LiveSession]) -> None:
        """Claude Code titles the sessions panopsis runs (when asked: see DrivenSession._name),
        and lists them as open sessions: take their title and worktree from there, and keep the
        title for next launch."""
        listed = {s.session_id: s for s in monitored}
        for driven in self.driven.values():
            seen = listed.get(driven.session_id or "")
            if seen is None:
                continue
            driven.worktree = seen.worktree
            if seen.title and seen.title != driven.title:
                driven.title = seen.title
                if driven.remembered:
                    self.config.remember_driven(driven.session_id, driven.cwd, driven.title, driven.permission_mode)
                    self._save_config()

    def _remember(self, driven: DrivenSession) -> None:
        if driven.cleared:
            # /clear started a new conversation in this pane: bring that one back next time.
            for old in driven.cleared:
                self.config.forget_driven(old)
                if space := self.config.unassign(old):
                    self.driven_space[driven.key] = space  # and keep it in the space you moved it to
            driven.cleared = []
            driven.remembered = False
            self._save_config()
        if driven.session_id and not driven.remembered:
            self.config.remember_driven(driven.session_id, driven.cwd, driven.title, driven.permission_mode)
            driven.remembered = True
            self._save_config()
        elif driven.remembered and self.config.retitle_driven(driven.session_id or "", driven.title):
            self._save_config()  # saved under your first message, before Claude Code titled it

    def _restore_driven(self, monitored: list[LiveSession]) -> None:
        """Bring back last time's panopsis sessions, paused, unless they're now open elsewhere."""
        open_elsewhere = {s.session_id for s in monitored}
        for saved in list(self.config.driven_sessions):
            session_id, cwd = saved["session_id"], saved["cwd"]
            if session_id in open_elsewhere or not Path(cwd).is_dir():
                self.config.forget_driven(session_id)
                continue
            self.open_driven(cwd, resume=session_id, title=saved.get("title", ""), mode=saved.get("mode") or None,
                             force=True, start=False, apply=False, focus=False)
        self._save_config()

    def _driven_live(self, driven: DrivenSession, now: datetime) -> LiveSession:
        live = as_live(driven, now)
        tail = self._driven_tails.get(driven.session_id or "")
        live.subagents, live.past_subagents = subagent_lists(tail, now)
        live.main_spans = tail.main.spans if tail else []
        live.changes = file_changes(tail)
        return live

    def _focus_key(self, key: str) -> None:
        for pane in self.query(SessionPane):
            if pane.session_id == key:
                pane.focus()

    def _driven_changed(self, driven: DrivenSession) -> None:
        self._mark_dirty(driven.key)

    def _mark_dirty(self, key: str) -> None:
        """Show a change to a session panopsis runs: on the next frame if nothing was shown just
        now (your message, an answered prompt), else with the next batch, so a streaming reply
        repaints a few times a second rather than on every word."""
        self._dirty.add(key)
        if self._flush_timer is None:
            wait = max(0.001, self._last_flush + FLUSH_SECONDS - time.monotonic())  # Textual's timers need > 0
            self._flush_timer = self.set_timer(wait, self._flush_when_due)

    def _flush_when_due(self) -> None:
        self._flush_timer = None
        self._flush_driven()

    def _flush_driven(self) -> None:
        if self._flush_timer is not None:  # called directly, ahead of the timer
            self._flush_timer.stop()
            self._flush_timer = None
        clock = self._last_flush = time.monotonic()
        if not self._dirty or not self.is_running:  # not while quitting: the screens may be gone
            return
        # The session you're on repaints every time. With many replies streaming at once, the
        # others repaint less often, so the wall keeps answering keys; a change of state (a
        # question, a permission prompt) still shows at once.
        looking_at = self.screen.session_id if isinstance(self.screen, ZoomScreen) else self._last_key
        others_every = FLUSH_SECONDS * max(1.0, len(self._dirty) / STREAMING_PANES)
        keys = {key for key in self._dirty
                if key == looking_at or clock - self._painted.get(key, 0.0) >= others_every
                or key not in self.driven or self.driven[key].state != getattr(self.sessions.get(key), "state", None)}
        self._dirty -= keys
        if self._dirty:
            self._flush_timer = self.set_timer(FLUSH_SECONDS, self._flush_when_due)
        for key in keys:
            self._painted[key] = clock
        now = datetime.now(timezone.utc)
        panes = {p.session_id: p for p in self.query(SessionPane)}
        for key in keys:
            driven = self.driven.get(key)
            if driven is None:
                continue
            live = self._driven_live(driven, now)
            self._remember(driven)
            if driven.catalog is not None and driven.catalog is not self.catalog:
                self.catalog = driven.catalog  # the freshest list, for boxes whose session hasn't started
                commands_mod.save_cached(driven.catalog)
            if key in self.driven_space and driven.session_id:
                # Remember a hand-picked space once Claude Code has given the session its id.
                space = self.driven_space.pop(key)
                self.config.assign(driven.session_id, space)
                self._save_config()
            if key in panes:
                panes[key].show(live, now)
            self.sessions[key] = live
            self._notify_if_needed(live)
            if isinstance(self.screen, ZoomScreen) and self.screen.session_id == key:
                self.screen.show(live, now)
        self._update_sidebar()
        self._summary()

    def _focused_driven(self, strict: bool = False) -> DrivenSession | None:
        session = self._focused_session(strict)
        return self.driven.get(session.session_id) if session else None

    def driven_has_request(self, key: str) -> bool:
        driven = self.driven.get(key)
        return driven is not None and driven.pending is not None

    def answer_request(self, key: str, number: int) -> None:
        driven = self.driven.get(key)
        p = driven.pending if driven else None
        if p is None:
            return
        if p.kind == "permission":
            if number == 1:
                driven.allow()
            elif number == 2:
                driven.allow(always=True)
            elif number == 3:
                driven.deny()
        elif p.kind == "question":
            driven.choose(number)
        elif p.kind == "plan":
            if number == 1:
                driven.approve_plan()
            elif number == 3:
                self.action_compose()  # type what should change; Enter sends it
        self._mark_dirty(key)

    def composer_submit(self, key: str, text: str) -> None:
        driven = self.driven.get(key)
        if driven is None:
            return
        p = driven.pending
        local = commands_mod.local_command(text)
        if p is None and local is not None:
            # Commands that mean something else here than in a terminal: panopsis does them.
            if local.name == "usage":
                self.action_usage()
            elif local.name == "exit":
                self._close_driven(driven)
            return
        if p is None:
            if text.strip() or driven.images:
                self.run_worker(driven.send(text), group="driven", exclusive=False)
        elif p.kind == "question":
            driven.answer(text)
        elif p.kind == "plan":
            driven.deny(text.strip() or "Keep planning.")
        else:
            driven.deny(text.strip())
        self._mark_dirty(key)

    def paste_image(self, key: str, otherwise: Callable[[], None] | None = None) -> None:
        """Take the image on the clipboard into a session's box, for its next message. With
        nothing there, `otherwise` runs (Ctrl+V's text paste), or you're told why not."""
        driven = self.driven.get(key)
        if driven is not None:
            self.paste_image_onto(driven.images, otherwise, then=lambda: self._mark_dirty(driven.key))

    def paste_image_onto(self, images: list, otherwise: Callable[[], None] | None = None,
                         then: Callable[[], None] = lambda: None) -> None:
        """Add the image on the clipboard to `images` and call `then`; as paste_image when there's none."""
        self.run_worker(self._paste_image(images, otherwise, then), group="paste", exclusive=True)

    async def _paste_image(self, images: list, otherwise: Callable[[], None] | None,
                           then: Callable[[], None]) -> None:
        try:
            image = await asyncio.to_thread(clipboard.paste_image)
        except clipboard.NoImage as e:
            if otherwise is not None:
                otherwise()
            else:
                self.notify(str(e), title="No image to paste", timeout=5)
            return
        images.append(image)
        then()

    def drop_image(self, key: str) -> bool:
        """Backspace in an empty box: take the last pasted image off. False if there was none."""
        driven = self.driven.get(key)
        if driven is None or not driven.images:
            return False
        driven.images.pop()
        self._mark_dirty(key)
        return True

    def command_catalog(self, key: str) -> commands_mod.Catalog:
        """The slash commands for a session's box: its own Claude Code's list once it has started."""
        driven = self.driven.get(key)
        catalog = driven.catalog if driven is not None and driven.catalog is not None else self.catalog
        return catalog.with_local()

    def action_choose(self, number: int) -> None:
        driven = self._focused_driven()
        if driven is not None:
            self.answer_request(driven.key, number)

    def action_compose(self) -> None:
        driven = self._focused_driven()
        if driven is None:
            return
        scope = self.screen if isinstance(self.screen, ZoomScreen) else self
        for composer in scope.query(Composer):
            if composer.key == driven.key:
                composer.focus_input()
                return

    def action_interrupt(self) -> None:
        driven = self._focused_driven()
        if driven is not None:
            self.run_worker(driven.interrupt(), group="driven", exclusive=False)

    # Ending, detaching, bringing back ------------------------------------------------------

    def _claude(self) -> str | None:
        return self.config.cli_path or shutil.which("claude")

    def action_end_session(self) -> None:
        """Close a session you're done with, like closing a chat in your editor. Conversations stay in
        History. A session panopsis runs ends; a background one stops; one open in a terminal or
        an editor is hidden from the wall (it stays open there). It always asks first: one stray
        key would do it."""
        session = self._focused_session()
        if session is None:
            return
        driven = self.driven.get(session.session_id)
        if driven is not None:
            body = (("Claude is in the middle of a turn; closing stops it. " if driven.state in BUSY else "")
                    + f"The conversation stays in History: {continue_keys()}.")
        elif session.kind == "background":
            body = (f"It stops working; the conversation is kept, and {keys_mod.name('bring-here')} brings it here "
                    f"or {keys_mod.name('attach')} opens it in Claude Code.")
        else:
            body = ("It stays open in its terminal or editor; this only takes it off the wall. "
                    "Ctrl+P > Show hidden sessions brings it back.")

        def chosen(choice: str | None) -> None:
            if not choice:
                return
            if driven is not None:
                self.run_worker(self._end_driven(driven), group="driven", exclusive=False)
            elif session.kind == "background":
                self.run_worker(self._stop_background(session), group="driven", exclusive=False)
            else:
                self._hide_session(session)

        title = f"Close {session.label}?" if session.label else "Close this session?"
        self.push_screen(ChoiceScreen(title, body, {keys_mod.current("close"): "close it"}), chosen)

    def _hide_session(self, session: LiveSession) -> None:
        self.config.hidden_sessions.append(session.session_id)
        self._save_config()
        self._apply(self._monitored, datetime.now(timezone.utc))
        self.notify("It's still open in its terminal or editor; close it there to end it. "
                    "Ctrl+P > Show hidden sessions brings it back here.", title="Hidden from the wall",
                    timeout=6)

    def _close_driven(self, driven: DrivenSession) -> None:
        if driven.state in BUSY:
            self.push_screen(
                ChoiceScreen("Close this session?", "Claude is in the middle of a turn; closing stops it. "
                             "The conversation stays in History.", {keys_mod.current("close"): "close it"}),
                lambda choice: choice and self.run_worker(self._end_driven(driven), group="driven",
                                                          exclusive=False))
        else:
            self.run_worker(self._end_driven(driven), group="driven", exclusive=False)

    def action_show_hidden(self) -> None:
        count = len(self.config.hidden_sessions)
        self.config.hidden_sessions = []
        self._save_config()
        self._apply(self._monitored, datetime.now(timezone.utc))
        self.notify(f"{count} hidden session{'s' if count != 1 else ''} back on the wall", timeout=3)

    async def _end_driven(self, driven: DrivenSession, quiet: bool = False) -> None:
        if isinstance(self.screen, ZoomScreen) and self.screen.session_id == driven.key:
            self.pop_screen()
        # Off the wall right away: Claude Code can take a moment to shut down.
        session_id = driven.session_id
        if session_id:
            self._closing_sessions[session_id] = None
        self.driven.pop(driven.key, None)
        self._painted.pop(driven.key, None)
        self.config.forget_driven(session_id)
        self._save_config()
        self._apply(self._monitored, datetime.now(timezone.utc))
        if not quiet:
            self.notify(f"It's in History: {continue_keys()}.", title="Session closed", timeout=5)
        try:
            if driven.state in BUSY:
                await driven.interrupt()
            await driven.close()
        finally:
            if session_id:
                self._closing_sessions[session_id] = self._loads

    async def _stop_background(self, session: LiveSession) -> None:
        ok, output = await asyncio.to_thread(stop_background, self._claude(), session.session_id, session.name,
                                             session.job_id)
        if ok:
            self.notify("The conversation is kept.", title=f"Stopped {session.label}", timeout=5)
        else:
            self.notify(output or "claude stop failed", title="Couldn't stop it", severity="error")
        self.action_refresh()

    def action_detach(self) -> None:
        """Hand the session to Claude Code's background service so it keeps working without panopsis."""
        driven = self._focused_driven()
        if driven is None:
            return
        if not driven.session_id:
            self.notify("This session hasn't started yet.", timeout=3)
            return
        working = driven.state in BUSY
        in_git = (Path(config_mod.repo_root(driven.cwd)) / ".git").exists()

        def chosen(result: dict | None) -> None:
            if not result:
                return
            if result["action"] == "trust":
                self.run_worker(self._trust_then_detach(driven, result["instruction"]), group="driven",
                                exclusive=False)
            else:
                self.run_worker(self._detach(driven, result["instruction"]), group="driven", exclusive=False)

        self.push_screen(DetachScreen(driven.title, working, in_git, cwd=driven.cwd,
                                      trust=folder_trust(driven.cwd), home=is_home(driven.cwd)), chosen)

    async def _trust_then_detach(self, driven: DrivenSession, instruction: str) -> None:
        """Open terminal Claude Code in the folder so you can accept its trust prompt, then detach."""
        self.notify("Accept the trust prompt, then type /exit to come back.", title="Trusting the folder",
                    timeout=6)
        self.handoff(None, driven.cwd)
        if folder_trust(driven.cwd):
            await self._detach(driven, instruction)
        else:
            self.notify("The folder still isn't trusted, so the session stays here.", title="Not detached",
                        severity="warning", timeout=8)

    async def _detach(self, driven: DrivenSession, instruction: str) -> bool:
        session_id, cwd, mode, title = driven.session_id, driven.cwd, driven.permission_mode, driven.title
        # Claude Code continues a session in place only if nothing else has it open.
        await self._end_driven(driven, quiet=True)
        if not instruction:
            self.notify("Continue it any time: History (o here, c in Claude Code) or your editor.",
                        title="Released", timeout=6)
            return True
        ok, output = await asyncio.to_thread(background_resume, self._claude(), session_id, cwd, mode, instruction)
        if ok:
            self.notify(output, title="Detached: it keeps working in the background", timeout=8)
        else:
            self.notify(output or "Claude Code couldn't take it over.", title="Couldn't detach, so it stays here",
                        severity="error", timeout=15)
            self.open_driven(cwd, resume=session_id, title=title, mode=mode, force=True)
        self.action_refresh()
        return ok

    def action_bring_here(self) -> None:
        """Take a background session back into panopsis so you can type into it here."""
        session = self._focused_session()
        if session is None or session.kind != "background":
            return
        if session.state in BUSY:
            self.push_screen(
                ChoiceScreen("Bring it here?", "It's working in the background; bringing it here stops that turn "
                             "first. The conversation carries over.",
                             {keys_mod.current("bring-here"): "bring it here"}),
                lambda choice: choice and self.run_worker(self._bring_here(session), group="driven",
                                                          exclusive=False))
        else:
            self.run_worker(self._bring_here(session), group="driven", exclusive=False)

    async def _bring_here(self, session: LiveSession) -> None:
        # Stopping takes a few seconds and the new pane looks like the old one, so say what's happening.
        name = session.label
        self.notify("Stopping its background run, then opening it here…", title=f"Bringing {name} here", timeout=4)
        ok, output = await asyncio.to_thread(stop_background, self._claude(), session.session_id, session.name,
                                             session.job_id)
        if not ok:
            self.notify(output or "claude stop failed", title=f"Couldn't bring {name} here", severity="error")
            return
        zoomed = isinstance(self.screen, ZoomScreen) and self.screen.session_id == session.session_id
        if zoomed:
            self.pop_screen()  # that pane is about to be replaced by the one panopsis runs
        driven = self.open_driven(session.cwd, resume=session.session_id, title=session.title, force=True)
        if driven is None:
            return  # open_driven has said why
        self.notify(f"Press {keys_mod.name('compose')} to type to it.", title=f"{name} is here now", timeout=6)
        if zoomed:  # into this one by name, not into whichever pane has focus by then
            self.call_after_refresh(self._zoom_into, driven.key)

    def _zoom_into(self, key: str) -> None:
        if key in self.sessions and not isinstance(self.screen, ZoomScreen):
            self.push_screen(ZoomScreen(key))

    def action_new_session(self) -> None:
        made = self._made_space()

        def space(folder: str, prompt: str) -> str:
            if made:
                return made
            return self.config.space_for("", folder, prompt) if folder and Path(folder).is_dir() else ""

        def chosen(result: dict | None) -> None:
            if result:
                self.config.default_mode = result["mode"]
                self.open_driven(result["cwd"], prompt=result["prompt"], images=result["images"], mode=result["mode"])

        self.push_screen(NewSessionScreen(self._new_session_folder(), self.config.default_mode, space), chosen)

    def _new_session_folder(self) -> str:
        """Where a new session works unless you change it: the folder of the session you're on, if
        it's in the space you're in, else the space's own folder or one of its sessions', open or
        closed."""
        here = [s for sid, s in self.sessions.items() if self.current_space in (ALL, self.space_of.get(sid))]
        focused = self._focused_session()
        if focused is not None and focused.session_id not in {s.session_id for s in here}:
            focused = None  # the pane you were last in, in another space: its folder would point there
        folders = [focused.cwd if focused else ""]
        if self.current_space != ALL:
            made = next((s for s in self.config.spaces if s.name == self.current_space), None)
            folders += [p for p in made.paths if Path(p).is_dir()] if made else []
            folders += [s.cwd for s in here]
            if not any(folders):  # the last folder you started one in would be another space's
                folders.append(self._closed_space_folder(made))
        return next((f for f in folders if f), None) or self.config.last_folder or str(Path.home())

    def _closed_space_folder(self, made: config_mod.Space | None) -> str:
        """Where the space you're in worked, with none of its sessions open: in the repository it's
        named after, if it is, whatever sessions from other folders you moved into it. Else, as one
        you made in the app has no folder, only those sessions, where they last worked."""
        for folder in worked_in_named(self.root, self.current_space):
            if config_mod.repo_name(folder) == self.current_space:
                repo = config_mod.repo_root(folder)
                if Path(repo).is_dir():
                    return repo
        picked = worked_in(self.root, made.sessions) if made else iter(())
        return next((f for f in picked if Path(f).is_dir()), "")

    def action_handoff_focused(self) -> None:
        """Open the selected session in full Claude Code, in this terminal; quitting it comes back here."""
        session = self._focused_session()
        if session is None:
            return
        driven = self.driven.get(session.session_id)
        if driven is not None:
            if not driven.session_id:
                self.notify("This session hasn't started yet.", timeout=3)
                return
            self.run_worker(self._handoff_driven(driven), group="driven", exclusive=False)
            return
        if session.kind == "background":
            self.action_attach()
            return
        self.notify("This session is open in a terminal or an editor; switch to it there.", title=session.label)

    async def _handoff_driven(self, driven: DrivenSession) -> None:
        if isinstance(self.screen, ZoomScreen):
            self.pop_screen()
        await driven.close()  # one client at a time on a session
        self.driven.pop(driven.key, None)
        self.handoff(driven.session_id, driven.cwd)
        self.open_driven(driven.cwd, resume=driven.session_id, title=driven.title, mode=driven.permission_mode)

    def handoff(self, session_id: str | None, cwd: str) -> None:
        if session_id and session_id in {s.session_id for s in self._monitored}:
            self.notify("That session is open somewhere else; switch to it there.", severity="warning")
            return
        claude = self.config.cli_path or shutil.which("claude")
        if not claude:
            self.notify("`claude` CLI not found on PATH", severity="error")
            return
        cmd = [claude, "--resume", session_id] if session_id else [claude]
        with self.suspend():
            subprocess.run(cmd, cwd=cwd if cwd and Path(cwd).is_dir() else None)
            terminal.set_title()  # Claude Code named the tab while you were in it
        self.action_refresh()

    # Your editor ------------------------------------------------------------------------

    def action_open_folder(self) -> None:
        """Open the selected session's repository or worktree in your editor."""
        session = self._focused_session()
        if session is None:
            return
        if not session.cwd:
            self.notify("This session hasn't said which folder it works in yet.", timeout=3)
            return
        self.open_in_code_editor(editor_mod.project_folder(session.cwd))

    def open_in_code_editor(self, path: str, line: int | None = None) -> None:
        """A file at a line, or a folder, in the editor picked on the settings screen. A windowed
        one opens it beside panopsis; one in the terminal takes over the screen until you quit it."""
        if not Path(path).exists():
            # Given a path that isn't there, VS Code starts a new file by that name and keeps it
            # as unsaved work, back in a window of its own every time it starts.
            self.notify(f"{path} isn't on disk any more: deleted or moved since.", title="Not opened",
                        severity="warning", timeout=6)
            return
        chosen = editor_mod.resolve(self.config.editor, self.config.editor_terminal)
        if chosen is None and not self.config.editor and not Path(path).is_dir():
            chosen = editor_mod.fallback()  # Notepad, or vi: a file can still be read
        if chosen is None:
            problem = (editor_mod.describe(self.config.editor, self.config.editor_terminal) if self.config.editor
                       else "No editor found to open a folder with.")
            self.notify(f"{problem} Press {keys_mod.name('settings')} to pick your editor.", title="No editor",
                        severity="warning", timeout=8)
            return
        self._run_editor(chosen, chosen.command(path, line))

    def edit_file(self, path: Path) -> None:
        """A settings file in your editor, waited on until you close it, so what you changed is
        read straight back. If your editor can't be found, Notepad or vi, to put that right."""
        chosen = editor_mod.resolve(self.config.editor, self.config.editor_terminal) or editor_mod.fallback()
        if chosen is None:
            self.notify(f"Nothing found to open {path} with. Press {keys_mod.name('settings')} to pick your editor.",
                        title="No editor", severity="warning")
            return
        self._run_editor(chosen, chosen.command(str(path), wait=True), wait=True)

    def _run_editor(self, chosen: editor_mod.Chosen, command: list[str], wait: bool = False) -> None:
        try:
            if chosen.terminal or wait:
                with self.suspend():
                    subprocess.run(command)
            else:
                self.launch_editor(command)
        except SuspendNotSupported:
            self.notify(f"{chosen.editor.label} needs this terminal, and panopsis can't step aside from it "
                        f"here. Press {keys_mod.name('settings')} to pick a windowed editor.", title="No editor",
                        severity="warning")
        except OSError as e:
            self.notify(f"Couldn't start {chosen.editor.label}: {e}", title="No editor", severity="error")

    async def action_quit(self) -> None:
        busy = [d for d in self.driven.values() if d.state in BUSY]
        if not busy:
            await self._close_all_and_exit()
            return

        def chosen(choice: str | None) -> None:
            if choice == "d":
                self.run_worker(self._detach_all_and_exit(busy), group="driven", exclusive=False)
            elif choice == "s":
                self.run_worker(self._close_all_and_exit(), group="driven", exclusive=False)

        count = f"{len(busy)} session{'s are' if len(busy) > 1 else ' is'}"
        self.push_screen(ChoiceScreen(
            f"{count} still working", "Detach hands them to Claude Code's background service so they keep "
            "working while panopsis is closed. Stop ends their current turn; they come back here, paused, "
            "next time you open panopsis.",
            {"d": "detach and quit", "s": "stop and quit"}), chosen)

    async def _close_all_and_exit(self) -> None:
        # They stay remembered: the next launch brings them back, paused.
        sessions = list(self.driven.values())
        if any(d.connected for d in sessions):
            self.notify("Waiting for Claude Code to shut down…", title="Closing sessions", timeout=30)
        # Together: each one waits for its Claude Code to exit.
        await asyncio.gather(*(d.close() for d in sessions), return_exceptions=True)
        self.exit()

    async def _detach_all_and_exit(self, busy: list[DrivenSession]) -> None:
        results = [await self._detach(d, "Continue where you left off.") for d in busy]
        if all(results):
            await self._close_all_and_exit()
        # Otherwise stay open: the failed ones were reopened here and the error is on screen.

    def _notify_if_needed(self, s: LiveSession) -> None:
        before = self._seen_states.get(s.session_id)
        self._seen_states[s.session_id] = s.state
        if s.state not in NEEDS_YOU:
            self._drop_alert(s.session_id)  # answered, here or wherever it's open
        focused = self._focused_session()
        if focused is not None and focused.session_id == s.session_id:
            # You're already looking at it: no alert. Zoomed in, it's read too. On the wall, the pane with
            # focus isn't always the one you're reading, so it needs you until you open it.
            if isinstance(self.screen, ZoomScreen) and self.screen.session_id == s.session_id:
                self.mark_read(s)
            return
        # A session that has just connected (brought here, continued from History) is ready, not
        # waiting on an answer; its pane takes focus a moment later.
        if before is not None and before not in NEEDS_YOU and before != STARTING and s.state in NEEDS_YOU:
            what = {QUESTION: "has a question", APPROVAL: "has a plan to approve"}.get(s.state, "is waiting on you")
            space = self.space_of.get(s.session_id, "")
            # A click anywhere on an alert dismisses it; one on its button also opens the session.
            toast = Notification(f"{escape(pane_place(s, space))}\n"
                                 f"[@click=app.open_session({s.session_id!r})] Open it [/]  "
                                 f"[dim]or press {escape(keys_mod.name('jump'))}[/dim]",
                                 title=f"{pane_title(s, space)} {what}", severity="warning", timeout=15, markup=True)
            self._alerts[s.session_id] = toast  # kept, so following the alert takes it down (notify doesn't return it)
            self.post_message(Notify(toast))
            self.bell()

    def needs_you(self, s: LiveSession) -> bool:
        """Whether it waits on you, and you haven't read it since it started to."""
        return s.state in NEEDS_YOU and not self.read.has_read(s.session_id, s.since)

    def mark_read(self, s: LiveSession) -> None:
        """You've had this session in front of you while it needs you: it stops counting as needing
        you, and its pane dims, until it next starts to."""
        if not self.needs_you(s):
            return
        self.read.mark(s.session_id, s.since)
        for pane in self.screen_stack[0].query(SessionPane):  # the wall's, also from a zoom
            if pane.session_id == s.session_id:
                pane.paint()
        self.call_later(self._show_read)  # once a refresh under way has the wall's sessions in

    def _show_read(self) -> None:
        # Not _apply_visibility: from a zoom, its focus fallback would move the wall's focus. With
        # only sessions that need you showing, this one goes at the next refresh.
        self._update_sidebar()
        self._summary()

    def _drop_alert(self, session_id: str) -> None:
        """Forget a session's alert, and take its toast off the screen if it's still up."""
        toast = self._alerts.pop(session_id, None)
        if toast is not None:
            self._unnotify(toast)

    def visited(self, session_id: str) -> None:
        """You're in this session now: commands act on it, and its alert has been followed."""
        self._last_key = session_id
        self._drop_alert(session_id)

    def action_jump(self) -> None:
        """Go to a session that needs you: the newest alert you haven't followed, else the next
        session that needs you after the one you're in, in wall order."""
        current = self._focused_session(strict=True)
        here = current.session_id if current else None
        waiting = [sid for sid, s in self.sessions.items() if self.needs_you(s) and sid != here]
        if not waiting:
            self.notify("Nothing else needs you right now.", timeout=3)
            return
        unfollowed = [sid for sid in waiting if sid in self._alerts]
        if unfollowed:
            target = max(unfollowed, key=lambda sid: self._alerts[sid].raised_at)
        else:
            order = list(self.sessions)
            start = order.index(here) if here in order else -1
            target = min(waiting, key=lambda sid: (order.index(sid) - start - 1) % len(order))
        self.action_open_session(target)

    def action_open_session(self, session_id: str) -> None:
        """Open a session full size from wherever you are; Esc then goes back to its pane."""
        session = self.sessions.get(session_id)
        if session is None:
            self.notify("That session isn't open any more.", timeout=3)
            return
        self.visited(session_id)
        if isinstance(self.screen, ZoomScreen) and self.screen.session_id == session_id:
            return
        while len(self.screen_stack) > 1:
            self.pop_screen()
        space = self.space_of.get(session_id, ALL)
        if self.current_space not in (ALL, space):
            self._set_space(space)
        if self.only_needs_you and not self.needs_you(session):
            self.action_toggle_filter()
        self._focus_key(session_id)
        screen = ZoomScreen(session_id)
        self.push_screen(screen)
        self.call_after_refresh(screen.show, session, datetime.now(timezone.utc))

    # Spaces and visibility ----------------------------------------------------------------

    def space_names(self) -> list[str]:
        configured = [s.name for s in self.config.spaces]
        automatic = sorted({name for name in self.space_of.values() if name not in configured})
        return [ALL, *configured, *automatic]

    def _made_space(self) -> str | None:
        """The space you're in, if you made it rather than it being named after a repository:
        sessions you start there go in it, whatever folder they work in."""
        if self.current_space != ALL and self.current_space in {s.name for s in self.config.spaces}:
            return self.current_space
        return None

    def _update_sidebar(self) -> None:
        try:
            tree = self.query_one(SpacesTree)
        except NoMatches:
            return
        names = self.space_names()
        if self.current_space not in names:
            self.current_space = ALL
        if list(self._space_nodes) != names:  # rebuilt in order, so the list matches what [ ] and ↑ ↓ step through
            tree.clear()
            self._space_nodes = {name: tree.root.add_leaf(name, data=name) for name in names}
        for name in names:
            members = [s for sid, s in self.sessions.items() if name == ALL or self.space_of.get(sid) == name]
            label = Text()
            label.append(("▶ " if name == self.current_space else "  ") + name,
                         style="bold" if name == self.current_space else "")
            label.append(f"  {len(members)}", style="dim")
            waiting = sum(1 for s in members if self.needs_you(s))
            if waiting:
                label.append(f"  ●{waiting}", style="bold yellow")
            node = self._space_nodes[name]
            if node.label != label:  # type: ignore[attr-defined]
                node.set_label(label)  # type: ignore[attr-defined]
        line = names.index(self.current_space)
        if tree.cursor_line != line:
            tree.scroll_to_line(line, animate=False)
        tree.cursor_line = line

    @on(Tree.NodeSelected, "#spaces")
    def _space_selected(self, event: Tree.NodeSelected) -> None:
        self._set_space(event.node.data)

    def _set_space(self, name: str | None) -> None:
        if not name or name == self.current_space:
            return
        self.current_space = name
        self.config.last_space = name
        self._save_config()
        self._update_sidebar()
        self._apply_visibility()

    def action_space(self, delta: int, wrap: bool = True) -> None:
        names = self.space_names()
        index = (names.index(self.current_space) if self.current_space in names else 0) + delta
        self._set_space(names[index % len(names) if wrap else max(0, min(len(names) - 1, index))])

    def _apply_visibility(self) -> None:
        for pane in self.query_one("#grid", Grid).query(SessionPane):
            s = self.sessions.get(pane.session_id)
            in_space = self.current_space == ALL or self.space_of.get(pane.session_id) == self.current_space
            pane.display = s is not None and in_space and (not self.only_needs_you or self.needs_you(s))
        self._layout()
        self._summary()
        if self.focused is None or (isinstance(self.focused, SessionPane) and not self.focused.display):
            first = next(iter(self._visible_panes()), None)
            if first:
                first.focus()

    def _summary(self) -> None:
        sessions = list(self.sessions.values())
        needing = frozenset(sid for sid, s in self.sessions.items() if self.needs_you(s))
        needs = len(needing)
        working = sum(1 for s in sessions if s.state == WORKING)
        background = sum(1 for s in sessions if s.state == BACKGROUND)
        if needing != self._needing:
            self._needing = needing
            self.refresh_bindings()  # show or hide j in the footer
        text = Text()
        # Clickable while it counts something, like j.
        text.append(f"{needs} need you", style=Style(bold=True, meta={"@click": "app.jump"}) if needs else "dim")
        text.append(f"  ·  {working} working  ·  {background} waiting on agents  ·  {len(sessions)} open")
        if self.current_space != ALL:
            text.append(f"   space: {self.current_space}", style="bold")
        if self.only_needs_you:
            text.append(f"   showing only sessions that need you ({keys_mod.name('toggle-filter')})", style="italic")
        if text != self._summary_text:  # this runs on every streamed word
            self._summary_text = text
            self.query_one("#summary", Static).update(text, layout=False)  # one line, always
        grid = self.query_one("#grid", Grid)
        empty = list(grid.query("#empty"))
        visible = self._visible_panes()
        if not visible:
            spaces = f"{keys_mod.name('space-prev')} and {keys_mod.name('space-next')} switch spaces"
            if self.current_space != ALL and self.current_space not in self.space_of.values():
                message = f"No sessions in {self.current_space}. {keys_mod.name('new-session')} starts one here; {spaces}."
            elif not sessions:
                message = "No open Claude Code sessions. Start one and it appears here."
            else:
                message = (f"Nothing to show in this view. {spaces}, {keys_mod.name('toggle-filter')} shows everything "
                           "again.")
            # As Text: as markup, "[ and ]" is a tag and vanishes.
            if not empty:
                grid.mount(Static(Text(message), id="empty"))
            elif message != self._empty_text:  # from one empty space to another
                empty[0].update(Text(message))
            self._empty_text = message
        else:
            for widget in empty:
                widget.remove()

    # Layout and navigation ----------------------------------------------------------------

    def _visible_panes(self) -> list[SessionPane]:
        return [p for p in self.query_one("#grid", Grid).query(SessionPane) if p.display]

    def _layout(self) -> None:
        n = len(self._visible_panes())
        cols = self.config.columns or (1 if n <= 1 else 2 if n <= 4 else 3 if n <= 9 else 4)
        grid = self.query_one("#grid", Grid)
        grid.styles.grid_size_columns = cols
        grid.styles.grid_size_rows = max(1, -(-n // cols))

    def _current_columns(self) -> int:
        return self.query_one("#grid", Grid).styles.grid_size_columns or 1

    def action_columns(self, delta: int) -> None:
        self.config.columns = max(1, min(6, self._current_columns() + delta))
        self._save_config()
        self._layout()

    def action_move(self, dx: int, dy: int) -> None:
        if self.screen is not self.screen_stack[0] or isinstance(self.focused, SpacesTree):
            return
        panes = self._visible_panes()
        columns = self._current_columns()
        index = panes.index(self.focused) if self.focused in panes else 0
        if dx < 0 and index % columns == 0 and self.config.sidebar:
            self.query_one(SpacesTree).focus()  # off the left edge of the grid, into the spaces list
            return
        if panes:
            panes[max(0, min(len(panes) - 1, index + dx + dy * columns))].focus()

    def action_focus_sessions(self) -> None:
        """Back to the grid, on the pane you were last in if it's still showing."""
        panes = self._visible_panes()
        pane = next((p for p in panes if p.session_id == self._last_key), panes[0] if panes else None)
        if pane is not None:
            pane.focus()

    def action_toggle_filter(self) -> None:
        self.only_needs_you = not self.only_needs_you
        self._apply_visibility()

    def action_toggle_sidebar(self) -> None:
        self.config.sidebar = not self.config.sidebar
        self._save_config()
        self._show_sidebar()

    def _show_sidebar(self) -> None:
        self.query_one(Sidebar).set_class(not self.config.sidebar, "hidden")
        if not self.config.sidebar and isinstance(self.focused, SpacesTree):
            self.action_focus_sessions()

    def action_toggle_view(self) -> None:
        self.view_mode = "activity" if self.view_mode == "conversation" else "conversation"
        self.notify(f"{self.view_mode.capitalize()} view", timeout=2)
        self._repaint()

    def action_toggle_tools(self) -> None:
        self.config.show_tools = not self.config.show_tools
        self._save_config()
        self.notify("Tool calls " + ("shown" if self.config.show_tools else "hidden"), timeout=2)
        self._repaint()

    def action_toggle_times(self) -> None:
        self.config.show_times = not self.config.show_times
        self._save_config()
        self._repaint()

    def _focused_session(self, strict: bool = False) -> LiveSession | None:
        """The session you're in: the zoomed one, or the pane that has focus (also while you
        type in its box). Unless strict, falls back to the pane you were last in, so commands
        from the palette (which takes focus while open) act on it."""
        if isinstance(self.screen, ZoomScreen):
            return self.sessions.get(self.screen.session_id)
        node = self.focused
        while node is not None:
            if isinstance(node, SessionPane):
                return self.sessions.get(node.session_id)
            node = node.parent
        if not strict and self.screen is self.screen_stack[0]:
            return self.sessions.get(self._last_key or "")
        return None

    def action_show_keys(self) -> None:
        self.push_screen(KeysScreen())

    def action_zoom(self) -> None:
        session = self._focused_session()
        if session is None or isinstance(self.screen, ZoomScreen):
            return
        self.push_screen(ZoomScreen(session.session_id))  # it shows the session as it mounts

    def action_move_to_space(self) -> None:
        session = self._focused_session()
        if session is None:
            return
        options = [n for n in self.space_names() if n != ALL]

        def chosen(name: str | None) -> None:
            if not name:
                return
            key = session.session_id
            driven = self.driven.get(key)
            if driven is not None and not driven.session_id:
                self.driven_space[key] = name  # saved once Claude Code gives it an id (see _flush_driven)
            else:
                # A session run here is saved under Claude Code's id: its pane's key is new every launch.
                self.driven_space.pop(key, None)
                self.config.assign(driven.session_id if driven else key, name)
                self._save_config()
            self.space_of[key] = name
            self.notify(f"Moved to {name}", timeout=3)
            self.action_refresh()

        self.push_screen(MoveToSpaceScreen(session.title, options), chosen)

    def action_attach(self) -> None:
        session = self._focused_session()
        if session is None:
            return
        if session.kind != "background":
            self.notify(
                "This is an interactive session; switch to it in its terminal or editor. "
                "Run /bg in it to make it attachable from here.",
                title=session.label,
            )
            return
        claude = shutil.which("claude")
        if not claude:
            self.notify("`claude` CLI not found on PATH", severity="error")
            return
        with self.suspend():
            subprocess.run([claude, "attach", session.job_id or session.session_id])
            terminal.set_title()  # Claude Code named the tab while you were in it
        self.action_refresh()

    # Copying and links ----------------------------------------------------------------------

    def copy_to_clipboard(self, text: str, what: str = "") -> None:
        self._copy(text)
        if text:
            self.notify(describe_copy(text), title=f"Copied {what}".strip(), timeout=2)

    def _copy(self, text: str) -> None:
        super().copy_to_clipboard(text)  # through the terminal (OSC 52), which also works over SSH
        clipboard.copy(text)  # and straight to the system clipboard, for terminals that ignore OSC 52

    def action_copy_reply(self) -> None:
        session = self._focused_session()
        reply = next((e.detail or e.text for e in reversed(session.feed) if e.kind == "claude"), "") if session else ""
        if not reply:
            self.notify("Nothing from Claude to copy yet.", timeout=3)
            return
        cut = session.kind != "panopsis" and len(reply) >= DETAIL_CHARS  # transcripts keep this much of a reply
        self.copy_to_clipboard(reply, f"the first {DETAIL_CHARS} characters of Claude's reply" if cut
                               else "Claude's last reply")

    def open_link(self, href: str, widget: Widget | None = None) -> None:
        """A link you clicked: web pages open in your browser, files in your editor. Anything
        else is copied rather than handed to the system to open."""
        if re.match(r"https?://", href, re.IGNORECASE):
            self.notify(href, title="Opening in your browser", timeout=3)
            self.open_url(href)
            return
        session = self._session_of(widget)
        found = file_link(href, session.cwd if session else "")
        if found is not None and found[0].is_file():
            self.open_in_code_editor(str(found[0]), found[1])
            return
        self._copy(href)
        reason = f"{found[0]} isn't a file." if found else "panopsis only opens web pages and files."
        self.notify(f"{reason} The link is copied instead.", title="Can't open that link", severity="warning",
                    timeout=6)

    def _session_of(self, widget: Widget | None) -> LiveSession | None:
        node = widget
        while node is not None:
            if isinstance(node, SessionPane):
                return self.sessions.get(node.session_id)
            if isinstance(node, ZoomScreen):
                return self.sessions.get(node.session_id)
            node = node.parent
        return self._focused_session()

    # Background jobs -------------------------------------------------------------------------

    def start_job(self, key: str, what: str, paced: bool = False) -> None:
        self.jobs.start(key, what, paced=paced)
        for widget in self.screen_stack[0].query(Activity) if self.screen_stack else []:
            widget.wake()

    def job_step(self, key: str, step: str) -> None:
        if (job := self.jobs.get(key)) is not None:
            job.steps.append(step)

    def finish_job(self, key: str) -> None:
        self.jobs.finish(key)

    # Plan usage ------------------------------------------------------------------------------

    def _schedule_usage(self) -> None:
        if self._usage_timer is not None:
            self._usage_timer.stop()
        self._usage_timer = None
        if self.config.usage_minutes > 0:
            self._usage_timer = self.set_interval(self.config.usage_minutes * 60, self.refresh_usage)

    def refresh_usage(self) -> None:
        if not self.usage_checking:
            self.usage_checking = True
            self.start_job("usage", "checking your plan usage", paced=True)
            self._check_usage()

    @work(thread=True, exclusive=True, group="usage")
    def _check_usage(self) -> None:
        now = datetime.now(timezone.utc)
        started = time.perf_counter()
        try:
            text = usage_mod.run_probe(self._claude())
        except RuntimeError as e:
            self.call_from_thread(self._usage_ready, None, [], str(e), now)
            return
        finally:
            if not self._usage_timed:
                self._usage_timed = True
                timings.mark(f"usage check answered, after {time.perf_counter() - started:.2f}s")
        windows, notes = usage_mod.parse_usage_text(text, now)
        self.call_from_thread(self._usage_ready, windows, notes, "", now)

    def _usage_ready(self, windows: list[usage_mod.Window] | None, notes: list[str], error: str,
                     now: datetime) -> None:
        self.usage_checking = False
        self.finish_job("usage")
        self.usage.error = error
        if windows is not None:
            self.usage.checked, self.usage.notes, self.usage.last_run = now, notes, False
            # /usage lists every window it knows; one it no longer prints has gone away.
            self.usage.windows = {k: w for k, w in self.usage.windows.items() if k in {x.key for x in windows}}
            self.usage.merge(windows)
            usage_mod.save_cached(self.usage)
        self._paint_usage()

    def _rate_limit_seen(self, windows: list[usage_mod.Window]) -> None:
        """A session panopsis runs heard from Anthropic how much of a limit is used."""
        if self.usage.merge(windows):
            self._paint_usage()

    def _paint_usage(self) -> None:
        for screen in self.screen_stack:  # the wall's meter, and a zoom's
            for meter in screen.query(UsageMeter):
                meter.paint()
            if isinstance(screen, UsageScreen):
                screen.paint()

    def action_usage(self) -> None:
        if not isinstance(self.screen, UsageScreen):
            self.push_screen(UsageScreen())

    # Analysis screens -----------------------------------------------------------------------

    def request_analysis(self, callback: Callable[[Analysis], None], force: bool = False) -> None:
        if self.analysis is not None and not force:
            callback(self.analysis)
            if datetime.now(timezone.utc) - self.analysis.loaded_at > HISTORY_FRESH:
                # Shown at once as it was read; read again quietly, and shown again if it changed.
                self._stale_waiters.append(callback)
                self._read_history()
            return
        self._analysis_waiters.append(callback)
        self._read_history(force)

    def preload_history(self) -> None:
        """Read history before a report screen asks for it, so it opens at once."""
        if self.analysis is None:
            self._read_history()

    def _read_history(self, force: bool = False) -> None:
        if self.jobs.get("history") is not None and not force:
            return  # it's being read: whoever waits gets this one
        self.start_job("history", "reading your history")
        self._build_analysis()

    def reevaluate_analysis(self, callback: Callable[[Analysis], None]) -> None:
        if self.analysis is None:
            self.request_analysis(callback)
            return
        self.analysis = analysis_mod.reevaluate(self.analysis)
        callback(self.analysis)

    @work(thread=True, exclusive=True, group="analysis")
    def _build_analysis(self) -> None:
        store = self.session_cache.store
        outdated = store.outdated if store is not None else 0
        posted = float("-inf")

        def progress(done: int, total: int) -> None:
            nonlocal posted
            if done == total or time.perf_counter() - posted > 0.1:  # a few times a second
                posted = time.perf_counter()
                again = store.outdated - outdated if store is not None else 0
                self.call_from_thread(self._history_progress, done, total, again)

        try:
            data = analysis_mod.build(self.root, cache=self.session_cache, progress=progress)
        except Exception as e:  # noqa: BLE001 - surfaced to the user, never fatal to the app
            self.call_from_thread(self.finish_job, "history")
            self.call_from_thread(self.notify, str(e), title="Couldn't read history", severity="error")
            return
        self.call_from_thread(self._analysis_ready, data)

    def _history_progress(self, done: int, total: int, read_again: int) -> None:
        job = self.jobs.get("history")
        if job is None:
            return
        job.done, job.total = done, total
        if read_again and not job.note:
            job.note = ("This panopsis reads transcripts differently from the one that kept your history, "
                        "so it reads them all again, this once.")

    @work(thread=True, exclusive=True, group="store")
    def _forget_old_sessions(self) -> None:
        """Forget what panopsis kept of sessions Claude Code deleted, past how long the settings keep them."""
        if self.session_cache.store is not None:
            self.session_cache.store.forget(self.root, self.config.history_keep_days)

    def _analysis_ready(self, data: Analysis) -> None:
        self.finish_job("history")
        previous, self.analysis = self.analysis, data
        changed = previous is None or _sessions_in(previous) != _sessions_in(data)
        waiters, self._analysis_waiters = self._analysis_waiters, []
        stale, self._stale_waiters = self._stale_waiters, []
        for callback in waiters + (stale if changed else []):  # unchanged: the screen stays as you left it
            callback(data)

    def action_report(self) -> None:
        self.push_screen(ReportScreen())

    def action_advise(self) -> None:
        self.push_screen(AdviseScreen())

    def action_profile(self) -> None:
        self.push_screen(ProfileScreen())

    def action_rules(self) -> None:
        self.push_screen(RulesScreen())

    def action_history(self) -> None:
        self.push_screen(HistoryScreen())

    def action_settings(self) -> None:
        if isinstance(self.screen, SettingsScreen):
            self.screen.dismiss()
        else:
            self.push_screen(SettingsScreen())

    def action_edit_settings(self) -> None:
        path = self._save_config_path()
        self.edit_file(path)
        self.config = config_mod.load(path)
        self._apply_colors()  # they may have changed
        self._use_keys()
        self._warn_bad_colors()
        self._warn_key_clashes()
        self.action_refresh()

    def action_edit_preferences(self) -> None:
        self.edit_file(preferences.ensure_file())
        self.preferences_changed()

    def preferences_changed(self) -> None:
        """Your goals or style changed: rank the findings again, if they've been worked out yet."""
        if self.analysis is not None:
            self.analysis = analysis_mod.reevaluate(self.analysis)

    def _save_config_path(self) -> Path:
        config_mod.save(self.config)
        return self.config.path or config_mod.config_path()

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        commands = [
            ("Report", "Where your time went, your working style, findings", self.action_report),
            ("Advise", "Personalized recommendations from Claude, checked against current practice", self.action_advise),
            ("History", "Browse past sessions", self.action_history),
            ("Usage", "Your plan's session and weekly limits, and what's using them", self.action_usage),
            ("Profile", "Your derived working style and stated preferences", self.action_profile),
            ("Rules", "Active rules; mute the ones that don't fit you", self.action_rules),
            ("Go to a session that needs you", f"The newest alert first, then each in turn ({keys_mod.name('jump')})",
             self.action_jump),
            ("New session", "Start a session panopsis runs, in a folder you pick", self.action_new_session),
            ("Detach this session", "Hand it to Claude Code's background service so it keeps working",
             self.action_detach),
            ("Close this session", "Done with it, like closing a chat in your editor; it stays in History",
             self.action_end_session),
            ("Show hidden sessions", "Bring back terminal and editor sessions you closed from the wall",
             self.action_show_hidden),
            ("Stop the current turn", "Interrupt what Claude is doing right now", self.action_interrupt),
            ("Bring this session here", "Take a background session back into panopsis", self.action_bring_here),
            ("Open this session in Claude Code", "Full Claude Code in this terminal; quit it to come back",
             self.action_handoff_focused),
            ("Open this session's folder in your editor", "Its repository or worktree, in the editor picked in "
             f"settings ({keys_mod.name('settings')})", self.action_open_folder),
            ("Move session to space", "Group the selected session with others", self.action_move_to_space),
            ("Toggle sidebar", "Show or hide the spaces sidebar", self.action_toggle_sidebar),
            ("Toggle conversation / activity view", "Readable conversation or one line per event",
             self.action_toggle_view),
            ("Toggle tool calls", "Show or hide tool calls in feeds", self.action_toggle_tools),
            ("Toggle timestamps", "Show or hide times in feeds", self.action_toggle_times),
            ("More columns", "Fit more panes side by side", lambda: self.action_columns(1)),
            ("Fewer columns", "Bigger panes", lambda: self.action_columns(-1)),
            ("Only sessions that need you", "Toggle the needs-you filter", self.action_toggle_filter),
            ("Settings", "Theme, colours, columns, keys, your editor, defaults, experimental screens; the files "
             f"open from there too ({keys_mod.name('settings')})",
             self.action_settings),
            ("Keys", f"Every key, and changing them: Enter on one, then the new key ({keys_mod.name('show-keys')})",
             self.action_show_keys),
        ]
        hidden = () if self.config.findings_and_advice else ("Report", "Advise", "Profile", "Rules")
        for title, help_text, callback in commands:
            if title not in hidden:
                yield SystemCommand(title, help_text, callback)


def run(interval: float | None = None, project: str | None = None, root: Path = DEFAULT_ROOT,
        provider: Callable[[], list[dict]] = open_sessions) -> None:
    timings.mark("app loaded")
    # Everything loaded so far (the modules: Textual, Rich, the SDK) stays until the app quits. Out
    # of the collector's sight, its full sweeps go over less: they held up keys while sessions streamed.
    gc.freeze()
    with terminal.titled():
        PanopsisApp(provider=provider, root=root, interval=interval, project=project).run()
    if timings.ENABLED:
        print(timings.report())
