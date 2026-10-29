import dataclasses
import json
import asyncio
import io
import threading
from datetime import datetime, timedelta, timezone

from conftest import T0, at, common, edit_result, human, plain, text, tool_result, tool_use, write_jsonl
from rich.console import Console
from textual.color import Color
from textual.widgets import Checkbox, Input, Select, Static

from agentlens import advise, tui
from agentlens import analysis as analysis_mod
from agentlens import config as config_mod
from agentlens import editor as editor_mod
from agentlens import feed as feed_mod
from agentlens import preferences
from agentlens.adapters.claude_transcripts import extract_events, load_tail
from agentlens.driver import STARTING
from agentlens.feed import Palette, _clip_lines, activity_log, collapse_repeats, conversation, tools_summary
from agentlens.live import QUESTION, WAITING, WORKING, LiveSession
from agentlens.model import Event
from agentlens.tui_settings import SettingsScreen
from agentlens.tui import (
    ALL,
    AdviseScreen,
    AgentlensApp,
    Feed,
    HistoryScreen,
    KeyFooter,
    MoveToSpaceScreen,
    ProfileScreen,
    ReportScreen,
    SessionPane,
    Sidebar,
    SpacesTree,
    WallScreen,
    ZoomScreen,
)


def entry(session_id, status, started_min, cwd="D:\\work\\demo"):
    return {"sessionId": session_id, "status": status, "name": session_id, "kind": "interactive",
            "cwd": cwd, "startedAt": int(T0.timestamp() * 1000) + started_min * 60_000}


def make_projects(projects):
    write_jsonl(projects / "demo" / "waiting.jsonl", common([human(0, "fix the build"), text(1, "**Done.**")], "waiting"))
    write_jsonl(projects / "demo" / "busy.jsonl", common([
        human(0), tool_use(1, "Bash", "t1", command='cd "D:\\work\\demo" && cmake --build build')], "busy"))
    write_jsonl(projects / "other" / "other.jsonl",
                common([human(0), text(1)], "other", cwd="D:\\work\\other"))


def run_app(app, steps, size=(160, 40)):
    async def go():
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause(0.2)
            await steps(app, pilot)
    asyncio.run(go())


def test_grid_navigation_zoom_and_filter(projects):
    make_projects(projects)
    entries = [entry("busy", "busy", 0), entry("waiting", "idle", 1), entry("other", "idle", 2, "D:\\work\\other")]
    app = AgentlensApp(provider=lambda: entries, root=projects, interval=60)

    async def steps(app, pilot):
        panes = list(app.query(SessionPane))
        assert [p.session_id for p in panes] == ["busy", "waiting", "other"]  # stable start order
        assert panes[0].has_class("st-working") and panes[1].has_class("st-waiting")
        assert app.focused is panes[0]
        await pilot.press("right")
        assert app.focused is panes[1]
        await pilot.press("down")  # 2 columns for 3 panes: down from index 1 clamps to the last pane
        assert app.focused is panes[2]
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ZoomScreen) and app.screen.session_id == "other"
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, ZoomScreen)
        await pilot.press("f")
        await pilot.pause(0.1)
        assert [p.session_id for p in app.query(SessionPane) if p.display] == ["waiting", "other"]
        await pilot.press("f", "minus")
        assert app.query_one("#grid").styles.grid_size_columns == 1

    run_app(app, steps)


def test_the_wall_says_it_is_looking_until_the_first_listing_is_in(projects, monkeypatch):
    # Starting `claude` can take seconds on a work PC. The usage check waits for the listing,
    # so the two don't slow each other down.
    import threading

    make_projects(projects)
    release, events = threading.Event(), []
    monkeypatch.setattr("agentlens.usage.run_probe", lambda claude, timeout=60: events.append("usage") or "")

    def provider():
        release.wait(5)
        events.append("listed")
        return [entry("waiting", "idle", 0)]

    app = AgentlensApp(provider=provider, root=projects, interval=60)

    async def go():
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause(1.5)  # past when the usage check used to start
            shown = [str(w.content) for w in app.query_one("#grid").query(Static)]
            assert shown == ["Looking for your open Claude Code sessions…"] and events == []
            release.set()
            for _ in range(50):
                await pilot.pause(0.05)
                if len(events) == 2 and app.query(SessionPane):
                    break
            assert events == ["listed", "usage"]
            assert [p.session_id for p in app.query(SessionPane)] == ["waiting"] and not app.query("#looking")

    asyncio.run(go())


def test_refreshes_run_one_at_a_time(projects):
    # Where listing and reading take longer than the interval, they piled up and slowed each other.
    import threading

    release, listings = threading.Event(), []

    def provider():
        listings.append(threading.current_thread().name)
        release.wait(5)
        return [entry("waiting", "idle", 0)]

    make_projects(projects)
    app = AgentlensApp(provider=provider, root=projects, interval=60)

    async def go():
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause(0.1)
            app.action_refresh()  # due while the first is still listing
            app.action_refresh()
            await pilot.pause(0.2)
            assert len(listings) == 1
            release.set()
            for _ in range(50):
                await pilot.pause(0.05)
                if len(listings) == 2 and not app._listing:
                    break
            assert len(listings) == 2  # the two that came due, as one, right after
            assert list(app.sessions) == ["waiting"]

    asyncio.run(go())


def test_a_failed_first_listing_stops_the_wall_looking(projects, monkeypatch):
    def provider():
        raise RuntimeError("`claude` CLI not found on PATH")

    app = AgentlensApp(provider=provider, root=projects, interval=60)
    errors = []
    monkeypatch.setattr(app, "notify", lambda message, **kw: errors.append((kw.get("title"), message)))

    async def steps(app, pilot):
        assert errors == [("Can't list sessions", "`claude` CLI not found on PATH")] and not app.query("#looking")

    run_app(app, steps)


def test_startup_timings_say_when_each_step_finished(projects, monkeypatch):
    # AGENTLENS_TIMINGS=1, for finding out why sessions are slow to show on one machine.
    from agentlens import live, timings

    make_projects(projects)
    monkeypatch.setattr(timings, "ENABLED", True)
    monkeypatch.setattr(timings, "_marks", [])
    monkeypatch.setattr(live, "open_sessions", lambda: [entry("waiting", "idle", 0)])
    app = AgentlensApp(provider=live.EarlyListing(), root=projects, interval=60)

    async def steps(app, pilot):
        noted = [what.split(", after ")[0] for _, what in sorted(timings._marks)]
        assert sorted(noted) == sorted([
            "`claude agents --json` answered", "wall drawn", "transcripts' ends read, 1 session",
            "restored 0 sessions agentlens ran last time", "sessions on the wall",
            "transcripts read in full, 1 session", "all of them filled in", "usage check answered"])
        assert noted.index("wall drawn") < noted.index("sessions on the wall")
        assert noted.index("transcripts' ends read, 1 session") < noted.index("transcripts read in full, 1 session")
        assert noted.index("sessions on the wall") < noted.index("all of them filled in")
        lines = timings.report().splitlines()
        assert lines[0] == "agentlens startup, in seconds since its code began loading:" and len(lines) == 9

    run_app(app, steps)


