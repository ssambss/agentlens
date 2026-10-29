"""Put text on the system clipboard, and take an image off it.

The app also asks the terminal to copy (OSC 52), which works over SSH, but not every
terminal supports that. This is the local way: the Windows clipboard API, pbcopy, or
wl-copy / xclip / xsel. It never raises; it says whether it worked.

A terminal only ever pastes text, so an image you copied (a screenshot, a picture from a
web page, an image file in Explorer or Finder) is read from the clipboard directly, as
Claude Code's own Alt+V / Ctrl+V does. That too only works where panopsis runs, not over SSH.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

LINUX_TOOLS = (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"])

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
    try:
        if sys.platform == "win32":
            return _windows(text)
        if sys.platform == "darwin":
            return _pipe(["pbcopy"], text)
        tool = next((cmd for cmd in LINUX_TOOLS if shutil.which(cmd[0])), None)
        return _pipe(tool, text) if tool else False
    except (OSError, subprocess.SubprocessError):
        return False


def _pipe(cmd: list[str], text: str) -> bool:
    # Output goes nowhere: xclip and wl-copy stay running to serve the clipboard, and would
    # keep a captured pipe open.
    result = subprocess.run(cmd, input=text.encode("utf-8"), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=5)
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
    except NotImplementedError:
        raise NoImage("Reading an image from the clipboard needs wl-paste or xclip installed.") from None
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


def _open_image(path: str):
    from PIL import Image

    try:
        picture = Image.open(path)
        picture.load()
        return picture
    except Exception:  # noqa: BLE001 - not an image, or unreadable: try the next file
        return None
