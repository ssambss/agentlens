"""The colours list (Change a colour… in the settings): every colour agentlens uses, and Enter on
one picks it, so a preset's colour you don't like doesn't have to stay.

Picking one makes the colours your own, starting from the ones you had: the settings' Colours
picker then says so, and a preset there puts its own back. Changes save and show as you make them.
"""

from __future__ import annotations

from typing import Any

from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.color import Color, ColorParseError
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from agentlens import config as config_mod
from agentlens.frames import PacedScreen
from agentlens.textbox import TextInput

# The theme's own colours, which follow it when you change theme: its variable, and what to call it.
THEME_COLORS = {"$foreground": "text", "$primary": "primary", "$secondary": "secondary", "$accent": "accent",
                "$warning": "warning", "$success": "success", "$error": "error"}
# Fixed colours to pick from, bright enough for dark themes. Any other you type.
SWATCHES = {
    "White": "#FFFFFF", "Silver": "#C0C0C0", "Grey": "#8A8A8A", "Red": "#FF5252", "Coral": "#FF7F6B",
    "Orange": "#FF9F40", "Amber": "#FFB000", "Gold": "#FFD54F", "Yellow": "#FFEB3B", "Lime": "#C6FF00",
    "Green": "#00E676", "Mint": "#69F0AE", "Teal": "#1DE9B6", "Cyan": "#00E5FF", "Sky": "#40C4FF",
    "Blue": "#448AFF", "Periwinkle": "#8C9EFF", "Violet": "#B388FF", "Purple": "#D500F9", "Magenta": "#E040FB",
    "Pink": "#FF4081", "Rose": "#F48FB1",
}


def resolve(value: str, variables: dict[str, str]) -> str | None:
    """A colour as the settings file has it (#ff8800, orange, $accent), as a hex colour; None if it isn't one."""
    if value.startswith("$"):
        value = variables.get(value[1:], "")
    try:
        return Color.parse(value).hex6 if value else None
    except ColorParseError:
        return None


def sample(name: str, color: str, text: str) -> Text:
    """`text` the way the colour `name` uses `color`: behind it, for the card behind your messages."""
    if name == "card":
        return Text(f" {text} ", style=f"on {color}")
    return Text(text, style=color)


def swatch(name: str, color: str) -> Text:
    """Four cells of the colour, as `name` uses it."""
    return Text("    ", style=f"on {color}") if name == "card" else Text("■■■■", style=color)


class ColorsScreen(PacedScreen, ModalScreen[None]):
    """Every colour, grouped, each in its colour now. Esc closes it."""

    BINDINGS = [Binding("escape", "close", "Close")]

    @property
    def _app(self) -> Any:
        """The agentlens app, untyped: tui imports this module, so this one can't import it."""
        return self.app

    def compose(self) -> ComposeResult:
        with Vertical(id="colors-dialog"):
            yield Label(Text("Colours", style="bold"))
            yield OptionList(*self._options(), id="colors-list", compact=True)
            yield Static(Text("Enter on a colour picks another for it. The settings' Colours picker puts a "
                              "preset's back. Esc closes this list.", style="dim"), id="colors-note")

    def on_mount(self) -> None:
        listing = self.query_one("#colors-list", OptionList)
        listing.highlighted = next(i for i, option in enumerate(listing.options) if not option.disabled)
        listing.focus()

    def _options(self) -> list[Option]:
        colors, variables = self._app.config.colors, self._app.get_css_variables()
        options = []
        for i, (group, names) in enumerate(config_mod.COLOR_GROUPS.items()):
            options.append(Option(Text.assemble("\n" if i else "", (group, "bold underline")), disabled=True))
            for name, about in names.items():
                grid = Table.grid(padding=(0, 2))
                grid.add_column(width=4, no_wrap=True)
                grid.add_column(ratio=1)
                grid.add_column(no_wrap=True, style="dim")
                value = colors.get(name, config_mod.DEFAULT_COLORS[name])
                grid.add_row(swatch(name, variables.get(f"al-{name}", "")), about, value)
                options.append(Option(grid, id=name))
        return options

    @on(OptionList.OptionSelected, "#colors-list")
    def _change(self, event: OptionList.OptionSelected) -> None:
        name = event.option.id or ""

        def chosen(value: str | None) -> None:
            colors = self._app.config.colors
            if value and value != colors.get(name):
                self._app.change_setting("colors", {**colors, name: value})
                self._refill()

        self.app.push_screen(ColorPickScreen(name), chosen)

    def _refill(self) -> None:
        listing = self.query_one("#colors-list", OptionList)
        at = listing.highlighted
        listing.set_options(self._options())
        listing.highlighted = at

    def action_close(self) -> None:
        self.dismiss(None)