def test_the_alert_for_a_session_that_needs_you_names_it_by_its_title(projects):
    make_projects(projects)
    write_jsonl(projects / "demo" / "titled.jsonl", common([
        human(0, "please make the build faster"), text(1), {"type": "ai-title", "aiTitle": "Demo build speedup"},
    ], "titled", cwd="D:\\work\\demo\\.claude\\worktrees\\speed"))
    entries = [entry("busy", "busy", 0), entry("titled", "busy", 1)]
    app = AgentlensApp(provider=lambda: list(entries), root=projects, interval=60)

    async def steps(app, pilot):
        entries[1] = entry("titled", "idle", 1)  # its turn ended while you're on the other pane
        app.action_refresh()
        await app.workers.wait_for_complete()
        await pilot.pause(0.2)
        # The alert's second line is its Open button.
        alerts = [(n.title, n.message.split("\n")[0]) for n in app._notifications]
        assert alerts == [("Build speedup is waiting on you", "speed · demo")]

    run_app(app, steps)


def test_moving_between_panes_repaints_only_what_changes(projects, monkeypatch):
    make_projects(projects)
    background = dict(entry("other", "idle", 2, "D:\\work\\other"), kind="background")
    app = AgentlensApp(provider=lambda: [entry("busy", "busy", 0), entry("waiting", "idle", 1), background],
                       root=projects, interval=60)
    rebuilt, whole_screen = [], []
    real_recompose, real_refresh = KeyFooter.recompose, WallScreen.refresh

    async def recompose(self):
        rebuilt.append(self)
        return await real_recompose(self)

    def refresh(self, *regions, **kw):
        if not regions and kw.get("repaint", True):
            whole_screen.append(self)
        return real_refresh(self, *regions, **kw)

    monkeypatch.setattr(KeyFooter, "recompose", recompose)
    monkeypatch.setattr(WallScreen, "refresh", refresh)

    async def steps(app, pilot):
        panes = list(app.query(SessionPane))
        rebuilt.clear()
        whole_screen.clear()
        await pilot.press("right")  # to another interactive session: the same keys apply
        await pilot.pause(0.1)
        assert app.focused is panes[1] and rebuilt == [] and whole_screen == []
        await pilot.press("right")  # to a background session, which can be brought here
        await pilot.pause(0.1)
        footer = app.screen.query_one(KeyFooter)
        assert app.focused is panes[2] and rebuilt == [footer]
        assert "Bring here" in [getattr(key, "description", None) for key in footer.walk_children()]

    run_app(app, steps)


def test_the_footer_offers_spaces_settings_and_the_command_search(projects):
    make_projects(projects)
    app = AgentlensApp(provider=lambda: [entry("waiting", "idle", 0)], root=projects, interval=60)

    async def steps(app, pilot):
        shown = {getattr(key, "key", None): getattr(key, "description", None)
                 for key in app.screen.query_one(KeyFooter).walk_children()}
        assert shown["g"] == "Move to space"
        assert shown["comma"] == "Settings"
        assert shown["ctrl+p"] == "Commands"

    run_app(app, steps)


def test_a_pane_follows_its_conversation_unless_you_scroll_up(projects):
    records = common([human(0, "go")] + [text(1 + i / 10, f"step {i}") for i in range(30)], "long")
    path = write_jsonl(projects / "demo" / "long.jsonl", records)
    app = AgentlensApp(provider=lambda: [entry("long", "busy", 0)], root=projects, interval=60)

    async def more(app, pilot, label):
        records.extend(common([text(10 + len(records) / 10, f"{label} {i}") for i in range(3)], "long"))
        write_jsonl(path, records)
        app.action_refresh()
        await app.workers.wait_for_complete()
        await pilot.pause(0.2)

    async def steps(app, pilot):
        feed = app.query_one(Feed)
        for _ in range(40):  # its text is laid out once the feed knows its width
            if feed.max_scroll_y:
                break
            await pilot.pause(0.05)
        assert 0 < feed.max_scroll_y == feed.scroll_y  # opens on the newest
        await more(app, pilot, "next")
        assert feed.scroll_y == feed.max_scroll_y
        feed.scroll_home(animate=False)
        await pilot.pause(0.1)
        await more(app, pilot, "while reading")
        assert feed.scroll_y == 0  # reading back: left where you are
        feed.scroll_end(animate=False)
        await pilot.pause(0.1)
        await more(app, pilot, "after")
        assert feed.scroll_y == feed.max_scroll_y

    run_app(app, steps)


def test_colours_come_from_the_settings_file(projects, tmp_path):
    cfg = config_mod.Config(path=tmp_path / "config.yaml")
    cfg.colors.update(selected="#ff00ff", you="orange", working="not-a-colour")
    config_mod.save(cfg)
    cfg = config_mod.load(cfg.path)
    assert cfg.colors["selected"] == "#ff00ff" and cfg.colors["claude"] == "$primary"  # unset ones keep the theme
    make_projects(projects)
    app = AgentlensApp(provider=lambda: [entry("waiting", "idle", 0)], root=projects, interval=60, config=cfg)

    async def steps(app, pilot):
        pane = app.query_one(SessionPane)
        assert pane.has_focus_within and pane.styles.border_top[1].hex6 == "#FF00FF"  # the pane you're on
        assert app.palette.you == "#FFA500"
        assert app._bad_colors == ["working: not-a-colour"]  # warned about, and the default is used

    run_app(app, steps)


def test_settings_screen_changes_the_look_as_you_pick(projects, tmp_path, isolated_home):
    cfg = config_mod.Config(path=tmp_path / "config.yaml")
    make_projects(projects)
    app = AgentlensApp(provider=lambda: [entry("waiting", "idle", 0)], root=projects, interval=60, config=cfg)

    async def steps(app, pilot):
        await pilot.press("question_mark")
        await pilot.pause(0.1)
        listed = "\n".join(plain(option.prompt) for option in app.screen.query_one("#keys-list").options)
        assert "Look and settings" in listed and "settings: theme, colours, columns, keys, your editor" in listed
        saved = cfg.path.read_text(encoding="utf-8")
        await pilot.press("escape", "comma")
        await pilot.pause(0.2)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        assert screen.query_one("#set-colors", Select).value == "theme"
        # Looking changes nothing.
        assert cfg.path.read_text(encoding="utf-8") == saved and not (isolated_home / "profile.yaml").exists()

        screen.query_one("#set-colors", Select).value = "vivid"
        screen.query_one("#set-theme", Select).value = "nord"
        screen.query_one("#set-columns", Select).value = 3
        screen.query_one("#set-sidebar", Checkbox).value = False
        screen.query_one("#set-usage", Select).value = 0.0
        screen.query_one("#set-history", Select).value = 30
        screen.query_one("#set-keep", Select).value = 0
        await pilot.pause(0.2)
        assert app.theme == "nord" and app.palette.claude == "#40C4FF"
        assert app.query_one("#grid").styles.grid_size_columns == 3
        assert app.query_one(Sidebar).has_class("hidden")
        saved = config_mod.load(cfg.path)
        assert saved.colors == config_mod.COLOR_PRESETS["vivid"][1] and saved.theme == "nord"
        assert (saved.columns, saved.sidebar, saved.usage_minutes, saved.history_days, saved.history_keep_days) == \
            (3, False, 0.0, 30, 0)

        screen.query_one("#goal-cost", Checkbox).value = True
        screen.query_one("#style-steering", Select).value = "close"
        await pilot.pause(0.2)
        prefs = preferences.load()
        assert prefs.goals == ["cost"] and prefs.style == {"steering": "close"}

        await pilot.press("escape")
        await pilot.pause(0.1)
        pane = app.query_one(SessionPane)
        assert pane.has_focus_within and pane.styles.border_top[1].hex6 == "#FFFFFF"  # vivid's selected

    run_app(app, steps)


