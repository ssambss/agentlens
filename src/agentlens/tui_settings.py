"""The settings screen (,): the look and the defaults, picked in the app instead of the file.

Every change shows on the wall behind it straight away and is saved as you make it, to
~/.agentlens/config.yaml, and your goals and style to ~/.agentlens/profile.yaml. Spaces and
colours of your own stay in the file; the screen has buttons that open it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

from agentlens import config as config_mod, editor as editor_mod, keys as keys_mod, preferences
from agentlens.frames import PacedScreen
from agentlens.tui_driven import MODE_LABELS
from agentlens.tui_keys import KeysScreen

OWN_COLORS = "own"  # the colours option for colours set in the file
OWN_KEYS = "own"  # the keys option for keys you changed one by one
AUTO_EDITOR = ""  # the editor option for $VISUAL, $EDITOR or the first one installed
OWN_EDITOR = "own"  # the editor option for a command of your own
JUDGE = ""  # the style option that leaves it to agentlens, from your sessions
USAGE_CHOICES = [("Off", 0.0), ("Every minute", 1.0), ("Every 2 minutes", 2.0), ("Every 5 minutes", 5.0),
                 ("Every 10 minutes", 10.0), ("Every 15 minutes", 15.0), ("Every 30 minutes", 30.0)]
CHECKBOXES = {"set-sidebar": "sidebar", "set-tools": "show_tools", "set-times": "show_times",
              "set-editor-terminal": "editor_terminal"}
INPUTS = {"set-editor": "editor", "set-cli": "cli_path"}
# A sample of the colours, as the panes and the conversation use them.
BORDER_SAMPLE = [("selected", "selected"), ("question", "needs you"), ("working", "working"),
                 ("background", "background"), ("failed", "failed")]
CHAT_SAMPLE = [("claude", "Claude"), ("tool", "tool calls"), ("notice", "notices"), ("error", "errors")]


def row(label: str, *widgets: Widget, id: str | None = None) -> Horizontal:
    return Horizontal(Label(label), *widgets, classes="settings-row", id=id)


def tilde(path: Path) -> str:
    try:
        return str(Path("~") / path.relative_to(Path.home()))
    except ValueError:
        return str(path)


class SettingsScreen(PacedScreen, ModalScreen[None]):
    """The look and the defaults. Esc, or the key that opened it, closes it."""

    BINDINGS = [Binding("escape", "close", "Close"), keys_mod.binding("settings", "close", "Close")]
    AUTO_FOCUS = "#set-theme"  # not the app's: there are no session panes here

    @property
    def _app(self) -> Any:
        """The agentlens app, untyped: tui imports this module, so this one can't import it."""
        return self.app

    def compose(self) -> ComposeResult:
        app = self._app
        cfg = app.config
        prefs = preferences.load()
        preset = config_mod.color_preset(cfg.colors)
        self._own_colors = None if preset else dict(cfg.colors)  # to go back to after trying a preset
        self._own_keys = None if keys_mod.preset_of(cfg.keys) else dict(cfg.keys)  # likewise
        colors = [(label, key) for key, (label, _) in config_mod.COLOR_PRESETS.items()]
        if preset is None:
            colors.append(("Your own, from the settings file", OWN_COLORS))
        usage = list(USAGE_CHOICES)
        if cfg.usage_minutes not in [minutes for _, minutes in usage]:
            usage.append((f"Every {cfg.usage_minutes:g} minutes", cfg.usage_minutes))
        history = [(label, days) for days, label in config_mod.HISTORY_WINDOWS.items()]
        if cfg.history_days not in config_mod.HISTORY_WINDOWS:
            history.append((f"Past {cfg.history_days} days", cfg.history_days))
        keep = [(label, days) for days, label in config_mod.HISTORY_KEEP.items()]
        if cfg.history_keep_days not in config_mod.HISTORY_KEEP:
            keep.append((f"{cfg.history_keep_days} days", cfg.history_keep_days))
        columns = [("Fit to the number of sessions", 0)] + [(f"{n} column{'s' * (n > 1)}", n) for n in range(1, 7)]
        # agentlens's looks first, then Textual's themes.
        themes = [(look.label, key) for key, look in config_mod.LOOKS.items()]
        themes += [(t, t) for t in sorted(app.available_themes) if t not in config_mod.LOOKS]

        with VerticalScroll(id="settings-dialog"):
            yield Label(Text("Settings", style="bold"))
            yield Static(Text(f"Saved as you change them, in {tilde(cfg.path or config_mod.config_path())}",
                              style="dim"))

            yield Label("Look", classes="settings-section")
            yield row("Theme", Select(themes, value=app.theme, allow_blank=False, compact=True, id="set-theme"))
            yield row("Colours", Select(colors, value=preset or OWN_COLORS, allow_blank=False, compact=True,
                                        id="set-colors"))
            yield Static(id="set-sample", classes="settings-note")
            looks = list(config_mod.LOOKS.values())
            yield Static(f"{looks[0].name} to {looks[-1].name} are themes too, for the background and panels. "
                         "Pick one as the theme and keep the theme's colours to have it everywhere.",
                         classes="settings-note")
            yield row("Columns", Select(columns, value=cfg.columns or 0, allow_blank=False, compact=True,
                                        id="set-columns"))
            yield row("Show",
                      Checkbox("Spaces sidebar", cfg.sidebar, compact=True, id="set-sidebar"),
                      Checkbox("Tool calls", cfg.show_tools, compact=True, id="set-tools"),
                      Checkbox("Timestamps", cfg.show_times, compact=True, id="set-times"))

            yield Label("Sessions", classes="settings-section")
            mode = cfg.default_mode if cfg.default_mode in MODE_LABELS else "default"
            yield row("New sessions", Select([(label, m) for m, label in MODE_LABELS.items()], value=mode,
                                             allow_blank=False, compact=True, id="set-mode"))
            editors = [("Automatic", AUTO_EDITOR)]
            editors += [(e.label + (", in this terminal" if e.terminal else ""), key)
                        for key, e in editor_mod.EDITORS.items()]
            editors.append(("Your own command", OWN_EDITOR))
            picked = cfg.editor if cfg.editor in editor_mod.EDITORS else OWN_EDITOR if cfg.editor else AUTO_EDITOR
            yield row("Editor", Select(editors, value=picked, allow_blank=False, compact=True, id="set-editor-pick"))
            own = row("", Input((cfg.editor or "") if picked == OWN_EDITOR else "",
                                placeholder="kate --line {line} {file}", compact=True, id="set-editor"),
                      Checkbox("In this terminal", cfg.editor_terminal, compact=True, id="set-editor-terminal"),
                      id="set-editor-own")
            own.display = picked == OWN_EDITOR
            yield own
            yield Static(id="set-editor-note", classes="settings-note")
            yield row("Claude Code", Input(cfg.cli_path or "", placeholder="empty: claude on your PATH",
                                           compact=True, id="set-cli"))
            yield row("Usage meter", Select(usage, value=cfg.usage_minutes, allow_blank=False, compact=True,
                                            id="set-usage"))
            yield row("History", Select(history, value=cfg.history_days, allow_blank=False, compact=True,
                                        id="set-history"))
            yield Static(f"How far back History ({keys_mod.name('history')}) goes, by when a session was last "
                         f"active. {keys_mod.name('history-older')} there shows older ones.", classes="settings-note")
            yield row("Keep sessions", Select(keep, value=cfg.history_keep_days, allow_blank=False, compact=True,
                                              id="set-keep"))
            yield Static("How long History keeps a session once Claude Code has deleted its transcript, which it "
                         "does 30 days after it was last active. agentlens keeps times, counts, titles and file "
                         "paths, never the conversation.", classes="settings-note")

            yield Label("Keys", classes="settings-section")
            yield row("Preset", Select(self._key_choices(), value=self._key_value(), allow_blank=False, compact=True,
                                       id="set-keys"),
                      Button("Change a key…", compact=True, id="set-keys-list"))
            yield Static(id="set-keys-note", classes="settings-note")

            yield Label("What you're aiming for", classes="settings-section")
            yield row("Goals", *[Checkbox(goal, goal in prefs.goals, compact=True, id=f"goal-{goal}")
                                 for goal in preferences.GOALS])
            for key, options in preferences.STYLE_OPTIONS.items():
                choices = [("Judge from my sessions", JUDGE)] + [(option, option) for option in options]
                yield row(key.capitalize(), Select(choices, value=prefs.style.get(key, JUDGE), allow_blank=False,
                                                   compact=True, id=f"style-{key}", classes="style"))
            yield Static("These change what agentlens points out to you, not how Claude works. Goals put the "
                         "findings about them first. A style you choose on purpose mutes rules that would nag "
                         "you about it (today, steering: close does), and your profile shows where your "
                         "sessions look different.", classes="settings-note")

            with Horizontal(id="settings-buttons"):
                yield Button("Open the settings file", compact=True, id="set-file")
                yield Button("Open the preferences file", compact=True, id="set-prefs-file")
            yield Static(Text("Spaces, colours of your own and rule thresholds are in the files. Esc closes.",
                              style="dim"))

    def on_mount(self) -> None:
        self._paint_sample()
        self._show_editor()
        self._show_keys_note()

    def _set(self, name: str, value: object) -> None:
        self._app.change_setting(name, value)

    def _paint_sample(self) -> None:
        variables = self._app.get_css_variables()
        text = Text()
        for i, (name, label) in enumerate(BORDER_SAMPLE):
            text.append("  " if i else "")
            text.append("■ " + label, style=variables.get(f"al-{name}", ""))
        text.append("\n")
        text.append(" you ", style=f"bold {variables.get('al-you', '')} on {variables.get('al-card', '')}")
        for name, label in CHAT_SAMPLE:
            text.append("  " + label, style=variables.get(f"al-{name}", ""))
        self.query_one("#set-sample", Static).update(text)

    # Select posts Changed when it first shows its value too, so each of these checks it's a change.

    @on(Select.Changed, "#set-theme")
    def _theme(self, event: Select.Changed) -> None:
        if event.value != self._app.theme:
            self._set("theme", event.value)
            self._paint_sample()

    @on(Select.Changed, "#set-colors")
    def _colors(self, event: Select.Changed) -> None:
        current = self._app.config.colors
        if event.value == OWN_COLORS:
            colors = self._own_colors
        else:
            colors = dict(config_mod.COLOR_PRESETS[str(event.value)][1])
        if colors is not None and colors != current:
            self._set("colors", colors)
            self._paint_sample()

    def _key_choices(self) -> list[tuple[str, str]]:
        choices = [(label, key) for key, (label, _) in keys_mod.KEY_PRESETS.items()]
        if self._own_keys is not None:
            choices.append(("Your own, from the keys list", OWN_KEYS))
        return choices

    def _key_value(self) -> str:
        return keys_mod.preset_of(self._app.config.keys) or OWN_KEYS

    def _show_keys_note(self) -> None:
        self.query_one("#set-keys-note", Static).update(
            f"Or change any key in the keys list ({keys_mod.name('show-keys')}): Enter on it, then press the new key.")

    @on(Select.Changed, "#set-keys")
    def _keys(self, event: Select.Changed) -> None:
        if event.value == OWN_KEYS:
            keys = self._own_keys
        else:
            keys = dict(keys_mod.KEY_PRESETS[str(event.value)][1])
        if keys is not None and keys != self._app.config.keys:
            self._set("keys", keys)
            self._show_keys_note()

    @on(Button.Pressed, "#set-keys-list")
    def _open_keys_list(self) -> None:
        def closed(_: None) -> None:
            if keys_mod.preset_of(self._app.config.keys) is None:
                self._own_keys = dict(self._app.config.keys)  # what you changed there
            select = self.query_one("#set-keys", Select)
            with select.prevent(Select.Changed):  # showing them changes nothing
                select.set_options(self._key_choices())
                select.value = self._key_value()
            self._show_keys_note()

        self.app.push_screen(KeysScreen(), closed)

    @on(Select.Changed, "#set-columns")
    def _columns(self, event: Select.Changed) -> None:
        columns = int(event.value) or None
        if columns != self._app.config.columns:
            self._set("columns", columns)

    @on(Select.Changed, "#set-mode")
    def _mode(self, event: Select.Changed) -> None:
        if event.value != self._app.config.default_mode:
            self._set("default_mode", event.value)

    @on(Select.Changed, "#set-editor-pick")
    def _editor(self, event: Select.Changed) -> None:
        own = event.value == OWN_EDITOR
        self.query_one("#set-editor-own").display = own
        typed = self.query_one("#set-editor", Input)
        cfg = self._app.config
        value = typed.value.strip() if own else str(event.value)
        if own and not value:
            # Start from the editor you had, to change the words after it; saved as it goes in.
            typed.value = editor_mod.own_command(cfg.editor, cfg.editor_terminal)
        elif (value or None) != cfg.editor:
            self._set("editor", value or None)
        self._show_editor()

    def _show_editor(self) -> None:
        cfg = self._app.config
        self.query_one("#set-editor-note", Static).update(editor_mod.describe(cfg.editor, cfg.editor_terminal))

    @on(Select.Changed, "#set-usage")
    def _usage(self, event: Select.Changed) -> None:
        if event.value != self._app.config.usage_minutes:
            self._set("usage_minutes", float(event.value))

    @on(Select.Changed, "#set-history")
    def _history(self, event: Select.Changed) -> None:
        if event.value != self._app.config.history_days:
            self._set("history_days", int(event.value))

    @on(Select.Changed, "#set-keep")
    def _keep(self, event: Select.Changed) -> None:
        if event.value != self._app.config.history_keep_days:
            self._set("history_keep_days", int(event.value))

    @on(Checkbox.Changed)
    def _checked(self, event: Checkbox.Changed) -> None:
        name = CHECKBOXES.get(event.checkbox.id or "")
        if name is not None:
            if event.value != getattr(self._app.config, name):
                self._set(name, event.value)
                self._show_editor()
            return
        ticked = {box.id for box in self.query(Checkbox) if box.value}
        goals = [goal for goal in preferences.GOALS if f"goal-{goal}" in ticked]
        if goals != [goal for goal in preferences.GOALS if goal in preferences.load().goals]:
            preferences.set_goals(goals)
            self._app.preferences_changed()

    @on(Select.Changed, ".style")
    def _style(self, event: Select.Changed) -> None:
        key = (event.select.id or "").removeprefix("style-")
        if event.value != preferences.load().style.get(key, JUDGE):
            preferences.set_style(key, str(event.value) or None)
            self._app.preferences_changed()

    @on(Input.Changed)
    def _typed(self, event: Input.Changed) -> None:
        name = INPUTS[event.input.id or ""]
        if name == "editor" and self.query_one("#set-editor-pick", Select).value != OWN_EDITOR:
            return  # it's only yours once you've picked Your own command
        value = event.value.strip() or None
        if value != getattr(self._app.config, name):
            self._set(name, value)
            if name == "editor":
                self._show_editor()

    @on(Button.Pressed, "#set-file")
    async def _open_file(self) -> None:
        self._app.action_edit_settings()
        await self._show_file_changes()

    @on(Button.Pressed, "#set-prefs-file")
    async def _open_prefs_file(self) -> None:
        self._app.action_edit_preferences()
        await self._show_file_changes()

    async def _show_file_changes(self) -> None:
        await self.recompose()  # with what you changed in the file
        self._paint_sample()

    def action_close(self) -> None:
        self.dismiss(None)