class ColorPickScreen(PacedScreen, ModalScreen[str | None]):
    """Picks one colour: one of the theme's, a fixed one, or one you type. Returns it as the settings
    file has it, or None to leave it as it is."""

    BINDINGS = [Binding("escape", "close", "Leave it")]

    def __init__(self, name: str) -> None:
        super().__init__()
        self.color_name = name
        self._values: list[str | None] = []  # each option's colour; None for the headings

    @property
    def _app(self) -> Any:
        return self.app

    def compose(self) -> ComposeResult:
        about = config_mod.COLOR_ABOUT[self.color_name]
        now = self._app.config.colors.get(self.color_name, config_mod.DEFAULT_COLORS[self.color_name])
        with Vertical(id="pick-dialog"):
            yield Label(Text.assemble("The colour for ", (about, "bold")))
            yield Static(Text(f"Now {now}", style="dim"))
            yield OptionList(*self._options(), id="pick-list", compact=True)
            yield TextInput("", placeholder="Or type one: #ff8800, orange, $accent", compact=True, id="pick-own")
            yield Static(id="pick-preview")
            yield Static(Text("Enter picks it; Esc leaves it as it is.", style="dim"))

    def _options(self) -> list[Option]:
        variables = self._app.get_css_variables()
        default = config_mod.DEFAULT_COLORS[self.color_name]
        theme = dict(THEME_COLORS)
        if default not in theme:  # like $panel-lighten-2: the theme's, but not one of its main colours
            theme = {default: default.removeprefix("$"), **theme}
        options: list[Option] = []
        self._values = []

        def add(prompt: Text | str, value: str | None) -> None:
            options.append(Option(prompt, disabled=value is None))
            self._values.append(value)

        add(Text("The theme's, which follow it", style="bold underline"), None)
        for value, called in theme.items():
            line = Text.assemble(swatch(self.color_name, resolve(value, variables) or ""), f"  {called}")
            if value == default:
                line.append("  agentlens's", style="dim")
            add(line, value)
        add(Text("\nFixed", style="bold underline"), None)
        for called, value in SWATCHES.items():
            add(Text.assemble(swatch(self.color_name, value), f"  {called}", (f"  {value}", "dim")), value)
        return options

    def on_mount(self) -> None:
        listing = self.query_one("#pick-list", OptionList)
        now = self._app.config.colors.get(self.color_name, config_mod.DEFAULT_COLORS[self.color_name])
        same = [i for i, value in enumerate(self._values) if value is not None and value.lower() == now.lower()]
        listing.highlighted = same[0] if same else next(i for i, value in enumerate(self._values) if value)
        listing.focus()
        if not same:
            own = self.query_one("#pick-own", Input)
            own.value = now  # one you typed, or set in the file: to change from
        self._preview(now)

    def _preview(self, value: str) -> None:
        color = resolve(value, self._app.get_css_variables())
        about = config_mod.COLOR_ABOUT[self.color_name]
        shown = (sample(self.color_name, color, f"╭─ {about} ─╮") if color
                 else Text(f"{value} isn't a colour: try #ff8800, orange or $accent.", style="italic"))
        self.query_one("#pick-preview", Static).update(shown)

    @on(OptionList.OptionHighlighted, "#pick-list")
    def _highlighted(self, event: OptionList.OptionHighlighted) -> None:
        value = self._values[event.option_index]
        if value:
            self._preview(value)

    @on(OptionList.OptionSelected, "#pick-list")
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self._values[event.option_index])

    @on(Input.Changed, "#pick-own")
    def _typing(self, event: Input.Changed) -> None:
        if event.value.strip():
            self._preview(event.value.strip())

    @on(Input.Submitted, "#pick-own")
    def _typed(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if value and resolve(value, self._app.get_css_variables()):
            self.dismiss(value)
        elif value:
            self._preview(value)

    def action_close(self) -> None:
        self.dismiss(None)
