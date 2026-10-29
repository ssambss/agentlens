"""Driven sessions against a fake Claude Code: no real CLI, no usage spent."""

import asyncio
import time

from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)

from agentlens.driver import APPROVAL, PERMISSION, QUESTION, WAITING, DrivenSession
from agentlens.tui import AgentlensApp, DrivenPane


class FakeClaude:
    """Scripted stand-in for ClaudeSDKClient. A script maps a prompt to steps: a message
    to emit, or ("ask", tool, input) to call the permission callback and wait for it."""

    info: dict | None = None  # what get_server_info() returns: its slash commands
    titles: dict[str, str] = {}  # the title it gives a session, by what the session is about

    def __init__(self, options, scripts):
        self.options, self.scripts = options, scripts
        self.queue: asyncio.Queue = asyncio.Queue()
        self.decisions: list = []
        self.sent: list[str] = []
        self.blocks: list[list[dict]] = []  # the content of each message sent with images
        self.controls: list[dict] = []  # control requests the SDK has no call for
        self.closed = False
        self._query = self  # where the SDK keeps its control channel

    async def _send_control_request(self, request):
        self.controls.append(request)
        return {"title": self.titles.get(request.get("description"))}

    async def connect(self):
        await self.queue.put(SystemMessage("init", {"session_id": "sess-42"}))

    async def get_server_info(self):
        return self.info

    async def query(self, prompt):
        if not isinstance(prompt, str):  # a message with images: keep its content blocks
            content = [m async for m in prompt][0]["message"]["content"]
            self.blocks.append(content)
            prompt = next((b["text"] for b in content if b["type"] == "text"), "")
        self.sent.append(prompt)
        asyncio.ensure_future(self._play(self.scripts.get(prompt, [])))

    async def _play(self, steps):
        for step in steps:
            if isinstance(step, tuple) and step[0] == "ask":
                _, tool, tool_input = step
                decision = await self.options.can_use_tool(tool, tool_input, ToolPermissionContext(title=None))
                self.decisions.append(decision)
            else:
                await self.queue.put(step)

    async def receive_messages(self):
        while True:
            message = await self.queue.get()
            if message is None:
                return  # Claude Code exited
            if isinstance(message, Exception):
                raise message
            yield message

    async def interrupt(self):
        pass

    async def disconnect(self):
        self.closed = True


def text(t):
    return AssistantMessage([TextBlock(t)], "claude-opus-5-5")


def done(cost=0.01):
    return ResultMessage("success", 10, 10, False, 1, "sess-42", total_cost_usd=cost)


