"""The boxes you type in, with a Windows text box's editing keys: Ctrl+Backspace and Ctrl+Delete
take a word, Shift with the arrows selects (with Ctrl, a word at a time), Ctrl+C copies what's
selected.

`TextInput` is one line. `MessageBox` is a message to Claude: Enter sends it, Shift+Enter starts a
new line, and the box grows with what you type. Ctrl+J starts one too, for terminals that send
Shift+Enter as a plain Enter. In Windows' terminals panopsis tells the two apart itself (see
terminal.py)."""

from __future__ import annotations

from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Input, TextArea

NEW_LINE_KEYS = {"shift+enter", "ctrl+j"}


class TextInput(Input):
    """A one-line box. Textual's own has Ctrl+Backspace take the word to the right."""

    BINDINGS = [
        Binding("ctrl+backspace", "delete_left_word", "Delete the word to the left", show=False),
        Binding("ctrl+delete", "delete_right_word", "Delete the word to the right", show=False),
    ]


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
