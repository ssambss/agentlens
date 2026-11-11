"""The X server, asked straight through libX11 for what Linux's terminals don't pass on, and for the
clipboard when no clipboard tool is installed.

Most Linux terminals send Shift+Enter as a plain Enter (those with the kitty keyboard protocol
apart), but on an X desktop the X server knows which keys are down, as Windows does.

The clipboard is xclip's way: `python -m agentlens.x11 copy` takes the text on stdin and holds it
in the background, handing it to whatever pastes it, until something else is copied; `paste` writes
what's on the clipboard to stdout. It's a process of its own, as xclip is, so what you copied
stays pastable after agentlens quits, and agentlens never waits on a program that's slow to answer.
"""

from __future__ import annotations

import ctypes
import functools
import os
import select
import sys
import time
from collections.abc import Callable
from ctypes import POINTER, byref, c_char_p, c_int, c_long, c_uint, c_ulong, c_void_p

SHIFT_MASK = 1
NONE = CURRENT_TIME = ANY_PROPERTY_TYPE = 0
XA_ATOM, XA_STRING = 4, 31
PROP_MODE_REPLACE = 0
PROPERTY_CHANGE_MASK = 1 << 22
PROPERTY_NOTIFY, SELECTION_CLEAR, SELECTION_REQUEST, SELECTION_NOTIFY = 28, 29, 30, 31
NEW_VALUE = 0

# The forms text is asked for in, best first: STRING is Latin-1.
TEXT_FORMS = ("UTF8_STRING", "text/plain;charset=utf-8", "STRING")
IMAGE_FORMS = ("image/png", "image/jpeg", "image/bmp", "image/gif", "image/webp", "image/tiff")
WAIT = 3.0  # seconds to wait on the program that has the clipboard, for each answer


class _SelectionRequest(ctypes.Structure):
    _fields_ = [("type", c_int), ("serial", c_ulong), ("send_event", c_int), ("display", c_void_p),
                ("owner", c_ulong), ("requestor", c_ulong), ("selection", c_ulong), ("target", c_ulong),
                ("property", c_ulong), ("time", c_ulong)]


class _SelectionNotify(ctypes.Structure):
    _fields_ = [("type", c_int), ("serial", c_ulong), ("send_event", c_int), ("display", c_void_p),
                ("requestor", c_ulong), ("selection", c_ulong), ("target", c_ulong), ("property", c_ulong),
                ("time", c_ulong)]


class _PropertyNotify(ctypes.Structure):
    _fields_ = [("type", c_int), ("serial", c_ulong), ("send_event", c_int), ("display", c_void_p),
                ("window", c_ulong), ("atom", c_ulong), ("time", c_ulong), ("state", c_int)]


class _Event(ctypes.Union):
    _fields_ = [("type", c_int), ("request", _SelectionRequest), ("notify", _SelectionNotify),
                ("property", _PropertyNotify), ("pad", c_long * 24)]


_ErrorHandler = ctypes.CFUNCTYPE(c_int, c_void_p, c_void_p)
# An error (a window gone before it's answered, say) is let pass: Xlib's own handler ends the program.
_IGNORE_ERRORS = _ErrorHandler(lambda display, error: 0)


@functools.cache
def xlib():
    """libX11, its functions declared; None where there isn't one. macOS's (XQuartz) isn't the
    desktop its terminals run on."""
    if sys.platform in ("win32", "darwin"):
        return None
    try:
        lib = ctypes.CDLL("libX11.so.6")
    except OSError:
        return None
    D, X = c_void_p, c_ulong  # a Display*, and a window or an atom
    for name, restype, argtypes in (
        ("XOpenDisplay", D, [c_char_p]),
        ("XCloseDisplay", c_int, [D]),
        ("XDefaultRootWindow", X, [D]),
        ("XQueryPointer", c_int, [D, X, POINTER(X), POINTER(X), POINTER(c_int), POINTER(c_int), POINTER(c_int),
                                  POINTER(c_int), POINTER(c_uint)]),
        ("XSetErrorHandler", c_void_p, [_ErrorHandler]),
        ("XInternAtom", X, [D, c_char_p, c_int]),
        ("XCreateSimpleWindow", X, [D, X, c_int, c_int, c_uint, c_uint, c_uint, c_ulong, c_ulong]),
        ("XSelectInput", c_int, [D, X, c_long]),
        ("XSetSelectionOwner", c_int, [D, X, X, c_ulong]),
        ("XGetSelectionOwner", X, [D, X]),
        ("XConvertSelection", c_int, [D, X, X, X, X, c_ulong]),
        ("XChangeProperty", c_int, [D, X, X, X, c_int, c_int, c_void_p, c_int]),
        ("XGetWindowProperty", c_int, [D, X, X, c_long, c_long, c_int, X, POINTER(X), POINTER(c_int),
                                       POINTER(c_ulong), POINTER(c_ulong), POINTER(c_void_p)]),
        ("XSendEvent", c_int, [D, X, c_int, c_long, POINTER(_Event)]),
        ("XNextEvent", c_int, [D, POINTER(_Event)]),
        ("XPending", c_int, [D]),
        ("XConnectionNumber", c_int, [D]),
        ("XFlush", c_int, [D]),
        ("XFree", c_int, [c_void_p]),
        ("XMaxRequestSize", c_long, [D]),
        ("XExtendedMaxRequestSize", c_long, [D]),
    ):
        function = getattr(lib, name)
        function.restype, function.argtypes = restype, argtypes
    lib.XSetErrorHandler(_IGNORE_ERRORS)
    return lib