def test_settings_screen_picks_your_editor(projects, tmp_path, isolated_home, monkeypatch):
    cfg = config_mod.Config(path=tmp_path / "config.yaml")
    make_projects(projects)
    app = AgentlensApp(provider=lambda: [entry("waiting", "idle", 0)], root=projects, interval=60, config=cfg)
    monkeypatch.setattr(editor_mod.shutil, "which", {"code": "C:/bin/code.cmd", "hx": "/bin/hx"}.get)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)

    async def steps(app, pilot):
        await pilot.press("comma")
        await pilot.pause(0.2)
        screen = app.screen
        pick = screen.query_one("#set-editor-pick", Select)
        own = screen.query_one("#set-editor-own")

        def note():
            return str(screen.query_one("#set-editor-note", Static).render())

        assert pick.value == "" and not own.display
        assert "VS Code, the first editor installed" in note()
        pick.value = "hx"
        await pilot.pause(0.1)
        assert config_mod.load(cfg.path).editor == "hx" and "Runs hx <file>:<line>. It takes over" in note()
        pick.value = "subl"
        await pilot.pause(0.1)
        assert "Sublime Text isn't on your PATH" in note()

        # Your own: it starts from the one you had that's installed, to change the words after it.
        pick.value = "code"
        await pilot.pause(0.1)
        pick.value = "own"
        await pilot.pause(0.1)
        typed = screen.query_one("#set-editor", Input)
        assert own.display and typed.value == "code --goto {file}:{line}"
        assert config_mod.load(cfg.path).editor == "code --goto {file}:{line}"
        typed.value = "hx --vsplit {file}:{line}"
        screen.query_one("#set-editor-terminal", Checkbox).value = True
        await pilot.pause(0.1)
        saved = config_mod.load(cfg.path)
        assert (saved.editor, saved.editor_terminal) == ("hx --vsplit {file}:{line}", True)
        assert "Runs hx --vsplit <file>:<line>" in note()

        pick.value = ""
        await pilot.pause(0.1)
        assert config_mod.load(cfg.path).editor is None and not own.display
        await pilot.press("escape")
        app.config.editor = "hx --vsplit {file}:{line}"
        await pilot.press("comma")  # opens on what's in the file
        await pilot.pause(0.2)
        assert app.screen.query_one("#set-editor-pick", Select).value == "own"
        assert app.screen.query_one("#set-editor", Input).value == "hx --vsplit {file}:{line}"

    run_app(app, steps)


def luminance(color: str) -> float:
    c = Color.parse(color)
    r, g, b = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in (c.r / 255, c.g / 255, c.b / 255)]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_looks_read_on_their_own_background_and_textual_darks():
    for key, look in config_mod.LOOKS.items():
        c = look.colors
        assert set(c) == set(config_mod.DEFAULT_COLORS), key
        for background in (look.background, "#121212"):
            assert all(contrast(c[k], background) >= 3 for k in ("selected", "question", "working", "background",
                                                                  "failed")), (key, background)
            assert all(contrast(c[k], background) >= 4.5 for k in ("claude", "tool", "notice", "error")), (key, background)
        assert contrast(c["you"], c["card"]) >= 4.5, key
        # The state shows in the strength: brightest when it needs you, dim when idle.
        strengths = [luminance(c[k]) for k in ("question", "working", "background", "other")]
        assert strengths == sorted(strengths, reverse=True), key


def test_a_look_as_the_theme_colours_the_whole_app(projects, tmp_path):
    cfg = config_mod.Config(path=tmp_path / "config.yaml", theme="oxblood")
    make_projects(projects)
    app = AgentlensApp(provider=lambda: [entry("waiting", "idle", 0)], root=projects, interval=60, config=cfg)

    async def steps(app, pilot):
        oxblood = config_mod.LOOKS["oxblood"]
        assert app.theme == "oxblood"  # restored on launch
        assert app.get_css_variables()["background"].upper() == oxblood.background
        # With the theme's colours, the look's (give or take Textual's rounding).
        a, b = Color.parse(app.palette.claude), Color.parse(oxblood.colors["claude"])
        assert max(abs(a.r - b.r), abs(a.g - b.g), abs(a.b - b.b)) <= 2
        # The settings screen opens the files, so the command palette doesn't list them.
        titles = [command.title for command in app.get_system_commands(app.screen)]
        assert "Settings" in titles and not any(title.startswith("Edit ") for title in titles)
        await pilot.press("comma")
        await pilot.pause(0.2)
        assert app.screen.query_one("#set-theme", Select).value == "oxblood"

    run_app(app, steps)


def test_settings_keep_your_own_colours_to_go_back_to(projects, tmp_path):
    cfg = config_mod.Config(path=tmp_path / "config.yaml")
    cfg.colors["selected"] = "#ff00ff"
    make_projects(projects)
    app = AgentlensApp(provider=lambda: [entry("waiting", "idle", 0)], root=projects, interval=60, config=cfg)

    async def steps(app, pilot):
        await pilot.press("comma")
        await pilot.pause(0.2)
        colors = app.screen.query_one("#set-colors", Select)
        assert colors.value == "own"
        colors.value = "calm"
        await pilot.pause(0.1)
        assert app.config.colors["selected"] == "$foreground"
        colors.value = "own"
        await pilot.pause(0.1)
        assert config_mod.load(cfg.path).colors["selected"] == "#ff00ff"

    run_app(app, steps)


