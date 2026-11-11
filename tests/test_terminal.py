"""The tab says agentlens, and the `claude` processes it starts leave that alone. In Windows' own
console window, the cursor stays hidden and everything is drawn with glyphs its font has."""

import asyncio
import io
import shutil
import threading
import subprocess

import pytest

from conftest import StreamingClaude, claude_result
from rich.cells import cell_len
from textual import _border
from textual.app import App
from textual.scrollbar import ScrollBarRender

from agentlens import advise, live, terminal, tui, usage, x11
from agentlens.driver import DrivenSession
from agentlens.usage import run_probe  # the real one; tests get a stand-in by default


def test_every_claude_agentlens_starts_leaves_the_tab_alone(monkeypatch):
    # Each shares agentlens's terminal and would rename the tab "claude".
    envs = []

    def run(cmd, **kw):
        envs.append(kw.get("env") or {})
        return subprocess.CompletedProcess(cmd, 0, '{"result": ""}' if "/usage" in cmd else "[]", "")

    streaming = StreamingClaude([claude_result("1. Plan first.")])  # advice streams in
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", streaming)
    monkeypatch.setattr(shutil, "which", lambda name: "claude")
    run_probe("claude")
    live.open_sessions()
    advise.run("advice, please")
    tui.stop_background("claude", "sess-1")
    tui.background_resume("claude", "sess-1", "", "default", "go on",
                          list_sessions=lambda: [{"sessionId": "sess-1", "kind": "background"}])
    envs += [run["env"] for run in streaming.runs]
    assert len(envs) == 5 and all(env.get("CLAUDE_CODE_DISABLE_TERMINAL_TITLE") == "1" for env in envs)
    assert DrivenSession("D:/work/demo", cli_path="claude").options().env == terminal.CLAUDE_ENV


def test_on_windows_the_tab_gets_its_old_name_back(monkeypatch):
    titles = ["Windows PowerShell"]

    class Kernel32:
        def GetConsoleTitleW(self, buffer, size):
            buffer.value = titles[-1]
            return len(titles[-1])

        def SetConsoleTitleW(self, title):
            titles.append(title)
            return 1

    monkeypatch.setattr(terminal, "WINDOWS", True)
    monkeypatch.setattr(terminal, "_kernel32", Kernel32)
    with terminal.titled():
        assert titles[-1] == "🟪 agentlens"
        titles.append("Fix the build")  # Claude Code you attached to named it after its conversation
        terminal.set_title()
        assert titles[-1] == "🟪 agentlens"
    assert titles[-1] == "Windows PowerShell"


def test_elsewhere_the_terminal_keeps_the_old_name_and_puts_it_back(monkeypatch):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(terminal, "WINDOWS", False)
    monkeypatch.setattr(terminal.sys, "__stdout__", out := Tty())
    with terminal.titled():
        pass
    assert out.getvalue() == "\x1b[22;0t\x1b]0;🟪 agentlens\x07\x1b[23;0t"

    monkeypatch.setattr(terminal.sys, "__stdout__", piped := io.StringIO())  # not a terminal: nothing written
    with terminal.titled():
        pass
    assert piped.getvalue() == ""


def test_in_windows_own_console_window_glyphs_its_font_lacks_are_drawn_with_ones_it_has(monkeypatch, projects):
    # Its font, Consolas, has no ▶ (the space you're in), no ⭘ (the header's button for the
    # commands) and none of the thin blocks of a tall border (an input box's) or the usage meter.
    written: list[str] = []

    class Driver:
        def write(self, data: str) -> None:
            written.append(data)

    def drawn(classic: bool, text: str) -> str:
        monkeypatch.setattr(terminal, "classic_console", lambda: classic)
        tui.AgentlensApp(provider=lambda: [], root=projects, interval=60).driver_class().write(text)
        return written.pop()

    monkeypatch.setattr(App, "get_driver_class", lambda self: Driver)
    line = "[1m▶ All[0m  ⭘ ▔▁▎ ███▍░░ ✓ ✗ ◆ ⟳ ╭─╮"
    assert drawn(False, line) == line  # other terminals' fonts have them
    assert drawn(True, line) == "[1m► All[0m  ○ ▀▄▌ ███▌░░ √ × ♦ ≈ ╭─╮"
    assert all(cell_len(new) == cell_len(old) for old, new in terminal.CONSOLE_GLYPHS.items())  # nothing moves

    thin = set("▔▁▂▃▅▆▇▏▎▍▋▊▉▕▖▗▘▝▙▛▜▟")  # the blocks Consolas lacks
    blocks = {ch for rows in _border.BORDER_CHARS.values() for row in rows for ch in row}
    blocks |= {*ScrollBarRender.VERTICAL_BARS, *ScrollBarRender.HORIZONTAL_BARS, *usage.BLOCKS}
    assert blocks & thin and not set(terminal.fit_glyphs("".join(blocks))) & thin


