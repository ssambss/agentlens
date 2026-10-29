"""UI for sessions panopsis runs itself (experimental): a pane you can type into, the
card that shows a pending approval / question / plan, and the new-session dialog."""

from __future__ import annotations

import os
import string
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Group, RenderableType
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DirectoryTree, Input, Label, OptionList, Select, Static, Tree

from panopsis import keys as keys_mod
from panopsis.commands import Catalog, Suggestion
from panopsis.driver import (
    APPROVAL,
    ENDED,
    FAILED,
    PAUSED,
    PERMISSION,
    PERMISSION_MODES,
    folder_trust,
    is_home,
    QUESTION,
    STARTING,
    WORKING,
    DrivenSession,
)
from panopsis.feed import Framed, Palette, markdown
from panopsis.frames import PacedScreen
from panopsis.live import LiveSession
from panopsis.model import Event
from panopsis.selectable import Selectable

MODE_LABELS = {
    "default": "Ask before edits and commands (default)",
    "acceptEdits": "Accept file edits, ask for commands",
    "plan": "Plan first, no changes until you approve",
    "auto": "Auto: a classifier approves safe actions",
}


def as_live(d: DrivenSession, now: datetime) -> LiveSession:
    """Present a driven session to the wall like any other session."""
    feed = list(d.events)
    if d.partial.strip():
        first = next((ln for ln in d.partial.splitlines() if ln.strip()), "")
        feed.append(Event(now, "claude", first[:240], d.partial))
    return LiveSession(
        name=d.name,
        session_id=d.key,
        project=Path(d.cwd).name or d.cwd,
        title=d.title,
        kind="panopsis",
        state=d.state,
        since=d.since,
        detail=d.detail,
        feed=feed,
        started=d.started,
        worktree=d.worktree,
        cwd=d.cwd,
    )


def request_card(d: DrivenSession, palette: Palette) -> RenderableType | None:
    p = d.pending
    if p is None:
        claude, new = keys_mod.name("claude-code"), keys_mod.name("new-session")
        if d.state == FAILED:
            return Text(f"✗ {d.error}\nPress {claude} to open it in Claude Code, or {new} for a new session.",
                        style=palette.error)
        if d.state == ENDED:
            return Text(f"This session has ended. Press {claude} to open it in Claude Code.", style="dim")
        if d.state == PAUSED and d.error:
            return Text.assemble((f"✗ {d.error}\n", palette.error),
                                 (f"The conversation is kept: type to continue it here, or press {claude} to open "
                                  "it in Claude Code.", "dim"))
        return None
    keys = Text(style="dim")
    if p.kind == "permission":
        head = Text.assemble(("Claude wants to use ", "bold"), (p.tool_name, f"bold {palette.you}"))
        body = Text(p.summary or "", style=palette.tool)
        keys.append("1 allow · 2 allow and don't ask again · 3 deny · or type a reason and Enter to deny")
        parts: list[RenderableType] = [head, body, keys]
    elif p.kind == "question":
        q = p.current_question or {}
        head = Text(str(q.get("question", "Claude has a question")), style="bold")
        options = Text()
        for i, option in enumerate(q.get("options") or [], start=1):
            if not isinstance(option, dict):
                continue
            options.append(f"{i}  ", style=f"bold {palette.you}")
            options.append(str(option.get("label", "")), style="bold")
            if option.get("description"):
                options.append(f"  {option['description']}", style="dim")
            options.append("\n")
        remaining = len(p.questions) - len(p.answers)
        more = f" · question {len(p.answers) + 1} of {len(p.questions)}" if len(p.questions) > 1 else ""
        keys.append(f"press a number, or type your own answer and Enter{more}")
        if remaining <= 0:
            return None
        parts = [head, options, keys]
    else:  # plan
        plan = str(p.tool_input.get("plan") or "")
        head = Text("Claude's plan is ready for review", style="bold")
        keys.append("1 approve and start · 3 keep planning (type what to change and Enter)")
        parts = [head, markdown(plan[:6000], palette.code_theme), keys]
    return Framed(Group(*parts), (0, 1), f"on {palette.card}")