def test_zoom_maps_subagents_without_their_details(projects):
    # Two agents launched together and finished; a later one that never handed back.
    write_jsonl(projects / "demo" / "waiting.jsonl", common([
        human(0), tool_use(1, "Agent", "a", msg_id="m1"), tool_use(1, "Agent", "b", msg_id="m1"),
        tool_result(1.1, "a"), tool_result(1.1, "b"), text(2), human(28), tool_use(29, "Agent", "c"),
        tool_result(29.1, "c"), text(29.2)], "waiting"))
    subs = projects / "demo" / "waiting" / "subagents"
    for name, call, desc, start in (("a", "a", "Summarize tests", 1.2), ("b", "b", "Map the source", 1.2)):
        write_jsonl(subs / f"agent-{name}.jsonl", common([
            tool_use(start, "Read", "r", file_path="D:/work/demo/a.py"), tool_result(start + 1, "r"),
            tool_use(start + 2, "SubagentHandback", "h", message="report"), tool_result(start + 2.1, "h")], "waiting"))
        (subs / f"agent-{name}.meta.json").write_text(json.dumps(
            {"description": desc, "toolUseId": call, "agentType": "Explore", "requestShape": "background"}),
            encoding="utf-8")
    write_jsonl(subs / "agent-c.jsonl", common([tool_use(29.5, "Bash", "x", command="make test")], "waiting"))
    (subs / "agent-c.meta.json").write_text(json.dumps(
        {"description": "Run the tests", "toolUseId": "c", "requestShape": "background"}), encoding="utf-8")
    app = AgentlensApp(provider=lambda: [entry("waiting", "idle", 0)], root=projects, interval=60)

    async def steps(app, pilot):
        await pilot.press("enter")
        await pilot.pause(0.2)
        out = app.screen.query_one("#zoom-subs-body").text
        assert "2 finished · 1 quiet · 2 waves" in out
        # Newest wave first; each agent once, with its type, and nothing of what it did.
        assert out.index("○ Run the tests") < out.index("2 in parallel") < out.index("✓ Summarize tests")
        assert "✓ Map the source · Explore" in out
        assert "a.py" not in out and "make test" not in out and "report" not in out

    run_app(app, steps)


def test_zoom_lists_changed_files_and_opens_them_in_your_editor(projects, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    a, b = str(repo / "a.py"), str(repo / "docs" / "b.md")
    write_jsonl(projects / "demo" / "waiting.jsonl", common([
        human(0, "first"), tool_use(1, "Edit", "e1", file_path=a), edit_result(2, "e1", a, new_start=5), text(3),
        human(4, "second"), tool_use(5, "Write", "e2", file_path=b), edit_result(6, "e2", b, created=True), text(7),
    ], "waiting", cwd=str(repo)))
    (repo / "docs").mkdir()
    (repo / "docs" / "b.md").write_text("# b", encoding="utf-8")  # a.py has been deleted since
    app = AgentlensApp(provider=lambda: [entry("waiting", "idle", 0, cwd=str(repo))], root=projects, interval=60)
    launched = []
    app.launch_editor = launched.append
    monkeypatch.setattr(editor_mod.shutil, "which", {"code": "code"}.get)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)

    async def steps(app, pilot):
        await pilot.press("E")  # from the wall: the session's repository
        assert launched == [["code", str(repo)]]
        await pilot.press("enter")
        await pilot.pause(0.2)
        files = app.screen.query_one("#zoom-files")
        assert [str(o.prompt) for o in files.options] == [
            "Since your last message", f"docs/b.md:1  {clock(6)}", "Earlier", f"a.py:5  {clock(2)}  not on disk"]
        await pilot.press("right", "enter")  # over to the list, where the newest file is already picked
        assert launched[-1] == ["code", "--goto", str(repo / "docs" / "b.md") + ":1"]
        await pilot.press("down", "enter")  # past the label to a.py, deleted since
        assert len(launched) == 2  # VS Code would start a new file by that name
        assert any("a.py isn't on disk" in n.message for n in app._notifications)

    run_app(app, steps)


def test_zoom_side_column_works_from_the_keyboard(projects, tmp_path):
    a, b = str(tmp_path / "a.py"), str(tmp_path / "b.py")
    write_jsonl(projects / "demo" / "waiting.jsonl", common([
        human(0), tool_use(1, "Edit", "e1", file_path=a), edit_result(2, "e1", a, new_start=3),
        tool_use(3, "Edit", "e2", file_path=b), edit_result(4, "e2", b, new_start=7), text(5)], "waiting"))
    write_jsonl(projects / "demo" / "busy.jsonl", common([human(0), text(1)], "busy"))  # changed nothing
    app = AgentlensApp(provider=lambda: [entry("waiting", "idle", 0), entry("busy", "idle", 1)], root=projects,
                       interval=60)

    def focused_id():
        return app.focused.id if app.focused else None

    def footer_keys():
        return {binding.description for _, binding, _, _ in app.screen.active_bindings.values() if binding.show}

    async def steps(app, pilot):
        app.query_one("#s-waiting").focus()
        await pilot.press("enter")
        await pilot.pause(0.2)
        head = app.screen.query_one("#zoom-files-head")
        assert focused_id() == "zoom-main" and "Files, subagents" in footer_keys()
        assert "→ to choose one" in str(head.content)
        await pilot.press("right")
        await pilot.pause()
        files = app.screen.query_one("#zoom-files")

        def picked():
            return files.highlighted_option.id if files.highlighted_option else None

        assert focused_id() == "zoom-files" and picked() == b  # the newest
        assert "Enter opens it in your editor" in str(head.content)
        assert {"Open in editor", "Conversation"} <= footer_keys()
        await pilot.press("up")
        assert focused_id() == "zoom-files" and picked() == b  # the column doesn't wrap
        await pilot.press("down", "down")  # past the last file, on down to the subagents
        assert focused_id() == "zoom-map"
        await pilot.press("up")  # and back up, to the file just above
        assert focused_id() == "zoom-files" and picked() == a
        await pilot.press("left")
        assert focused_id() == "zoom-main"
        await pilot.press("tab", "tab")  # Tab goes the same way round
        assert focused_id() == "zoom-map"
        await pilot.press("left", "escape")
        assert not isinstance(app.screen, ZoomScreen)

        # With no files changed, → goes straight to the subagents.
        app.query_one("#s-busy").focus()
        await pilot.press("enter", "right")
        await pilot.pause()
        assert focused_id() == "zoom-map"

    run_app(app, steps)


def clock(minutes):
    return f"{(T0 + timedelta(minutes=minutes)).astimezone():%H:%M}"


def test_a_session_that_just_connected_is_ready_not_waiting_on_you(projects):
    app = AgentlensApp(provider=lambda: [], root=projects, interval=60)

    async def steps(app, pilot):
        app.bell = lambda: None
        session = LiveSession("new-1", "d-1", "demo", "Fix the build", "agentlens", STARTING, None, "")
        for state in (STARTING, WAITING, WORKING, WAITING):  # brought here, then a turn you asked for
            session.state = state
            app._notify_if_needed(session)
        await pilot.pause()
        assert [n.title for n in app._notifications] == ["Fix the build is waiting on you"]

    run_app(app, steps)


