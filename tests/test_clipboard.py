"""Copying to the system clipboard and pasting text off it: the desktop's own tool, tried in turn,
and with none installed, agentlens's own X clipboard."""

import os
import subprocess
import sys
import uuid

import pytest

from agentlens import clipboard, x11
from agentlens.clipboard import copy as real_copy  # conftest stands in for both in the app
from agentlens.clipboard import paste_text as real_paste_text

OWN_COPY = [sys.executable, "-I", "-m", "agentlens.x11", "copy"]
OWN_PASTE = [sys.executable, "-I", "-m", "agentlens.x11", "paste"]


@pytest.fixture
def linux(monkeypatch):
    """Linux, with the tools given installed; libX11 is there unless the test takes it away."""
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setattr(clipboard.x11, "xlib", lambda: object())
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)

    def install(*names: str) -> None:
        monkeypatch.setattr(clipboard.shutil, "which", lambda name: f"/usr/bin/{name}" if name in names else None)

    install()
    return install


def test_each_desktop_copies_and_pastes_with_its_own_tool_first(linux, monkeypatch):
    linux("wl-copy", "wl-paste", "xclip", "xsel")
    monkeypatch.setenv("DISPLAY", ":0")
    assert clipboard.tools(copying=True) == [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"],
                                             OWN_COPY]  # X's: wl-copy can't reach it
    assert clipboard.tools(copying=False) == [["xclip", "-selection", "clipboard", "-o"],
                                              ["xsel", "--clipboard", "--output"], OWN_PASTE]
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")  # Wayland's, which runs X programs too
    assert clipboard.tools(copying=True)[0] == ["wl-copy"]
    assert clipboard.tools(copying=False)[0] == ["wl-paste", "--no-newline", "--type", "text"]
    linux()  # nothing installed: agentlens's own, through X
    assert clipboard.tools(copying=True) == [OWN_COPY]
    monkeypatch.setattr(clipboard.x11, "xlib", lambda: None)  # and no X library either
    assert clipboard.tools(copying=True) == []
    monkeypatch.delenv("DISPLAY")
    monkeypatch.delenv("WAYLAND_DISPLAY")  # over SSH: only the terminal can copy
    linux("wl-copy", "xclip")
    assert clipboard.tools(copying=True) == []


def test_a_tool_that_fails_hands_over_to_the_next(linux, monkeypatch):
    linux("xclip", "xsel")
    monkeypatch.setenv("DISPLAY", ":0")
    ran = []

    def run(cmd, **kwargs):
        ran.append(cmd[0])
        works = cmd[0] == "xsel"
        return subprocess.CompletedProcess(cmd, 0 if works else 1, b"pasted\r\ntext" if works else b"", b"")

    monkeypatch.setattr(clipboard.subprocess, "run", run)
    assert real_copy("some text") is True and ran == ["xclip", "xsel"]
    ran.clear()
    assert real_paste_text() == "pasted\ntext" and ran == ["xclip", "xsel"]
    linux()
    monkeypatch.setattr(clipboard.x11, "xlib", lambda: None)
    assert real_copy("some text") is False and real_paste_text() is None  # nothing reaches it here


def test_windows_terminals_paste_themselves(monkeypatch):
    monkeypatch.setattr(clipboard.sys, "platform", "win32")
    assert real_paste_text() is None


@pytest.mark.skipif(not os.environ.get("DISPLAY") or x11.xlib() is None, reason="needs an X display")
def test_agentlens_own_x_clipboard_holds_what_it_copied_until_something_else_is():
    # On a selection of its own, not the clipboard you're using.
    selection = f"AGENTLENS_TEST_{uuid.uuid4().hex}"
    holds = f"import sys; from agentlens import x11; sys.exit(x11.own({selection!r}, sys.stdin.read()))"
    held = subprocess.run([sys.executable, "-I", "-c", holds], input="naïve ✓\nsecond line".encode(), timeout=10)
    assert held.returncode == 0  # back straight away; held in the background
    try:
        assert x11.fetch(selection, x11.TEXT_FORMS) == ("UTF8_STRING", "naïve ✓\nsecond line".encode())
        assert x11.fetch(selection, ("STRING",)) == ("STRING", "naïve ?\nsecond line".encode("latin-1"))
        assert x11.fetch(selection, x11.IMAGE_FORMS) is None  # not a form it's in
    finally:
        taker = x11._Connection()  # something else is copied: what held it lets go
        taker.lib.XSetSelectionOwner(taker.display, taker.atom(selection), taker.window, x11.CURRENT_TIME)
        taker.lib.XCloseDisplay(taker.display)
    assert x11.fetch(selection, x11.TEXT_FORMS) is None