def test_in_windows_own_console_window_the_cursor_is_hidden_again_after_a_resize(monkeypatch, projects):
    # Textual hides it once, at the start; resizing that console's window shows it again.
    def written_after_resize(classic: bool) -> list[str]:
        monkeypatch.setattr(terminal, "classic_console", lambda: classic)
        written: list[str] = []

        async def go():
            app = tui.AgentlensApp(provider=lambda: [], root=projects, interval=60)
            async with app.run_test(size=(120, 30)) as pilot:
                monkeypatch.setattr(app._driver, "write", written.append)
                await pilot.resize_terminal(140, 40)
                await pilot.pause()

        asyncio.run(go())
        return written

    assert terminal.HIDE_CURSOR in written_after_resize(classic=True)
    assert terminal.HIDE_CURSOR not in written_after_resize(classic=False)  # other terminals keep it hidden


def test_shift_enter_and_ctrl_backspace_come_through_as_such(monkeypatch):
    # Most terminals send both Enters as \r, and Ctrl+Backspace as \x08, which Textual reads as Backspace.
    from textual import events

    def meant(key: str, character: str, shift: bool | None = False, ctrl_h: bool = True) -> str:
        shift_down = None if shift is None else lambda: shift
        return terminal.meant_key(events.Key(key, character), shift_down, ctrl_h).key

    assert meant("enter", "\r", shift=True) == "shift+enter"
    assert meant("enter", "\r") == "enter"
    assert meant("enter", "\r", shift=None) == "enter"  # nothing can say whether Shift is down
    assert meant("backspace", "\x08") == "ctrl+backspace"
    assert meant("backspace", "\x08", ctrl_h=False) == "backspace"  # a terminal whose Backspace is \x08
    assert meant("backspace", "\x7f", shift=True) == "backspace"  # Backspace, and Shift+Backspace
    assert meant("a", "a", shift=True) == "a"

    class Driver:
        def process_message(self, message) -> None:
            self.got = message

    def through(**environment) -> str:
        driver = terminal.read_keys(Driver)()
        driver.process_message(events.Key("enter", "\r"))
        return driver.got.key

    monkeypatch.setattr(terminal, "windows_shift_down", lambda: True)
    monkeypatch.setattr(terminal, "WINDOWS", True)
    assert through() == "shift+enter"
    # On a Linux desktop's X, the X server says.
    monkeypatch.setattr(terminal, "WINDOWS", False)
    monkeypatch.setattr(terminal, "_read_alt_enter", lambda: None)
    monkeypatch.setattr(x11, "shift_down", lambda: True)
    monkeypatch.setattr(x11, "knows_the_keys", lambda: True)
    assert through() == "shift+enter"
    monkeypatch.setattr(x11, "knows_the_keys", lambda: False)  # Wayland's, or over SSH: the terminal says
    assert through() == "enter"


@pytest.mark.parametrize("display, wayland, knows", [
    (":0", "", True), (":10.0", "", True),  # a desktop, and one over remote desktop (xrdp)
    (":0", "wayland-0", False),  # X only hears of keys pressed in its own windows
    ("localhost:10.0", "", False),  # forwarded over SSH: a round trip each Enter
    ("", "", False),
])
def test_x_is_asked_about_shift_on_this_computers_desktop_only(monkeypatch, display, wayland, knows):
    monkeypatch.setenv("DISPLAY", display)
    monkeypatch.setenv("WAYLAND_DISPLAY", wayland)
    monkeypatch.setattr(x11, "xlib", lambda: object())
    assert x11.knows_the_keys() is knows


def test_alt_enter_comes_through_as_such(monkeypatch):
    # Textual reads ESC and \r as Enter; it's Alt+Enter, and Shift+Enter in terminals Claude
    # Code's /terminal-setup set up.
    from textual._xterm_parser import XTermParser
    from textual.drivers import linux_driver

    monkeypatch.setattr(linux_driver, "XTermParser", XTermParser)
    terminal._read_alt_enter()

    def keys(data: str) -> list[str]:
        parser = linux_driver.XTermParser()  # as the driver makes one
        return [e.key for e in [*parser.feed(data), *parser.feed("")]]

    assert keys("\x1b\r") == ["alt+enter"]
    assert keys("\r") == ["enter"]
    assert keys("\x1b[13;3u") == ["alt+enter"]  # the kitty keyboard protocol's
    assert keys("\x1b\x7f") == ["ctrl+w"]  # the others as Textual reads them: Alt+Backspace takes a word
    terminal._read_alt_enter()  # only once
    assert linux_driver.XTermParser.__mro__[1] is XTermParser


