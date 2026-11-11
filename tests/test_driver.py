"""Driven sessions against a fake Claude Code: no real CLI, no usage spent."""

import asyncio
import contextlib
import time
from datetime import datetime, timezone

import pytest

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

from agentlens.adapters.claude_transcripts import KEEP_PLANNING
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
            if isinstance(step, tuple) and step[0] == "pause":  # ("pause", seconds): working on something
                await asyncio.sleep(step[1])
            elif isinstance(step, tuple) and step[0] == "ask":  # ("ask", tool, input[, the call's id])
                _, tool, tool_input, *call = step
                context = ToolPermissionContext(title=None, tool_use_id=call[0] if call else None)
                decision = await self.options.can_use_tool(tool, tool_input, context)
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
        await settle(lambda: len(fakes[0].controls) == 2)
        assert d.title == "Parser performance" and d.titled
        assert fakes[0].controls == [
            {"subtype": "generate_session_title", "persist": True,
             "description": "why is the parser so slow on big repos?"},
            # and named after it, so your other sessions can tell it from the rest in its folder
            {"subtype": "rename_session", "title": "Parser performance", "source": "host", "session_id": "sess-42"}]
        await d.send("and the tokenizer?")
        await asyncio.sleep(0.05)
        assert len(fakes[0].controls) == 2  # titled and named once
        await d.close()
    asyncio.run(go())


def test_a_resumed_session_with_no_name_takes_its_title_and_one_without_a_title_keeps_claude_codes():
    async def go():
        d, fakes = make({}, resume="sess-42")  # from before agentlens named its sessions
        d.title = "Parser performance"
        await d.start()
        await settle(lambda: fakes[0].controls)
        assert fakes[0].controls == [{"subtype": "rename_session", "title": "Parser performance", "source": "host",
                                      "session_id": "sess-42"}]
        assert d.known_as == "Parser performance"
        await d.close()

        untitled, fakes = make({}, resume="sess-43")
        await untitled.start()
        await asyncio.sleep(0.05)
        assert fakes[0].controls == []  # its folder's name and two characters, as Claude Code gave it
        await untitled.close()
    asyncio.run(go())


def test_a_resumed_session_with_a_name_keeps_it(monkeypatch):
    monkeypatch.setattr(FakeClaude, "titles", {"Parser performance\nand the tokenizer?": "Parser performance"})

    async def go():
        d, fakes = make({}, resume="sess-42")
        d.title, d.known_as = "Parser performance", "parser"  # saved in Claude Code: agentlens's, or your /rename
        await d.start()
        await d.send("and the tokenizer?")
        await settle(lambda: d.titled)
        await asyncio.sleep(0.05)
        assert [c["subtype"] for c in fakes[0].controls] == ["generate_session_title"]  # titled, not renamed
        await d.close()
    asyncio.run(go())


def test_a_paused_session_woken_for_another_starts_under_its_name_and_a_message_waits_for_it():
    async def go():
        d, fakes = make({}, resume="sess-42")  # from before agentlens named its sessions, paused since
        d.title = "Parser performance"
        d.park([])
        await d.wake("ready · started so Fix the build can message it")
        assert fakes[0].controls == [{"subtype": "rename_session", "title": "Parser performance", "source": "host",
                                      "session_id": "sess-42"}]
        assert (d.state, d.detail) == (WAITING, "ready · started so Fix the build can message it")
        assert fakes[0].sent == []  # nothing to do until another session messages it
        await d.close()

        other, others = make({"ask it": [text("Asked."), done()]})
        await other.start()
        woken = asyncio.get_running_loop().create_future()
        sending = asyncio.ensure_future(other.send("ask it", first=woken))
        await asyncio.sleep(0.05)
        assert others[0].sent == [] and other.events[-1].text == "ask it"  # shown, not read yet
        assert other.detail == "starting the sessions it names…"
        woken.set_result(None)
        await sending
        assert others[0].sent == ["ask it"]
        await other.close()
    asyncio.run(go())


def test_an_older_claude_code_that_cant_rename_keeps_its_own_name(monkeypatch):
    monkeypatch.setattr(FakeClaude, "titles", {"fix the flaky login test": "Flaky login test"})
    original = FakeClaude._send_control_request

    async def no_rename(self, request):
        if request["subtype"] == "rename_session":
            raise Exception("Unsupported control request subtype: rename_session")
        return await original(self, request)

    monkeypatch.setattr(FakeClaude, "_send_control_request", no_rename)

    async def go():
        d, fakes = make({"fix the flaky login test": [text("On it."), done()]})
        await d.start("fix the flaky login test")
        await settle(lambda: d.titled and d.state == WAITING and d.events[-1].kind == "claude")
        assert d.title == "Flaky login test" and d.error is None
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
        await settle(lambda: fakes and len(fakes[0].controls) == 2)
        [titling] = [c for c in fakes[0].controls if c["subtype"] == "generate_session_title"]
        assert titling["description"] == f"{d.title}\nany luck?"
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


def test_what_claude_asked_and_what_you_answered_stay_in_the_conversation():
    from claude_agent_sdk import ToolResultBlock, UserMessage

    questions = {"questions": [
        {"question": "Which colour?", "header": "Colour", "options": [{"label": "Red"}, {"label": "Blue"}]},
        {"question": "Which size?", "header": "Size", "options": [{"label": "S"}, {"label": "L"}]},
    ]}
    d, _ = make({})
    d.handle(AssistantMessage([ToolUseBlock("q1", "AskUserQuestion", questions)], "claude-opus-5-5"))
    asked = d.events[-1]
    assert (asked.kind, asked.text) == ("question", "Which colour? (+1 more)")
    assert asked.detail == "**Which colour?**\n\n- Red\n- Blue\n\n**Which size?**\n\n- S\n- L"
    answers = {"Which colour?": "Blue", "Which size?": "extra large, please"}
    d.handle(UserMessage([ToolResultBlock("q1", "Your questions have been answered.")],
                         tool_use_result={**questions, "answers": answers}))
    assert (d.events[-1].kind, d.events[-1].detail) == ("answer", "Colour: Blue\nSize: extra large, please")
    # Declined, as Esc does: an error result, which here is no error, just a question left unanswered.
    d.handle(AssistantMessage([ToolUseBlock("q2", "AskUserQuestion", questions)], "claude-opus-5-5"))
    d.handle(UserMessage([ToolResultBlock("q2", "The user declined to answer.", is_error=True)]))
    assert [(e.kind, e.tag) for e in d.events] == [("question", ""), ("answer", ""), ("question", ""),
                                                   ("answer", "declined")]


def test_what_you_made_of_a_plan_stays_in_the_conversation():
    # Sent back or approved, it reads as your answer, not as a tool call that failed.
    from claude_agent_sdk import ToolResultBlock, UserMessage

    d, _ = make({})
    for call, result, is_error in [
        ("p1", "also add tests", True),  # 3, and what to change
        ("p2", KEEP_PLANNING, True),  # 3, and nothing said
        ("p3", "User has approved your plan. You can now start coding.", False),
    ]:
        d.handle(AssistantMessage([ToolUseBlock(call, "ExitPlanMode", {"plan": "## Plan"})], "claude-opus-5-5"))
        d.handle(UserMessage([ToolResultBlock(call, result, is_error=is_error)]))
    assert [(e.kind, e.tag, e.detail) for e in d.events if e.kind != "plan"] == [
        ("answer", "sent back", "also add tests"), ("answer", "sent back", ""), ("answer", "approved", "")]


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


