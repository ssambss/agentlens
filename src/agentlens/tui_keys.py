"""The keys list (?): every key, grouped, and Enter on one changes it.

The footer only has room for the keys that apply right now; this lists them all, with the
ones you changed. Changes save as you make them, and the settings (,) have presets. See
keys.py for which keys there are and how they're kept.
"""

from __future__ import annotations

from typing import Any

from rich.table import Table
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList, Static
from textual.widgets.option_list import Option

from agentlens import keys as keys_mod
from agentlens.frames import PacedScreen


class KeysScreen(PacedScreen, ModalScreen[None]):
    """Every key, grouped. Esc, the key that opened it, or quit's key closes it."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        keys_mod.binding("show-keys", "close", "Close"),
        keys_mod.binding("quit", "close", "Close"),
    ]

    @property
    def _app(self) -> Any:
        """The agentlens app, untyped: tui imports this module, so this one can't import it."""
        return self.app

    def compose(self) -> ComposeResult:
        with Vertical(id="keys-dialog"):
            yield Label(Text("Keys", style="bold"))
            yield OptionList(*self._options(), id="keys-list", compact=True)
            yield Static(self._footnote(), id="keys-note")

    def on_mount(self) -> None:
        listing = self.query_one("#keys-list", OptionList)
        listing.highlighted = next(i for i, option in enumerate(listing.options) if not option.disabled)
        listing.focus()

    def _options(self) -> list[Option]:
        keymap = self._app.config.keys
        shown = [keys_mod.names(e.id, keymap) if isinstance(e, keys_mod.Action) else e.keys
                 for _, entries in keys_mod.HELP for e in entries]
        width = max(map(len, shown)) + 3

        def line(keys: str, what: Text) -> Table:
            """The keys, then what they do, wrapping under itself."""
            grid = Table.grid()
            grid.add_column(width=width, no_wrap=True, style="bold")
            grid.add_column()
            grid.add_row(keys, what)
            return grid

        options = []
        for i, (group, entries) in enumerate(keys_mod.HELP):
            options.append(Option(Text.assemble("\n" if i else "", (group, "bold underline")), disabled=True))
            for entry in entries:
                if isinstance(entry, keys_mod.Fixed):
                    options.append(Option(line(entry.keys, Text(entry.help)), disabled=True))
                    continue
                what = Text(entry.help)
                if entry.id in keymap:
                    what.append(f"  (agentlens's: {keys_mod.names(entry.id, {})})", style="dim")
                options.append(Option(line(keys_mod.names(entry.id, keymap), what), id=entry.id))
        return options

    @on(OptionList.OptionSelected, "#keys-list")
    def _change(self, event: OptionList.OptionSelected) -> None:
        def chosen(changes: dict[str, str] | None) -> None:
            if changes:
                self._app.change_keys(changes)
                self._refill()

        action = keys_mod.ACTIONS[event.option.id or ""]
        self.app.push_screen(KeyCaptureScreen(action, dict(self._app.config.keys)), chosen)

    def _refill(self) -> None:
        listing = self.query_one("#keys-list", OptionList)
        at = listing.highlighted
        listing.set_options(self._options())
        listing.highlighted = at
        self.query_one("#keys-note", Static).update(self._footnote())

    @staticmethod
    def _footnote() -> Text:
        return Text(f"Enter on a key changes it, and the settings ({keys_mod.name('settings')}) have presets. "
                    "Esc closes this list.", style="dim")

    def action_close(self) -> None:
        self.dismiss(None)


class KeyCaptureScreen(PacedScreen, ModalScreen[dict[str, str] | None]):
    """Waits for the new key for one action. Returns the keys to change: that action's, and
    those of any action that gives the key up to it."""

    def __init__(self, target: keys_mod.Action, keymap: dict[str, str]) -> None:
        super().__init__()
        self.target, self.keymap = target, keymap
        self._taking: str | None = None  # another action's key, pressed once: again takes it

    def compose(self) -> ComposeResult:
        now, own = keys_mod.names(self.target.id, self.keymap), keys_mod.names(self.target.id, {})
        with Vertical(id="capture-dialog"):
            yield Label(Text.assemble("The key to ", (self.target.help, "bold")))
            yield Static(Text(f"Now {now}" + (f", agentlens's {own}" if now != own else ""), style="dim"))
            yield Static("Press the new key. Backspace puts agentlens's back; Esc leaves it as it is.",
                         classes="capture-ask")
            yield Static(id="capture-note")

    def on_key(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        key = event.key
        if key == "escape":
            self.dismiss(None)
        elif key == "backspace":
            self._back_to_own()
        elif key in keys_mod.RESERVED:
            self._note(Text.assemble((keys_mod.show(key), "bold"), f" can't be changed: {keys_mod.RESERVED[key]}."))
        else:
            self._take(key)

    def _take(self, key: str) -> None:
        changes, others = keys_mod.assign(self.target.id, key, self.keymap)
        shown = keys_mod.show(key)
        if not changes:
            self._note(Text.assemble((shown, "bold"), " is the key to ", self._helps(others),
                                     ", and giving it up leaves that sharing a key. Press another key."))
        elif not others or self._taking == key:
            self.dismiss(changes)
        else:
            self._taking = key
            after = keys_mod.changed(self.keymap, changes)
            then = "; ".join(f"{'it' if len(others) == 1 else o.help} then has {keys_mod.names(o.id, after)}"
                             for o in others)
            self._note(Text.assemble((shown, "bold"), " is already the key to ", self._helps(others), ". Press ",
                                     (shown, "bold"), f" again to take it ({then}), or another key."))

    def _back_to_own(self) -> None:
        changes = {self.target.id: self.target.keys}
        after = keys_mod.changed(self.keymap, changes)
        clash = next(((k, b if a.id == self.target.id else a) for k, a, b in keys_mod.clashes(after)
                      if self.target.id in (a.id, b.id)), None)
        if clash is None:
            self.dismiss(changes)
        else:
            key, other = clash
            self._note(Text.assemble("agentlens's ", (keys_mod.show(key), "bold"), " is the key to ",
                                     (other.help, "italic"), " now. Change that one first."))

    @staticmethod
    def _helps(actions: list[keys_mod.Action]) -> Text:
        return Text("; ").join(Text(a.help, style="italic") for a in actions)

    def _note(self, text: Text) -> None:
        self.query_one("#capture-note", Static).update(text)