def test_spaces_group_by_repo_and_can_be_assigned(projects, isolated_home):
    make_projects(projects)
    entries = [entry("busy", "busy", 0), entry("waiting", "idle", 1), entry("other", "idle", 2, "D:\\work\\other")]
    app = AgentlensApp(provider=lambda: entries, root=projects, interval=60)

    async def steps(app, pilot):
        assert app.space_names() == [ALL, "demo", "other"]
        await pilot.press("right_square_bracket")  # next space: demo
        await pilot.pause(0.1)
        assert app.current_space == "demo"
        assert [p.session_id for p in app.query(SessionPane) if p.display] == ["busy", "waiting"]
        # Move the focused session into a new hand-made space.
        await pilot.press("g")
        await pilot.pause(0.1)
        assert isinstance(app.screen, MoveToSpaceScreen)
        await pilot.click("#move-new")
        await pilot.press(*"TV work", "enter")
        await app.workers.wait_for_complete()
        await pilot.pause(0.2)
        assert "TV work" in app.space_names()
        saved = config_mod.load(isolated_home / "config.yaml")
        assert saved.spaces[0].name == "TV work" and saved.spaces[0].sessions == ["busy"]
        assert saved.last_space == "demo"

    run_app(app, steps)


def test_spaces_list_works_from_the_keyboard(projects):
    make_projects(projects)
    entries = [entry("busy", "busy", 0), entry("waiting", "idle", 1), entry("other", "idle", 2, "D:\\work\\other")]
    app = AgentlensApp(provider=lambda: entries, root=projects, interval=60)

    async def steps(app, pilot):
        tree = app.query_one(SpacesTree)
        busy, other = app.query_one("#s-busy"), app.query_one("#s-other")
        await pilot.press("right", "left")  # left inside the grid still moves between panes
        assert app.focused is busy
        await pilot.press("left")  # off the grid's left edge, into the spaces list
        assert app.focused is tree and tree.cursor_line == 0  # on "All sessions"
        await pilot.press("down")  # switches straight away; no enter needed
        assert app.current_space == "demo" and tree.cursor_line == 1
        assert [p.session_id for p in app.query(SessionPane) if p.display] == ["busy", "waiting"]
        await pilot.press("down", "down")  # stops at the last space instead of wrapping round
        assert app.current_space == "other" and tree.cursor_line == 2
        await pilot.press("enter")  # back to the sessions; busy is hidden now, so the first one showing
        assert app.focused is other
        await pilot.press("left", "up", "right")
        assert app.current_space == "demo" and app.focused is busy
        await pilot.press("right_square_bracket")  # [ and ] move the list's cursor too
        assert tree.cursor_line == 2
        await pilot.press("left", "s")  # hiding the list while in it goes back to the sessions
        assert app.focused is other
        await pilot.press("left")  # and with it hidden, left stays in the grid
        assert app.focused is other

    run_app(app, steps)


async def finish_turn(app, pilot, entries, index):
    entries[index]["status"] = "idle"
    app.action_refresh()
    await app.workers.wait_for_complete()
    await pilot.pause(0.2)


def test_first_look_shows_the_panes_then_fills_them_in_without_false_alerts(projects, monkeypatch):
    # The first look reads too little of "asking" to find a whole record, so it seems to be just
    # starting; the full read finds the question it was already asking. That's no news to alert.
    monkeypatch.setattr("agentlens.tui.QUICK_TAIL_BYTES", 10)
    make_projects(projects)
    write_jsonl(projects / "demo" / "asking.jsonl",
                common([human(0), tool_use(1, "AskUserQuestion", "q1")], "asking"))
    app = AgentlensApp(provider=lambda: [entry("waiting", "idle", 0), entry("asking", "busy", 1)],
                       root=projects, interval=60)
    applied = []
    apply = app._apply

    def recording(monitored, now, driven_tails=None, generation=None, first_look=False):
        applied.append((first_look, {s.session_id: s.state for s in monitored}["asking"]))
        apply(monitored, now, driven_tails, generation, first_look)

    app._apply = recording

    async def steps(app, pilot):
        assert applied[:2] == [(True, WORKING), (False, QUESTION)]
        assert app.sessions["asking"].state == QUESTION and app.focused.session_id == "waiting"
        assert not app._alerts

    run_app(app, steps)


def test_clicking_an_alert_opens_its_session(projects):
    make_projects(projects)
    entries = [entry("waiting", "idle", 0), entry("busy", "busy", 1)]
    app = AgentlensApp(provider=lambda: entries, root=projects, interval=60)

    async def go():
        async with app.run_test(size=(160, 40), notifications=True) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause(0.2)
            assert app.focused.session_id == "waiting"
            await finish_turn(app, pilot, entries, 1)  # while you're on another pane
            assert list(app._alerts) == ["busy"]
            toast = app.screen.query_one("Toast")
            await pilot.click(toast, offset=(4, toast.region.height - 2))  # its Open button, on the last line
            await pilot.pause(0.2)
            assert isinstance(app.screen, ZoomScreen) and app.screen.session_id == "busy"
            assert not app._alerts and not app.screen.query("Toast")  # followed, and dismissed
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert app.focused.session_id == "busy"  # back on its pane

    asyncio.run(go())


def test_j_takes_down_the_alert_it_follows(projects):
    make_projects(projects)
    entries = [entry("waiting", "idle", 0), entry("busy", "busy", 1), entry("other", "busy", 2, "D:\\work\\other")]
    app = AgentlensApp(provider=lambda: entries, root=projects, interval=60)

    def toasts():
        return [toast._notification for toast in app.screen.query("Toast")]

    async def go():
        async with app.run_test(size=(160, 40), notifications=True) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause(0.2)
            await finish_turn(app, pilot, entries, 1)
            await finish_turn(app, pilot, entries, 2)
            assert toasts() == [app._alerts["busy"], app._alerts["other"]]
            await pilot.press("j")  # the newest alert
            await pilot.pause(0.2)
            assert app.screen.session_id == "other"
            assert toasts() == [app._alerts["busy"]]  # gone at once, not left up for its 15 seconds
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert toasts() == [app._alerts["busy"]]  # off the wall too
            entries[1]["status"] = "busy"  # answered somewhere else
            app.action_refresh()
            await app.workers.wait_for_complete()
            await pilot.pause(0.2)
            assert not app._alerts and not toasts()

    asyncio.run(go())


