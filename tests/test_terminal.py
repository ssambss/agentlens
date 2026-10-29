"""The tab says panopsis, and the `claude` processes it starts leave that alone. In Windows' own
console window, the cursor stays hidden and everything is drawn with glyphs its font has."""

import asyncio
import io
import shutil
import subprocess

from conftest import StreamingClaude, claude_result
from rich.cells import cell_len
from textual import _border
from textual.app import App
from textual.scrollbar import ScrollBarRender

from panopsis import advise, live, terminal, tui, usage
from panopsis.driver import DrivenSession
from panopsis.usage import run_probe  # the real one; tests get a stand-in by default


def test_every_claude_panopsis_starts_leaves_the_tab_alone(monkeypatch):
    # Each shares panopsis's terminal and would rename the tab "claude".
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
        assert titles[-1] == "panopsis"
        titles.append("Fix the build")  # Claude Code you attached to named it after its conversation
        terminal.set_title()
        assert titles[-1] == "panopsis"
    assert titles[-1] == "Windows PowerShell"


def test_elsewhere_the_terminal_keeps_the_old_name_and_puts_it_back(monkeypatch):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(terminal, "WINDOWS", False)
    monkeypatch.setattr(terminal.sys, "__stdout__", out := Tty())
    with terminal.titled():
        pass
    assert out.getvalue() == "\x1b[22;0t\x1b]0;panopsis\x07\x1b[23;0t"

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
        tui.PanopsisApp(provider=lambda: [], root=projects, interval=60).driver_class().write(text)
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
            app = tui.PanopsisApp(provider=lambda: [], root=projects, interval=60)
            async with app.run_test(size=(120, 30)) as pilot:
                monkeypatch.setattr(app._driver, "write", written.append)
                await pilot.resize_terminal(140, 40)
                await pilot.pause()

        asyncio.run(go())
        return written

    assert terminal.HIDE_CURSOR in written_after_resize(classic=True)
    assert terminal.HIDE_CURSOR not in written_after_resize(classic=False)  # other terminals keep it hidden
