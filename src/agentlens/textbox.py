"""The boxes you type in, with a Windows text box's editing keys: Ctrl+Backspace and Ctrl+Delete
take a word, Shift with the arrows selects (with Ctrl, a word at a time), Ctrl+C copies what's
selected and Ctrl+V pastes what's on the clipboard.

`TextInput` is one line. `MessageBox` is a message to Claude: Enter sends it, Shift+Enter starts a
new line, and the box grows with what you type. Alt+Enter and Ctrl+J start one too, for terminals
that send Shift+Enter as a plain Enter. In Windows' terminals, and on a Linux desktop's X, agentlens
tells the two apart itself (see terminal.py)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, TextArea

from agentlens import clipboard

NEW_LINE_KEYS = {"shift+enter", "alt+enter", "ctrl+j"}


def paste(box: Widget, insert: Callable[[str], object]) -> None:
    """Ctrl+V: what's on the clipboard, into the box. Textual's own takes only what was copied in
    agentlens, which is what's left where agentlens can't read the clipboard (over SSH)."""

    async def pasting() -> None:
        text = await asyncio.to_thread(clipboard.paste_text)
        if text is None:
            text = box.app.clipboard
        if text:
            insert(text)

    box.run_worker(pasting(), group="paste-text", exclusive=True)


class TextInput(Input):
    """A one-line box. Textual's own has Ctrl+Backspace take the word to the right."""

    BINDINGS = [
        Binding("ctrl+backspace", "delete_left_word", "Delete the word to the left", show=False),
        Binding("ctrl+delete", "delete_right_word", "Delete the word to the right", show=False),
    ]

    def action_paste(self) -> None:
        paste(self, lambda text: self.replace(text.splitlines()[0], *self.selection))  # a line, as the terminal's


class MessageBox(TextArea):
    """A message, over as many lines as it takes. `value` is its text, as an Input's is."""

    DEFAULT_CSS = """
    MessageBox { height: auto; max-height: 10; }
    """
    BINDINGS = [Binding("ctrl+delete", "delete_word_right", "Delete the word to the right", show=False)]

    class Submitted(Message):
        """Enter: the message, as typed."""

        def __init__(self, box: MessageBox, value: str) -> None:
            super().__init__()
            self.box, self.value = box, value

        @property
        def control(self) -> MessageBox:
            return self.box

    def __init__(self, value: str = "", *, placeholder: str = "", **kwargs) -> None:
        super().__init__(value, soft_wrap=True, tab_behavior="focus", highlight_cursor_line=False,
                         placeholder=placeholder, **kwargs)

    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, text: str) -> None:
        self.text = text
        self.move_cursor(self.document.end)  # to go on typing after it

    def action_paste(self) -> None:
        if not self.read_only:
            paste(self, lambda text: self.replace(text, *self.selection, maintain_selection_offset=False))

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self, self.text))
            return
        if event.key in NEW_LINE_KEYS:
            event.stop()
            event.prevent_default()
            if not self.read_only:
                self.replace("\n", *self.selection, maintain_selection_offset=False)
            return
        await super()._on_key(event)