def test_j_goes_to_the_newest_alert_then_each_session_that_needs_you(projects):
    make_projects(projects)
    entries = [entry("waiting", "busy", 0), entry("busy", "busy", 1), entry("other", "busy", 2, "D:\\work\\other")]
    app = AgentlensApp(provider=lambda: entries, root=projects, interval=60)

    async def steps(app, pilot):
        await pilot.press("right_square_bracket")  # the demo space
        await pilot.pause(0.1)
        assert app.current_space == "demo" and not app.check_action("jump", ())  # nothing needs you
        await finish_turn(app, pilot, entries, 1)
        await finish_turn(app, pilot, entries, 2)
        await pilot.press("j")  # the newest alert, though it's in another space
        await pilot.pause(0.2)
        assert app.screen.session_id == "other" and app.current_space == "other"
        await pilot.press("j")  # the older alert
        await pilot.pause(0.2)
        assert app.screen.session_id == "busy" and app.current_space == "demo"
        entries[2]["status"] = "busy"  # other gets going again
        await finish_turn(app, pilot, entries, 0)  # you're in busy, so waiting alerts
        await pilot.press("escape")
        await pilot.pause(0.1)
        await pilot.click("#summary", offset=(1, 0))  # the "2 need you" count works like j
        await pilot.pause(0.2)
        assert app.screen.session_id == "waiting"
        await pilot.press("j", "escape")  # every alert followed: the next one after this, in wall order
        await pilot.pause(0.2)
        assert app.focused.session_id == "busy"
        entries[0]["status"] = "busy"
        await finish_turn(app, pilot, entries, 1)
        assert not app.check_action("jump", ())  # you're on the only one that needs you

    run_app(app, steps)


def test_report_and_history_screens_read_full_history(projects):
    make_projects(projects)  # sessions from a while ago: History looks at all of it here
    app = AgentlensApp(provider=lambda: [], root=projects, interval=60, config=config_mod.Config(history_days=0))

    async def steps(app, pilot):
        assert len(app.query("#empty")) == 1
        await pilot.press("R")
        await pilot.pause(0.1)
        await app.workers.wait_for_complete()
        await pilot.pause(0.3)
        assert isinstance(app.screen, ReportScreen)
        console = Console(record=True, width=120, file=io.StringIO())
        console.print(app.screen.query_one("#body").content)
        assert "Where main-thread time went" in console.export_text()
        await pilot.press("escape", "H")
        await pilot.pause(0.3)
        assert isinstance(app.screen, HistoryScreen)
        assert app.screen.query_one("#history").row_count == 3

    run_app(app, steps)


def test_history_reads_the_past_week_and_m_reaches_further_back(projects, monkeypatch):
    # Reading every transcript whole took long, so History reads only the sessions active in
    # the window from the settings, and keeps them: neither going further back nor the report
    # reads one again.
    import os
    from datetime import timezone

    from agentlens.adapters import claude_transcripts

    now = datetime.now(timezone.utc)
    for sid, days in (("yesterday", 1), ("last-month", 20), ("long-ago", 200)):
        start = now - timedelta(days=days)
        records = common([human(0, f"work from {sid}"), text(1)], sid)
        for i, r in enumerate(records):
            r["timestamp"] = (start + timedelta(minutes=i)).isoformat().replace("+00:00", "Z")
        path = write_jsonl(projects / "demo" / f"{sid}.jsonl", records)
        os.utime(path, (start.timestamp() + 60, start.timestamp() + 60))
    read, load = [], claude_transcripts.load_session
    monkeypatch.setattr(claude_transcripts, "load_session", lambda path, *a, **k: read.append(path.stem) or
                        load(path, *a, **k))
    app = AgentlensApp(provider=lambda: [], root=projects, interval=60)

    async def listed(pilot):
        for _ in range(100):
            await pilot.pause(0.05)
            if not str(app.screen.query_one("#history-status").content).startswith("Reading"):
                break
        table = app.screen.query_one("#history")
        return [table.coordinate_to_cell_key((i, 0)).row_key.value for i in range(table.row_count)]

    async def steps(app, pilot):
        await pilot.press("H")
        assert isinstance(app.screen, HistoryScreen)
        assert await listed(pilot) == ["yesterday"] and read == ["yesterday"]  # the others weren't opened
        assert str(app.screen.query_one("#history-status").content).startswith("1 session active in the past week.")
        await pilot.press("m")  # the past month
        assert await listed(pilot) == ["yesterday", "last-month"] and read == ["yesterday", "last-month"]
        await pilot.press("m", "m")  # 3 months, then everything
        assert await listed(pilot) == ["yesterday", "last-month", "long-ago"]
        assert read == ["yesterday", "last-month", "long-ago"]
        assert str(app.screen.query_one("#history-status").content) == "3 sessions in all your history."
        assert not app.screen.check_action("older", ())
        assert app.analysis is None  # History doesn't wait for the report's analysis
        await pilot.press("escape", "R")
        await app.workers.wait_for_complete()
        await pilot.pause(0.2)
        assert len(app.analysis.sessions) == 3 and len(read) == 3  # all of them from what History read

    run_app(app, steps)


def test_history_keeps_a_session_claude_code_deleted_but_cant_continue_it(projects):
    make_projects(projects)
    cfg = config_mod.Config(history_days=0)
    first = AgentlensApp(provider=lambda: [], root=projects, interval=60, config=cfg)

    async def read_once(app, pilot):
        await pilot.press("H")
        await pilot.pause(0.5)

    run_app(first, read_once)  # what agentlens kept of them
    (projects / "demo" / "waiting.jsonl").unlink()  # Claude Code's cleanup, 30 days on
    app = AgentlensApp(provider=lambda: [], root=projects, interval=60, config=cfg)
    notes = []

    async def steps(app, pilot):
        app.notify = lambda message, **kw: notes.append(kw.get("title"))
        await pilot.press("H")
        for _ in range(100):
            await pilot.pause(0.05)
            if str(app.screen.query_one("#history-status").content) == "3 sessions in all your history.":
                break
        table = app.screen.query_one("#history")
        row = table.get_row_index("waiting")
        assert str(table.get_row_at(row)[2]).endswith("· deleted by Claude Code")
        table.move_cursor(row=row)
        await pilot.press("o")
        assert notes == ["Can't continue it"] and isinstance(app.screen, HistoryScreen)
        await pilot.press("enter")  # its details are still there
        await pilot.pause(0.2)
        assert type(app.screen).__name__ == "SessionDetailScreen" and app.screen.session_id == "waiting"
        assert app.screen.data.session("waiting").human_prompts

    run_app(app, steps)


def test_fresh_advice_follows_up_on_the_last_and_both_are_kept(projects, monkeypatch):
    make_projects(projects)
    advise.save("1. Plan first.", at=datetime(2026, 10, 19, 9, 0))
    prompts = []
    monkeypatch.setattr(advise, "run", lambda prompt, model=None, **kw: prompts.append(prompt) or "1. Fork instead.")
    app = AgentlensApp(provider=lambda: [], root=projects, interval=60)

    async def steps(app, pilot):
        await pilot.press("A")
        await pilot.pause(0.1)
        await app.workers.wait_for_complete()
        await pilot.pause(0.3)
        assert isinstance(app.screen, AdviseScreen)
        assert "Last advice, from 2026-10-19 09:00" in str(app.screen.query_one("#advice-meta").content)
        await pilot.press("g")
        await pilot.pause(0.1)
        await app.workers.wait_for_complete()
        await pilot.pause(0.3)
        assert "> 1. Plan first." in prompts[0]
        assert advise.latest().text == "1. Fork instead."
        assert len(list(advise.history_dir().glob("*.md"))) == 2

    run_app(app, steps)