# The line above the box while Claude works on your message: Claude Code's own spinner, what it's
# doing, for how long, and the tokens it has written, which keep climbing while it writes a long
# file or thinks, so you can tell it's getting on even with tool calls hidden.
SPINNER = "·✢*✶✻✽✻✶*✢"
SPIN_SECONDS = 0.12
ACTIVE = {STARTING, WORKING}


def elapsed(seconds: float) -> str:
    """How long a turn has run, counting the seconds: `12s`, `1m 04s`, `1h 02m`."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def token_count(n: int) -> str:
    return str(n) if n < 1000 else f"{n / 1000:.1f}k"


def activity_line(d: DrivenSession, at: datetime, frame: int, palette: Palette, typing: bool = False,
                  width: int = 0) -> Text:
    """`✻ Running Bash: npm test (1m 04s · ↓ 1.2k tokens · x stops it)`. To fit `width`, what
    it's doing is cut, so the time and tokens stay in view."""
    doing = d.detail.rstrip("…") or "working"
    doing = doing[0].upper() + doing[1:] + ("" if ": " in doing else "…")
    facts = [elapsed((at - (d.turn_started or d.since)).total_seconds())]
    if d.tokens:
        facts.append(f"↓ {token_count(d.tokens)} tokens")
    if d.state == WORKING and d.connected:
        stop = keys_mod.name("interrupt")
        facts.append(f"Esc, then {stop} stops it" if typing else f"{stop} stops it")
    tail = Text(f" ({' · '.join(facts)})", style="dim")
    what = Text(doing, style=palette.claude)
    if width:
        what.truncate(max(8, width - 2 - tail.cell_len), overflow="ellipsis")
    return Text.assemble((SPINNER[frame % len(SPINNER)] + " ", f"bold {palette.claude}"), what, tail,
                         no_wrap=True, overflow="ellipsis")


def placeholder(d: DrivenSession) -> str:
    if d.state == PERMISSION:
        return "Type a reason to deny, or press Esc and 1/2/3"
    if d.state == QUESTION:
        return "Type your own answer…"
    if d.state == APPROVAL:
        return "What should change in the plan?"
    if d.state == STARTING:
        return "Starting Claude Code…"
    if d.state == PAUSED:
        return "Type to continue this session…  (Enter to send, Esc to leave the box)"
    if d.state in (ENDED, FAILED):
        return "Session closed"
    if d.state == WORKING:
        return "Message Claude… it reads this at its next step  (Enter to send, Esc to leave the box)"
    return "Message Claude…  (Enter to send, Esc to leave the box)"


class ImageInput(Input):
    """A box a message is typed in, with images to go with it: Alt+V (or Ctrl+V, where the
    terminal lets it through) pastes an image from the clipboard, and Backspace in the empty
    box takes the last one off."""

    def paste_image(self, otherwise: Callable[[], None] | None = None) -> None:
        raise NotImplementedError

    def drop_image(self) -> bool:
        raise NotImplementedError

    async def _on_key(self, event) -> None:
        if event.key == "alt+v":
            event.stop()
            event.prevent_default()
            self.paste_image()
            return
        if event.key == "backspace" and not self.value and self.drop_image():
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)

    def action_paste(self) -> None:
        # Ctrl+V: an image on the clipboard goes with your message, else paste text as usual.
        self.paste_image(otherwise=super().action_paste)