# Shift ---------------------------------------------------------------------------------------------

def knows_the_keys() -> bool:
    """Whether the X server on this computer can say which keys are down: there's one, it's the
    desktop's (on Wayland's, X only hears of keys pressed in its own windows), and it isn't one
    forwarded over SSH, which would cost a round trip on every Enter."""
    return (os.environ.get("DISPLAY", "").startswith(":") and not os.environ.get("WAYLAND_DISPLAY")
            and xlib() is not None)


def shift_down() -> bool:
    """Whether a Shift key is down now. Read as the key comes in, it's down for Shift+Enter. A
    connection of its own each time, a fraction of a millisecond: one kept open would end agentlens
    with the X server, as Xlib ends a program whose connection drops."""
    lib = xlib()
    display = lib.XOpenDisplay(None) if lib is not None else None
    if not display:
        return False
    try:
        root, child, mask = c_ulong(), c_ulong(), c_uint()
        x, y, window_x, window_y = c_int(), c_int(), c_int(), c_int()
        lib.XQueryPointer(display, lib.XDefaultRootWindow(display), byref(root), byref(child), byref(x), byref(y),
                          byref(window_x), byref(window_y), byref(mask))
        return bool(mask.value & SHIFT_MASK)
    finally:
        lib.XCloseDisplay(display)


# The clipboard ---------------------------------------------------------------------------------------

class _Connection:
    """A connection to the X server, with a window of its own to own a selection or ask for one."""

    def __init__(self) -> None:
        self.lib = xlib()
        self.display = self.lib.XOpenDisplay(None) if self.lib is not None else None
        if not self.display:
            raise OSError("There's no X display to reach.")
        root = self.lib.XDefaultRootWindow(self.display)
        self.window = self.lib.XCreateSimpleWindow(self.display, root, 0, 0, 1, 1, 0, 0, 0)

    def atom(self, name: str) -> int:
        return self.lib.XInternAtom(self.display, name.encode(), False)

    def flush(self) -> None:
        self.lib.XFlush(self.display)

    def wait(self, wanted: Callable[[_Event], bool]) -> _Event | None:
        """The next event that's `wanted`; None if none comes in time."""
        deadline = time.monotonic() + WAIT
        connection = self.lib.XConnectionNumber(self.display)
        event = _Event()
        while True:
            while self.lib.XPending(self.display):
                self.lib.XNextEvent(self.display, byref(event))
                if wanted(event):
                    return event
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            select.select([connection], [], [], left)

    def take(self, prop: int) -> tuple[int, bytes]:
        """A property of the window, and its type, deleting it: that tells an owner sending it in
        parts to send the next."""
        kind, form, count, after, data = c_ulong(), c_int(), c_ulong(), c_ulong(), c_void_p()
        self.lib.XGetWindowProperty(self.display, self.window, prop, 0, 0x1FFFFFFF, True, ANY_PROPERTY_TYPE,
                                    byref(kind), byref(form), byref(count), byref(after), byref(data))
        try:
            # Xlib hands 32-bit items over as longs.
            size = count.value * {8: 1, 16: ctypes.sizeof(ctypes.c_short), 32: ctypes.sizeof(c_long)}.get(form.value, 0)
            return kind.value, ctypes.string_at(data, size) if data and size else b""
        finally:
            if data:
                self.lib.XFree(data)


