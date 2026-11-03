"""The terminal tab panopsis runs in: its name, kept while the `claude` processes it starts share it,
and in Windows, the keys its consoles don't pass on as such, an icon and taskbar button of its own,
and in Windows' own console window, glyphs its font can draw and a cursor that stays hidden."""

from __future__ import annotations

import functools
import os
import re
import sys
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual import events
    from textual.driver import Driver

TITLE = "panopsis"
WINDOWS = os.name == "nt"
# The taskbar's name for the window panopsis is in, while it is: a button of its own, apart from
# the other terminals' and consoles'.
APP_ID = "panopsis.wall"
# The windows that can take panopsis's icon: Windows' own console window and Windows Terminal's.
# Other terminals (an editor's, say) are one window for much else, so they keep theirs.
ICON_HOSTS = {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"}

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


# Keys --------------------------------------------------------------------------------------------
# Windows' consoles pass keys on to an app as text, as Textual reads them, and the text leaves two
# out. Enter and Shift+Enter are both \r: whether Shift is down as it comes tells them apart.
# Ctrl+Backspace is \x08, which Textual takes for Backspace (\x7f, as the consoles send it).

VK_SHIFT = 0x10


def read_windows_keys(driver_class: type[Driver]) -> type[Driver]:
    """In Windows, Textual's driver passing on Shift+Enter and Ctrl+Backspace as such."""
    if not WINDOWS:
        return driver_class

    class KeysDriver(driver_class):  # type: ignore[valid-type, misc]
        def process_message(self, message) -> None:
            from textual import events

            if isinstance(message, events.Key):
                message = windows_key(message, shift_down)
            super().process_message(message)

    return KeysDriver


def windows_key(key: events.Key, shift_down: Callable[[], bool]) -> events.Key:
    """The key a Windows console meant by the one Textual read."""
    from textual import events

    if key.key == "enter" and shift_down():
        return events.Key("shift+enter", None)
    if key.key == "backspace" and key.character == "\x08":
        return events.Key("ctrl+backspace", None)
    return key


def shift_down() -> bool:
    """Whether a Shift key is down now. Read as the key comes in, it's down for Shift+Enter."""
    import ctypes

    return bool(ctypes.WinDLL("user32").GetAsyncKeyState(VK_SHIFT) & 0x8000)


# The icon ------------------------------------------------------------------------------------------
# A window takes an icon with WM_SETICON, but Windows Terminal's taskbar button shows Windows
# Terminal's whatever its window's is. With an app id of its own, the window is an app of its own to
# the taskbar: a button of its own, with the window's icon.

WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
SM_CXICON, SM_CXSMICON = 11, 49
GA_ROOTOWNER = 3
SMTO_ABORTIFHUNG = 0x0002
CTRL_CLOSE_EVENT = 2
VT_LPWSTR = 31
IID_PROPERTY_STORE = "886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"
APP_ID_KEY = ("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3", 5)  # PKEY_AppUserModel_ID


@contextmanager
def iconed() -> Iterator[None]:
    """In Windows, the window panopsis is in shows panopsis's icon, on a taskbar button of its own,
    while panopsis runs; then it has its own again. That holds if you close the tab panopsis is in,
    and the window stays open with others."""
    if not WINDOWS:
        yield
        return
    icon = _Icon()
    giving = threading.Thread(target=icon.give, name="panopsis-icon", daemon=True)
    giving.start()  # drawing it is a moment's work the app needn't wait for
    try:
        yield
    finally:
        giving.join(5)
        icon.put_back()
        if icon.on_close is not None:
            _kernel32().SetConsoleCtrlHandler(icon.on_close, False)


class _Icon:
    """panopsis's icon on the window it's in, and what puts the window's own back."""

    def __init__(self) -> None:
        self.restore: Callable[[], None] | None = None
        self.on_close = None  # the console's close handler, while the icon is on
        self._lock = threading.Lock()

    def give(self) -> None:
        window = host_window()
        if window is None:
            return
        try:
            restore = _give_icon(window)
        except Exception:  # noqa: BLE001 - an icon isn't worth more than this
            return
        with self._lock:
            self.restore = restore
            self.on_close = _closing(self.put_back)

    def put_back(self) -> None:
        """On quitting, or from the console's close handler: whichever comes first."""
        with self._lock:
            restore, self.restore = self.restore, None
        if restore is not None:
            restore()


def host_window() -> int | None:
    """The window panopsis shows in: Windows' own console window, or Windows Terminal's, which
    owns the hidden one that stands in for its console. None in another terminal, or none at all."""
    import ctypes
    from ctypes import wintypes

    kernel32, user32 = _kernel32(), ctypes.WinDLL("user32")
    kernel32.GetConsoleWindow.restype = wintypes.HWND
    user32.GetAncestor.argtypes, user32.GetAncestor.restype = [wintypes.HWND, wintypes.UINT], wintypes.HWND
    console = kernel32.GetConsoleWindow()
    window = console and (user32.GetAncestor(console, GA_ROOTOWNER) or console)
    if not window or not user32.IsWindowVisible(wintypes.HWND(window)):
        return None
    name = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(wintypes.HWND(window), name, len(name))
    return window if name.value in ICON_HOSTS else None


def _give_icon(window: int) -> Callable[[], None]:
    """Give the window panopsis's icon and app id; returns what puts its own back. If either
    can't be given, nothing is, and it raises."""
    import ctypes
    from ctypes import wintypes

    from panopsis import icon

    user32 = ctypes.WinDLL("user32")
    user32.CreateIconFromResourceEx.argtypes = [ctypes.c_char_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
                                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.CreateIconFromResourceEx.restype = wintypes.HICON
    user32.GetDpiForWindow.argtypes, user32.GetDpiForWindow.restype = [wintypes.HWND], wintypes.UINT
    dpi = user32.GetDpiForWindow(window) or 96
    icons = {}
    for which, metric in ((ICON_SMALL, SM_CXSMICON), (ICON_BIG, SM_CXICON)):
        size = user32.GetSystemMetricsForDpi(metric, dpi)
        data = icon.png(size)
        icons[which] = user32.CreateIconFromResourceEx(data, len(data), True, 0x00030000, size, size, 0)
    had: dict[int, int | None] = {}
    had_id: list[str | None] = []

    def restore() -> None:
        try:
            if had_id:
                _set_app_id(window, had_id[0])
        finally:
            for which, handle in had.items():
                _set_icon(window, which, handle)
            for handle in icons.values():
                if handle:
                    user32.DestroyIcon(wintypes.HICON(handle))

    try:
        if not all(icons.values()):
            raise OSError("no icon")
        had_id.append(_set_app_id(window, APP_ID))
        for which, handle in icons.items():
            had[which] = _set_icon(window, which, handle)
    except Exception:
        restore()  # what was done, undone
        raise
    return restore


def _set_icon(window: int, which: int, handle: int | None) -> int | None:
    """Set one of the window's icons; returns the one it had. Not if its window has hung."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                           wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    had = ctypes.c_size_t()
    user32.SendMessageTimeoutW(window, WM_SETICON, which, handle or 0, SMTO_ABORTIFHUNG, 1000, ctypes.byref(had))
    return had.value or None


def _set_app_id(window: int, app_id: str | None) -> str | None:
    """Give the window an app id of its own, or take it off (None); returns the one it had."""
    import ctypes
    from ctypes import wintypes

    class Guid(ctypes.Structure):
        _fields_ = [("bytes", ctypes.c_ubyte * 16)]

    class PropertyKey(ctypes.Structure):
        _fields_ = [("fmtid", Guid), ("pid", wintypes.DWORD)]

    class PropVariant(ctypes.Structure):
        _fields_ = [("vt", wintypes.USHORT), ("reserved", wintypes.USHORT * 3), ("value", ctypes.c_void_p),
                    ("more", ctypes.c_void_p)]

    def guid(text: str) -> Guid:
        g = Guid()
        ctypes.memmove(g.bytes, uuid.UUID(text).bytes_le, 16)
        return g

    ole32 = ctypes.WinDLL("ole32")
    started = ole32.CoInitializeEx(None, 2) in (0, 1)  # S_OK, S_FALSE; not where COM runs another way already
    store = ctypes.c_void_p()
    try:
        ctypes.OleDLL("shell32").SHGetPropertyStoreForWindow(
            wintypes.HWND(window), ctypes.byref(guid(IID_PROPERTY_STORE)), ctypes.byref(store))
        vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents

        def method(index: int, *args):  # IPropertyStore's, by its place in the interface
            return ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *args)(vtable[index])

        try:
            key = PropertyKey(guid(APP_ID_KEY[0]), APP_ID_KEY[1])
            had = PropVariant()
            method(5, ctypes.POINTER(PropertyKey), ctypes.POINTER(PropVariant))(store, key, had)  # GetValue
            had_id = ctypes.wstring_at(had.value) if had.vt == VT_LPWSTR and had.value else None
            ole32.PropVariantClear(ctypes.byref(had))
            text = ctypes.create_unicode_buffer(app_id or "")
            # An empty value takes it off.
            new = PropVariant(VT_LPWSTR, value=ctypes.cast(text, ctypes.c_void_p).value) if app_id else PropVariant()
            method(6, ctypes.POINTER(PropertyKey), ctypes.POINTER(PropVariant))(store, key, new)  # SetValue
            method(7)(store)  # Commit
            return had_id
        finally:
            method(2)(store)  # Release
    finally:
        if started:
            ole32.CoUninitialize()


def _closing(put_back: Callable[[], None]):
    """Put the window's own icon back when its console closes: the tab panopsis is in, say. Kept by
    the caller, so it outlives this call, as the console's handler."""
    import ctypes
    from ctypes import wintypes

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    def handler(event: int) -> bool:
        if event == CTRL_CLOSE_EVENT:
            put_back()
        return False  # and on to Python's, and Windows' own, which end panopsis

    _kernel32().SetConsoleCtrlHandler(handler, True)
    return handler


def _kernel32():
    import ctypes

    return ctypes.WinDLL("kernel32")


def _write(sequence: str) -> None:
    out = sys.__stdout__
    if out is not None and out.isatty():
        out.write(sequence)
        out.flush()