def test_a_plan_is_in_the_conversation_once():
    # The call and the request to approve it can each carry the plan, and come in either order.
    plan = {"plan": "## Plan\n1. Do the thing", "planFilePath": "C:/Users/me/.claude/plans/p.md"}

    def call(call_id, tool_input):
        return AssistantMessage([ToolUseBlock(call_id, "ExitPlanMode", tool_input)], "claude-opus-5-5")

    async def go():
        d, fakes = make({
            "plan": [call("p1", plan), ("pause", 0.05), ("ask", "ExitPlanMode", plan, "p1"), done()],
            "again": [("ask", "ExitPlanMode", plan, "p2"), call("p2", {}), done()],
        })
        await d.start("plan")
        await settle(lambda: d.state == APPROVAL)
        d.approve_plan()
        await settle(lambda: d.state == WAITING)
        await d.send("again")
        await settle(lambda: d.state == APPROVAL)
        d.approve_plan()
        await settle(lambda: d.state == WAITING)
        await d.close()
        return d

    d = asyncio.run(go())
    assert [(e.kind, e.text, e.detail) for e in d.events if e.kind != "you"] == [
        ("plan", "Plan", plan["plan"]), ("plan", "Plan", plan["plan"])]


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
            assert fakes[0].options.model is None  # Claude Code's default: no model asked for
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


def test_new_session_starts_on_the_model_picked_and_the_dialog_on_the_default(projects, tmp_path, isolated_home,
                                                                              server_info):
    """The dialog offers the models Claude Code last listed, on its default each time; one picked
    is the session's, and it comes back on it next launch."""
    from textual.widgets import Select

    from agentlens import commands as commands_mod
    from agentlens.tui_driven import NewSessionScreen

    commands_mod.save_cached(commands_mod.Catalog.from_server_info(server_info))  # as a session last said
    app, folder, fakes = app_with_session(projects, tmp_path, {"map the parser": [text("Mapped."), done()]})
    app._new_session_folder = lambda: str(folder)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.press("n")
            await until(pilot, lambda: isinstance(app.screen, NewSessionScreen))
            model = app.screen.query_one("#new-model", Select)
            assert model.value == "default" and model.display
            assert [label for label, _ in app.screen.models] == [
                "Default (recommended) · Opus 5.5 · Org default", "Opus 5.5 · For complex work", "Haiku 5.5 · Fastest"]
            model.value = "haiku"
            await pilot.press(*"map the parser", "enter")
            await until(pilot, lambda: app.driven and next(iter(app.driven.values())).session_id)
            [driven] = app.driven.values()
            assert driven.model == "haiku" and fakes[0].options.model == "haiku"
            await until(pilot, lambda: _config.load(isolated_home / "config.yaml").driven_sessions)
            assert _config.load(isolated_home / "config.yaml").driven_sessions[0]["model"] == "haiku"

            await pilot.press("n")  # what you picked was for that session: the next starts on the default
            await until(pilot, lambda: isinstance(app.screen, NewSessionScreen))
            assert app.screen.query_one("#new-model", Select).value == "default"
            await pilot.press("escape")

    asyncio.run(go())


def test_the_dialog_offers_claude_codes_model_names_before_any_session_has_listed_them():
    from agentlens.commands import Choice
    from agentlens.tui_driven import model_options

    assert model_options([]) == [("Default model, as Claude Code picks it", "default"), ("Opus", "opus"),
                                 ("Sonnet", "sonnet"), ("Haiku", "haiku")]
    assert model_options([Choice("sonnet", "Sonnet 5.5")]) == [("Default model, as Claude Code picks it", "default"),
                                                                 ("Sonnet 5.5", "sonnet")]


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


def test_a_session_started_in_a_repositorys_space_goes_by_its_folder(projects, tmp_path, isolated_home):
    """Moving a session into a repository's space saves the space in your settings, but it's still
    the repository's, not one you made: a session you start there in another folder goes in that
    folder's space, as it would before you moved anything into it."""
    import re

    from conftest import common, human, write_jsonl

    from agentlens import config as config_mod
    from agentlens.tui_driven import NewSessionScreen

    repo, acme = tmp_path / "lens", tmp_path / "acme"
    for folder in (repo / ".git", acme):
        folder.mkdir(parents=True)
    write_jsonl(projects / re.sub(r"[^A-Za-z0-9]", "-", str(repo)) / "past-1.jsonl",
                common([human(0)], "past-1", cwd=str(repo)))  # none of lens's sessions is open
    config_mod.save(config_mod.Config(path=isolated_home / "config.yaml", last_space="lens",
                                      spaces=[config_mod.Space("lens", sessions=["moved-1"])]))
    app = AgentlensApp(provider=lambda: [], root=projects, interval=60)
    app.client_factory = lambda options: FakeClaude(options, {})

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await until(pilot, lambda: app._restored and not app._listing, steps=300)  # the first listing is in
            assert app.current_space == "lens"
            await pilot.press("n")
            await until(pilot, lambda: isinstance(app.screen, NewSessionScreen) and app.screen.query("#new-space"))
            box, space = app.screen.query_one("#new-folder"), lambda: str(app.screen.query_one("#new-space").render())
            await until(pilot, lambda: box.value == str(repo) and space() == "Opens in the lens space")
            box.value = str(acme)
            await until(pilot, lambda: space() == "Opens in the acme space")
            await pilot.press(*"hi", "enter")
            # Waits long enough for a full test run's load: the session's id comes from Claude Code.
            await settle(lambda: app.driven and all(d.session_id for d in app.driven.values()), steps=1000)
            driven = next(iter(app.driven.values()))
            app.action_refresh()
            # The listing it starts applied, not one from before: one still going as the app closes fails there.
            await settle(lambda: driven.key in app.space_of and not app._listing and not app._refresh_due, steps=1000)
            assert app.space_of[driven.key] == "acme"
            assert app.config.spaces[0].sessions == ["moved-1"]

    asyncio.run(go())


def test_a_repositorys_space_you_started_a_session_in_stays_once_its_sessions_close(projects, tmp_path,
                                                                                     isolated_home):
    """A repository's space shows while one of its sessions is open. Start one in it with n and
    the space is kept: closing its sessions leaves it in the list, and n there starts one in its folder."""
    import re

    from conftest import common, human, write_jsonl

    from agentlens import config as config_mod
    from agentlens.tui_driven import NewSessionScreen

    repo = tmp_path / "lens"
    (repo / ".git").mkdir(parents=True)
    # What Claude Code writes for the session: the fake doesn't, and it's how n finds the folder again.
    write_jsonl(projects / re.sub(r"[^A-Za-z0-9]", "-", str(repo)) / "past-1.jsonl",
                common([human(0)], "past-1", cwd=str(repo)))
    app = AgentlensApp(provider=lambda: [], root=projects, interval=60)
    app.client_factory = lambda options: FakeClaude(options, {})

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await until(pilot, lambda: app._restored and not app._listing, steps=300)
            await pilot.press("n")
            await until(pilot, lambda: isinstance(app.screen, NewSessionScreen) and app.screen.query("#new-space"))
            app.screen.query_one("#new-folder").value = str(repo)
            await pilot.pause(0.1)
            await pilot.press(*"hi", "enter")
            await settle(lambda: app.driven and all(d.session_id for d in app.driven.values()), steps=1000)
            assert [s.name for s in config_mod.load(isolated_home / "config.yaml").spaces] == ["lens"]

            await pilot.press("w", "w")
            await until(pilot, lambda: not app.driven and not app.query(DrivenPane))
            assert "lens" in app.space_names()
            await pilot.press("right_square_bracket")
            await pilot.pause(0.1)
            assert app.current_space == "lens"
            await pilot.press("n")
            await until(pilot, lambda: isinstance(app.screen, NewSessionScreen) and app.screen.query("#new-space"))
            box, space = app.screen.query_one("#new-folder"), lambda: str(app.screen.query_one("#new-space").render())
            await until(pilot, lambda: box.value == str(repo) and space() == "Opens in the lens space")

    asyncio.run(go())