def partial(t):
    return StreamEvent("u", "sess-42", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": t}})


async def settle(condition, steps=200):
    for _ in range(steps):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


def make(scripts, **kw):
    fakes = []

    def factory(options):
        fakes.append(FakeClaude(options, scripts))
        return fakes[-1]

    return DrivenSession("D:/work/demo", client_factory=factory, cli_path="claude", **kw), fakes


def test_conversation_streams_and_finishes():
    async def go():
        d, fakes = make({"hi": [partial("Hel"), partial("lo"), text("Hello there."), done(0.02)]})
        await d.start("hi")
        await settle(lambda: d.state == WAITING and d.events[-1].kind == "claude")
        assert [(e.kind, e.text) for e in d.events] == [("you", "hi"), ("claude", "Hello there.")]
        assert d.session_id == "sess-42" and d.partial == "" and abs(d.cost_usd - 0.02) < 1e-9
        assert d.title == "hi"
        await d.close()
    asyncio.run(go())


def test_claude_code_titles_a_session_run_here(monkeypatch):
    monkeypatch.setattr(FakeClaude, "titles", {"why is the parser so slow on big repos?": "Parser performance"})

    async def go():
        d, fakes = make({})
        await d.start()
        await d.send("/model haiku")  # a command isn't what the session is about
        assert fakes[0].controls == []
        await d.send("why is the parser so slow on big repos?")
        await settle(lambda: d.titled)
        assert d.title == "Parser performance"
        assert fakes[0].controls == [{"subtype": "generate_session_title", "persist": True,
                                      "description": "why is the parser so slow on big repos?"}]
        await d.send("and the tokenizer?")
        assert len(fakes[0].controls) == 1  # titled once
        await d.close()
    asyncio.run(go())


def test_too_little_to_title_asks_again_with_the_next_message(monkeypatch):
    monkeypatch.setattr(FakeClaude, "titles", {"test\nfix the flaky login test": "Flaky login test"})

    async def go():
        d, fakes = make({})
        await d.start("test")
        await settle(lambda: fakes[0].controls and d._naming is None)
        assert d.title == "test" and not d.titled  # Claude Code gives "test" no title
        await d.send("fix the flaky login test")
        await settle(lambda: d.titled)
        assert d.title == "Flaky login test"
        await d.close()
    asyncio.run(go())


def test_a_resumed_session_is_titled_from_the_message_it_is_called_by():
    async def go():
        d, fakes = make({}, resume="sess-42")
        d.title = "there are still question marks displayed on my screen in agentlens. for"
        d.park([])
        await d.send("any luck?")
        await settle(lambda: fakes and fakes[0].controls)
        assert fakes[0].controls[0]["description"] == f"{d.title}\nany luck?"
        await d.close()
    asyncio.run(go())


def test_permission_allow_always_and_deny():
    async def go():
        write = ("ask", "Write", {"file_path": "D:/work/demo/a.txt", "content": "x"})
        d, fakes = make({
            "write it": [write, text("written"), done()],
            "again": [write, text("denied ok"), done()],
        })
        await d.start("write it")
        await settle(lambda: d.state == PERMISSION)
        assert d.pending.tool_name == "Write" and d.pending.summary == "a.txt"
        d.allow(always=True)
        await settle(lambda: d.state == WAITING)
        assert isinstance(fakes[0].decisions[0], PermissionResultAllow)
        await d.send("again")
        await settle(lambda: d.state == PERMISSION)
        d.deny("not that file")
        await settle(lambda: d.state == WAITING)
        decision = fakes[0].decisions[1]
        assert isinstance(decision, PermissionResultDeny) and decision.message == "not that file"
    asyncio.run(go())


def test_questions_are_answered_in_the_sdk_shape():
    questions = {"questions": [
        {"question": "Which colour?", "options": [{"label": "Red"}, {"label": "Blue"}], "multiSelect": False},
        {"question": "Which size?", "options": [{"label": "S"}, {"label": "L"}], "multiSelect": False},
    ]}

    async def go():
        d, fakes = make({"ask me": [("ask", "AskUserQuestion", questions), text("ok"), done()]})
        await d.start("ask me")
        await settle(lambda: d.state == QUESTION)
        d.choose(2)
        assert d.state == QUESTION and d.pending.current_question["question"] == "Which size?"
        d.answer("extra large, please")
        await settle(lambda: d.state == WAITING)
        decision = fakes[0].decisions[0]
        assert decision.updated_input["answers"] == {"Which colour?": "Blue", "Which size?": "extra large, please"}
        assert decision.updated_input["questions"] == questions["questions"]
    asyncio.run(go())


def test_plan_approval_and_feedback():
    plan = {"plan": "## Plan\n1. Do the thing"}

    async def go():
        d, fakes = make({"plan": [("ask", "ExitPlanMode", plan), ("ask", "ExitPlanMode", plan), done()]})
        await d.start("plan")
        await settle(lambda: d.state == APPROVAL)
        d.deny("also add tests")
        await settle(lambda: len(fakes[0].decisions) == 1 and d.state == APPROVAL)
        assert fakes[0].decisions[0].message == "also add tests"
        d.approve_plan()
        await settle(lambda: d.state == WAITING)
        assert isinstance(fakes[0].decisions[1], PermissionResultAllow)
    asyncio.run(go())


def test_start_failure_is_shown_not_raised():
    def broken(options):
        raise RuntimeError("no claude here")

    async def go():
        d = DrivenSession("D:/work/demo", client_factory=broken, cli_path="claude")
        await d.start()
        assert d.state == "failed" and "no claude here" in d.error
    asyncio.run(go())


def test_new_session_typed_in_a_pane(projects, tmp_path):
    folder = tmp_path / "repo"
    folder.mkdir()
    scripts = {
        "fix the build": [text("On it."), ("ask", "Bash", {"command": "make"}), text("Built."), done()],
        "thanks": [text("Any time."), done()],
    }
    fakes = []

    def factory(options):
        fakes.append(FakeClaude(options, scripts))
        return fakes[-1]

    app = AgentlensApp(provider=lambda: [], root=projects, interval=60)
    app.client_factory = factory

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("n")
            await pilot.pause(0.2)
            app.screen.query_one("#new-folder").value = str(folder)
            await pilot.press(*"fix the build", "enter")
            for _ in range(100):
                await pilot.pause(0.05)
                if app.driven and next(iter(app.driven.values())).state == PERMISSION:
                    break
            driven = next(iter(app.driven.values()))
            assert driven.state == PERMISSION and driven.cwd == str(folder)
            pane = app.query_one(DrivenPane)
            assert app.focused is pane
            await pilot.press("1")  # allow, from the focused pane
            for _ in range(100):
                await pilot.pause(0.05)
                if driven.state == WAITING:
                    break
            assert driven.state == WAITING and isinstance(fakes[0].decisions[0], PermissionResultAllow)
            await pilot.press("i", *"thanks", "enter")
            for _ in range(100):
                await pilot.pause(0.05)
                if driven.events[-1].text == "Any time.":
                    break
            assert [e.text for e in driven.events if e.kind == "you"] == ["fix the build", "thanks"]
            assert app.config.last_folder == str(folder)
            await pilot.press("escape", "enter")  # leave the box, zoom
            await pilot.pause(0.3)
            assert app.screen.query("#zoom-composer")
            await pilot.press("escape")  # out of the zoomed box: to the conversation, so → reaches the files
            assert app.focused is app.screen.query_one("#zoom-main")
            await pilot.press("right")
            assert app.focused is app.screen.query_one("#zoom-map")  # this session changed no files

    asyncio.run(go())


def test_new_session_folder_picked_with_the_mouse(projects, tmp_path):
    from pathlib import Path

    from agentlens.tui_driven import FolderPickerScreen, FolderTree, NewSessionScreen

    work = tmp_path / "work"
    (work / "repo" / "src").mkdir(parents=True)
    (work / "repo" / ".git").mkdir()
    (work / "other" / "docs").mkdir(parents=True)
    (work / "notes.txt").write_text("x", encoding="utf-8")
    app = AgentlensApp(provider=lambda: [], root=projects, interval=60)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:

            async def folders_in(folder):
                tree = app.screen.query_one(FolderTree)
                for _ in range(100):
                    await pilot.pause(0.05)
                    if Path(tree.path) == folder and tree.root.children:
                        return [n.data.path.name for n in tree.root.children]
                raise AssertionError(f"{folder} never loaded")

            await pilot.press("n")
            await pilot.pause(0.2)
            box = app.screen.query_one("#new-folder")
            box.value = str(work / "repo")
            await pilot.pause(0.1)  # its note changes, and the dialog with it
            await pilot.click("#new-browse")
            await pilot.pause(0.2)
            assert isinstance(app.screen, FolderPickerScreen)
            assert await folders_in(work / "repo") == ["src"]  # no hidden folders
            await pilot.click("#folder-up")
            assert await folders_in(work) == ["other", "repo"]  # folders only
            await pilot.click(FolderTree, offset=(10, 2), times=2)  # the first folder, under the root's line
            await pilot.pause(0.2)
            assert isinstance(app.screen, NewSessionScreen)
            assert box.value == str(work / "other")
            assert app.focused is app.screen.query_one("#new-prompt")

            await pilot.click("#new-browse")  # or by keyboard, and confirming with the button
            await pilot.pause(0.2)
            await folders_in(work / "other")
            await pilot.press("backspace")
            await folders_in(work)
            await pilot.press("down", "down")
            assert "repo" in str(app.screen.query_one("#folder-chosen").render())
            await pilot.click("#folder-use")
            await pilot.pause(0.2)
            assert box.value == str(work / "repo")

            await pilot.click("#new-browse")  # Esc leaves the folder as it was
            await pilot.pause(0.2)
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert isinstance(app.screen, NewSessionScreen) and box.value == str(work / "repo")

    asyncio.run(go())


def test_new_session_dialog_says_which_space_it_opens_in(projects, tmp_path, isolated_home):
    """A session started in a space you made goes in it, whatever folder it works in. So the
    dialog says which space, and doesn't offer the folder of a session in another space."""
    from pathlib import Path

    from agentlens import config as config_mod
    from agentlens.tui_driven import NewSessionScreen

    repo, acme = tmp_path / "repo", tmp_path / "acme"
    repo.mkdir()
    acme.mkdir()
    config_mod.save(config_mod.Config(path=isolated_home / "config.yaml", spaces=[
        config_mod.Space("AcmeDev"), config_mod.Space("Docs", paths=[str(acme)])]))
    entry = {"sessionId": "vs-1", "status": "idle", "name": "vscode-1", "kind": "interactive", "cwd": str(repo)}
    app = AgentlensApp(provider=lambda: [entry], root=projects, interval=60)
    app.client_factory = lambda options: FakeClaude(options, {})

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause(0.2)

            async def dialog():
                await pilot.press("n")
                await pilot.pause(0.2)
                assert isinstance(app.screen, NewSessionScreen)
                return app.screen.query_one("#new-folder"), lambda: str(app.screen.query_one("#new-space").render())

            box, space = await dialog()  # all sessions: the one you're on's folder, and the space that puts it in
            assert box.value == str(repo) and space() == "Opens in the repo space"
            box.value = str(acme)
            await pilot.pause(0.1)
            assert space() == "Opens in the Docs space"  # Docs takes sessions in that folder
            await pilot.press("escape")

            await pilot.press("right_square_bracket")  # AcmeDev: nothing in it
            await pilot.pause(0.1)
            assert str(app.query_one("#empty").render()) == \
                "No sessions in AcmeDev. n starts one here; [ and ] switch spaces."
            await pilot.press("right_square_bracket")  # Docs: also empty
            await pilot.pause(0.1)
            assert str(app.query_one("#empty").render()).startswith("No sessions in Docs.")
            box, space = await dialog()
            assert box.value == str(acme) and space() == "Opens in the Docs space"  # the space's own folder
            await pilot.press("escape")

            await pilot.press("left_square_bracket")
            box, space = await dialog()
            assert box.value == str(Path.home())  # not repo's: you were last on that pane, in another space
            assert space() == "Opens in the AcmeDev space"
            box.value = str(repo)
            await pilot.pause(0.1)
            assert space() == "Opens in the AcmeDev space"  # in a space you made, whatever the folder
            await pilot.press(*"hi", "enter")
            await settle(lambda: app.driven and app._visible_panes())
            driven = next(iter(app.driven.values()))
            assert driven.cwd == str(repo) and app.space_of[driven.key] == "AcmeDev"
            assert [p.session_id for p in app._visible_panes()] == [driven.key]

    asyncio.run(go())


def test_new_session_in_a_closed_space_works_where_its_sessions_did(projects, tmp_path, isolated_home):
    """With none of a space's sessions open, the dialog doesn't offer where you last started one
    (another space's folder): a space named after a repository works in it, whatever sessions you
    moved into it from elsewhere; one you made, with no folder of its own, where its sessions last did."""
    import os
    import re

    from conftest import common, human, write_jsonl

    from agentlens import config as config_mod
    from agentlens.tui_driven import NewSessionScreen

    repo, server, lens, acme = (tmp_path / name for name in ("repo", "server", "lens", "AcmeDev"))
    for folder in (repo, server, lens, acme / ".git", acme / "tools"):
        folder.mkdir(parents=True)

    def worked(session_id: str, folder, hours_ago: float) -> None:
        project = projects / re.sub(r"[^A-Za-z0-9]", "-", str(folder))  # as Claude Code names it
        path = write_jsonl(project / f"{session_id}.jsonl", common([human(0)], session_id, cwd=str(folder)))
        os.utime(path, (time.time() - hours_ago * 3600,) * 2)

    worked("vd-1", acme / "tools", 5)  # in AcmeDev's own repository, longest ago
    worked("moved-1", lens, 2)
    worked("moved-2", server, 1)
    worked("mix-1", lens, 3)
    worked("mix-2", server, 4)
    config_mod.save(config_mod.Config(path=isolated_home / "config.yaml", last_folder=str(repo), spaces=[
        config_mod.Space("AcmeDev", sessions=["moved-1", "moved-2"]), config_mod.Space("Mixed", sessions=["mix-1", "mix-2"])]))
    app = AgentlensApp(provider=lambda: [], root=projects, interval=60)
    app.client_factory = lambda options: FakeClaude(options, {})

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause(0.2)

            async def dialog(space: str) -> str:
                await pilot.press("right_square_bracket")
                await pilot.pause(0.1)
                assert app.current_space == space
                await pilot.press("n")
                await pilot.pause(0.2)
                assert isinstance(app.screen, NewSessionScreen)
                assert str(app.screen.query_one("#new-space").render()) == f"Opens in the {space} space"
                folder = app.screen.query_one("#new-folder").value
                await pilot.press("escape")
                return folder

            assert await dialog("AcmeDev") == str(acme)  # its repository, not the folders moved in
            assert await dialog("Mixed") == str(lens)  # where its sessions last worked

    asyncio.run(go())


# Ending, detaching, bringing back ------------------------------------------------------------

import subprocess as _subprocess  # noqa: E402

from agentlens import tui as tui_mod  # noqa: E402
from agentlens.tui_driven import ChoiceScreen, DetachScreen  # noqa: E402


def app_with_session(projects, tmp_path, scripts=None, info=None):
    folder = tmp_path / "repo"
    folder.mkdir(exist_ok=True)
    fakes = []

    def factory(options):
        fakes.append(FakeClaude(options, scripts or {}))
        fakes[-1].info = info
        return fakes[-1]

    app = AgentlensApp(provider=lambda: [], root=projects, interval=60)
    app.client_factory = factory
    return app, folder, fakes


async def started(app, pilot, folder, prompt=""):
    driven = app.open_driven(str(folder), prompt=prompt)
    for _ in range(100):
        await pilot.pause(0.03)
        if driven.session_id and driven.state != "starting":
            break
    await pilot.pause(0.1)
    return driven


def test_end_idle_and_busy_sessions(projects, tmp_path):
    app, folder, _ = app_with_session(projects, tmp_path, {"slow": [("ask", "Bash", {"command": "make"})]})

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            idle = await started(app, pilot, folder)
            await pilot.press("w")  # close, like closing a chat in VS Code; it asks first
            await pilot.pause(0.1)
            assert isinstance(app.screen, ChoiceScreen) and "History" in app.screen.body
            await pilot.press("escape")  # a stray w changes nothing
            await pilot.pause(0.1)
            assert idle.key in app.driven and not isinstance(app.screen, ChoiceScreen)
            await pilot.press("w", "w")
            await pilot.pause(0.2)
            assert idle.key not in app.driven and not app.query(tui_mod.DrivenPane)
            busy = await started(app, pilot, folder, prompt="slow")
            assert busy.state == PERMISSION
            await pilot.press("e")
            await pilot.pause(0.1)
            assert isinstance(app.screen, ChoiceScreen)  # mid-turn: asks first
            await pilot.press("w")
            await pilot.pause(0.3)
            assert busy.key not in app.driven and busy.state == "ended"

    asyncio.run(go())


def test_closing_a_session_leaves_you_on_the_wall_not_in_another_box(projects, tmp_path):
    app, folder, _ = app_with_session(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            first, middle, last = [await started(app, pilot, folder) for _ in range(3)]
            # On the pane that takes its place, or the one before it if it was the last.
            for closing, lands_on in [(middle, last), (last, first)]:
                app._focus_key(closing.key)
                await pilot.pause(0.1)
                await pilot.press("w", "w")
                await pilot.pause(0.2)
                assert closing.key not in app.driven
                assert isinstance(app.focused, DrivenPane) and app.focused.session_id == lands_on.key

    asyncio.run(go())


def pane_text(pane):
    return pane.query_one(".feed-body").text


def test_changes_show_at_once_then_in_batches_while_a_reply_streams(projects, tmp_path):
    app, folder, _ = app_with_session(projects, tmp_path)
    flushes = []

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            driven = await started(app, pilot, folder)
            await pilot.pause(0.3)  # nothing shown for a while
            flush = app._flush_driven
            app._flush_driven = lambda: (flushes.append(time.monotonic()), flush())
            began = time.monotonic()
            for i in range(30):  # a reply streaming in, word by word
                driven.handle(partial(f"word{i} "))
                await asyncio.sleep(0.01)
            await pilot.pause(0.3)
            assert flushes[0] - began < 0.05  # the first word without waiting for a batch
            assert len(flushes) <= 5  # the rest a few times a second, not on every word
            assert "word29" in pane_text(app.query_one(DrivenPane))

    asyncio.run(go())


def test_with_many_replies_streaming_the_pane_you_are_on_repaints_most(projects, tmp_path, monkeypatch):
    app, folder, _ = app_with_session(projects, tmp_path, {"slow": [("ask", "Bash", {"command": "make"})]})
    painted = {}
    show = DrivenPane.show

    def counting_show(self, data, now):
        painted[self.session_id] = painted.get(self.session_id, 0) + 1
        show(self, data, now)

    monkeypatch.setattr(DrivenPane, "show", counting_show)

    async def stream(sessions, words):
        for i in range(words):
            for d in sessions:
                d.handle(partial(f"w{i} "))
            await asyncio.sleep(0.01)

    async def go():
        async with app.run_test(size=(200, 50)) as pilot:
            sessions = [await started(app, pilot, folder) for _ in range(6)]
            mine, *others = sessions
            app._focus_key(mine.key)
            await pilot.pause(0.3)
            painted.clear()
            await stream(sessions, 60)
            await pilot.pause(0.3)
            assert painted[mine.key] > max(painted.get(d.key, 0) for d in others)
            # A permission prompt in a pane you're not on shows with the next repaint, not a later one.
            asking = others[2]
            app.run_worker(asking.send("slow"))
            await stream(sessions, 15)
            assert asking.state == PERMISSION
            assert app.query_one(f"#s-{asking.key}").has_class("st-question")

    asyncio.run(go())


def test_closing_takes_the_pane_off_the_wall_while_claude_code_shuts_down(projects, tmp_path):
    folder = tmp_path / "repo"
    folder.mkdir()
    listed = []  # what `claude agents` reports
    fakes = []

    class SlowToExit(FakeClaude):
        async def disconnect(self):
            await self.exited.wait()

    def factory(options):
        fakes.append(SlowToExit(options, {}))
        fakes[-1].exited = asyncio.Event()
        return fakes[-1]

    app = AgentlensApp(provider=lambda: list(listed), root=projects, interval=60)
    app.client_factory = factory

    async def refresh(pilot):
        app.action_refresh()
        for worker in [w for w in app.workers if w.group == "live"]:  # not the close, which is still waiting
            await worker.wait()
        await pilot.pause(0.1)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            driven = await started(app, pilot, folder)
            # Claude Code lists the session it runs for agentlens, as long as it's running.
            listed.append({"sessionId": driven.session_id, "status": "idle", "name": "x", "kind": "interactive",
                           "cwd": str(folder)})
            await pilot.press("w", "w")
            await pilot.pause(0.1)
            assert not app.query(tui_mod.SessionPane) and driven.state != "ended"  # still shutting down
            await refresh(pilot)
            assert not app.query(tui_mod.SessionPane)  # still listed, but it's going: not back as a VS Code pane
            fakes[0].exited.set()
            await pilot.pause(0.1)
            assert driven.state == "ended"
            listed.clear()
            await refresh(pilot)
            assert not app.query(tui_mod.SessionPane) and not app._closing_sessions

    asyncio.run(go())


def test_quitting_closes_sessions_together(projects, tmp_path, monkeypatch):
    app, folder, fakes = app_with_session(projects, tmp_path)
    closing, most, titles = [0], [0], []
    monkeypatch.setattr(app, "notify", lambda message, **kw: titles.append(kw.get("title", "")))

    async def disconnect():
        closing[0] += 1
        most[0] = max(most[0], closing[0])
        await asyncio.sleep(0.2)  # Claude Code exiting
        closing[0] -= 1

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await started(app, pilot, folder)
            await started(app, pilot, folder)
            for fake in fakes:
                fake.disconnect = disconnect
            await pilot.press("q")
            await pilot.pause(0.5)
        assert most[0] == 2 and "Closing sessions" in titles

    asyncio.run(go())


def test_j_takes_you_to_a_permission_prompt_ready_to_answer(projects, tmp_path):
    app, folder, fakes = app_with_session(projects, tmp_path, {"slow": [("ask", "Bash", {"command": "make"})]})

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            asking = await started(app, pilot, folder, prompt="slow")
            here = await started(app, pilot, folder)  # you're in another session now
            assert asking.state == PERMISSION and app._focused_session().session_id == here.key
            await pilot.press("j")
            await pilot.pause(0.3)
            assert isinstance(app.screen, tui_mod.ZoomScreen) and app.screen.session_id == asking.key
            await pilot.press("1")  # its box has focus, and an empty box answers with digits
            await settle(lambda: fakes[0].decisions)
            assert isinstance(fakes[0].decisions[0], PermissionResultAllow)

    asyncio.run(go())


def test_detach_release_handoff_and_failure(projects, tmp_path, monkeypatch):
    app, folder, _ = app_with_session(projects, tmp_path)
    calls = []
    outcome = {"ok": True}

    def fake_resume(claude, session_id, cwd, mode, instruction, **kw):
        calls.append((session_id, cwd, mode, instruction))
        return (True, "backgrounded · 1715db93") if outcome["ok"] else (False, "Workspace not trusted.")

    monkeypatch.setattr(tui_mod, "background_resume", fake_resume)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            # Idle with no instruction: just released, nothing handed over.
            driven = await started(app, pilot, folder)
            await pilot.press("d")
            await pilot.pause(0.1)
            assert isinstance(app.screen, DetachScreen)
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert not app.driven and calls == []
            # With an instruction: handed to the background service.
            driven = await started(app, pilot, folder)
            await pilot.press("d")
            await pilot.pause(0.1)
            await pilot.press(*"run the tests", "enter")
            await pilot.pause(0.4)
            assert calls == [("sess-42", str(folder), "default", "run the tests")] and not app.driven
            # If Claude Code refuses, the session comes back here rather than being lost.
            outcome["ok"] = False
            driven = await started(app, pilot, folder)
            await pilot.press("d")
            await pilot.pause(0.1)
            await pilot.press(*"keep going", "enter")
            await pilot.pause(0.5)
            [reopened] = app.driven.values()
            assert reopened.resume == "sess-42" and reopened is not driven

    asyncio.run(go())


def test_dialog_text_wraps_inside_the_box(projects, tmp_path):
    folder = tmp_path / "repo"

    def dialogs():
        return [
            DetachScreen("A session with a title long enough to need more than one line of this dialog",
                         working=True, in_git_repo=True, cwd=str(folder), trust=False),
            DetachScreen("Home", working=False, in_git_repo=False, home=True),
            ChoiceScreen("Close this session?", "Claude is in the middle of a turn; closing stops it. " * 3,
                         {"w": "close it"}),
        ]

    async def go(size):
        app, _, _ = app_with_session(projects, tmp_path)
        async with app.run_test(size=size) as pilot:
            for dialog in dialogs():
                app.push_screen(dialog)
                await pilot.pause(0.1)
                outer = dialog.query_one("#new-dialog")
                assert outer.region.right <= size[0]  # a narrow terminal shrinks the box, not cuts it off
                texts = list(dialog.query("#new-dialog > Static"))
                assert texts and all(t.region.right <= outer.content_region.right for t in texts)
                assert any(t.region.height > 1 for t in texts)  # wrapped, not cut off at the edge
                dialog.dismiss(None)
                await pilot.pause(0.1)

    asyncio.run(go((160, 44)))
    asyncio.run(go((60, 44)))


def test_quit_asks_when_sessions_are_working(projects, tmp_path):
    app, folder, _ = app_with_session(projects, tmp_path, {"slow": [("ask", "Bash", {"command": "make"})]})

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await started(app, pilot, folder, prompt="slow")
            await pilot.press("escape", "q")
            await pilot.pause(0.2)
            assert isinstance(app.screen, ChoiceScreen) and "still working" in app.screen.title_text

    asyncio.run(go())


def test_background_resume_judges_success_by_the_session_appearing(monkeypatch):
    runs = []

    class Done:
        returncode, stdout, stderr = 0, "backgrounded · 1715db93\n  claude attach 1715db93    open", ""

    monkeypatch.setattr(_subprocess, "run", lambda cmd, **kw: runs.append(cmd) or Done())
    listings = iter([[], [{"sessionId": "sess-1", "kind": "background"}]])
    ok, msg = tui_mod.background_resume("claude", "sess-1", "D:/w", "acceptEdits", "go on",
                                        list_sessions=lambda: next(listings), wait_seconds=1)
    assert ok and msg == "backgrounded · 1715db93"
    assert runs[0] == ["claude", "--resume", "sess-1", "--bg", "--permission-mode", "acceptEdits", "--", "go on"]
    # Exit code 0 but nothing appeared (e.g. "Workspace not trusted"): a failure.
    Done.stdout = "Workspace not trusted. Run `claude` in D:/w once."
    ok, msg = tui_mod.background_resume("claude", "sess-1", "D:/w", "default", "go on",
                                        list_sessions=lambda: [], wait_seconds=0.1)
    assert not ok and "not trusted" in msg


def test_bringing_a_background_session_here_says_what_is_happening(projects, tmp_path, monkeypatch):
    folder = tmp_path / "repo"
    folder.mkdir()
    listed = [{"sessionId": "sess-42", "id": "sess4200", "status": "idle", "name": "bg-1", "kind": "background",
               "cwd": str(folder)}]
    app = AgentlensApp(provider=lambda: list(listed), root=projects, interval=60)
    app.client_factory = lambda options: FakeClaude(options, {})
    stopped, notes = [], []

    def fake_stop(claude, session_id, name="", job_id=""):
        stopped.append(job_id)
        listed.clear()
        return True, ""

    monkeypatch.setattr(tui_mod, "stop_background", fake_stop)
    monkeypatch.setattr(app, "notify", lambda message, **kw: notes.append(kw.get("title", "")))

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause(0.2)
            app._focus_key("sess-42")
            await pilot.pause(0.1)
            # The footer offers only what applies to a background session.
            assert app.check_action("bring_here", ()) and app.check_action("attach", ())
            assert not app.check_action("compose", ()) and not app.check_action("interrupt", ())
            await pilot.press("question_mark")
            await pilot.pause(0.1)
            assert isinstance(app.screen, tui_mod.KeysScreen)
            await pilot.press("escape", "o")
            await pilot.pause(0.1)
            await app.workers.wait_for_complete()  # stopping it, then starting it here
            await pilot.pause(0.2)
            assert stopped == ["sess4200"]  # `claude stop` gets the short id
            assert notes == ["Bringing bg-1 here", "bg-1 is here now"]
            [driven] = app.driven.values()
            assert driven.resume == "sess-42" and app._focused_session().session_id == driven.key

    asyncio.run(go())


def test_bringing_a_session_here_from_its_zoom_stays_in_it(projects, tmp_path, monkeypatch):
    # Its pane is replaced by the one agentlens runs. Focus went to the session beside it, as
    # when one is closed, so the zoom opened again on that one and its alert was taken as seen.
    folder = tmp_path / "repo"
    folder.mkdir()
    listed = [{"sessionId": "sess-42", "id": "sess4200", "status": "idle", "name": "bg-1", "kind": "background",
               "cwd": str(folder), "startedAt": 1_000},
              {"sessionId": "beside", "status": "idle", "name": "beside-1", "kind": "interactive",
               "cwd": str(folder), "startedAt": 2_000}]
    app = AgentlensApp(provider=lambda: list(listed), root=projects, interval=60)
    app.client_factory = lambda options: FakeClaude(options, {})

    def fake_stop(claude, session_id, name="", job_id=""):
        listed[:] = [e for e in listed if e["sessionId"] != session_id]
        return True, ""

    monkeypatch.setattr(tui_mod, "stop_background", fake_stop)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause(0.2)
            app._alerts["beside"] = 1.0  # it needs you, and you haven't been there yet
            seen = []
            visited = app.visited
            app.visited = lambda sid: (seen.append(sid), visited(sid))
            app._focus_key("sess-42")
            await pilot.press("enter")
            await pilot.pause(0.1)
            assert isinstance(app.screen, tui_mod.ZoomScreen) and app.screen.session_id == "sess-42"
            await pilot.press("o")
            await settle(lambda: app.driven and isinstance(app.screen, tui_mod.ZoomScreen))
            await pilot.pause(0.2)
            [driven] = app.driven.values()
            assert app.screen.session_id == driven.key  # the session you brought here, not the one beside it
            assert "beside" in app._alerts, seen
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert app._focused_session(strict=True).session_id == driven.key and "beside" in app._alerts

    asyncio.run(go())


def test_stop_background_tries_the_job_id_first_then_falls_back(monkeypatch):
    tried = []
    accepted = {"e04cd49e"}

    def run(cmd, **kw):
        tried.append(cmd[-1])
        return type("R", (), {"returncode": 0 if cmd[-1] in accepted else 1, "stdout": "", "stderr": "no"})()

    monkeypatch.setattr(_subprocess, "run", run)
    ok, _ = tui_mod.stop_background("claude", "e04cd49e-full-id", "skyline-84", job_id="e04cd49e")
    assert ok and tried == ["e04cd49e"]
    tried.clear()
    accepted = {"skyline-84"}
    ok, _ = tui_mod.stop_background("claude", "e04cd49e-full-id", "skyline-84")
    assert ok and tried == ["e04cd49e-full-id", "skyline-84"]


def test_messages_sent_while_working_are_marked_and_state_follows_new_turns():
    async def go():
        d, fakes = make({"long task": [("ask", "Bash", {"command": "make"})], "also this": []})
        await d.start("long task")
        await settle(lambda: d.state == PERMISSION)
        d.allow()
        await settle(lambda: d.state == "working")
        await d.send("also this")
        assert d.events[-1].tag == "queued" and d.state == "working"
        assert fakes[0].sent == ["long task", "also this"]  # delivered to Claude Code, which feeds it in mid-turn
        d.handle(done())
        assert d.state == WAITING and not any(e.tag for e in d.events)
        d.handle(text("Picking up your second message."))  # Claude Code starts a turn for the queued one
        assert d.state == "working"
    asyncio.run(go())


def test_large_messages_from_claude_code_are_read():
    from agentlens.driver import MAX_MESSAGE_BYTES

    d, _ = make({})
    assert d.options().max_buffer_size == MAX_MESSAGE_BYTES > 1024 * 1024  # an image result can pass 1 MiB


def test_when_claude_code_stops_the_session_pauses_and_your_next_message_continues_it():
    from agentlens.driver import MAX_MESSAGE_BYTES, PAUSED
    from agentlens.feed import Palette
    from agentlens.tui_driven import placeholder, request_card

    oversized = Exception(f"JSON message exceeded maximum buffer size of {MAX_MESSAGE_BYTES} bytes")

    async def go():
        d, fakes = make({"look at shot.png": [("ask", "Read", {"file_path": "shot.png"}), oversized],
                         "carry on": [text("Picking up."), done()]})
        await d.start("look at shot.png")
        await settle(lambda: d.state == PERMISSION)
        d.allow()
        await settle(lambda: d.state == PAUSED)
        assert fakes[0].closed  # shut down, so only one client writes to the conversation
        assert d.pending is None and d.resume == "sess-42" and "more than agentlens reads" in d.error
        assert d.events[-1].kind == "error"
        card = request_card(d, Palette()).plain
        assert "type to continue it here" in card and "Session closed" not in placeholder(d)

        await d.send("carry on")
        await settle(lambda: d.state == WAITING and d.events[-1].text == "Picking up.")
        assert len(fakes) == 2 and fakes[1].options.resume == "sess-42" and fakes[1].sent == ["carry on"]
        assert d.error is None and request_card(d, Palette()) is None
    asyncio.run(go())


def test_claude_code_exiting_or_failing_to_start_again_leaves_the_session_continuable():
    from agentlens.driver import PAUSED

    async def go():
        d, fakes = make({"bye": [None]})
        await d.start("bye")
        await settle(lambda: d.state == PAUSED)
        assert d.error == "Claude Code exited."

        class WontStart(FakeClaude):
            async def connect(self):
                raise RuntimeError("no network")

        d.client_factory = lambda o: WontStart(o, {})
        await d.send("hello?")
        assert d.state == PAUSED and "no network" in d.error  # still there to try again
        d.client_factory = lambda o: fakes.append(FakeClaude(o, {"again": [done()]})) or fakes[-1]
        await d.send("again")
        await settle(lambda: d.state == WAITING)
        assert fakes[-1].sent == ["again"]
    asyncio.run(go())


def test_a_session_that_stops_before_its_conversation_starts_fails():
    from agentlens.driver import FAILED

    class Crashes(FakeClaude):
        async def connect(self):
            await self.queue.put(RuntimeError("boom"))  # before Claude Code gave it a session id

    async def go():
        d = DrivenSession("D:/work/demo", client_factory=lambda o: Crashes(o, {}), cli_path="claude")
        await d.start()
        await settle(lambda: d.state == FAILED)
        assert "boom" in d.error
    asyncio.run(go())


def test_the_old_claude_code_ending_late_does_not_stop_the_new_one():
    async def go():
        d, fakes = make({})
        await d.start()
        await settle(lambda: d.session_id == "sess-42")
        old = fakes[0]
        await d._stopped(old, "Claude Code exited.")
        await d.send("hi")
        await settle(lambda: len(fakes) == 2 and d.state == "working")
        await old.queue.put(None)  # the first reader only now sees its stream end
        await asyncio.sleep(0.05)
        assert d.state == "working" and not fakes[1].closed
    asyncio.run(go())


def test_claude_codes_stderr_stays_off_the_screen_and_explains_a_failure():
    """Left on agentlens's terminal, Claude Code's stderr lands on the screen, and the
    "show the cursor" it sends on exit leaves a stray cursor there."""
    from agentlens.driver import FAILED

    class Crashing(FakeClaude):
        async def receive_messages(self):
            self.options.stderr("\x1b[?25h")
            self.options.stderr("Error: \x1b[31mNo conversation found\x1b[0m")
            raise Exception("Command failed with exit code 1")
            yield

    async def go():
        d = DrivenSession("D:/work/demo", client_factory=lambda o: Crashing(o, {}), cli_path="claude")
        assert d.options().stderr is not None  # the SDK pipes stderr only when given a callback
        await d.start()
        await settle(lambda: d.state == FAILED)
        assert d.events[-1].detail.endswith("Claude Code's last output:\nError: No conversation found")
    asyncio.run(go())


def test_a_paused_session_shows_your_message_before_claude_code_has_started():
    async def go():
        ready = asyncio.Event()

        class SlowStart(FakeClaude):
            async def connect(self):
                await ready.wait()  # Claude Code takes a few seconds to start
                await super().connect()

        fakes = []

        def factory(options):
            fakes.append(SlowStart(options, {}))
            return fakes[-1]

        d = DrivenSession("D:/work/demo", client_factory=factory, cli_path="claude", resume="sess-42")
        d.park([])
        first = asyncio.ensure_future(d.send("carry on"))
        await asyncio.sleep(0.05)
        assert [e.text for e in d.events] == ["carry on"]  # shown at once
        assert d.state == "working" and d.detail == "starting Claude Code…"
        began = d.turn_started  # the line above the box counts from your message
        second = asyncio.ensure_future(d.send("and then this"))  # typed while it starts: not lost
        await asyncio.sleep(0.05)
        ready.set()
        await asyncio.gather(first, second)
        assert fakes[0].sent == ["carry on", "and then this"] and d.state == "working"
        assert began is not None and d.turn_started == began  # not from when Claude Code was ready
    asyncio.run(go())


# Remembered across launches; actions while typing -------------------------------------------

from conftest import common as _common, human as _human, text as _text, write_jsonl as _write  # noqa: E402

from agentlens import config as _config  # noqa: E402
from agentlens.driver import PAUSED  # noqa: E402


def test_sessions_come_back_paused_with_their_conversation(projects, tmp_path, isolated_home):
    folder = tmp_path / "repo"
    folder.mkdir()
    _write(projects / "repo" / "sess-42.jsonl",
           _common([_human(0, "add a parser"), _text(1, "Parser added.")], "sess-42", cwd=str(folder)))
    cfg = _config.Config(path=isolated_home / "config.yaml")
    cfg.remember_driven("sess-42", str(folder), "Parser work", "acceptEdits")
    _config.save(cfg)
    app, _, fakes = app_with_session(projects, tmp_path, {"continue": [text("Continuing."), done()]})

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause(0.3)
            [driven] = app.driven.values()
            assert driven.state == PAUSED and driven.resume == "sess-42" and driven.title == "Parser work"
            assert [e.text for e in driven.events] == ["add a parser", "Parser added."]
            assert fakes == []  # not connected until you type
            assert "need you" in app.query_one("#summary").content.plain and app.query_one("#summary").content.plain.startswith("0")
            app._focus_key(driven.key)
            await pilot.press("i", *"continue", "enter")
            for _ in range(100):
                await pilot.pause(0.03)
                if driven.events[-1].text == "Continuing.":
                    break
            assert fakes[0].options.resume == "sess-42" and fakes[0].options.permission_mode == "acceptEdits"
            assert fakes[0].sent == ["continue"]
            # Closing takes it off the list for next time.
            await pilot.press("escape", "delete", "w")
            await pilot.pause(0.3)
            assert _config.load(isolated_home / "config.yaml").driven_sessions == []

    asyncio.run(go())


def test_zoom_on_a_session_agentlens_runs_lists_its_subagents(projects, tmp_path, isolated_home):
    from conftest import tool_result, tool_use

    folder = tmp_path / "repo"
    folder.mkdir()
    _write(projects / "repo" / "sess-42.jsonl", _common([_human(0, "map the code"), _text(1, "Asked a subagent.")],
                                                        "sess-42", cwd=str(folder)))
    subs = projects / "repo" / "sess-42" / "subagents"
    _write(subs / "agent-a.jsonl", _common([tool_use(0.5, "SubagentHandback", "h", message="**Mapped.**"),
                                            tool_result(0.6, "h")], "sess-42"))
    (subs / "agent-a.meta.json").write_text('{"description": "Map the code"}', encoding="utf-8")
    cfg = _config.Config(path=isolated_home / "config.yaml")
    cfg.remember_driven("sess-42", str(folder), "Mapping", "default")
    _config.save(cfg)
    app, _, _ = app_with_session(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause(0.3)
            app.action_refresh()  # the first refresh restored the session; this one reads its subagents
            await app.workers.wait_for_complete()
            await pilot.pause(0.3)
            [driven] = app.driven.values()
            app._focus_key(driven.key)
            await pilot.press("enter")
            await pilot.pause(0.3)
            app._driven_changed(driven)  # a message from Claude must not wipe the list
            app._flush_driven()
            out = app.screen.query_one("#zoom-subs-body").text
            assert "✓ Map the code" in out and "1 finished" in out
            assert "Mapped." not in out  # the map shows which subagents ran, not what they said

    asyncio.run(go())


def test_sessions_come_back_under_claude_codes_title(projects, tmp_path, isolated_home):
    folder = tmp_path / "repo"
    folder.mkdir()
    tree = str(folder / ".claude" / "worktrees" / "parser")
    _write(projects / "repo" / "sess-42.jsonl", _common([
        _human(0, "can we add a parser?"), {"type": "ai-title", "aiTitle": "Repo parser support"},
        {**_text(1, "Added."), "cwd": tree}], "sess-42", cwd=str(folder)))
    cfg = _config.Config(path=isolated_home / "config.yaml")
    cfg.remember_driven("sess-42", str(folder), "can we add a parser?", "default")  # saved by an older agentlens
    _config.save(cfg)
    app, _, _ = app_with_session(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause(0.3)
            [driven] = app.driven.values()
            assert driven.title == "Repo parser support" and driven.worktree == "parser"
            pane = app.query_one(f"#s-{driven.key}", tui_mod.SessionPane)
            assert pane.border_title == "parser · repo · here"
            assert pane.query_one(".headline").content.plain.startswith("Parser support\n")

    asyncio.run(go())


def test_a_session_run_here_takes_its_title_once_claude_code_gives_it_one(projects, tmp_path, isolated_home):
    app, folder, _ = app_with_session(projects, tmp_path)
    listed = []
    app.provider = lambda: list(listed)  # Claude Code lists the sessions agentlens runs as open ones too

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await app.workers.wait_for_complete()
            driven = await started(app, pilot, folder, prompt="could you look at why the parser is slow")
            assert driven.title == "could you look at why the parser is slow"  # until Claude Code names it
            tree = str(folder / ".claude" / "worktrees" / "parser-speed")
            _write(projects / "repo" / "sess-42.jsonl", _common([
                _human(0, "could you look at why the parser is slow"),
                {"type": "ai-title", "aiTitle": "Parser performance"}, {**_text(1, "Looking."), "cwd": tree}],
                "sess-42", cwd=str(folder)))
            listed.append({"sessionId": "sess-42", "status": "busy", "name": "repo-4f", "kind": "interactive",
                           "cwd": tree})
            app.action_refresh()
            await app.workers.wait_for_complete()
            await pilot.pause(0.2)
            assert driven.title == "Parser performance" and driven.worktree == "parser-speed"
            assert [p.session_id for p in app.query(tui_mod.SessionPane)] == [driven.key]  # still the one pane
            saved = _config.load(isolated_home / "config.yaml").driven_sessions
            assert [d["title"] for d in saved] == ["Parser performance"]  # what it comes back as next time

    asyncio.run(go())


def test_the_title_claude_code_gives_replaces_the_first_message_it_was_saved_under(projects, tmp_path,
                                                                                     isolated_home, monkeypatch):
    app, folder, fakes = app_with_session(projects, tmp_path)
    prompt = "it's actually weird that the session titles are so bloated despite me fixing them yesterday"
    monkeypatch.setattr(FakeClaude, "titles", {prompt: "Session titles bloated"})

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            driven = await started(app, pilot, folder, prompt=prompt)
            await until(pilot, lambda: driven.titled)
            await pilot.pause(0.2)
            saved = _config.load(isolated_home / "config.yaml").driven_sessions
            assert [d["title"] for d in saved] == ["Session titles bloated"]
            pane = app.query_one(DrivenPane)
            assert pane.query_one(".headline").content.plain.startswith("Session titles bloated\n")

    asyncio.run(go())


def test_retitling_a_remembered_session_keeps_its_place():
    cfg = _config.Config()
    for session_id in ("a", "b"):
        cfg.remember_driven(session_id, "D:/w", "first prompt", "default")
    cfg.remember_driven("a", "D:/w", "Claude Code's title", "default")
    assert [(d["session_id"], d["title"]) for d in cfg.driven_sessions] == [("a", "Claude Code's title"),
                                                                            ("b", "first prompt")]


def test_remembered_session_open_elsewhere_is_not_restored(projects, tmp_path, isolated_home):
    folder = tmp_path / "repo"
    folder.mkdir()
    cfg = _config.Config(path=isolated_home / "config.yaml")
    cfg.remember_driven("sess-42", str(folder), "Parser work", "default")
    _config.save(cfg)
    entry = {"sessionId": "sess-42", "status": "idle", "name": "vscode-1", "kind": "interactive", "cwd": str(folder)}
    app = AgentlensApp(provider=lambda: [entry], root=projects, interval=60)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause(0.3)
            assert not app.driven  # it's in VS Code now; two clients must not share it
            assert _config.load(isolated_home / "config.yaml").driven_sessions == []

    asyncio.run(go())


def test_new_sessions_are_remembered_and_quitting_keeps_them(projects, tmp_path, isolated_home):
    app, folder, _ = app_with_session(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await started(app, pilot, folder)
            await pilot.pause(0.3)
            saved = _config.load(isolated_home / "config.yaml").driven_sessions
            assert [d["session_id"] for d in saved] == ["sess-42"]
            await pilot.press("q")
            await pilot.pause(0.3)
        assert [d["session_id"] for d in _config.load(isolated_home / "config.yaml").driven_sessions] == ["sess-42"]

    asyncio.run(go())


def test_session_actions_work_while_typing_in_its_box(projects, tmp_path):
    app, folder, _ = app_with_session(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            driven = await started(app, pilot, folder)
            app._focus_key(driven.key)
            await pilot.press("i")
            assert isinstance(app.focused, tui_mod.Input)
            assert app._focused_driven(strict=True) is driven  # typing in the box still counts as "in" it
            assert app.check_action("detach", ()) is False  # 'd' would type a letter, so it's hidden
            await pilot.press("d")
            assert app.focused.value == "d" and not isinstance(app.screen, DetachScreen)
            app.focused.value = ""
            await pilot.press("escape")
            assert app.check_action("detach", ()) is True
            await pilot.press("d")
            await pilot.pause(0.1)
            assert isinstance(app.screen, DetachScreen)
            await pilot.press("escape")
            # From the command palette, which takes focus, it acts on the pane you were last in.
            app.set_focus(None)
            app.action_detach()
            await pilot.pause(0.1)
            assert isinstance(app.screen, DetachScreen)

    asyncio.run(go())


# Closing VS Code panes; detach and folder trust ---------------------------------------------

import json as _json  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

from agentlens.driver import folder_trust as _folder_trust  # noqa: E402


def test_closing_a_vscode_pane_hides_it_until_shown_again(projects, isolated_home):
    entry = {"sessionId": "vs-1", "status": "idle", "name": "skyline-84", "kind": "interactive",
             "cwd": r"D:\work\skyline"}
    app = AgentlensApp(provider=lambda: [entry], root=projects, interval=60)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause(0.2)
            assert [p.session_id for p in app.query(tui_mod.SessionPane)] == ["vs-1"]
            await pilot.press("w", "w")
            await pilot.pause(0.2)
            assert not app.query(tui_mod.SessionPane)
            assert _config.load(isolated_home / "config.yaml").hidden_sessions == ["vs-1"]
            app.action_show_hidden()
            await pilot.pause(0.2)
            assert [p.session_id for p in app.query(tui_mod.SessionPane)] == ["vs-1"]

    asyncio.run(go())


def test_detach_from_home_folder_offers_release_only(projects, tmp_path, monkeypatch):
    app, _, _ = app_with_session(projects, tmp_path)
    calls = []
    monkeypatch.setattr(tui_mod, "background_resume", lambda *a, **k: calls.append(a) or (True, "ok"))
    monkeypatch.setattr(tui_mod, "is_home", lambda cwd: True)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await started(app, pilot, tmp_path / "repo")
            await pilot.press("d")
            await pilot.pause(0.1)
            assert isinstance(app.screen, DetachScreen) and app.screen.home
            assert not app.screen.query("#detach-instruction")  # nothing to send to the background
            await pilot.press("r")
            await pilot.pause(0.3)
            assert not app.driven and calls == []  # released, not handed over

    asyncio.run(go())


def test_folder_trust_reads_claude_code_config(tmp_path):
    cfg = tmp_path / ".claude.json"
    cfg.write_text(_json.dumps({"projects": {
        "D:/work/demo": {"hasTrustDialogAccepted": True},
        "d:/recipebox": {"hasTrustDialogAccepted": False},
    }}), encoding="utf-8")
    assert _folder_trust(r"D:\work\demo", cfg) is True
    assert _folder_trust(r"D:\work\demo\src", cfg) is True  # trusting a folder covers what's inside
    assert _folder_trust(r"D:\recipebox", cfg) is False
    assert _folder_trust(r"D:\games\space", cfg) is None  # never asked
    assert _folder_trust(r"D:\x", tmp_path / "missing.json") is None


# Claude Code's slash commands in the box ----------------------------------------------------

from claude_agent_sdk import ConversationResetMessage, RateLimitEvent, RateLimitInfo  # noqa: E402

from agentlens.tui import UsageScreen  # noqa: E402
from agentlens.tui_driven import CommandMenu, Composer, ComposerInput  # noqa: E402


def command_output(t):
    return AssistantMessage([TextBlock(t)], "<synthetic>")  # what Claude Code sends for /model, /context...


async def until(pilot, condition, steps=100):
    for _ in range(steps):
        if condition():
            return
        await pilot.pause(0.03)
    raise AssertionError("condition never became true")


def test_slash_lists_the_sessions_commands_and_their_arguments(projects, tmp_path, server_info):
    scripts = {"/model haiku": [command_output("Set model to Haiku 5.5"), done()]}
    app, folder, fakes = app_with_session(projects, tmp_path, scripts, info=server_info)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            driven = await started(app, pilot, folder)
            app._focus_key(driven.key)
            composer = app.query_one(Composer)
            box = composer.query_one(ComposerInput)
            await pilot.press("i", "/")
            assert composer.query_one(CommandMenu).display
            listed = [s.command.name for s in composer.suggestions]
            assert "model" in listed and "engineering:code-review" in listed and "fast" not in listed
            await pilot.press("m", "o")
            assert [s.command.name for s in composer.suggestions] == ["model"]
            await pilot.press("enter")  # /model needs an argument: filled in, and the models are offered
            await until(pilot, lambda: box.value == "/model " and composer.suggestions)
            assert [s.choice.value for s in composer.suggestions] == ["default", "opus", "haiku"]
            await pilot.press("down", "down", "enter")
            await until(pilot, lambda: driven.events and driven.events[-1].tag == "command")
            assert fakes[0].sent == ["/model haiku"] and box.value == "" and not composer.menu_open
            assert driven.events[-1].text == "Set model to Haiku 5.5"
            assert driven.title == ""  # a command isn't what the session is about
            # Tab fills a command in; Esc closes the list but stays in the box.
            await pilot.press("/", "c", "o", "m", "tab")
            await until(pilot, lambda: box.value == "/compact ")
            assert not composer.menu_open  # free text from here: the hint says what it takes
            assert "summarization" in str(composer.query_one(".composer-hint").content)
            box.value = ""
            await pilot.press("/", "c")
            assert composer.menu_open
            await pilot.press("escape")
            assert not composer.menu_open and app.focused is box
            await pilot.press("escape")
            assert app.focused is not box

    asyncio.run(go())


def test_typing_a_message_leaves_the_line_under_the_box_alone(projects, tmp_path, server_info):
    app, folder, fakes = app_with_session(projects, tmp_path, info=server_info)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            driven = await started(app, pilot, folder)
            app._focus_key(driven.key)
            composer = app.query_one(Composer)
            hint = composer.query_one(".composer-hint")
            shown, real_update = [], hint.update
            hint.update = lambda content, **kw: shown.append(str(content)) or real_update(content, **kw)
            await pilot.press("i", *"fix it")
            assert shown == []  # a new hint lays the whole screen out again: not on every key
            await pilot.press("ctrl+u", "/")
            assert len(shown) == 1 and "choose" in shown[0]  # the command list's keys

    asyncio.run(go())


def test_zoomed_in_what_streams_onto_the_wall_beneath_waits_until_it_shows(projects, tmp_path, monkeypatch):
    from textual._compositor import Compositor

    from agentlens.tui import ZoomScreen

    scripts = {"go": [partial("Underneath "), partial("the "), text("Underneath the zoom, all along."), done()]}
    app, folder, fakes = app_with_session(projects, tmp_path, scripts)
    whole = []
    real_full = Compositor.render_full_update
    monkeypatch.setattr(Compositor, "render_full_update", lambda self, *a, **kw: whole.append(1) or real_full(self, *a, **kw))

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            driven = await started(app, pilot, folder)
            app._focus_key(driven.key)
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert isinstance(app.screen, ZoomScreen)
            whole.clear()
            app.composer_submit(driven.key, "go")
            await until(pilot, lambda: driven.events and driven.events[-1].kind == "claude")
            await pilot.pause(0.3)
            assert whole == []  # the zoom covers the wall: its pane changing doesn't repaint all of the zoom
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert "Underneath" in app.export_screenshot()  # the wall is up to date as it shows again

    asyncio.run(go())


def test_usage_and_exit_are_done_by_agentlens(projects, tmp_path, server_info):
    app, folder, fakes = app_with_session(projects, tmp_path, info=server_info)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            driven = await started(app, pilot, folder)
            app._focus_key(driven.key)
            await pilot.press("i", *"/cost", "enter")
            await until(pilot, lambda: isinstance(app.screen, UsageScreen))
            await pilot.press("escape")
            await pilot.pause(0.1)
            app._focus_key(driven.key)
            await pilot.press("i", *"/exit", "enter")
            await until(pilot, lambda: not app.driven)
            assert fakes[0].sent == []  # neither went to Claude Code

    asyncio.run(go())


def test_clear_starts_a_new_conversation_that_is_the_one_restored(projects, tmp_path, server_info):
    new = "sess-43"
    scripts = {"/clear": [ConversationResetMessage("conv-2", "u1", "sess-42"),
                          SystemMessage("session_title_changed", {"title": "fix the build", "session_id": new}),
                          ResultMessage("success", 1, 0, False, 0, new)]}
    app, folder, fakes = app_with_session(projects, tmp_path, scripts, info=server_info)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            driven = await started(app, pilot, folder)
            assert [d["session_id"] for d in app.config.driven_sessions] == ["sess-42"]
            app._focus_key(driven.key)
            await pilot.press("i", *"/clear", "enter")
            await until(pilot, lambda: [d["session_id"] for d in app.config.driven_sessions] == [new])
            assert driven.session_id == new and driven.title == "fix the build"
            assert any(e.text.startswith("Context cleared") for e in driven.events)

    asyncio.run(go())


def test_a_session_run_here_moves_to_the_space_you_pick_and_stays_there(projects, tmp_path, isolated_home,
                                                                        server_info):
    new = "sess-43"
    scripts = {"/clear": [ConversationResetMessage("conv-2", "u1", "sess-42"),
                          ResultMessage("success", 1, 0, False, 0, new)]}
    app, folder, _ = app_with_session(projects, tmp_path, scripts, info=server_info)

    def picked() -> list[tuple[str, list[str]]]:
        return [(s.name, s.sessions) for s in _config.load(isolated_home / "config.yaml").spaces]

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            driven = await started(app, pilot, folder)
            assert app.space_of[driven.key] == "repo"  # its folder's, until you move it
            app._focus_key(driven.key)
            await pilot.press("g")
            await pilot.pause(0.1)
            await pilot.click("#move-new")
            await pilot.press(*"Parsers", "enter")
            await app.workers.wait_for_complete()
            await pilot.pause(0.2)
            assert app.space_of[driven.key] == "Parsers"
            assert picked() == [("Parsers", ["sess-42"])]  # saved under Claude Code's id, not the pane's
            # /clear starts a new conversation with a new id in the same pane: it stays where you put it.
            app._focus_key(driven.key)
            await pilot.press("i", *"/clear", "enter")
            await until(pilot, lambda: driven.session_id == new)
            app.action_refresh()
            await app.workers.wait_for_complete()
            await pilot.pause(0.2)
            assert app.space_of[driven.key] == "Parsers" and picked() == [("Parsers", [new])]

    asyncio.run(go())


def test_sessions_keep_the_usage_meter_current(projects, tmp_path):
    info = RateLimitInfo("allowed", raw={"unifiedWindows": {
        "five_hour": {"utilization": 0.42, "resetsAt": 1792941000, "status": "allowed"}}})
    scripts = {"hi": [RateLimitEvent(info, "u", "sess-42"), text("Hello."), done()]}
    app, folder, _ = app_with_session(projects, tmp_path, scripts)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await started(app, pilot, folder, prompt="hi")
            await until(pilot, lambda: "five_hour" in app.usage.windows)
            assert app.usage.windows["five_hour"].used == 0.42
            meter = str(app.query_one("#usage-meter").content)
            assert "Session" in meter and "42%" in meter

    asyncio.run(go())


# Pasting images into the box ------------------------------------------------------------------

from agentlens.clipboard import PastedImage as _PastedImage  # noqa: E402


def _image(width=1200, height=800):
    return _PastedImage("image/png", b"\x89PNG fake", width, height)


def test_pasted_images_go_with_the_next_message_ahead_of_its_text():
    async def go():
        d, fakes = make({"what's wrong here?": [text("The button overlaps."), done()]})
        await d.start()
        await settle(lambda: d.state == WAITING)
        d.images = [_image(), _image(640, 480)]
        await d.send("/compact")  # a command doesn't take them
        assert len(d.images) == 2 and fakes[0].blocks == []
        await d.send("what's wrong here?")
        await settle(lambda: d.events[-1].text == "The button overlaps.")
        [content] = fakes[0].blocks
        assert [b["type"] for b in content] == ["image", "image", "text"]
        assert content[0]["source"] == {"type": "base64", "media_type": "image/png",
                                        "data": "iVBORyBmYWtl"}  # the bytes, base64
        assert content[2] == {"type": "text", "text": "what's wrong here?"}
        assert d.images == [] and d.events[-2].text == "[Image #1] [Image #2] what's wrong here?"

        d.images = [_image()]
        await d.send("")  # an image on its own is a message too
        assert [b["type"] for b in fakes[0].blocks[-1]] == ["image"] and d.events[-1].text == "[Image #1]"
    asyncio.run(go())


def test_alt_v_pastes_an_image_into_the_box_and_backspace_takes_it_off(projects, tmp_path, clipboard_images):
    from textual.widgets import Static

    app, folder, fakes = app_with_session(projects, tmp_path, {"see this": [text("Seen."), done()]})

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            driven = await started(app, pilot, folder)
            app._focus_key(driven.key)
            await pilot.press("i", "alt+v")  # nothing on the clipboard: you're told so
            await pilot.pause(0.1)
            assert driven.images == [] and any("no image" in n.message for n in app._notifications)

            clipboard_images.extend([_image(1920, 1080), _image(800, 600)])
            await pilot.press("alt+v")
            await until(pilot, lambda: len(driven.images) == 1)
            await pilot.press("alt+v")
            await until(pilot, lambda: len(driven.images) == 2)
            line = app.query_one(tui_mod.DrivenPane).query_one(".composer-images", Static)
            await until(pilot, lambda: line.display and "[Image #2] 800×600" in str(line.render()))

            await pilot.press("backspace")  # the box is empty: the last image comes off
            await until(pilot, lambda: len(driven.images) == 1)
            await pilot.press(*"see this", "enter")
            await until(pilot, lambda: fakes[0].blocks)
            assert [b["type"] for b in fakes[0].blocks[0]] == ["image", "text"]
            await until(pilot, lambda: not line.display)

    asyncio.run(go())


def test_the_new_session_dialog_takes_images_with_the_first_message(projects, tmp_path, clipboard_images):
    from textual.widgets import Static

    from agentlens.tui_driven import NewSessionScreen

    app, folder, fakes = app_with_session(projects, tmp_path, {"see this": [text("Seen."), done()]})
    app._new_session_folder = lambda: str(folder)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            app.action_new_session()
            await pilot.pause(0.1)
            dialog = app.screen
            assert isinstance(dialog, NewSessionScreen)
            line = dialog.query_one("#new-images", Static)
            assert not line.display
            clipboard_images.extend([_image(1920, 1080), _image(800, 600)])
            await pilot.press("alt+v")
            await until(pilot, lambda: len(dialog.images) == 1)
            await pilot.press("ctrl+v")
            await until(pilot, lambda: len(dialog.images) == 2)
            await until(pilot, lambda: line.display and "[Image #2] 800×600" in str(line.render()))
            await pilot.press("backspace")  # the box is empty: the last image comes off
            await until(pilot, lambda: len(dialog.images) == 1)
            await pilot.press(*"see this", "enter")
            await until(pilot, lambda: fakes and fakes[0].blocks)
            [content] = fakes[0].blocks
            assert [b["type"] for b in content] == ["image", "text"]
            [driven] = app.driven.values()
            assert driven.images == [] and any(e.text == "[Image #1] see this" for e in driven.events)

    asyncio.run(go())


def test_an_image_on_its_own_starts_a_session_with_it():
    async def go():
        d, fakes = make({})
        await d.start("", [_image()])
        assert [b["type"] for b in fakes[0].blocks[-1]] == ["image"] and d.images == []
        d2, fakes2 = make({})
        await d2.start()  # no first message: nothing goes
        assert fakes2[0].sent == []
    asyncio.run(go())


def test_ctrl_v_pastes_text_when_there_is_no_image(projects, tmp_path, clipboard_images):
    app, folder, _ = app_with_session(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            driven = await started(app, pilot, folder)
            app._focus_key(driven.key)
            await pilot.press("i")
            app._clipboard = "some text"  # copied in the app: what Textual's own paste puts in
            await pilot.press("ctrl+v")
            await until(pilot, lambda: app.focused.value == "some text")
            assert driven.images == []
            clipboard_images.append(_image())
            await pilot.press("ctrl+v")
            await until(pilot, lambda: len(driven.images) == 1)
            assert app.focused.value == "some text"

    asyncio.run(go())


# The line that shows Claude working on your message -----------------------------------------

from datetime import timedelta as _timedelta  # noqa: E402

from claude_agent_sdk import ToolResultBlock, UserMessage  # noqa: E402
from rich.cells import cell_len as _cell_len  # noqa: E402

from agentlens.feed import Palette as _Palette  # noqa: E402
from agentlens.tui_driven import SPINNER, activity_line, elapsed  # noqa: E402


def streamed(event, subagent=None):
    return StreamEvent("u", "sess-42", event, subagent)


def test_a_turn_says_what_claude_is_doing_and_counts_the_tokens_it_writes():
    async def go():
        d, _ = make({})
        await d.start()
        assert d.turn_started is None and d.tokens == 0
        await d.send("fix the tests")
        assert d.state == "working" and d.turn_started is not None and d.detail == "thinking"
        began = d.turn_started
        d.handle(streamed({"type": "message_start", "message": {}}))
        d.handle(streamed({"type": "content_block_start", "content_block": {"type": "thinking"}}))
        d.handle(streamed({"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "x" * 400}}))
        assert d.detail == "thinking" and d.tokens == 100  # estimated while it streams
        d.handle(streamed({"type": "content_block_start", "content_block": {"type": "text"}}))
        d.handle(partial("Let me run them."))
        assert d.detail == "writing"
        d.handle(streamed({"type": "content_block_start", "content_block": {"type": "tool_use", "name": "Bash"}}))
        d.handle(streamed({"type": "content_block_delta",
                           "delta": {"type": "input_json_delta", "partial_json": '{"command": "uv run pytest"}'}}))
        assert d.detail == "preparing Bash"
        d.handle(streamed({"type": "message_delta", "usage": {"output_tokens": 250}}))
        assert d.tokens == 250  # Claude Code's own count once the reply is done
        d.handle(AssistantMessage([ToolUseBlock("t1", "Bash", {"command": "cd D:/work/demo && uv run pytest"})],
                                  "claude-opus-5-5"))
        assert d.detail == "running Bash: uv run pytest"
        # A subagent's writing counts too, but what the session is doing stays the main thread's.
        d.handle(streamed({"type": "message_start", "message": {}}, subagent="t1"))
        d.handle(streamed({"type": "content_block_start", "content_block": {"type": "text"}}, subagent="t1"))
        d.handle(streamed({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "y" * 40}},
                          subagent="t1"))
        assert d.tokens == 260 and d.detail == "running Bash: uv run pytest" and d.partial == "Let me run them."
        d.handle(UserMessage([ToolResultBlock("t1", "3 passed")]))
        assert d.detail == "thinking"  # over what it got back
        assert d.turn_started == began
        d.handle(done())
        assert d.turn_started is None
        await d.send("and the docs")
        assert d.turn_started is not None and d.tokens == 0  # a new turn counts from zero
        await d.close()
    asyncio.run(go())


def test_after_you_answer_it_says_what_it_went_on_with():
    async def go():
        write = ("ask", "Write", {"file_path": "D:/work/demo/a.txt", "content": "x"})
        question = ("ask", "AskUserQuestion", {"questions": [{"question": "Which?", "options": [{"label": "A"}]}]})
        d, fakes = make({"write it": [write], "ask me": [question]})
        await d.start("write it")
        await settle(lambda: d.state == PERMISSION)
        d.allow()
        await settle(lambda: fakes[0].decisions)
        assert d.state == "working" and d.detail == "running Write: a.txt"
        d.handle(done())
        await d.send("ask me")
        await settle(lambda: d.state == QUESTION)
        d.choose(1)
        await settle(lambda: len(fakes[0].decisions) == 2)
        assert d.state == "working" and d.detail == "thinking"
        await d.close()
    asyncio.run(go())


def test_the_activity_line_keeps_the_time_and_tokens_in_view():
    d, _ = make({})
    d.state, d.detail = "working", "running Bash: " + "very long command " * 10
    d.turn_started = d.since
    d.turn_tokens = 1234
    line = activity_line(d, d.since + _timedelta(seconds=64), 3, _Palette(), width=60)
    assert line.plain.startswith(f"{SPINNER[3]} Running Bash: very long")
    assert line.plain.endswith("… (1m 04s · ↓ 1.2k tokens)") and _cell_len(line.plain) <= 60
    d.detail = "thinking"
    d._client = object()  # running: x stops it, once you're out of the box
    assert activity_line(d, d.since, 0, _Palette()).plain == "· Thinking… (0s · ↓ 1.2k tokens · x stops it)"
    assert activity_line(d, d.since, 0, _Palette(), typing=True).plain.endswith("· Esc, then x stops it)")
    assert [elapsed(s) for s in (9, 64, 3720)] == ["9s", "1m 04s", "1h 02m"]


def test_a_line_above_the_box_shows_claude_working_on_your_message(projects, tmp_path):
    app, folder, _ = app_with_session(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            driven = await started(app, pilot, folder)
            app.config.show_tools = False  # with tool calls hidden, the line still says what it runs
            app._focus_key(driven.key)
            composer = app.query_one(Composer)
            status = composer.query_one(".composer-status")
            assert not status.display
            await pilot.press("i", *"go", "enter")
            await until(pilot, lambda: status.display)  # with your message, before Claude says anything
            assert "Thinking…" in str(status.content) and "Esc, then x stops it" in str(status.content)
            driven.handle(AssistantMessage([ToolUseBlock("t1", "Bash", {"command": "uv run pytest"})],
                                           "claude-opus-5-5"))
            await until(pilot, lambda: "Running Bash: uv run pytest" in str(status.content))
            assert "pytest" not in pane_text(app.query_one(DrivenPane))
            frames = set()
            for _ in range(12):
                frames.add(str(status.content)[0])
                await pilot.pause(0.05)
            assert len(frames) > 2  # it turns
            driven.handle(done())
            await until(pilot, lambda: not status.display)

    asyncio.run(go())