def test_while_claude_writes_advice_the_screen_lists_what_it_is_on(projects, monkeypatch):
    make_projects(projects)
    gate = threading.Event()

    def run(prompt, model=None, on_step=lambda step: None, **kw):
        on_step("searching the web for claude code hooks")
        on_step("reading code.claude.com/docs/en/hooks")
        gate.wait(5)  # until the test has looked
        return "1. Fork instead."

    monkeypatch.setattr(advise, "run", run)
    app = AgentlensApp(provider=lambda: [], root=projects, interval=60)

    async def steps(app, pilot):
        await pilot.press("A")
        await pilot.pause(0.1)
        await app.workers.wait_for_complete()
        await pilot.pause(0.2)
        await pilot.press("g")
        await pilot.pause(0.3)
        shown = str(app.screen.query_one("#loading-text").render())
        assert "searching the web for claude code hooks" in shown and "reading code.claude.com/docs/en/hooks" in shown
        assert "minute" not in str(app.screen.query_one("#advice-meta").render())  # no times, anywhere
        app.screen._ready(app.analysis)  # history read again meanwhile: Claude's steps stay
        await pilot.pause(0.2)
        assert app.screen.query_one("#loading-text").display
        assert "reading code.claude.com/docs/en/hooks" in str(app.screen.query_one("#loading-text").render())
        await pilot.press("g")  # while it's at it: no second Claude
        assert app.jobs.get("advice").step == "reading code.claude.com/docs/en/hooks"
        gate.set()
        await app.workers.wait_for_complete()
        await pilot.pause(0.2)
        assert app.jobs.get("advice") is None and advise.latest().text == "1. Fork instead."
        assert len(list(advise.history_dir().glob("*.md"))) == 1

    run_app(app, steps)


def test_history_is_read_once_the_wall_is_filled_in_so_report_screens_open_at_once(projects, monkeypatch):
    make_projects(projects)
    monkeypatch.setattr(tui, "PRELOAD_SECONDS", 0)
    builds = []
    build = analysis_mod.build
    monkeypatch.setattr(analysis_mod, "build", lambda *a, **k: builds.append(1) or build(*a, **k))
    app = AgentlensApp(provider=lambda: [entry("busy", "busy", 0)], root=projects, interval=60)

    async def steps(app, pilot):
        await pilot.pause(0.3)
        await app.workers.wait_for_complete()
        assert app.analysis is not None and len(app.analysis.sessions) == 3 and builds == [1]
        await pilot.press("P")
        await pilot.pause(0.1)
        assert isinstance(app.screen, ProfileScreen)
        assert not app.screen.query_one("#loading-text").display  # nothing to wait for
        await app.workers.wait_for_complete()
        assert builds == [1]  # and it wasn't read again for it

    run_app(app, steps)


def test_a_screen_on_history_read_a_while_ago_shows_it_at_once_and_again_if_it_changed(projects, monkeypatch):
    make_projects(projects)
    populated = []
    populate = ProfileScreen.populate
    monkeypatch.setattr(ProfileScreen, "populate",
                        lambda self, data: populated.append(len(data.sessions)) or populate(self, data))
    app = AgentlensApp(provider=lambda: [], root=projects, interval=60)

    async def reopen(pilot):
        await pilot.press("escape")
        await pilot.press("P")
        await pilot.pause(0.1)

    def read_long_ago():
        app.analysis = dataclasses.replace(app.analysis, loaded_at=app.analysis.loaded_at - timedelta(minutes=5))

    async def steps(app, pilot):
        await pilot.press("P")
        await pilot.pause(0.1)
        await app.workers.wait_for_complete()
        await pilot.pause(0.1)
        assert populated == [3]
        await reopen(pilot)  # just read: shown, not read again
        assert populated == [3, 3] and app.jobs.get("history") is None
        # A while later, with a session more: shown at once as it was, then as it is.
        read_long_ago()
        write_jsonl(projects / "demo" / "new.jsonl", common([human(0), text(1)], "new"))
        await reopen(pilot)
        assert populated[:3] == [3, 3, 3] and not app.screen.query_one("#loading-text").display
        await app.workers.wait_for_complete()
        await pilot.pause(0.1)
        assert populated == [3, 3, 3, 4]
        # Read again with nothing new: the screen stays as it is.
        read_long_ago()
        await reopen(pilot)
        await app.workers.wait_for_complete()
        await pilot.pause(0.1)
        assert populated == [3, 3, 3, 4, 4]
        assert datetime.now(timezone.utc) - app.analysis.loaded_at < timedelta(minutes=1)

    run_app(app, steps)


def test_reading_history_shows_how_far_it_got_and_why_it_is_slower_after_an_update(projects, monkeypatch):
    make_projects(projects)
    app = AgentlensApp(provider=lambda: [], root=projects, interval=60)
    monkeypatch.setattr(app, "_build_analysis", lambda: None)  # keep the history read pending

    async def steps(app, pilot):
        await pilot.press("P")
        await pilot.pause(0.1)
        app._history_progress(312, 1072, read_again=300)
        await pilot.pause(0.3)
        shown = str(app.screen.query_one("#loading-text").render())
        assert "312 of 1072 transcripts" in shown and "reads them all again, this once" in shown
        await pilot.press("escape")
        # Back on the wall, the line at the top says it's still at it.
        app.jobs.get("history").started -= 1
        await pilot.pause(0.3)
        line = app.query_one("#activity")
        assert line.display and "reading your history" in str(line.render()) and "312 of 1072" in str(line.render())
        app._analysis_ready(analysis_mod.build(projects))
        await pilot.pause(0.3)
        assert not line.display

    run_app(app, steps)


def test_closing_a_screen_before_history_loads_does_not_crash(projects, monkeypatch):
    make_projects(projects)
    app = AgentlensApp(provider=lambda: [], root=projects, interval=60)
    monkeypatch.setattr(app, "_build_analysis", lambda: None)  # keep the history read pending

    async def steps(app, pilot):
        await pilot.press("P")
        await pilot.pause(0.1)
        assert isinstance(app.screen, ProfileScreen)
        await pilot.press("escape")
        await pilot.pause(0.1)
        app._analysis_ready(analysis_mod.build(projects))
        await pilot.pause(0.1)
        assert app.is_running and not isinstance(app.screen, ProfileScreen)

    run_app(app, steps)