def test_sessions_come_back_in_their_own_space_not_the_one_you_quit_in(projects, tmp_path, isolated_home):
    """A session you start in a space you made goes in it. One that comes back at launch doesn't:
    it keeps its own space, whichever space you were in when you quit."""
    folder = tmp_path / "repo"
    folder.mkdir()
    _write(projects / "repo" / "sess-42.jsonl",
           _common([_human(0, "add a parser"), _text(1, "Parser added.")], "sess-42", cwd=str(folder)))
    cfg = _config.Config(path=isolated_home / "config.yaml", last_space="AcmeDev", spaces=[_config.Space("AcmeDev")])
    cfg.remember_driven("sess-42", str(folder), "Parser work", "default")
    _config.save(cfg)
    app, _, _ = app_with_session(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause(0.3)
            assert app.current_space == "AcmeDev"
            [driven] = app.driven.values()
            app.action_refresh()
            await app.workers.wait_for_complete()
            await pilot.pause(0.3)
            assert app.space_of.get(driven.key) == "repo"
            assert _config.load(isolated_home / "config.yaml").spaces[0].sessions == []

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

    async def stream_until(sessions, condition, words=500):
        """Replies streaming in, word by word, until `condition`: as many words as the machine takes."""
        for i in range(words):
            for d in sessions:
                d.handle(partial(f"w{i} "))
            await asyncio.sleep(0.01)
            if condition():
                return
        raise AssertionError("condition never became true")

    async def go():
        async with app.run_test(size=(200, 50)) as pilot:
            sessions = [await started(app, pilot, folder) for _ in range(6)]
            mine, *others = sessions
            app._focus_key(mine.key)
            await until(pilot, lambda: app._last_key == mine.key and not app._dirty)  # on it, and all shown
            painted.clear()
            # Counted while they stream: once they stop, the others' last repaints catch up with yours.
            await stream_until(sessions, lambda: painted.get(mine.key, 0) >= 2 + max(painted.get(d.key, 0)
                                                                                 for d in others))
            # A permission prompt in a pane you're not on shows with the next repaint, not a later one.
            asking = others[2]
            shown = []  # at each repaint once it asks: whether its pane says so
            flush = app._flush_driven

            def flushing():
                asked = asking.state == PERMISSION
                flush()
                if asked:
                    shown.append(app.query_one(f"#s-{asking.key}").has_class("st-question"))

            app._flush_driven = flushing
            app.run_worker(asking.send("slow"))
            await stream_until(sessions, lambda: shown)
            assert shown[0]

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


def test_a_new_session_you_have_not_written_to_is_on_the_wall_once(projects, tmp_path):
    folder = tmp_path / "repo"
    folder.mkdir()
    listed = []  # what `claude agents` reports
    fakes = []

    class LikeClaudeCode(FakeClaude):
        async def connect(self):
            pass  # its conversation's id comes with the first message, but it's listed as open from the start

        async def disconnect(self):
            await self.exited.wait()

    def factory(options):
        fakes.append(LikeClaudeCode(options, {}))
        fakes[-1].exited = asyncio.Event()
        return fakes[-1]

    app = AgentlensApp(provider=lambda: list(listed), root=projects, interval=60)
    app.client_factory = factory

    async def refresh(pilot):
        app.action_refresh()
        for worker in [w for w in app.workers if w.group == "live"]:
            await worker.wait()
        await pilot.pause(0.1)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("n")
            await pilot.pause(0.2)
            app.screen.query_one("#new-folder").value = str(folder)
            await pilot.press("enter")  # no first message
            await until(pilot, lambda: app.driven and next(iter(app.driven.values())).state == WAITING)
            driven = next(iter(app.driven.values()))
            assert driven.session_id is None
            listed.append({"sessionId": fakes[0].options.session_id, "status": "idle", "name": "repo-a1",
                           "kind": "interactive", "cwd": str(folder)})
            await refresh(pilot)
            assert [type(p) for p in app.query(tui_mod.SessionPane)] == [DrivenPane]
            await pilot.press("w", "w")
            await refresh(pilot)
            assert not app.query(tui_mod.SessionPane)  # still listed while Claude Code shuts down, but it's going
            fakes[0].exited.set()
            await until(pilot, lambda: driven.state == "ended")
            listed.clear()
            await refresh(pilot)
            assert not app.query(tui_mod.SessionPane) and not app._closing_sessions

    asyncio.run(go())


def test_a_new_session_starts_under_an_id_it_knows_and_a_resumed_one_under_its_conversations():
    async def go():
        new, fakes = make({})
        await new.start()
        assert fakes[0].options.session_id == new.new_id and fakes[0].options.resume is None
        new.handle(ConversationResetMessage("conv-2", "u1", "sess-42"))  # /clear
        assert new.listed_as is None  # the id it started under is the old conversation's now
        back, fakes = make({}, resume="sess-7")
        await back.start()
        assert fakes[0].options.session_id is None and fakes[0].options.resume == "sess-7"
        await new.close()
        await back.close()
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

    def fake_resume(claude, session_id, cwd, mode, instruction, name=""):
        calls.append((session_id, cwd, mode, instruction, name))
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
            # With an instruction: handed to the background service, under the name it has. Without
            # it, the service names it anew, and the sessions that know it by its name lose it.
            driven = await started(app, pilot, folder)
            driven.title = driven.known_as = "Plan review"
            await pilot.press("d")
            await pilot.pause(0.1)
            await pilot.press(*"run the tests", "enter")
            await pilot.pause(0.4)
            assert calls == [("sess-42", str(folder), "default", "run the tests", "Plan review")] and not app.driven
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


def test_quit_asks_only_about_sessions_that_are_working(projects, tmp_path, monkeypatch):
    # One waiting on your answer isn't working: it asked to detach it, where it would only wait.
    scripts = {"ask": [("ask", "Bash", {"command": "make"})], "slow": [("pause", 10)]}
    app, folder, _ = app_with_session(projects, tmp_path, scripts)
    closed = []

    async def close_all():
        closed.append(True)

    monkeypatch.setattr(app, "_close_all_and_exit", close_all)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            asking = await started(app, pilot, folder, prompt="ask")
            await settle(lambda: asking.state == PERMISSION)
            await pilot.press("escape", "q")
            await pilot.pause(0.2)
            assert closed and not isinstance(app.screen, ChoiceScreen)
            working = await started(app, pilot, folder, prompt="slow")
            assert working.state == "working"
            await pilot.press("escape", "q")
            await pilot.pause(0.2)
            assert isinstance(app.screen, ChoiceScreen) and app.screen.title_text == "1 session is still working"

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
    # One Claude Code has no name saved for goes by its title there, as it does here.
    listings = iter([[], [{"sessionId": "sess-1", "kind": "background"}]])
    tui_mod.background_resume("claude", "sess-1", "D:/w", "default", "go on", "Parser performance",
                              list_sessions=lambda: next(listings), wait_seconds=1)
    assert runs[1][:5] == ["claude", "--resume", "sess-1", "--name", "Parser performance"]
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


@pytest.mark.parametrize("status", ["busy", "idle"])
def test_a_session_brought_here_mid_turn_carries_on(projects, tmp_path, monkeypatch, status):
    # Stopping its background run cut the turn off, and here it only waited ("No response
    # requested") until you asked whether it was working. One that had finished waits for you.
    folder = tmp_path / "repo"
    folder.mkdir()
    listed = [{"sessionId": "sess-42", "id": "sess4200", "status": status, "name": "bg-1", "kind": "background",
               "cwd": str(folder)}]
    app = AgentlensApp(provider=lambda: list(listed), root=projects, interval=60)
    fakes = []
    app.client_factory = lambda options: fakes.append(FakeClaude(options, {})) or fakes[-1]
    monkeypatch.setattr(tui_mod, "stop_background", lambda *a, **kw: (listed.clear(), (True, ""))[1])

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause(0.2)
            app._focus_key("sess-42")
            await pilot.press("o")
            await pilot.pause(0.1)
            if status == "busy":  # it asks first, and says it goes on here
                assert isinstance(app.screen, ChoiceScreen) and "carries on here" in app.screen.body
                await pilot.press("o")
            await settle(lambda: app.driven)
            [driven] = app.driven.values()
            await settle(lambda: fakes and driven.state != "starting")
            await pilot.pause(0.1)
            assert fakes[0].sent == ([tui_mod.CARRY_ON] if status == "busy" else [])

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


LONG_REPLY = "Parser added.\n\n" + "It reads every comment now. " * 200 + "Decisions for you: none."


def test_sessions_come_back_paused_with_their_conversation(projects, tmp_path, isolated_home):
    folder = tmp_path / "repo"
    folder.mkdir()
    _write(projects / "repo" / "sess-42.jsonl",
           _common([_human(0, "add a parser"), _text(1, LONG_REPLY)], "sess-42", cwd=str(folder)))
    cfg = _config.Config(path=isolated_home / "config.yaml")
    cfg.remember_driven("sess-42", str(folder), "Parser work", "acceptEdits", model="sonnet")
    _config.save(cfg)
    app, _, fakes = app_with_session(projects, tmp_path, {"continue": [text("Continuing."), done()]})

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause(0.3)
            [driven] = app.driven.values()
            assert driven.state == PAUSED and driven.resume == "sess-42" and driven.title == "Parser work"
            assert [e.text for e in driven.events] == ["add a parser", "Parser added."]
            assert driven.events[-1].detail == LONG_REPLY  # all of it, as it read before you quit
            assert fakes == []  # not connected until you type
            assert "need you" in app.query_one("#summary").content.plain and app.query_one("#summary").content.plain.startswith("0")
            app._focus_key(driven.key)
            await pilot.press("i", *"continue", "enter")
            for _ in range(100):
                await pilot.pause(0.03)
                if driven.events[-1].text == "Continuing.":
                    break
            assert fakes[0].options.resume == "sess-42" and fakes[0].options.permission_mode == "acceptEdits"
            assert fakes[0].options.model == "sonnet"  # the one you started it on
            assert fakes[0].sent == ["continue"]
            # Closing takes it off the list for next time.
            await pilot.press("escape", "delete", "w")
            await pilot.pause(0.3)
            assert _config.load(isolated_home / "config.yaml").driven_sessions == []

    asyncio.run(go())


def test_zoom_on_a_session_agentlens_runs_lists_its_subagents_and_context(projects, tmp_path, isolated_home):
    from conftest import tool_result, tool_use

    folder = tmp_path / "repo"
    folder.mkdir()
    reply = _text(1, "Asked a subagent.")
    reply["message"]["usage"].update(input_tokens=0, cache_read_input_tokens=250_000)
    _write(projects / "repo" / "sess-42.jsonl", _common([_human(0, "map the code"), reply], "sess-42", cwd=str(folder)))
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
            # How full its context is comes from its transcript too.
            assert app.screen.query_one("#zoom-context").content.plain.endswith("25% 250K of 1M")

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
            assert pane.border_title == "parser · repo · here · Opus 5.5"  # the model after here
            assert pane.query_one(".headline").content.plain.startswith("Parser support\n")

    asyncio.run(go())


def test_a_session_from_before_agentlens_named_its_sessions_takes_its_title_wherever_it_continues(
        projects, tmp_path, monkeypatch):
    app, folder, fakes = app_with_session(projects, tmp_path)
    _write(projects / "repo" / "s-old.jsonl", _common([
        _human(0, "can we add a parser?"), {"type": "ai-title", "aiTitle": "Repo parser support"}],
        "s-old", cwd=str(folder)))
    _write(projects / "repo" / "s-named.jsonl", _common([
        _human(0, "why is it slow?"), {"type": "ai-title", "aiTitle": "Parser performance"},
        {"type": "agent-name", "agentName": "parser"}], "s-named", cwd=str(folder)))  # you ran /rename parser
    runs = []
    monkeypatch.setattr(_subprocess, "run", lambda cmd, **kw: runs.append(cmd))
    monkeypatch.setattr(tui_mod.terminal, "set_title", lambda *a, **kw: None)

    def renamed():
        return {f.options.resume: [c["title"] for c in f.controls if c["subtype"] == "rename_session"]
                for f in fakes}

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            monkeypatch.setattr(app, "suspend", contextlib.nullcontext)
            app.config.cli_path = "claude"
            # Continued here: the one without a name takes its title, and yours stays.
            old, named = (app.open_driven(str(folder), resume=s) for s in ("s-old", "s-named"))
            await until(pilot, lambda: len(fakes) == 2 and all(d.state == WAITING for d in (old, named)))
            await pilot.pause(0.1)
            assert renamed() == {"s-old": ["Repo parser support"], "s-named": []}
            await app._end_driven(old, quiet=True)
            await app._end_driven(named, quiet=True)
            # Paused, as after a restart, then opened in Claude Code: the same there.
            for session_id in ("s-old", "s-named"):
                driven = app.open_driven(str(folder), resume=session_id, start=False)
                await pilot.pause(0.1)
                await app._handoff_driven(driven)
                await pilot.pause(0.1)
            assert runs == [["claude", "--resume", "s-old", "--name", "Repo parser support"],
                            ["claude", "--resume", "s-named"]]

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


def test_remembered_session_open_elsewhere_comes_back_once_it_is_not(projects, tmp_path, isolated_home):
    folder = tmp_path / "repo"
    folder.mkdir()
    cfg = _config.Config(path=isolated_home / "config.yaml")
    cfg.remember_driven("sess-42", str(folder), "Parser work", "default")
    _config.save(cfg)
    listed = [{"sessionId": "sess-42", "status": "idle", "name": "vscode-1", "kind": "interactive", "cwd": str(folder)}]
    app = AgentlensApp(provider=lambda: list(listed), root=projects, interval=60)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause(0.3)
            assert not app.driven  # it's in VS Code now; two clients must not share it
            saved = _config.load(isolated_home / "config.yaml").driven_sessions
            assert [d["session_id"] for d in saved] == ["sess-42"]  # but it isn't forgotten
            # Closed there, or listed only for a moment after the agentlens that ran it quit.
            listed.clear()
            for _ in range(2):  # back once, not again with each refresh
                app.action_refresh()
                await app.workers.wait_for_complete()
                await pilot.pause(0.2)
            [driven] = app.driven.values()
            assert driven.resume == "sess-42" and driven.state == PAUSED and driven.title == "Parser work"
            assert [p.session_id for p in app.query(tui_mod.SessionPane)] == [driven.key]

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
            assert isinstance(app.focused, tui_mod.MessageBox)
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
from agentlens.tui_driven import CommandMenu, Composer, ComposerInput, as_live  # noqa: E402


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


def test_an_at_names_a_session_as_claude_code_does_and_finds_it_by_what_the_wall_calls_it():
    from agentlens.tui_driven import Mention, mention_at, suggest_mentions

    assert mention_at("ask @sh") == "sh" and mention_at("@") == "" and mention_at("ask\n@sh") == "sh"
    assert mention_at("mail me@work.com") is None  # not after a space
    assert mention_at('ask @"Shared links" ') is None  # put in: what follows is your message again
    shared = Mention("Shared links", "Shared links", "tubeless · shared-links", "waiting on you")
    build = Mention("tubeless-d4", "Fix the Android build", "tubeless", "working")  # a terminal's: not renamed
    track = Mention("Track editor", "Track editor", "agentic-game", "working")
    assert shared.text == '@"Shared links"' and build.text == "@tubeless-d4"
    wall = [build, track, shared]
    assert suggest_mentions(wall, "") == wall
    assert suggest_mentions(wall, "sh") == [shared]
    assert suggest_mentions(wall, "t") == [build, track, shared]  # its name or title starts with it, then where
    assert suggest_mentions(wall, "ed") == [track, shared]  # a word of its title starts with it, then inside one
    assert suggest_mentions(wall, "game") == [track]  # where it is
    assert suggest_mentions(wall, "android") == [build]


def test_at_lists_your_other_sessions_and_puts_in_the_name_claude_code_knows_them_by(projects, tmp_path):
    app, folder, fakes = app_with_session(projects, tmp_path)
    tree = str(tmp_path / "tubeless" / ".claude" / "worktrees" / "shared-links")
    _write(projects / "tubeless" / "s-77.jsonl", _common([
        _human(0, "share links from the app"), {"type": "ai-title", "aiTitle": "Shared links"}], "s-77", cwd=tree))
    listed = [{"sessionId": "s-77", "status": "idle", "name": "tubeless-d4", "kind": "interactive", "cwd": tree,
               "startedAt": 1793731913649},
              {"sessionId": "s-88", "status": "busy", "name": "carcalculator-1a", "kind": "interactive",
               "cwd": str(tmp_path / "carcalculator"), "startedAt": 1793731982956}]
    app.provider = lambda: list(listed)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            driven = await started(app, pilot, folder)
            listed.append({"sessionId": "sess-42", "status": "idle", "name": "repo-4f", "kind": "interactive",
                           "cwd": str(folder)})  # this one too, as Claude Code lists the sessions agentlens runs
            app.action_refresh()
            await app.workers.wait_for_complete()
            await pilot.pause(0.1)
            app._focus_key(driven.key)
            composer = app.query_one(Composer)
            box = composer.query_one(ComposerInput)
            await pilot.press("i", *"ask ", "@")
            assert composer.menu_open and [m.title for m in composer.suggestions] == ["Shared links",
                                                                                       "carcalculator-1a"]
            assert "Claude can ask it" in str(composer.query_one(".composer-hint").content)
            await pilot.press("s", "h", "enter")  # Enter puts it in, and you go on typing
            await until(pilot, lambda: not composer.menu_open)
            assert box.value == "ask @tubeless-d4 "
            await pilot.press(*"what it decided", "enter")
            await until(pilot, lambda: fakes[0].sent)
            # Sent through the SDK, a mention is only text: Claude is told it's a session, as Claude Code's
            # own box tells it. The pane shows your message as you typed it.
            assert fakes[0].sent == ["ask @tubeless-d4 what it decided\n\n(@tubeless-d4 is another of my Claude Code "
                                     "sessions on this machine, which you can message by that name.)"]
            assert driven.events[-1].detail == "ask @tubeless-d4 what it decided"

    asyncio.run(go())


def test_at_names_a_session_paused_here_and_your_message_starts_it_first(projects, tmp_path, monkeypatch):
    app, folder, fakes = app_with_session(projects, tmp_path)
    _write(projects / "repo" / "s-old.jsonl", _common([
        _human(0, "the icon on the taskbar"), {"type": "ai-title", "aiTitle": "Taskbar icon"}], "s-old",
        cwd=str(folder)))
    order = []  # what each session's Claude Code was asked, by the session it resumed
    query, control = FakeClaude.query, FakeClaude._send_control_request

    async def logged_query(self, prompt):
        order.append((self.options.resume, "message"))
        await query(self, prompt)

    async def logged_control(self, request):
        order.append((self.options.resume, request["subtype"]))
        return await control(self, request)

    monkeypatch.setattr(FakeClaude, "query", logged_query)
    monkeypatch.setattr(FakeClaude, "_send_control_request", logged_control)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            paused = app.open_driven(str(folder), resume="s-old", start=False)  # as after a restart
            driven = await started(app, pilot, folder)
            driven.title = "Fix the build"
            app._focus_key(driven.key)
            composer = next(c for c in app.query(Composer) if c.key == driven.key)
            box = composer.query_one(ComposerInput)
            await pilot.press("i", *"ask ", "@")
            # Claude Code doesn't list it, as it isn't running, but it's on the wall.
            [mention] = composer.suggestions
            assert (mention.title, mention.state) == ("Taskbar icon", "paused · your message starts it")
            await pilot.press("enter")
            await until(pilot, lambda: not composer.menu_open)
            assert box.value == 'ask @"Taskbar icon" '
            await pilot.press(*"what it decided", "enter")
            await until(pilot, lambda: (None, "message") in order and paused.state == WAITING)
            # It starts under its title before Claude reads your message, so it can message it.
            assert order.index(("s-old", "rename_session")) < order.index((None, "message"))
            assert fakes[0].sent[-1].endswith('(@"Taskbar icon" is another of my Claude Code sessions on this '
                                              "machine, which you can message by that name.)")
            assert paused.detail == "ready · started so Fix the build can message it"
            assert not app.needs_you(as_live(paused, datetime.now(timezone.utc)))  # nothing for you there yet

    asyncio.run(go())


def test_a_message_names_the_sessions_it_mentions_and_nothing_else():
    from agentlens.tui_driven import Mention, mention_note, mentions_in

    shared = Mention("Shared links", "Shared links", "tubeless · shared-links", "idle")
    build = Mention("tubeless-d4", "Fix the Android build", "tubeless", "working")
    assert mentions_in('tell @"Shared links" and @tubeless-d4', [shared, build]) == [shared, build]
    assert mentions_in("@tubeless-d40 or tubeless-d4", [build]) == []  # another name, and no @
    assert mention_note([]) == ""
    assert mention_note([shared, build]) == ('(@"Shared links" and @tubeless-d4 are other Claude Code sessions of '
                                             'mine on this machine, which you can message by those names.)')


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

            async def typed_in_its_box(command):
                app._focus_key(driven.key)
                await until(pilot, lambda: getattr(app.focused, "session_id", None) == driven.key)
                await pilot.press("i")
                await until(pilot, lambda: isinstance(app.focused, ComposerInput))  # in its box, not on the wall
                await pilot.press(*command)
                await until(pilot, lambda: getattr(app.focused, "value", None) == command)
                await pilot.press("enter")

            await typed_in_its_box("/cost")
            await until(pilot, lambda: isinstance(app.screen, UsageScreen))
            await pilot.press("escape")
            await until(pilot, lambda: not isinstance(app.screen, UsageScreen))  # back on the wall, where its pane is
            await typed_in_its_box("/exit")
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
            # The launch's /usage check first: it keeps only the windows /usage lists, and the fake one lists
            # none, so answering after the session's event it would take that window off the meter again.
            await until(pilot, lambda: app.usage.checked is not None, steps=300)
            await started(app, pilot, folder, prompt="hi")
            await until(pilot, lambda: "five_hour" in app.usage.windows)
            assert app.usage.windows["five_hour"].used == 0.42
            await until(pilot, lambda: "Session" in str(app.query_one("#usage-meter").content)
                        and "42%" in str(app.query_one("#usage-meter").content))

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


def test_ctrl_v_pastes_text_when_there_is_no_image(projects, tmp_path, clipboard_images, clipboard_text):
    # Linux's and macOS's terminals pass Ctrl+V on (theirs is Ctrl+Shift+V, or Cmd+V), and Textual's
    # own pastes only what was copied in agentlens.
    app, folder, _ = app_with_session(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            driven = await started(app, pilot, folder)
            app._focus_key(driven.key)
            await pilot.press("i")
            app._clipboard = "copied in agentlens"
            clipboard_text.append("copied in the browser\nover two lines")
            await pilot.press("ctrl+v")
            await until(pilot, lambda: app.focused.value == "copied in the browser\nover two lines")
            assert driven.images == []
            clipboard_images.append(_image())
            await pilot.press("ctrl+v")
            await until(pilot, lambda: len(driven.images) == 1)
            assert app.focused.value == "copied in the browser\nover two lines"
            app.focused.value = ""
            clipboard_text.append(None)  # where agentlens can't read the clipboard: what it copied itself
            await pilot.press("ctrl+v")
            await until(pilot, lambda: app.focused.value == "copied in agentlens")

    asyncio.run(go())


def test_a_message_takes_lines_and_is_edited_as_in_a_windows_text_box(projects, tmp_path, system_clipboard):
    app, folder, fakes = app_with_session(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            driven = await started(app, pilot, folder)
            app._focus_key(driven.key)
            await pilot.press("i", *"fix the parser")
            box = app.focused
            height = box.outer_size.height
            await pilot.press("shift+enter", *"and its tests", "ctrl+j", *"then push", "alt+enter", *"now")
            await pilot.pause()
            assert box.value == "fix the parser\nand its tests\nthen push\nnow" and fakes[0].sent == []
            assert box.outer_size.height == height + 3  # the box grows with the message
            await pilot.press("ctrl+backspace", "backspace", "ctrl+backspace")  # a word at a time
            assert box.value == "fix the parser\nand its tests\nthen "
            # Select by words (the end of a line is a stop, as in Windows), and copy.
            await pilot.press("ctrl+shift+left", "ctrl+shift+left", "ctrl+shift+left", "ctrl+c")
            assert system_clipboard == ["tests\nthen "]
            await pilot.press("enter")
            await until(pilot, lambda: fakes[0].sent)
            assert fakes[0].sent == ["fix the parser\nand its tests\nthen"] and box.value == ""
            assert driven.events[-1].detail == "fix the parser\nand its tests\nthen"  # your card shows every line

    asyncio.run(go())


def test_a_second_question_shows_in_the_card_not_the_first_again(projects, tmp_path):
    # Answered, the card hides, with no width, while Claude works; the next question shows in it at
    # the width it had before.
    def asking(question, options):
        return ("ask", "AskUserQuestion", {"questions": [{"question": question,
                                                          "options": [{"label": o} for o in options]}]})

    script = {"ask me": [
        asking("What would you like to do?", ["Fix an issue", "Explain something"]), ("pause", 0.5),
        asking("Which part should I explain?", ["Playing a video", "Updates"])]}

    def card(app) -> str:
        shown = app.screen.query_one(".request")
        return shown.text if shown.display else ""

    async def go(zoomed: bool):
        app, folder, _ = app_with_session(projects, tmp_path, script)
        async with app.run_test(size=(160, 44)) as pilot:
            driven = await started(app, pilot, folder, prompt="ask me")
            app._focus_key(driven.key)
            if zoomed:
                await pilot.press("enter")
                await until(pilot, lambda: isinstance(app.screen, tui_mod.ZoomScreen))
            await until(pilot, lambda: "What would you like to do?" in card(app))
            await pilot.press(*([] if zoomed else ["i"]), "2")  # in an empty box, a number answers
            await until(pilot, lambda: card(app) == "")
            await pilot.pause(0.2)  # laid out without it
            await until(pilot, lambda: "Which part should I explain?" in card(app))
            assert "What would you like to do?" not in card(app) and "Playing a video" in card(app)

    for zoomed in (False, True):  # on the wall and zoomed in
        asyncio.run(go(zoomed))


def test_a_long_plan_reads_in_full_and_shows_how_to_approve_it(projects, tmp_path):
    # A plan runs longer than the card is tall: it's in the conversation, and the card says how to answer it.
    plan = "# Plan\n\n" + "\n".join(f"{n}. Step {n}" for n in range(1, 41))
    script = {"plan it": [("ask", "ExitPlanMode", {"plan": plan}, "p1")]}

    def card_on_screen(app) -> str:
        shown = app.screen.query_one(".request")
        return "\n".join(shown.text.splitlines()[:shown.size.height]) if shown.display else ""

    def conversation_text(app) -> str:
        return app.screen.query_one(".feed-body").text

    async def go(zoomed: bool):
        app, folder, _ = app_with_session(projects, tmp_path, script)
        async with app.run_test(size=(160, 44)) as pilot:
            driven = await started(app, pilot, folder, prompt="plan it")
            app._focus_key(driven.key)
            if zoomed:
                await pilot.press("enter")
                await until(pilot, lambda: isinstance(app.screen, tui_mod.ZoomScreen))
            await until(pilot, lambda: "ready for review" in card_on_screen(app))
            await pilot.pause(0.1)
            assert "1 approve and start" in card_on_screen(app) and "keep planning" in card_on_screen(app)
            assert "Step" not in card_on_screen(app)  # once is enough
            await until(pilot, lambda: "Claude's plan" in conversation_text(app))
            if zoomed:
                assert "Step 40" in conversation_text(app)
            else:  # a pane clips it, as a long reply
                assert "Step 1" in conversation_text(app) and "Enter to read the rest" in conversation_text(app)

    for zoomed in (False, True):  # on the wall and zoomed in
        asyncio.run(go(zoomed))


def test_one_line_boxes_take_a_word_with_ctrl_backspace_too(projects, tmp_path, clipboard_text):
    app, folder, _ = app_with_session(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.press("n")
            await pilot.pause(0.1)
            box = app.screen.query_one("#new-folder", tui_mod.TextInput)
            box.focus()
            await pilot.pause()  # focusing it selects all of it
            box.value = "D:/work/repo two"
            box.cursor_position = len(box.value)
            await pilot.press("ctrl+backspace")  # Textual's own Input takes the word to the right
            assert box.value == "D:/work/repo "
            box.cursor_position = 0
            await pilot.press("ctrl+delete")
            assert box.value == "work/repo "
            clipboard_text.append("D:/other\nand more")
            await pilot.press("ctrl+v")  # the clipboard's, as the terminal pastes it: its first line
            await until(pilot, lambda: box.value == "D:/otherwork/repo ")

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


# What Claude has left going, that brings it back by itself ----------------------------------

from claude_agent_sdk import TaskNotificationMessage, TaskStartedMessage, TaskUpdatedMessage  # noqa: E402

from agentlens import background  # noqa: E402
from agentlens import driver as driver_mod  # noqa: E402


def background_command(call="b1", task="bt1", command="uv run pytest"):
    """Claude running a command in the background, as Claude Code's stream has it: the call, the task it
    starts, and the call's result with the task's id."""
    return [AssistantMessage([ToolUseBlock(call, "Bash", {"command": command, "description": "Run the tests",
                                                          "run_in_background": True})], "claude-opus-5-5"),
            TaskStartedMessage("task_started", {}, task, "Run the tests", "u1", "sess-42", tool_use_id=call,
                               task_type="local_bash"),
            UserMessage([ToolResultBlock(call, f"Command running in background with ID: {task}.")],
                        tool_use_result={"stdout": "", "backgroundTaskId": task})]


def background_subagent(call="a1", agent="ag1"):
    """A subagent in the background, from the call and its result alone (no task_started)."""
    return [AssistantMessage([ToolUseBlock(call, "Agent", {"description": "Find the callers", "prompt": "...",
                                                           "run_in_background": True})], "claude-opus-5-5"),
            UserMessage([ToolResultBlock(call, "Async agent launched successfully.")],
                        tool_use_result={"isAsync": True, "status": "async_launched", "agentId": agent})]


def task_done(task="bt1", status="completed"):
    return TaskNotificationMessage("task_notification", {}, task, status, "", "Background command finished",
                                   "u2", "sess-42")


def notified(task, status="completed"):
    """The notification Claude Code gives Claude when a task ends, which starts its next turn."""
    return UserMessage(f"<task-notification>\n<task-id>{task}</task-id>\n<tool-use-id>x</tool-use-id>\n"
                       f"<status>{status}</status>\n<summary>Agent finished</summary>\n</task-notification>",
                       origin={"kind": "task-notification", "producer": "session-task"})


def called(d, call, tool, tool_input, result=None, is_error=False, said="ok"):
    """A tool call in the main conversation and its result."""
    d.handle(AssistantMessage([ToolUseBlock(call, tool, tool_input)], "claude-opus-5-5"))
    d.handle(UserMessage([ToolResultBlock(call, said, is_error=is_error)], tool_use_result=result))


def test_background_commands_and_subagents_are_outstanding_until_they_end():
    d, _ = make({})
    for message in background_command():
        d.handle(message)
    assert d.work.outstanding() == ["Run the tests"]
    for message in background_subagent():
        d.handle(message)
    assert d.work.outstanding() == ["Run the tests", "Find the callers"]
    d.handle(task_done("bt1"))
    d.handle(notified("ag1"))  # the subagent's end, from the notification Claude reads
    assert d.work.outstanding() == []
    for message in background_command(call="b2", task="bt2"):
        d.handle(message)
    d.handle(TaskUpdatedMessage("task_updated", {}, "bt2", {"status": "killed"}, status="killed"))
    assert d.work.outstanding() == []  # stopped, with no notification


def test_a_monitor_goes_on_through_its_events_and_ends_with_its_stream_or_taskstop():
    d, _ = make({})
    called(d, "m1", "Monitor", {"description": "CI run", "command": "gh run watch"},
           {"taskId": "mt1", "timeoutMs": 600000, "persistent": False})
    d.handle(UserMessage("<task-notification>\n<task-id>mt1</task-id>\n<summary>Monitor event: \"CI run\"</summary>\n"
                         "<event>build started</event>\n</task-notification>",
                         origin={"kind": "task-notification", "producer": "session-task"}))
    assert d.work.outstanding() == ["CI run"]  # an event: it watches on
    called(d, "s1", "TaskStop", {"task_id": "mt1"}, {"message": "Successfully stopped task: mt1"})
    assert d.work.outstanding() == []
    called(d, "m2", "Monitor", {"description": "log"}, {"taskId": "mt2"})
    d.handle(notified("mt2"))  # its stream ended
    assert d.work.outstanding() == []


def test_a_call_that_failed_or_a_task_that_doesnt_bring_claude_back_isnt_outstanding():
    d, _ = make({})
    called(d, "b1", "Bash", {"command": "nope", "run_in_background": True}, is_error=True, said="denied")
    d.handle(TaskStartedMessage("task_started", {}, "dr1", "Memory upkeep", "u", "sess-42", task_type="dream"))
    assert d.work.outstanding() == []
    d.handle(task_done("bt9"))  # ended before its call's result was read: that doesn't bring it back
    called(d, "b9", "Bash", {"command": "echo hi", "run_in_background": True}, {"backgroundTaskId": "bt9"})
    assert d.work.outstanding() == []
    # A subagent that ran in the call: its result says it's over, even if no end of its task came.
    d.handle(AssistantMessage([ToolUseBlock("a1", "Agent", {"description": "Explore", "prompt": "..."})],
                              "claude-opus-5-5"))
    d.handle(TaskStartedMessage("task_started", {}, "fg1", "Explore", "u", "sess-42", tool_use_id="a1",
                                task_type="local_agent"))
    assert d.work.outstanding() == ["Explore"]
    d.handle(UserMessage([ToolResultBlock("a1", "Found 3 callers.")], tool_use_result={"status": "completed"}))
    assert d.work.outstanding() == []


def test_a_wakeup_is_outstanding_until_its_time_or_until_its_cancelled():
    d, _ = make({})
    soon = time.time() + 600
    called(d, "w1", "ScheduleWakeup", {"delaySeconds": 600, "prompt": "check"},
           {"scheduledFor": soon * 1000, "clampedDelaySeconds": 600, "wasClamped": False})
    assert d.work.outstanding() == ["a wakeup"]
    assert 595 < d.work.lapses_in() <= 600
    assert d.work.outstanding(now=soon + 1) == []  # its time came: it starts a turn then, or nothing will
    called(d, "w2", "ScheduleWakeup", {"delaySeconds": 600}, {"scheduledFor": soon * 1000})
    called(d, "w3", "ScheduleWakeup", {"stop": True}, {"scheduledFor": 0, "stopped": True, "cancelledWakeups": 1})
    assert d.work.outstanding() == []


def test_a_cron_is_outstanding_until_its_deleted_and_a_one_off_until_it_fires():
    d, _ = make({})
    called(d, "c1", "CronCreate", {"cron": "*/5 * * * *", "prompt": "check CI"},
           {"id": "job1", "humanSchedule": "every 5 minutes", "recurring": True,
            "nextFireAt": (time.time() + 300) * 1000})
    assert d.work.outstanding() == ["a scheduled prompt"] and d.work.lapses_in() is None
    called(d, "c2", "CronDelete", {"id": "job1"}, {"id": "job1"})
    assert d.work.outstanding() == []
    called(d, "c3", "CronCreate", {"cron": "0 9 * * *", "prompt": "standup", "recurring": False},
           {"id": "job2", "humanSchedule": "at 9:00", "recurring": False})
    assert d.work.outstanding() == ["a scheduled prompt"]
    d.handle(ResultMessage("success", 10, 10, False, 1, "sess-42",
                           origin={"kind": "task-notification", "subkind": "scheduled-trigger"}))
    assert d.work.outstanding() == []  # it fired


def test_waiting_for_another_session_to_be_idle_lasts_until_its_notice():
    d, _ = make({})
    called(d, "s1", "SendMessage", {"to": "tubeless-95", "notify_when_idle": True, "message": ""},
           {"success": True, "message": "Subscribed: you will get one notice here when \"tubeless-95\" is next idle"})
    called(d, "s2", "SendMessage", {"to": "DJ build", "notify_when_idle": True, "message": ""},
           {"success": True, "message": "Subscribed"})
    called(d, "s3", "SendMessage", {"to": "Docs", "message": "rebased"}, {"success": True, "message": "delivered"})
    assert d.work.outstanding() == ["waiting for tubeless-95 to be idle", "waiting for DJ build to be idle"]
    d.handle(UserMessage("[Cross-session idle notice] \"DJ build\", which you asked to be notified about, is idle "
                         "now, it finished a turn at 16:19."))
    assert d.work.outstanding() == ["waiting for tubeless-95 to be idle"]
    d.handle(done())  # the turn that notice started, which agentlens didn't: that one is its own
    assert d.work.outstanding() == ["waiting for tubeless-95 to be idle"]
    # A turn nothing says began, and no notice showed: the notice that likely started it was folded in.
    d.handle(done())
    assert d.work.outstanding() == []


def test_waiting_for_a_session_lapses_if_its_notice_never_shows():
    d, _ = make({})
    called(d, "s1", "SendMessage", {"to": "tubeless-95", "notify_when_idle": True}, {"success": True})
    assert 0 < d.work.lapses_in() <= background.SUBSCRIPTION_LIFE
    assert d.work.outstanding(now=time.time() + background.SUBSCRIPTION_LIFE) == []


def test_what_it_had_going_stops_with_claude_code():
    async def go():
        d, fakes = make({"go": [*background_command(), text("Running."), done()]})
        await d.start("go")
        await settle(lambda: d.state == WAITING and d.events[-1].kind == "claude")
        assert d.work.outstanding() == ["Run the tests"]
        await fakes[0].queue.put(None)  # Claude Code exits
        await settle(lambda: d.state == driver_mod.PAUSED)
        assert d.work.outstanding() == []
        await d.close()
    asyncio.run(go())


def test_the_coordinators_quiet_wait_isnt_made_yours(monkeypatch):
    monkeypatch.setattr(driver_mod, "QUIET_SECONDS", 0.05)

    async def go():
        d, _ = make({"go": [text("Done."), done()]})
        d.on_turn_end = lambda session, yours, reply, failed: (True, "the coordinator is reading this")
        await d.start("go")
        await settle(lambda: d.state == driver_mod.BACKGROUND)
        await asyncio.sleep(0.2)
        assert d.state == driver_mod.BACKGROUND and d.detail == "the coordinator is reading this"
        await d.close()
    asyncio.run(go())


def subagent_called(call="s1", tool="Grep", agent_call="a1"):
    """A tool call by the subagent that the main conversation's call `agent_call` started."""
    return AssistantMessage([ToolUseBlock(call, tool, {})], "claude-haiku-4-5", parent_tool_use_id=agent_call)


def test_a_background_subagent_working_on_after_the_turn_leaves_it_yours():
    """The subagent's own messages after the turn are no turn of Claude's: the session waits on you until
    Claude's next turn starts."""
    async def go():
        d, fakes = make({"find the callers": [*background_subagent(), text("A subagent is looking."), done()]})
        await d.start("find the callers")
        await settle(lambda: d.state == WAITING and d.detail == "turn finished")
        await fakes[0]._play([subagent_called(),
                              UserMessage([ToolResultBlock("s1", "3 matches")], parent_tool_use_id="a1")])
        await settle(lambda: fakes[0].queue.empty())
        assert d.state == WAITING and d.detail == "turn finished" and d.turn_started is None
        await fakes[0]._play([partial("Found")])  # Claude's next turn
        await settle(lambda: d.state == driver_mod.WORKING)
        assert d.turn_started is not None
        await d.close()
    asyncio.run(go())


def test_stopping_with_no_turn_under_way_leaves_the_next_turn_as_it_would_be():
    """A background subagent asking after the turn, and you stopping it: no turn ends saying it stopped,
    so the next one is taken as usual, here by the coordinator."""
    ends = []

    async def go():
        d, fakes = make({"find the callers": [*background_subagent(), text("A subagent is looking."), done()],
                         "and the docs": [text("Docs done."), done()]})
        d.on_turn_end = lambda session, yours, reply, failed: ends.append(reply)
        await d.start("find the callers")
        await settle(lambda: d.state == WAITING and ends)
        asking = asyncio.ensure_future(fakes[0]._play([("ask", "Bash", {"command": "ls"}, "s1")]))
        await settle(lambda: d.state == PERMISSION)
        await d.interrupt()  # Claude Code stops the subagent, and sends nothing
        await asking
        await d.send("and the docs")
        await settle(lambda: len(ends) == 2)
        assert ends == ["A subagent is looking.", "Docs done."]
        await d.close()
    asyncio.run(go())


def test_a_turn_you_stop_shows_no_error_though_claude_code_ends_it_as_one():
    """As Claude Code 2.1.318 ends a turn you stop: "[Request interrupted by user]", then a result that's an error
    with a diagnostic of its own. The turn didn't fail: its pane had an error line in it at each x. One that fails
    still shows its error."""
    def result(error):
        return ResultMessage("error_during_execution", 10, 10, True, 1, "sess-42", errors=[error])

    async def go():
        d, fakes = make({"count": [text("1, 2, 3")], "again": [text("Hi."), result("API Error: 529 Overloaded")]})
        await d.start("count")
        await settle(lambda: d.events[-1].text == "1, 2, 3")
        await d.interrupt()
        await fakes[0]._play([UserMessage("[Request interrupted by user]"),
                              result("[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=null")])
        await settle(lambda: d.state == WAITING)
        assert d.detail == "turn finished" and not [e for e in d.events if e.kind == "error"]
        await d.send("again")
        await settle(lambda: d.state == WAITING and d.events[-1].kind == "error")
        assert d.events[-1].text == "API Error: 529 Overloaded"
        await d.close()
    asyncio.run(go())


def test_zoom_says_which_model_a_session_here_runs_on(projects, tmp_path):
    switched = AssistantMessage([TextBlock("Set model to `Sonnet 5.5` for this session only")], "<synthetic>")
    app, folder, _ = app_with_session(projects, tmp_path, {"hi": [text("Hello."), done()],
                                                           "/model sonnet": [switched, done()]})

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            driven = await started(app, pilot, folder, prompt="hi")
            await until(pilot, lambda: driven.state == WAITING and driven.events[-1].kind == "claude")
            assert driven.running_model == "claude-opus-5-5"  # what its reply came from
            app._focus_key(driven.key)
            app.action_zoom()
            await pilot.pause(0.2)
            meter = app.screen.query_one("#zoom-context")
            assert meter.content.plain == "Model Opus 5.5"
            # /model changes it at once, from Claude Code's answer: no reply from the model says so until the next turn.
            await driven.send("/model sonnet")
            await until(pilot, lambda: meter.content.plain == "Model Sonnet 5.5")

    asyncio.run(go())
