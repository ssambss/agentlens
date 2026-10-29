"""The terminal tab panopsis runs in: its name, kept while the `claude` processes it starts share it,
and in Windows' own console window, glyphs its font can draw and a cursor that stays hidden."""

from __future__ import annotations

import functools
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.driver import Driver

TITLE = "panopsis"
WINDOWS = os.name == "nt"

# Every `claude` panopsis starts shares its terminal and would rename the tab "claude".
# This is Claude Code's own setting for leaving the title alone.
CLAUDE_ENV = {"CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1"}

# Windows' own console window draws with Consolas, which lacks some of the glyphs Textual and
# panopsis draw: each shows as a box with a question mark in it. These are the nearest it has.
CONSOLE_GLYPHS = {
    # The thin blocks most borders (an input box's, a button's), scrollbars and the usage meter are
    # made of: half blocks for the eighths and quarters, a full one for seven eighths.
    "▔": "▀", "▁": "▄", "▂": "▄", "▃": "▄", "▅": "▄", "▆": "▄", "▇": "█",
    "▏": "▌", "▎": "▌", "▍": "▌", "▋": "▌", "▊": "▌", "▉": "█", "▕": "▐",
    "▘": "▀", "▝": "▀", "▖": "▄", "▗": "▄", "▛": "█", "▜": "█", "▙": "█", "▟": "█",
    # The space you're in, the header's button for the commands, a feed's errors, notices and
    # compactions, finished subagents, the advice's bullets and the command palette's search.
    "▶": "►", "◀": "◄", "⭘": "○", "✗": "×", "◆": "♦", "⟳": "≈", "✓": "√", "‣": "▸", "⭑": "*", "🔎": "> ",
    # The spinner while Claude works on your message.
    "✢": "+", "✻": "✶", "✽": "☼",
}
_LACKING = re.compile("|".join(map(re.escape, CONSOLE_GLYPHS)))

# Textual hides the cursor once, when it starts. Resizing Windows' own console window shows it
# again, and it then sits in the message box and flickers wherever Textual writes.
HIDE_CURSOR = "\x1b[?25l"


def claude_env() -> dict[str, str]:
    """The environment for a `claude` panopsis runs: yours, leaving the tab's name alone."""
    return {**os.environ, **CLAUDE_ENV}


def set_title(title: str = TITLE) -> None:
    """Name the tab. Outside Windows, only while Textual isn't drawing: before or after the
    app runs, or while it's suspended."""
    if WINDOWS:
        _kernel32().SetConsoleTitleW(title)
    else:
        _write(f"\x1b]0;{title}\x07")


@contextmanager
def titled(title: str = TITLE) -> Iterator[None]:
    """Name the tab while panopsis runs; the name it had comes back when panopsis quits."""
    if WINDOWS:
        import ctypes

        before = ctypes.create_unicode_buffer(1024)
        had = _kernel32().GetConsoleTitleW(before, len(before))
    else:
        _write("\x1b[22;0t")  # the terminal keeps the tab's name
    set_title(title)
    try:
        yield
    finally:
        if not WINDOWS:
            _write("\x1b[23;0t")  # and puts it back
        elif had:
            _kernel32().SetConsoleTitleW(before.value)


@functools.cache
def classic_console() -> bool:
    """Whether panopsis is in Windows' own console window. Windows Terminal, VS Code and other
    terminals run the console behind a hidden stand-in window, and draw it themselves."""
    if not WINDOWS:
        return False
    import ctypes
    from ctypes import wintypes

    kernel32, user32 = _kernel32(), ctypes.WinDLL("user32")
    kernel32.GetConsoleWindow.restype = wintypes.HWND
    window = kernel32.GetConsoleWindow()
    if not window:
        return False
    name = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(wintypes.HWND(window), name, len(name))
    return name.value == "ConsoleWindowClass" and bool(user32.IsWindowVisible(wintypes.HWND(window)))


def fit_glyphs_to_console(driver_class: type[Driver]) -> type[Driver]:
    """In Windows' own console window, Textual's driver writing glyphs its font has in place of
    the ones it lacks. Elsewhere, the driver as it is."""
    if not classic_console():
        return driver_class

    class ConsoleDriver(driver_class):  # type: ignore[valid-type, misc]
        def write(self, data: str) -> None:
            super().write(fit_glyphs(data))

    return ConsoleDriver


def fit_glyphs(text: str) -> str:
    """The text with the glyphs Windows' own console window's font lacks swapped for ones it has."""
    return _LACKING.sub(lambda m: CONSOLE_GLYPHS[m[0]], text)


def _kernel32():
    import ctypes

    return ctypes.WinDLL("kernel32")


def _write(sequence: str) -> None:
    out = sys.__stdout__
    if out is not None and out.isatty():
        out.write(sequence)
        out.flush()
