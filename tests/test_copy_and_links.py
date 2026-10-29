import asyncio
import io
from pathlib import Path

from conftest import T0, common, human, text, write_jsonl
from rich.console import Console
from rich.text import Text
from textual.strip import Strip

from agentlens import editor as editor_mod
from agentlens import selectable
from agentlens.feed import Palette, ReadableMarkdown, conversation, with_links
from agentlens.model import Event
from agentlens.selectable import Selectable, copyable, flatten
from agentlens.tui import AgentlensApp, SessionPane, ZoomScreen, file_link

REPLY = """## Done

Open http://localhost:5173 (or `http://localhost:3000`). Docs: https://docs.example.com/a_(b).
Edited [app.py:3](src/app.py#L3) and README.md.

```bash
npm install
npm run dev
```

> a quote
"""


def links_in(renderable, width=60) -> set[str]:
    console = Console(file=io.StringIO(), width=width)
    lines, _ = flatten(renderable, width, console, console.options)
    return {segment.style.link for line in lines for segment in line if segment.style and segment.style.link}


def test_copying_leaves_out_card_margins_code_padding_and_quote_bars():
    events = [Event(T0, "you", "fix it", "fix it\nplease"), Event(T0, "claude", "Done", REPLY)]
    console = Console(file=io.StringIO(), width=60)
    lines, copy = flatten(conversation(events, Palette(), show_times=False), 60, console, console.options)
    assert " fix it" in "".join(segment.text for segment in lines[1])  # on screen the card has a margin
    assert copyable(copy).splitlines() == [
        "You", "fix it", "please", "",
        "Claude", "Done", "",
        "Open http://localhost:5173 (or http://localhost:3000). Docs:",
        "https://docs.example.com/a_(b). Edited app.py:3 and",  # wrapped as on screen
        "README.md.",
        "",
        "npm install",
        "npm run dev",
        "",
        "a quote",
    ]


def test_web_addresses_and_file_links_are_links():
    assert links_in(ReadableMarkdown(REPLY)) == {
        "http://localhost:5173", "http://localhost:3000", "https://docs.example.com/a_(b)", "src/app.py#L3"}
    assert links_in(with_links(Text("see https://x.org/a), or (https://y.org/b)."))) == {
        "https://x.org/a", "https://y.org/b"}
    assert links_in(ReadableMarkdown("tui.py, notes.md and `npm run dev`")) == set()


def test_file_links_resolve_against_the_session_folder():
    cwd = "D:/work/demo"
    assert file_link("src/app.py#L42", cwd) == (Path(cwd) / "src/app.py", 42)
    assert file_link("src/app.py#L4-L9", cwd) == (Path(cwd) / "src/app.py", 4)
    assert file_link("my%20notes.md:7", cwd) == (Path(cwd) / "my notes.md", 7)
    assert file_link("app.py:42:5", cwd) == (Path(cwd) / "app.py", 42)
    assert file_link("D:/other/x.py", cwd) == (Path("D:/other/x.py"), None)
    assert file_link("/d:/other/x.py:3", cwd) == (Path("d:/other/x.py"), 3)
    assert file_link("https://example.com", cwd) is None
    assert file_link("mailto:a@b.c", cwd) is None


def entry(session_id, cwd):
    return {"sessionId": session_id, "status": "idle", "name": session_id, "kind": "interactive", "cwd": cwd,
            "startedAt": int(T0.timestamp() * 1000)}


def make_app(projects, cwd):
    write_jsonl(projects / "demo" / "s1.jsonl", common([human(0, "fix the build"), text(1, REPLY)], "s1", cwd=cwd))
    return AgentlensApp(provider=lambda: [entry("s1", cwd)], root=projects, interval=60)


def run_app(app, steps, size=(160, 40)):
    async def go():
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause(0.2)
            await steps(app, pilot)
    asyncio.run(go())


def find(widget: Selectable, needle: str) -> tuple[int, int]:
    for y, line in enumerate(widget.text.splitlines()):
        if needle in line:
            return line.index(needle), y
    raise AssertionError(f"{needle!r} not on screen")


def test_select_text_in_a_pane_and_copy_it(projects, tmp_path, system_clipboard):
    app = make_app(projects, str(tmp_path))

    async def steps(app, pilot):
        body = app.query_one(SessionPane).query_one(".feed-body", Selectable)
        x, y = find(body, "npm install")
        await pilot.mouse_down(body, offset=(x, y))
        await pilot.hover(body, offset=(x + 7, y + 1))
        await pilot.mouse_up(body, offset=(x + 7, y + 1))
        await pilot.press("ctrl+c")
        assert system_clipboard == ["npm install\nnpm run"]
        assert app._clipboard == "npm install\nnpm run"  # and through the terminal

    run_app(app, steps)