def test_the_icon_is_four_panes_on_violet_at_every_size():
    from io import BytesIO

    from PIL import Image

    from agentlens import icon

    for size in (16, 20, 24, 32, 48, 256):
        image = icon.draw(size)
        assert image.size == (size, size) and image.mode == "RGBA"
        assert image.getpixel((0, 0))[3] == 0  # rounded corners
        margin, gap, pane = icon._layout(size)
        assert 2 * margin + 2 * pane + gap == size and pane >= 5
        lit = image.getpixel((margin + pane // 2, margin + pane // 2 if size < 48 else margin + 1))
        assert lit[:3] == (0xF0, 0x7B, 0xE0)  # the pane that needs you
    assert Image.open(BytesIO(icon.png(32))).size == (32, 32)


def test_the_window_gets_its_own_icon_back_once_however_agentlens_ends(monkeypatch):
    done: list[str] = []
    handlers: list = []
    monkeypatch.setattr(terminal, "WINDOWS", True)
    monkeypatch.setattr(terminal, "host_window", lambda: 0x508C4)
    monkeypatch.setattr(terminal, "_give_icon", lambda window: done.append(f"given {window:#x}") or
                        (lambda: done.append("put back")))
    monkeypatch.setattr(terminal, "_closing", lambda put_back: handlers.append(put_back) or "handler")

    class Kernel32:
        def SetConsoleCtrlHandler(self, handler, add):
            done.append(f"handler {'on' if add else 'off'}")

    monkeypatch.setattr(terminal, "_kernel32", Kernel32)
    with terminal.iconed():
        for _ in range(100):
            if handlers:
                break
            threading.Event().wait(0.01)
        handlers[0]()  # the tab it's in closed
    assert done == ["given 0x508c4", "put back", "handler off"]  # not put back twice

    done.clear()
    monkeypatch.setattr(terminal, "host_window", lambda: None)  # another terminal: its window is left alone
    with terminal.iconed():
        pass
    assert done == []


def test_konsole_takes_the_icon_from_a_file_that_holds_it_drawn_for_each_size():
    from io import BytesIO

    from PIL import Image

    from agentlens import icon

    held = Image.open(BytesIO(icon.ico())).ico
    assert held.sizes() == {(size, size) for size in icon.TAB_SIZES}
    for size in icon.TAB_SIZES:
        assert held.getimage((size, size)).tobytes() == icon.draw(size).tobytes()  # not one shrunk


def test_elsewhere_konsole_and_iterm2_mark_the_tab_while_agentlens_runs(monkeypatch, isolated_home):
    from agentlens import icon

    class Tty(io.StringIO):
        def isatty(self):
            return True

    def marked() -> str:
        monkeypatch.setattr(terminal.sys, "__stdout__", out := Tty())
        with terminal.iconed():
            pass
        return out.getvalue()

    monkeypatch.setattr(terminal, "WINDOWS", False)
    for name in ("TERM_PROGRAM", "LC_TERMINAL", "KONSOLE_VERSION"):
        monkeypatch.delenv(name, raising=False)
    assert marked() == ""  # GNOME Terminal's, say: only its name

    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    assert marked() == ("\x1b]6;1;bg;red;brightness;106\x07\x1b]6;1;bg;green;brightness;72\x07"
                        "\x1b]6;1;bg;blue;brightness;216\x07"  # violet
                        "\x1b]6;1;bg;*;default\x07")  # and its own colour back
    monkeypatch.setenv("TERM_PROGRAM", "tmux")
    monkeypatch.setenv("LC_TERMINAL", "iTerm2")  # what it says over ssh, or in tmux
    assert marked().startswith("\x1b]6;1;bg;red;brightness;106\x07")

    monkeypatch.delenv("TERM_PROGRAM")
    monkeypatch.delenv("LC_TERMINAL")
    monkeypatch.setenv("KONSOLE_VERSION", "250402")
    isolated_home.mkdir(parents=True, exist_ok=True)
    (isolated_home / "icon-00000000.ico").write_bytes(b"the icon, before it was drawn as it is now")
    written = marked()
    [file] = isolated_home.glob("icon-*.ico")  # the old one gone
    assert written == f"\x1b]32;{file}\x07\x1b]32;utilities-terminal\x07"  # the profile's icon, shown as none
    assert file.read_bytes() == icon.ico()
    drawn = file.stat().st_mtime_ns
    assert marked() == written and file.stat().st_mtime_ns == drawn  # not drawn again

    file.unlink()
    monkeypatch.setattr(terminal.sys, "__stdout__", io.StringIO())  # not a terminal: nothing drawn or written
    with terminal.iconed():
        pass
    assert not file.exists()