class ComposerInput(ImageInput):
    """With the box empty and a request pending, digits answer the request instead of typing.
    While the command menu is open, the arrows, Tab, Enter and Esc work the menu."""

    MENU_KEYS = {"up", "down", "pageup", "pagedown", "tab", "enter", "escape"}

    def __init__(self, key: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.session_key = key

    def paste_image(self, otherwise: Callable[[], None] | None = None) -> None:
        self.app.paste_image(self.session_key, otherwise)  # type: ignore[attr-defined]

    def drop_image(self) -> bool:
        return self.app.drop_image(self.session_key)  # type: ignore[attr-defined]

    async def _on_key(self, event) -> None:
        if event.character and event.character.isdigit() and not self.value:
            if self.app.driven_has_request(self.session_key):  # type: ignore[attr-defined]
                event.stop()
                event.prevent_default()
                self.app.answer_request(self.session_key, int(event.character))  # type: ignore[attr-defined]
                return
        composer = self.parent
        if event.key in self.MENU_KEYS and isinstance(composer, Composer) and composer.menu_open:
            event.stop()
            event.prevent_default()
            composer.menu_key(event.key)
            return
        await super()._on_key(event)


class FirstMessageInput(ImageInput):
    """The new-session dialog's first message: images pasted here go with it."""

    def paste_image(self, otherwise: Callable[[], None] | None = None) -> None:
        self.screen.paste_image(otherwise)  # type: ignore[attr-defined]

    def drop_image(self) -> bool:
        return self.screen.drop_image()  # type: ignore[attr-defined]


def images_line(images: list) -> Text:
    line = Text()
    for n, image in enumerate(images, start=1):
        line.append(f"[Image #{n}]", style="bold")
        line.append(f" {image.size}  ", style="dim")
    line.append("· Backspace in an empty box removes the last", style="dim")
    return line


class CommandMenu(OptionList, can_focus=False):
    """Claude Code's slash commands matching what you've typed. The box keeps the focus."""


def default_hint() -> Text:
    """Under the box: what to type, with your key for the keys list."""
    return Text(f"Type / for Claude Code's commands · Esc leaves the box; then {keys_mod.name('show-keys')} lists "
                "every key · Alt+V pastes an image", style="dim")


MENU_HINT = "↑↓ choose · Tab fills it in · Enter runs it · Esc closes the list"


def menu_left(s: Suggestion) -> tuple[str, str]:
    """The name column: `/model` and its argument hint, or an argument's value."""
    if s.choice is not None:
        return s.choice.value, ""
    hint = s.command.hint if len(s.command.hint) <= 24 else s.command.hint[:23] + "…"
    return f"/{s.command.name}", f" {hint}" if hint else ""


def menu_line(s: Suggestion, width: int, max_width: int) -> Text:
    """`/model <model>   Set the AI model for Claude Code`, or for an argument `opus   Opus 5.5 · ...`.
    One line each, cut to fit: skills' descriptions run to paragraphs."""
    name, hint = menu_left(s)
    line = Text(no_wrap=True, overflow="ellipsis")
    line.append(name, style="bold")
    line.append(hint, style="dim")
    line.append(" " * max(2, width - len(name) - len(hint)))
    if s.choice is not None:
        about = " · ".join(part for part in (s.choice.label, s.choice.description) if part)
    else:
        about = s.command.description
    if about:
        line.append(about.splitlines()[0], style="dim")
    if max_width > 0:
        line.truncate(max_width, overflow="ellipsis")
    return line


def command_hint(catalog: Catalog, text: str) -> Text | None:
    """Under the box while you type a command's argument: what it takes, as Claude Code says."""
    if not text.startswith("/") or " " not in text:
        return None
    command = catalog.find(text[1:].split(" ", 1)[0])
    if command is None:
        return None
    hint = Text(f"/{command.name}", style="bold")
    if command.hint:
        hint.append(f" {command.hint}")
    if command.description:
        hint.append(f"  {command.description.splitlines()[0]}", style="dim")
    hint.no_wrap, hint.overflow = True, "ellipsis"
    return hint


class Composer(Vertical):
    """Request card plus the input box for one driven session, the line that shows Claude working
    on your message, and the command menu that opens when you type / (Claude Code's own commands,
    as in its terminal)."""

    BINDINGS = [Binding("escape", "leave", "Leave box", show=False)]
    MENU_ROWS = 50  # the list scrolls; its height is set in CSS

    def __init__(self, key: str, **kwargs) -> None:
        super().__init__(classes="composer", **kwargs)
        self.key = key
        self._card_key: tuple | None = None
        self._images: list = []  # the pasted images the line above the box shows
        self.suggestions: list[Suggestion] = []
        self._blocked = False  # a request is pending: the box answers it, so no menu
        self._driven: DrivenSession | None = None
        self._palette = Palette()
        self._spinner = None  # turns the activity line while Claude works
        self._hint = default_hint()  # the line under the box

    def compose(self) -> ComposeResult:
        yield Selectable(classes="request")  # so you can copy from a plan, or click its links
        yield Static(classes="composer-status")  # shown while Claude works (the composer's .working)
        yield Static(classes="composer-images")
        yield ComposerInput(self.key, classes="composer-input")
        yield CommandMenu(classes="command-menu", compact=True, markup=False)
        yield Static(self._hint, classes="composer-hint")

    def on_mount(self) -> None:
        self.query_one(CommandMenu).display = False
        self.query_one(".composer-images", Static).display = False

    def show(self, d: DrivenSession, palette: Palette) -> None:
        try:
            card = self.query_one(".request", Selectable)
            box = self.query_one(Input)
            images = self.query_one(".composer-images", Static)
        except Exception:  # noqa: BLE001 - not composed yet
            return
        if d.images != self._images:
            self._images = list(d.images)
            images.display = bool(d.images)
            images.update(images_line(d.images))
        # This runs on every streamed word; only rebuild the card when what it shows changes.
        p = d.pending
        card_key = (p, len(p.answers) if p else 0, d.state, d.error, palette, keys_mod.name("claude-code"),
                    keys_mod.name("new-session"))  # the keys it names
        if card_key != self._card_key:
            self._card_key = card_key
            rendered = request_card(d, palette)
            card.display = rendered is not None
            if rendered is not None:
                card.show(rendered)
        box.placeholder = placeholder(d)
        box.disabled = d.state in (ENDED, FAILED, STARTING)
        blocked = d.pending is not None or box.disabled
        if blocked != self._blocked:
            self._blocked = blocked
            self.update_menu(box.value)
        self._driven, self._palette = d, palette
        working = d.state in ACTIVE
        if working != self.has_class("working"):
            self.set_class(working, "working")
            if self._spinner is None:
                self._spinner = self.set_interval(SPIN_SECONDS, self._spin, pause=True)
            if working:
                self._spin()  # at once, with the message you just sent
                self._spinner.resume()
            else:
                self._spinner.pause()

    def _spin(self) -> None:
        d = self._driven
        try:
            if d is None or not self.screen.is_current:
                return  # under the zoomed-in view, say: nothing to draw
            status = self.query_one(".composer-status", Static)
            box = self.query_one(Input)
        except Exception:  # noqa: BLE001 - being removed
            return
        line = activity_line(d, now(), int(time.monotonic() / SPIN_SECONDS), self._palette, typing=box.has_focus,
                             width=self.content_region.width)
        status.update(line, layout=False)  # one line, always: no need to lay out the screen again

    # The command menu ----------------------------------------------------------------------

    @property
    def menu_open(self) -> bool:
        return bool(self.suggestions)

    def _catalog(self) -> Catalog:
        return self.app.command_catalog(self.key)  # type: ignore[attr-defined]

    @on(Input.Changed)
    def _changed(self, event: Input.Changed) -> None:
        self.update_menu(event.value)

    def update_menu(self, text: str) -> None:
        try:
            menu = self.query_one(CommandMenu)
        except Exception:  # noqa: BLE001 - not composed yet
            return
        catalog = self._catalog()
        self.suggestions = [] if self._blocked else catalog.suggest(text)[:self.MENU_ROWS]
        menu.display = bool(self.suggestions)
        if self.suggestions:
            room = self.content_region.width - 2  # the list's scrollbar
            width = min(32, max(sum(map(len, menu_left(s))) for s in self.suggestions) + 2, max(12, room // 2))
            menu.set_options([menu_line(s, width, room) for s in self.suggestions])
            menu.highlighted = 0
            self._show_hint(Text(MENU_HINT, style="dim"))
        else:
            if menu.option_count:
                menu.clear_options()
            self._show_hint(command_hint(catalog, text) or default_hint())

    def _show_hint(self, hint: Text) -> None:
        # This runs on every key you type. A new hint lays the screen out again, in case it wraps
        # differently; the same one, as while you type a message, leaves it be.
        if hint != self._hint:
            self._hint = hint
            self.query_one(".composer-hint", Static).update(hint)

    def close_menu(self) -> None:
        self.suggestions = []
        self.query_one(CommandMenu).display = False
        self._show_hint(default_hint())

    def menu_key(self, key: str) -> None:
        menu = self.query_one(CommandMenu)
        if key == "up":
            menu.action_cursor_up()
        elif key == "down":
            menu.action_cursor_down()
        elif key == "pageup":
            menu.action_page_up()
        elif key == "pagedown":
            menu.action_page_down()
        elif key == "escape":
            self.close_menu()
        else:
            self.accept(run=key == "enter")

    def accept(self, run: bool) -> None:
        """Tab fills the highlighted command in; Enter runs it, unless it needs an argument,
        in which case it's filled in and the menu offers the argument's values if there are any."""
        if not self.suggestions:
            return
        index = self.query_one(CommandMenu).highlighted or 0
        s = self.suggestions[min(index, len(self.suggestions) - 1)]
        box = self.query_one(ComposerInput)
        if s.choice is None and (not run or s.command.needs_argument):
            box.value = s.text + " "
            box.cursor_position = len(box.value)
            return  # Input.Changed reopens the menu with the argument's values, if any
        if not run:
            box.value = s.text
            box.cursor_position = len(box.value)
            return
        box.value = ""
        self.close_menu()
        self.app.composer_submit(self.key, s.text)  # type: ignore[attr-defined]

    @on(OptionList.OptionSelected)
    def _clicked(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.query_one(CommandMenu).highlighted = event.option_index
        self.accept(run=True)
        self.focus_input()

    def focus_input(self) -> None:
        box = self.query_one(Input)
        if not box.disabled:
            box.focus()

    def action_leave(self) -> None:
        parent = self.parent
        while parent is not None and not getattr(parent, "can_focus", False):
            parent = parent.parent
        if parent is not None:
            parent.focus()
        elif hasattr(self.screen, "action_focus_conversation"):
            self.screen.action_focus_conversation()  # zoomed in: the conversation, where → reaches the files
        else:
            self.screen.set_focus(None)

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        text = event.value
        event.input.value = ""
        self.app.composer_submit(self.key, text)  # type: ignore[attr-defined]


class NewSessionScreen(PacedScreen, ModalScreen[dict | None]):
    """Start a session panopsis runs itself: folder, first message, permission mode."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, folder: str, mode: str, space: Callable[[str, str], str] | None = None) -> None:
        super().__init__()
        self.folder, self.mode = folder, mode if mode in PERMISSION_MODES else "default"
        self.space = space  # the space a session started in this folder, with this first message, goes in
        self.images: list = []  # pasted into the first message's box, to go with it

    def compose(self) -> ComposeResult:
        with Vertical(id="new-dialog"):
            yield Label(Text("New session", style="bold"))
            yield Static(Text("Runs your own Claude Code in this folder, typeable right here (experimental).",
                             style="dim"))
            with Horizontal(id="new-folder-row"):
                yield Input(self.folder, placeholder="Folder to work in", id="new-folder")
                yield Button("Browse…", id="new-browse")
            # The folder isn't always the space's: in a space you made, it goes in that space wherever it works.
            yield Static(self._space_line(self.folder, ""), id="new-space")
            yield Select([(label, mode) for mode, label in MODE_LABELS.items()], value=self.mode,
                         allow_blank=False, id="new-mode")
            yield Static(folder_note(self.folder), id="new-note")
            yield FirstMessageInput(placeholder="First message (optional, Alt+V pastes an image), Enter to start",
                                    id="new-prompt")
            yield Static(id="new-images")
            yield Static(id="new-error")

    def on_mount(self) -> None:
        self.query_one("#new-prompt", Input).focus()
        self._show_images()

    def paste_image(self, otherwise: Callable[[], None] | None = None) -> None:
        self.app.paste_image_onto(self.images, otherwise, then=self._show_images)  # type: ignore[attr-defined]

    def drop_image(self) -> bool:
        if not self.images:
            return False
        self.images.pop()
        self._show_images()
        return True

    def _show_images(self) -> None:
        line = self.query_one("#new-images", Static)
        line.display = bool(self.images)
        line.update(images_line(self.images))

    @on(Input.Changed, "#new-folder")
    def _folder_changed(self, event: Input.Changed) -> None:
        self.query_one("#new-note", Static).update(folder_note(event.value.strip().strip('"')))
        self._show_space()

    @on(Input.Changed, "#new-prompt")
    def _show_space(self) -> None:
        folder = self.query_one("#new-folder", Input).value.strip().strip('"')
        self.query_one("#new-space", Static).update(self._space_line(folder, self.query_one("#new-prompt", Input).value))

    def _space_line(self, folder: str, prompt: str) -> Text:
        name = self.space(folder, prompt) if self.space else ""
        return Text.assemble("Opens in the ", (name, "bold"), " space") if name else Text("")

    @on(Button.Pressed, "#new-browse")
    def _browse(self) -> None:
        box = self.query_one("#new-folder", Input)

        def picked(folder: str | None) -> None:
            if folder:
                box.value = folder
                self.query_one("#new-error", Static).update("")
            self.query_one("#new-prompt", Input).focus()

        self.app.push_screen(FolderPickerScreen(box.value.strip().strip('"')), picked)

    @on(Input.Submitted)
    def _start(self, event: Input.Submitted) -> None:
        folder = self.query_one("#new-folder", Input).value.strip().strip('"')
        if not folder or not Path(folder).is_dir():
            self.query_one("#new-error", Static).update(Text(f"No such folder: {folder}", style="red"))
            self.query_one("#new-folder", Input).focus()
            return
        self.dismiss({
            "cwd": str(Path(folder)),
            "prompt": self.query_one("#new-prompt", Input).value,
            "images": list(self.images),
            "mode": self.query_one("#new-mode", Select).value,
        })

    def action_cancel(self) -> None:
        self.dismiss(None)


def folder_note(folder: str) -> Text:
    """What starting a session here means for detaching it later."""
    if not folder or not Path(folder).is_dir():
        return Text("")
    if is_home(folder):
        return Text("Your home folder: fine for a quick session, but Claude Code can't take sessions here into "
                    "the background, so they can't be detached later. Use a project folder for that.",
                    style="yellow")
    if folder_trust(folder):
        return Text("Trusted in Claude Code: sessions here can be detached to the background.", style="dim")
    return Text("Not trusted in terminal Claude Code yet. Sessions work here; detaching one later asks you to "
                "trust the folder first (Ctrl+T in the detach dialog).", style="dim")


SKIPPED_FOLDERS = {"__pycache__", "node_modules", "System Volume Information"}


def nearest_folder(text: str) -> Path:
    """The folder typed, or the closest one above it that exists, or your home folder."""
    if text:
        path = Path(text).expanduser().absolute()
        for folder in (path, *path.parents):
            if folder.is_dir():
                return folder
    return Path.home()


def list_drives() -> list[str]:
    """Drive roots on Windows (C:, D: and so on); none elsewhere, where everything is under /."""
    if os.name != "nt":
        return []
    listdrives = getattr(os, "listdrives", None)  # Python 3.12+
    if listdrives is not None:
        return listdrives()
    return [f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]


class FolderTree(DirectoryTree):
    """Folders only, without hidden ones; double-clicking a folder picks it."""

    class Picked(Message):
        def __init__(self, path: Path) -> None:
            super().__init__()
            self.path = path

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [p for p in paths
                if not p.name.startswith((".", "$")) and p.name not in SKIPPED_FOLDERS and self._safe_is_dir(p)]

    def on_click(self, event: events.Click) -> None:
        node = self.cursor_node
        if (event.chain >= 2 and node is not None and node.data is not None
                and event.style.meta.get("line") == self.cursor_line):
            event.prevent_default()  # the first click opened it; don't fold it back up
            self.post_message(self.Picked(node.data.path))


class FolderPickerScreen(PacedScreen, ModalScreen[str | None]):
    """Pick the new session's folder by clicking through folders instead of typing the path."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("backspace", "up", "Parent folder"),
    ]

    def __init__(self, start: str) -> None:
        super().__init__()
        self.start = nearest_folder(start)
        self.chosen = self.start
        self.drives = list_drives()
        if self.drives and self.start.anchor not in self.drives:
            self.drives.append(self.start.anchor)  # a network share, say

    def compose(self) -> ComposeResult:
        with Vertical(id="folder-dialog"):
            yield Label(Text("Choose a folder", style="bold"))
            yield Static(Text("Click a folder to open it, double-click to choose it. Keys: arrows, Enter opens "
                             "or closes, Backspace goes up a level.", style="dim"))
            with Horizontal(id="folder-bar"):
                yield Button("↑ Up", id="folder-up")
                yield Button("Home", id="folder-home")
                if len(self.drives) > 1:
                    yield Select([(d, d) for d in self.drives], value=self.start.anchor, allow_blank=False,
                                 id="folder-drive")
            yield FolderTree(self.start, id="folder-tree")
            yield Static(id="folder-chosen")
            with Horizontal(id="folder-buttons"):
                yield Button("Use this folder", variant="primary", id="folder-use")
                yield Button("Cancel", id="folder-cancel")

    def on_mount(self) -> None:
        self.query_one(FolderTree).focus()
        self._show(self.start)

    def _show(self, folder: Path) -> None:
        self.chosen = folder
        self.query_one("#folder-chosen", Static).update(Text.assemble(("Folder: ", "dim"), str(folder)))
        root = Path(self.query_one(FolderTree).path)
        self.query_one("#folder-up", Button).disabled = root.parent == root

    def _go(self, folder: Path) -> None:
        self.query_one(FolderTree).path = folder
        self._show(folder)
        for drive in self.query("#folder-drive").results(Select):
            if folder.anchor in self.drives and drive.value != folder.anchor:
                drive.value = folder.anchor

    @on(Tree.NodeHighlighted, "#folder-tree")
    def _highlighted(self, event: Tree.NodeHighlighted) -> None:
        if event.node.data is not None:
            self._show(event.node.data.path)

    @on(FolderTree.Picked)
    def _picked(self, event: FolderTree.Picked) -> None:
        self.dismiss(str(event.path))

    @on(Button.Pressed, "#folder-use")
    def _use(self) -> None:
        self.dismiss(str(self.chosen))

    @on(Button.Pressed, "#folder-up")
    def action_up(self) -> None:
        root = Path(self.query_one(FolderTree).path)
        if root.parent != root:
            self._go(root.parent)

    @on(Button.Pressed, "#folder-home")
    def _home(self) -> None:
        self._go(Path.home())

    @on(Select.Changed, "#folder-drive")
    def _drive(self, event: Select.Changed) -> None:
        if event.value != Path(self.query_one(FolderTree).path).anchor:
            self._go(Path(str(event.value)))

    @on(Button.Pressed, "#folder-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class DetachScreen(PacedScreen, ModalScreen[dict | None]):
    """Hand a session to Claude Code's background service, with an instruction for while you're away.

    Returns {"action": "detach" | "release" | "trust", "instruction": str}, or None to cancel."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+t", "trust", "Trust this folder", priority=True),
    ]

    def __init__(self, title: str, working: bool, in_git_repo: bool, cwd: str = "",
                 trust: bool | None = True, home: bool = False) -> None:
        super().__init__()
        self.session_title, self.working, self.in_git_repo = title, working, in_git_repo
        self.cwd, self.trust, self.home = cwd, trust, home

    def compose(self) -> ComposeResult:
        here, attach = keys_mod.name("bring-here"), keys_mod.name("attach")
        with Vertical(id="new-dialog"):
            yield Static(Text.assemble("Detach ", (self.session_title or "this session", "bold")))
            if self.home:
                yield Static(Text(
                    "Claude Code doesn't run background sessions in your home folder, so this one can't be "
                    f"detached. You can release it instead: it stays in History, where {here} continues it here and "
                    f"{keys_mod.name('claude-code')} in Claude Code. Start sessions in a project folder to be able "
                    "to detach them.", style="yellow"))
                keys = Text()
                keys.append(" r ", style="bold reverse")
                keys.append(" release it   ")
                keys.append(" esc ", style="bold reverse")
                keys.append(" cancel")
                yield Label(keys)
                return
            explain = Text(
                "Claude Code's background service takes the session over: it keeps working even if you close "
                f"panopsis, shows on the wall as a background session (bg), {attach} attaches to it in full Claude "
                f"Code, and {here} brings it back here.", style="dim")
            if self.in_git_repo:
                explain.append("\nIn a git repository, a background session may move into its own worktree "
                               "(.claude/worktrees/) before editing files.", style="dim")
            yield Static(explain)
            if self.trust is not True:
                yield Static(Text(
                    f"{self.cwd} isn't trusted in terminal Claude Code yet, and background sessions need that. "
                    "Ctrl+T opens Claude Code there: accept the trust prompt, type /exit, and detaching "
                    "continues. Enter tries anyway.", style="yellow"))
            yield Input("Continue where you left off." if self.working else "",
                        placeholder="What should it do while you're away? (empty: just release it)",
                        id="detach-instruction")
            yield Static(id="detach-error")

    def on_mount(self) -> None:
        for box in self.query("#detach-instruction"):
            box.focus()

    def on_key(self, event) -> None:
        if self.home and event.character == "r":
            event.stop()
            self.dismiss({"action": "release", "instruction": ""})

    @on(Input.Submitted)
    def _go(self, event: Input.Submitted) -> None:
        instruction = event.value.strip()
        self.dismiss({"action": "detach" if instruction else "release", "instruction": instruction})

    def action_trust(self) -> None:
        if self.home:
            return
        instruction = self.query_one("#detach-instruction", Input).value.strip()
        if not instruction:
            self.query_one("#detach-error", Static).update(
                Text("Say what it should do in the background first.", style="red"))
            return
        self.dismiss({"action": "trust", "instruction": instruction})

    def action_cancel(self) -> None:
        self.dismiss(None)


class ChoiceScreen(PacedScreen, ModalScreen[str | None]):
    """A small question with single-key answers, e.g. confirm ending a working session. A choice
    can be several keys, comma-separated like a binding's (the keys of the action that asked),
    and shows the first."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, body: str, choices: dict[str, str]) -> None:
        super().__init__()
        self.title_text, self.body, self.choices = title, body, choices  # keys -> label

    def compose(self) -> ComposeResult:
        keys = Text()
        for key, label in self.choices.items():
            keys.append(f" {keys_mod.show(key.split(',')[0])} ", style="bold reverse")
            keys.append(f" {label}   ")
        keys.append(" esc ", style="bold reverse")
        keys.append(" cancel")
        with Vertical(id="new-dialog"):
            yield Label(Text(self.title_text, style="bold"))
            yield Static(Text(self.body, style="dim"))
            yield Label(keys)

    def on_key(self, event) -> None:
        choice = next((keys for keys in self.choices if event.key in keys.split(",")), None)
        if choice is not None:
            event.stop()
            self.dismiss(choice)

    def action_cancel(self) -> None:
        self.dismiss(None)


def now() -> datetime:
    return datetime.now(timezone.utc)