def highlighted(body: Selectable, y: int) -> str:
    """The characters on row y drawn as selected: those whose background the selection changed."""
    plain = Strip(body._lines[y]).apply_style(body.visual_style.rich_style)
    shown = body.render_line(y)
    under = [segment.style.bgcolor for segment in plain for _ in segment.text]
    return "".join(ch for segment in shown for ch in segment.text
                   if segment.style.bgcolor != under.pop(0))


def test_dragging_highlights_what_copying_takes_without_laying_the_feed_out_again(projects, tmp_path,
                                                                                   monkeypatch):
    app = make_app(projects, str(tmp_path))
    laid_out = []
    real_flatten = selectable.flatten
    monkeypatch.setattr(selectable, "flatten", lambda *args: laid_out.append(1) or real_flatten(*args))

    async def steps(app, pilot):
        body = app.query_one(SessionPane).query_one(".feed-body", Selectable)
        x, y = find(body, "npm install")
        assert highlighted(body, y) == ""
        laid_out.clear()
        await pilot.mouse_down(body, offset=(x, y))
        for dx in (2, 5, 8):  # each move of the mouse repaints the rows on screen, nothing more
            await pilot.hover(body, offset=(x + dx, y))
            assert highlighted(body, y) == app.screen.get_selected_text() == "npm install"[:dx + 1]
        assert laid_out == []
        app.screen.clear_selection()
        await pilot.pause()
        assert highlighted(body, y) == ""
        # A drag that ends on a blank line selects up to it, not the rest of the feed.
        x2, y2 = find(body, "Edited")
        await pilot.mouse_down(body, offset=(x2, y2))
        await pilot.hover(body, offset=(x2 + 4, y2 + 1))
        assert app.screen.get_selected_text() == "Edited app.py:3 and README.md."
        assert highlighted(body, y) == ""

    run_app(app, steps)


def test_a_long_feed_re_wraps_once_you_stop_resizing_the_window(projects, tmp_path):
    app = make_app(projects, str(tmp_path))

    def widest(body: Selectable) -> int:
        return max(len(line) for line in body.text.splitlines())

    async def steps(app, pilot):
        pane = app.query_one(SessionPane).query_one(".feed-body", Selectable)
        await pilot.resize_terminal(120, 40)
        assert widest(pane) <= pane.size.width  # a short feed re-wraps as you drag
        await pilot.resize_terminal(160, 40)
        await pilot.press("enter")
        await pilot.pause(0.2)
        body = app.screen.query_one("#zoom-main .feed-body", Selectable)
        body._rewrap_seconds = 1.0  # as laying out a long conversation takes
        before = widest(body)
        await pilot.resize_terminal(100, 40)
        await pilot.resize_terminal(90, 40)
        assert widest(body) == before > body.size.width  # the window keeps up; the text waits
        await pilot.pause(selectable.SETTLE + 0.1)
        assert widest(body) <= body.size.width

    run_app(app, steps)


def test_feed_holds_still_while_text_is_selected(projects, tmp_path):
    app = make_app(projects, str(tmp_path))

    async def steps(app, pilot):
        body = app.query_one(SessionPane).query_one(".feed-body", Selectable)
        x, y = find(body, "npm install")
        await pilot.mouse_down(body, offset=(x, y))
        await pilot.hover(body, offset=(x + 3, y))
        await pilot.mouse_up(body, offset=(x + 3, y))
        body.show(Text("something new"))
        await pilot.pause()
        assert "npm install" in body.text
        app.screen.clear_selection()
        await pilot.pause()
        assert body.text == "something new"

    run_app(app, steps)


def test_clicking_links_opens_them(projects, tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    app = make_app(projects, str(tmp_path))
    app.config.editor = "zed"  # the one picked in settings
    opened, files = [], []
    monkeypatch.setattr(app, "open_url", lambda url, **kw: opened.append(url))
    monkeypatch.setattr(editor_mod.shutil, "which", {"zed": "C:/bin/zed.exe"}.get)
    app.launch_editor = files.append

    async def steps(app, pilot):
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ZoomScreen)
        body = app.screen.query_one("#zoom-main .feed-body", Selectable)
        await pilot.click(body, offset=find(body, "localhost:5173"))
        await pilot.click(body, offset=find(body, "app.py:3"))
        assert opened == ["http://localhost:5173"]
        assert files == [["C:/bin/zed.exe", f"{tmp_path / 'src' / 'app.py'}:3"]]
        assert isinstance(app.screen, ZoomScreen)  # a click on a link doesn't zoom or go back

    run_app(app, steps)


def test_y_copies_claudes_last_reply(projects, tmp_path, system_clipboard):
    app = make_app(projects, str(tmp_path))

    async def steps(app, pilot):
        await pilot.press("y")
        assert system_clipboard == [REPLY]

    run_app(app, steps)