def test_events_are_readable(projects):
    path = write_jsonl(projects / "demo" / "s.jsonl", common([
        human(0, "please fix\nthe build"),
        tool_use(1, "Read", "r1", file_path="D:\\work\\demo\\src\\main.cpp"),
        tool_use(2, "Bash", "b1", command='cd "D:\\work\\demo" && cmake --build build'),
        {"type": "user", "timestamp": at(3), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "b1", "is_error": True, "content": "error: missing ;"}]}},
        {"type": "user", "timestamp": at(4), "origin": {"kind": "task-notification"},
         "message": {"role": "user", "content": "<task-notification><summary>Agent \"x\" finished</summary>"}},
        text(5, "Fixed it.\nDetails follow."),
    ], "s"))
    session, events = load_tail(path, tail_bytes=10_000)
    feed = [(e.kind, e.text) for e in events["main"]]
    assert feed == [
        ("you", "please fix"),
        ("tool", "Read: src/main.cpp"),
        ("tool", "Bash: cmake --build build"),
        ("error", "error: missing ;"),
        ("notice", 'Agent "x" finished'),
        ("claude", "Fixed it."),
    ]
    assert events["main"][-1].detail == "Fixed it.\nDetails follow."


def test_images_pasted_with_a_message_show_as_marks(projects):
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0K"}}
    path = write_jsonl(projects / "demo" / "s.jsonl", common([
        {"type": "user", "timestamp": at(0), "origin": {"kind": "human"}, "imagePasteIds": [1, 2],
         "message": {"role": "user", "content": [image, image, {"type": "text", "text": "why is this cut off?"}]}},
        {"type": "user", "timestamp": at(1), "origin": {"kind": "human"},
         "message": {"role": "user", "content": [image]}},
    ], "s"))
    _, events = load_tail(path, tail_bytes=10_000)
    assert [e.text for e in events["main"]] == ["[Image #1] [Image #2] why is this cut off?", "[Image #1]"]


def test_conversation_reads_like_a_chat():
    events = [
        Event(T0, "you", "fix it", "fix it"),
        Event(T0, "tool", "Read: src/a.py"), Event(T0, "tool", "Edit: src/a.py"), Event(T0, "tool", "Edit: src/a.py"),
        Event(T0, "tool", "Bash: make"), Event(T0, "tool", "Bash: make test"), Event(T0, "error", "boom"),
        Event(T0, "claude", "Done", "## Done\n\n**Fixed** the `bug`."),
    ]
    summary = tools_summary(events[1:7], Palette()).plain
    assert summary == "▸ Read a.py · Edit a.py ×2 · Bash ×2   ✗ 1 error"
    console = Console(record=True, width=80, file=io.StringIO())
    console.print(conversation(events, Palette()))
    out = console.export_text()
    assert "You" in out and "Claude" in out and "Fixed the bug." in out
    assert "**" not in out and "##" not in out  # markdown rendered, not shown raw
    assert "make test" not in out  # folded into the summary in pane view
    console = Console(record=True, width=80, file=io.StringIO())
    console.print(conversation(events, Palette(), full=True))
    assert "make test" in console.export_text()  # zoom lists every call


def render_text(renderable, width):
    console = Console(record=True, width=width, file=io.StringIO(), force_terminal=True, color_system="truecolor")
    console.print(renderable)
    return console.export_text(styles=True)


def test_conversation_blocks_render_once_and_like_new():
    feed_mod._blocks.clear()
    reply = "## Done\n\n" + "Fixed the **bug** in the parser, then " * 6 + "\n\n```python\nx = 1\n```"
    events = [Event(T0, "you", "fix it", "fix it"), Event(T0, "claude", "Done", reply)]
    first = conversation(events, Palette())
    later = conversation([*events, Event(T0, "tool", "Bash: make")], Palette())
    # The blocks that didn't change are the same objects, so their rendering is reused.
    assert later.renderables[0] is first.renderables[0] and later.renderables[2] is first.renderables[2]
    narrow, wide = render_text(later, 40), render_text(later, 100)
    assert render_text(later, 40) == narrow  # each width keeps its own rendering
    assert len(wide.splitlines()) < len(narrow.splitlines())
    feed_mod._blocks.clear()
    fresh = conversation([*events, Event(T0, "tool", "Bash: make")], Palette())
    assert render_text(fresh, 40) == narrow and render_text(fresh, 100) == wide


def test_clipping_keeps_code_fences_closed():
    clipped = _clip_lines("```python\n" + "\n".join(f"x = {i}" for i in range(30)) + "\n```", 5)
    assert clipped.count("```") == 2 and "Enter to read the rest" in clipped


def test_activity_log_collapses_repeats():
    events = [Event(T0, "tool", "Edit: a.py"), Event(T0, "tool", "Edit: a.py"), Event(T0, "claude", "ok"),
              Event(T0, "tool", "Edit: a.py")]
    assert [n for _, n in collapse_repeats(events)] == [2, 1, 1]
    assert "×2" in activity_log(events, Palette()).plain


def test_subagent_feeds_are_extracted():
    from datetime import datetime

    records = [dict(r, _ts=datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")), _timed=True)
               for r in [tool_use(1, "WebFetch", "w", url="https://example.com"), tool_result(2, "w"),
                         tool_use(3, "SubagentHandback", "h", message="**Found it.**\n" + "line\n" * 20),
                         tool_result(4, "h")]]
    events = extract_events(records, is_main=False)
    assert [(e.kind, e.text) for e in events] == [("tool", "WebFetch: https://example.com"),
                                                  ("claude", "**Found it.**")]  # the report is a reply
    console = Console(record=True, width=60, file=io.StringIO())
    console.print(conversation(events, Palette(), clip=False))
    out = console.export_text()
    assert "Found it." in out and "**" not in out and out.count("line") == 20 and "Enter" not in out


def test_config_spaces_and_repo_roots(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    (repo / "sub").mkdir()
    worktree = repo / ".claude" / "worktrees" / "feature-x"
    cfg = config_mod.Config(spaces=[config_mod.Space("TV", titles=["tv"]), config_mod.Space("Docs", paths=[str(tmp_path / "docs")])])
    assert config_mod.repo_name(str(repo / "sub")) == "proj"
    assert config_mod.repo_name(str(worktree)) == "proj"
    assert cfg.space_for("a", str(repo), "Samsung TV firmware") == "TV"
    assert cfg.space_for("b", str(tmp_path / "docs" / "x"), "notes") == "Docs"
    assert cfg.space_for("c", str(repo), "build fix") == "proj"
    cfg.assign("c", "Docs")
    assert cfg.space_for("c", str(repo), "build fix") == "Docs"
    cfg.assign("a", "Docs")  # moved by hand: that beats TV's words, though TV comes first
    assert cfg.space_for("a", str(repo), "Samsung TV firmware") == "Docs"
    cfg.assign("a", "TV")
    assert [s.sessions for s in cfg.spaces] == [["a"], ["c"]]
    cfg.path = tmp_path / "config.yaml"
    config_mod.save(cfg)
    again = config_mod.load(cfg.path)
    assert [s.name for s in again.spaces] == ["TV", "Docs"] and again.spaces[1].sessions == ["c"]
    # Sessions run here were once saved under their pane's key, which is new every launch.
    cfg.assign("d-c5f6c3ff88", "Docs")
    config_mod.save(cfg)
    assert config_mod.load(cfg.path).spaces[1].sessions == ["c"]