def own(selection: str, text: str) -> int:
    """Hold `text` as the selection (CLIPBOARD, or PRIMARY: what's selected, for a middle click),
    handing it to whatever pastes it until something else is copied. Returns 0 once it's held, and
    holds it on in the background; 1 if it couldn't be."""
    x = _Connection()
    data = text.encode("utf-8")
    utf8, text_atom = x.atom("UTF8_STRING"), x.atom("TEXT")
    forms = {utf8: data, x.atom("text/plain;charset=utf-8"): data, x.atom("text/plain"): data, text_atom: data,
             XA_STRING: text.encode("latin-1", "replace")}
    biggest = (x.lib.XExtendedMaxRequestSize(x.display) or x.lib.XMaxRequestSize(x.display)) * 4 - 64
    if len(data) > biggest:  # it would take sending in parts (INCR), which a copy from agentlens never needs
        return 1
    held = x.atom(selection)
    x.lib.XSetSelectionOwner(x.display, held, x.window, CURRENT_TIME)
    if x.lib.XGetSelectionOwner(x.display, held) != x.window:
        return 1
    x.flush()
    if os.fork():
        os._exit(0)  # it's copied; the child holds it, as xclip's does
    os.setsid()  # closing the terminal doesn't take the copy with it
    os.chdir("/")
    nowhere = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(nowhere, fd)

    targets = x.atom("TARGETS")
    offered = (c_ulong * (len(forms) + 1))(targets, *forms)
    event = _Event()
    while True:
        x.lib.XNextEvent(x.display, byref(event))
        if event.type == SELECTION_CLEAR:  # something else is copied
            return 0
        if event.type != SELECTION_REQUEST:
            continue
        request = event.request
        into = request.property or request.target  # clients from before ICCCM name no property
        if request.target == targets:
            x.lib.XChangeProperty(x.display, request.requestor, into, XA_ATOM, 32, PROP_MODE_REPLACE, offered,
                                  len(offered))
        elif request.target in forms:
            answer = forms[request.target]
            kind = utf8 if request.target == text_atom else request.target
            x.lib.XChangeProperty(x.display, request.requestor, into, kind, 8, PROP_MODE_REPLACE, answer, len(answer))
        else:
            into = NONE  # not a form it's in
        reply = _Event()
        reply.notify = _SelectionNotify(SELECTION_NOTIFY, 0, True, x.display, request.requestor, request.selection,
                                        request.target, into, request.time)
        x.lib.XSendEvent(x.display, request.requestor, False, 0, byref(reply))
        x.flush()


def fetch(selection: str, forms: tuple[str, ...]) -> tuple[str, bytes] | None:
    """What's on the selection in the first of `forms` it comes in, and which; None if nothing's
    copied, or not in any of them."""
    x = _Connection()
    x.lib.XSelectInput(x.display, x.window, PROPERTY_CHANGE_MASK)
    held = x.atom(selection)
    if x.lib.XGetSelectionOwner(x.display, held) == NONE:
        return None
    into, incr = x.atom("AGENTLENS_PASTE"), x.atom("INCR")
    for form in forms:
        x.lib.XConvertSelection(x.display, held, x.atom(form), into, x.window, CURRENT_TIME)
        x.flush()
        answered = x.wait(lambda e: e.type == SELECTION_NOTIFY and e.notify.requestor == x.window)
        if answered is None:
            return None  # the program that has it doesn't answer
        if answered.notify.property == NONE:
            continue
        kind, data = x.take(into)
        if kind != incr:
            return form, data
        parts: list[bytes] = []  # a big one comes in parts, each once the last is taken
        while True:
            if x.wait(lambda e: e.type == PROPERTY_NOTIFY and e.property.atom == into
                      and e.property.state == NEW_VALUE) is None:
                return None
            _, part = x.take(into)
            if not part:
                return form, b"".join(parts)
            parts.append(part)
    return None


def main(args: list[str]) -> int:
    """`copy [--primary]` the text on stdin; `paste [--primary] [--image]` to stdout, text as UTF-8.
    0 when done; 1 with nothing to paste, or not in that form; 2 with no X display to reach."""
    selection = "PRIMARY" if "--primary" in args else "CLIPBOARD"
    try:
        if args[:1] == ["copy"]:
            return own(selection, sys.stdin.buffer.read().decode("utf-8", "replace"))
        if args[:1] == ["paste"]:
            found = fetch(selection, IMAGE_FORMS if "--image" in args else TEXT_FORMS)
            if found is None:
                return 1
            form, data = found
            sys.stdout.buffer.write(data.decode("latin-1").encode("utf-8") if form == "STRING" else data)
            sys.stdout.flush()
            return 0
    except OSError:
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
