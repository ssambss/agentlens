"""Put text on the system clipboard, and take text or an image off it.

The app also asks the terminal to copy (OSC 52), which works over SSH, but not every
terminal supports that: GNOME Terminal doesn't. This is the local way: the Windows clipboard
API, pbcopy, or in Linux the desktop's own tool (wl-copy on Wayland, xclip or xsel on X), and
with none installed, agentlens's own X clipboard (x11.py). It never raises; it says whether it
worked.

Ctrl+V reads the clipboard here too: Windows' terminals paste on it themselves, but Linux's
and macOS's (whose paste is Ctrl+Shift+V, or Cmd+V) pass it on to the app, and Textual's own
Ctrl+V pastes only what was copied in the app.

A terminal only ever pastes text, so an image you copied (a screenshot, a picture from a
web page, an image file in Explorer or Finder) is read from the clipboard directly, as
Claude Code's own Alt+V / Ctrl+V does. That too only works where agentlens runs, not over SSH.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

from agentlens import x11

# Claude takes images up to 2576 px on the long edge, but a request carrying more than 20 of
# them is refused if any is over 2000 px, and a long session gathers screenshots. Over
# 5 MB (of base64, which is 4/3 of the file) an image is refused outright.
MAX_EDGE = 2000
MAX_BYTES = 3_750_000


class NoImage(Exception):
    """Why there's no image to paste, in words for the person."""


@dataclass(frozen=True)
class PastedImage:
    media_type: str  # image/png or image/jpeg
    data: bytes
    width: int
    height: int

    @property
    def size(self) -> str:
        return f"{self.width}×{self.height}"


def copy(text: str) -> bool:
    if sys.platform == "win32":
        try:
            return _windows(text)
        except OSError:
            return False
    return any(_pipe(cmd, text) for cmd in tools(copying=True))


def paste_text() -> str | None:
    """The text on the clipboard; None where agentlens can't read it (in Windows its terminals
    paste themselves, and over SSH there's no clipboard here)."""
    for cmd in [] if sys.platform == "win32" else tools(copying=False):
        try:
            result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return result.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
    return None


def tools(copying: bool) -> list[list[str]]:
    """The commands that copy (text on stdin) or paste (to stdout) here, to try in turn: the
    desktop's own tool, then agentlens's own on X. Wayland's desktops run X programs too, and
    hand the clipboard between the two."""
    if sys.platform == "darwin":
        return [["pbcopy" if copying else "pbpaste"]]
    found = []
    if os.environ.get("WAYLAND_DISPLAY"):
        found.append(["wl-copy"] if copying else ["wl-paste", "--no-newline", "--type", "text"])
    if os.environ.get("DISPLAY"):
        found += [["xclip", "-selection", "clipboard", *([] if copying else ["-o"])],
                  ["xsel", "--clipboard", "--input" if copying else "--output"]]
    found = [cmd for cmd in found if shutil.which(cmd[0])]
    if os.environ.get("DISPLAY") and x11.xlib() is not None:
        found.append(own_x_clipboard("copy" if copying else "paste"))
    return found


def own_x_clipboard(*args: str) -> list[str]:
    """agentlens's own X clipboard, for when no tool is installed."""
    return [sys.executable, "-I", "-m", "agentlens.x11", *args]


def _pipe(cmd: list[str], text: str) -> bool:
    # Output goes nowhere: xclip, wl-copy and agentlens's own stay running to serve the
    # clipboard, and would keep a captured pipe open.
    try:
        result = subprocess.run(cmd, input=text.encode("utf-8"), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _windows(text: str) -> bool:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GetConsoleWindow.restype = wintypes.HWND
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    cf_unicodetext, gmem_moveable = 13, 0x0002

    data = text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-16-le") + b"\x00\x00"
    owner = kernel32.GetConsoleWindow()
    for _ in range(10):  # another program may have it open for a moment
        if user32.OpenClipboard(owner):
            break
        time.sleep(0.02)
    else:
        return False
    try:
        if not user32.EmptyClipboard():
            return False
        handle = kernel32.GlobalAlloc(gmem_moveable, len(data))
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(cf_unicodetext, handle):
            kernel32.GlobalFree(handle)
            return False
        return True  # the clipboard owns the memory now
    finally:
        user32.CloseClipboard()


def paste_image() -> PastedImage:
    """The image on the clipboard, shrunk and encoded for Claude. Raises NoImage saying why not."""
    try:
        from PIL import ImageGrab

        grabbed = ImageGrab.grabclipboard()
    except NotImplementedError:  # Linux, with neither wl-paste nor xclip
        grabbed = _x_image()
    except Exception as e:  # noqa: BLE001 - Pillow raises what the platform's clipboard does
        raise NoImage(f"Couldn't read the clipboard: {e}") from None
    if isinstance(grabbed, list):  # files copied in Explorer or Finder: the first that's an image
        grabbed = next((picture for picture in map(_open_image, grabbed) if picture is not None), None)
    if grabbed is None:
        raise NoImage("There's no image on the clipboard. Copy a screenshot or an image, then paste it.")
    return encode(grabbed)


def encode(picture) -> PastedImage:
    """A Pillow image as Claude takes it: at most MAX_EDGE on the long edge, PNG, or JPEG if the
    PNG would be too big."""
    from PIL import Image

    picture.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)  # only ever shrinks
    if picture.mode not in ("RGB", "RGBA", "L", "LA"):
        picture = picture.convert("RGBA" if "transparency" in picture.info or "A" in picture.mode else "RGB")
    out = io.BytesIO()
    picture.save(out, "PNG")
    media_type = "image/png"
    if out.tell() > MAX_BYTES:  # a photo: JPEG is a fraction of the size
        out = io.BytesIO()
        picture.convert("RGB").save(out, "JPEG", quality=85)
        media_type = "image/jpeg"
    if out.tell() > MAX_BYTES:
        raise NoImage("That image is too big for Claude even after shrinking it.")
    return PastedImage(media_type, out.getvalue(), picture.width, picture.height)


def _x_image():
    """The image on the clipboard through agentlens's own X clipboard, as Pillow reads it through
    xclip; None if there isn't one."""
    from PIL import Image

    if not os.environ.get("DISPLAY") or x11.xlib() is None:
        raise NoImage("Reading an image from the clipboard needs wl-paste or xclip installed.")
    try:
        result = subprocess.run(own_x_clipboard("paste", "--image"), stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        raise NoImage(f"Couldn't read the clipboard: {e}") from None
    if result.returncode != 0:
        return None
    try:
        picture = Image.open(io.BytesIO(result.stdout))
        picture.load()
    except Exception:  # noqa: BLE001 - a form Pillow doesn't read
        return None
    return picture


def _open_image(path: str):
    from PIL import Image

    try:
        picture = Image.open(path)
        picture.load()
        return picture
    except Exception:  # noqa: BLE001 - not an image, or unreadable: try the next file
        return None
