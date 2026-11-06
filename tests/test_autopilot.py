"""Autopilot: what it asks Sonnet, and how a session agentlens runs acts on the answer. No real CLI."""

import asyncio
import json
from datetime import datetime, timezone

import pytest
from claude_agent_sdk import ResultMessage
from rich.console import Console
from rich.text import Text
from conftest import at, common, human, text as said, write_jsonl
from test_driver import app_with_session, done, make, settle, started, text, until

from agentlens import autopilot, config as config_mod, tui as tui_mod
from agentlens.adapters.claude_transcripts import load_tail
from agentlens.autopilot import NOTE, Decision
from agentlens.driver import BACKGROUND, WAITING, WORKING
from agentlens.feed import Palette, conversation
from agentlens.model import AUTOPILOT, MODEL, WAITING_ON_YOU, Event
from agentlens.tui_driven import placeholder


def border(app, driven):
    """A pane's bottom border, as it reads."""
    return Text.from_markup(app.query_one(f"#s-{driven.key}", tui_mod.SessionPane).border_subtitle).plain


def fake_check(*answers):
    """Autopilot's call to Sonnet, giving `answers` in turn (the last one again after that), and what it was asked."""
    asked = []

    async def decide(level, title, said, reply):
        asked.append((level, said, reply))
        answer = answers[min(len(asked), len(answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return decide, asked


def by_autopilot(message):
    return f"{message}\n\n{NOTE}"


# What it asks Sonnet -----------------------------------------------------------------------

def test_the_prompt_has_the_levels_rules_your_messages_and_the_end_of_the_reply():
    said = [("fix the null handling", False), ("go ahead", True)]
    reply = "x" * 9000 + "Should I update the call sites too?"
    asked = autopilot.prompt("balanced", "Null handling", said, reply)
    assert autopilot.RULES["balanced"] in asked and autopilot.RULES["autonomous"] not in asked
    assert autopilot.ALWAYS_YOURS["balanced"] in asked and autopilot.ALWAYS_YOURS["autonomous"] not in asked
    assert "Session: Null handling" in asked
    assert "fix the null handling\n---\n(autopilot) go ahead" in asked
    assert "Should I update the call sites too?" in asked and "x" * 9000 not in asked  # the reply's end
    autonomous = autopilot.prompt("autonomous", "", said, reply)
    assert autopilot.RULES["autonomous"] in autonomous and autopilot.ALWAYS_YOURS["autonomous"] in autonomous
    assert autopilot.RULES["balanced"] not in autonomous and autopilot.ALWAYS_YOURS["balanced"] not in autonomous


def test_it_runs_your_claude_on_sonnet_at_medium_effort_with_no_tools_and_leaves_no_session():
    cmd = autopilot.command("claude")
    assert cmd[:2] == ["claude", "-p"] and "--no-session-persistence" in cmd
    assert cmd[cmd.index("--tools") + 1] == "" and cmd[cmd.index("--model") + 1] == "sonnet"
    assert cmd[cmd.index("--effort") + 1] == "medium"
    assert cmd[cmd.index("-n") + 1] == autopilot.SESSION_NAME
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == autopilot.SCHEMA


def result(answer, **over):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "structured_output": answer, **over})


def test_its_answer_is_a_message_to_send_or_a_line_for_you():
    go = autopilot.parse(result({"action": "continue", "message": " go ahead ", "reason": "in scope"}))
    assert go == Decision("go ahead", "in scope")
    yours = autopilot.parse(result({"action": "ask", "message": "Redis", "reason": "wants a pick:\n Redis or not"}))
    assert yours == Decision("", "wants a pick: Redis or not")  # asking sends nothing, whatever it wrote
    with pytest.raises(RuntimeError, match="out of credit"):
        autopilot.parse(result(None, is_error=True, result="out of credit"))
    with pytest.raises(RuntimeError, match="shape"):
        autopilot.parse(result(None))


def test_its_own_runs_are_left_off_the_wall(monkeypatch):
    from agentlens import live

    listed = [{"sessionId": "a", "name": autopilot.SESSION_NAME}, {"sessionId": "b", "name": "Parser work"}]

    class Done:
        returncode, stdout, stderr = 0, json.dumps(listed), ""

    monkeypatch.setattr(live.shutil, "which", lambda name: "claude")
    monkeypatch.setattr(live.subprocess, "run", lambda *a, **k: Done())
    assert [e["sessionId"] for e in live.open_sessions()] == ["b"]


def test_what_it_decided_is_kept_and_read_back(isolated_home, monkeypatch):
    first = autopilot.Entry(datetime(2026, 11, 4, 20, 0, tzinfo=timezone.utc), "s1", "Parser", "repo",
                            "Claude asked whether to add a test", "Yes, add a test for it.")
    left = autopilot.Entry(datetime(2026, 11, 4, 20, 5, tzinfo=timezone.utc), "s1", "Parser", "repo",
                           "Claude fixed the parser and asks if you want it committed")
    autopilot.record(first)
    with autopilot.log_path().open("a", encoding="utf-8") as f:
        f.write("not a decision\n")
    autopilot.record(left)
    assert autopilot.load() == [first, left]  # the line it can't read is left out
    monkeypatch.setattr(autopilot, "KEPT", 2)
    monkeypatch.setattr(autopilot, "TRIM_BYTES", 10)
    autopilot.record(first)  # past the size: only the newest are kept
    assert autopilot.load() == [left, first] and len(autopilot.log_path().read_text("utf-8").splitlines()) == 2
    assert autopilot.load_seen() is None
    autopilot.save_seen(left.at)
    assert autopilot.load_seen() == left.at


# A session acting on it --------------------------------------------------------------------

def test_where_it_answered_reads_as_the_pane_does():
    from agentlens.driver import DrivenSession

    d = DrivenSession("D:/work/Tubeless/.claude/worktrees/linux-build")
    d.worktree = "linux-build"
    assert d._entry("Claude asked whether to go on").where == "linux-build · Tubeless"  # not the folder twice
    assert DrivenSession("D:/work/demo")._entry("Claude asked whether to go on").where == "demo"


def test_it_says_what_it_decided_about_each_turn():
    decide, _ = fake_check(Decision("go ahead", "Claude asked whether to go on with cli.py"),
                           Decision("", "Claude fixed all three"))
    scripts = {"fix the null bugs": [text("Fixed parser.py. Should I go on with cli.py?"), done()],
               by_autopilot("go ahead"): [text("All three fixed."), done()]}
    decided = []

    async def go():
        d, _ = make(scripts, autopilot="balanced", decide=decide, on_pilot=decided.append)
        await d.start("fix the null bugs")
        await settle(lambda: len(decided) == 2)
        assert [(e.asked, e.said) for e in decided] == [("Claude asked whether to go on with cli.py", "go ahead"),
                                                        ("Claude fixed all three", "")]
        assert {(e.session_id, e.title, e.where) for e in decided} == {("sess-42", "fix the null bugs", "demo")}
        await d.close()
    asyncio.run(go())


def test_balanced_gives_the_go_ahead_then_leaves_the_finished_work_for_you():
    decide, asked = fake_check(Decision("go ahead", "in scope"), Decision("", "done: all three fixed"))
    scripts = {"fix the null bugs": [text("Fixed parser.py. Should I go on with cli.py?"), done()],
               by_autopilot("go ahead"): [text("All three fixed."), done()]}

    async def go():
        d, fakes = make(scripts, autopilot="balanced", decide=decide)
        await d.start("fix the null bugs")
        await settle(lambda: d.state == WAITING and d.detail == "done: all three fixed")
        assert fakes[0].sent == ["fix the null bugs", by_autopilot("go ahead")]
        assert [(e.kind, e.text, e.tag) for e in d.events] == [
            ("you", "fix the null bugs", ""), ("claude", "Fixed parser.py. Should I go on with cli.py?", ""),
            ("you", "go ahead", AUTOPILOT), ("claude", "All three fixed.", "")]  # no note: the reply says it
        assert asked == [("balanced", [("fix the null bugs", False)], "Fixed parser.py. Should I go on with cli.py?"),
                         ("balanced", [("fix the null bugs", False), ("go ahead", True)], "All three fixed.")]
        assert d.title == "fix the null bugs"  # its message doesn't name the session
        await d.close()
    asyncio.run(go())


def test_off_it_asks_nothing():
    decide, asked = fake_check(Decision("go ahead", ""))

    async def go():
        d, fakes = make({"hi": [text("Shall I go on?"), done()]}, decide=decide)
        await d.start("hi")
        await settle(lambda: d.state == WAITING and d.events[-1].kind == "claude")
        assert asked == [] and fakes[0].sent == ["hi"] and d.detail == "turn finished"
        await d.close()
    asyncio.run(go())


def test_after_a_few_go_aheads_in_a_row_one_is_yours(monkeypatch):
    monkeypatch.setitem(autopilot.IN_A_ROW, "autonomous", 2)
    decide, asked = fake_check(Decision("yes", "more of the same"))
    scripts = {"go": [text("Step done. Next?"), done()], by_autopilot("yes"): [text("Step done. Next?"), done()],
               "keep going": [text("Step done. Next?"), done()]}

    async def go():
        d, fakes = make(scripts, autopilot="autonomous", decide=decide)
        await d.start("go")
        await settle(lambda: d.state == WAITING and d.events[-1].kind == "notice")
        assert fakes[0].sent == ["go", by_autopilot("yes"), by_autopilot("yes")]
        assert "answered 2 times in a row" in d.events[-1].detail and "It would have said: yes" in d.events[-1].detail
        await d.send("keep going")  # yours: it counts again from here
        await settle(lambda: len(fakes[0].sent) == 5)
        assert fakes[0].sent[-1] == by_autopilot("yes") and d.piloted == 1
        await d.close()
    asyncio.run(go())


async def reading(d):
    await settle(lambda: d.state == WORKING and d.detail == "autopilot is reading the reply")


def never():
    async def decide(level, title, said, reply):
        await asyncio.sleep(60)
    return decide


def test_your_message_while_it_reads_goes_instead_of_its_own():
    async def go():
        d, fakes = make({"go": [text("Next?"), done()]}, autopilot="balanced", decide=never())
        await d.start("go")
        await reading(d)
        await d.send("no, do the tests first")
        assert fakes[0].sent == ["go", "no, do the tests first"]
        assert d.events[-1].tag == "" and d._piloting is None  # not "sent while Claude was working"
        await d.close()
    asyncio.run(go())


def test_stopping_the_turn_while_it_reads_leaves_it_to_you():
    async def go():
        d, fakes = make({"go": [text("Next?"), done()]}, autopilot="balanced", decide=never())
        await d.start("go")
        await reading(d)
        await d.interrupt()
        assert d.state == WAITING and d.detail == "turn finished" and fakes[0].sent == ["go"]
        assert d.events[-1].text.startswith("Autopilot stopped")
        await d.close()
    asyncio.run(go())


def test_a_turn_you_stopped_is_yours():
    decide, asked = fake_check(Decision("go on", ""))

    async def go():
        d, _ = make({"go": [("pause", 0.2), text("Half done."), done()]}, autopilot="balanced", decide=decide)
        await d.start("go")
        await d.interrupt()  # the fake goes on to the end of its turn, as Claude Code does after an interrupt
        await settle(lambda: d.state == WAITING)
        assert asked == [] and d.detail == "turn finished"
        await d.close()
    asyncio.run(go())


def test_when_it_cant_decide_the_turn_is_yours():
    decide, _ = fake_check(RuntimeError("out of credit"))

    async def go():
        d, fakes = make({"go": [text("Next?"), done()]}, autopilot="balanced", decide=decide)
        await d.start("go")
        await settle(lambda: d.state == WAITING and d.events[-1].kind == "notice")
        assert d.events[-1].text == "Autopilot couldn't decide (out of credit), so this one is yours."
        assert fakes[0].sent == ["go"] and d.detail == "turn finished"
        await d.close()
    asyncio.run(go())


def test_a_slash_command_is_not_a_turn_for_it():
    decide, asked = fake_check(Decision("go on", ""))

    async def go():
        d, _ = make({"/context": [text("Context: 12k tokens"), done()]}, autopilot="balanced", decide=decide)
        await d.start("/context")
        await settle(lambda: d.state == WAITING and d.events[-1].kind == "claude")
        assert asked == []
        await d.close()
    asyncio.run(go())


def test_turned_on_while_a_session_waits_it_reads_that_turn():
    decide, asked = fake_check(Decision("go ahead", "in scope"))

    async def go():
        d, fakes = make({"go": [text("Shall I go on?"), done()]}, decide=decide)
        await d.start("go")
        await settle(lambda: d.state == WAITING and d.events[-1].kind == "claude")
        d.set_autopilot("balanced")
        await settle(lambda: len(fakes[0].sent) == 2)
        assert fakes[0].sent[1] == by_autopilot("go ahead") and asked[0][0] == "balanced"
        await d.close()
    asyncio.run(go())


def test_a_turn_claude_code_started_itself_is_yours():
    # Another session's message, or a background task finishing: your messages say nothing of it.
    decide, asked = fake_check(Decision("", "done"), Decision("Done. Restart agentlens.", "echoed"))

    async def go():
        d, fakes = make({"go": [text("Done with that."), done()], "next": [text("Shall I go on?"), done()]},
                        autopilot="autonomous", decide=decide)
        await d.start("go")
        await settle(lambda: d.state == WAITING and len(asked) == 1)
        await fakes[0]._play([text("Not me: this session works on something else."), done()])
        await settle(lambda: d.state == WAITING and d.events[-1].text.startswith("Not me"))
        await asyncio.sleep(0.1)
        assert len(asked) == 1 and fakes[0].sent == ["go"] and d.detail == "turn finished"
        d.set_autopilot("balanced")  # p on it, waiting: still not one to read
        await asyncio.sleep(0.1)
        assert len(asked) == 1 and d.state == WAITING
        await d.send("next")  # yours again: it reads the turn your message starts
        await settle(lambda: len(asked) == 2)
        assert asked[1][2] == "Shall I go on?"
        await d.close()
    asyncio.run(go())


def finished(origin):
    """The end of a turn something other than a message from agentlens started, as Claude Code says it."""
    return ResultMessage("success", 10, 10, False, 1, "sess-42", origin=origin)


def test_while_claude_waits_on_its_own_work_it_answers_nothing_and_doesnt_call_you():
    decide, asked = fake_check(Decision("", "Claude is waiting on the test run", waiting=True),
                               Decision("Yes, push it.", "Claude asked whether to push"))
    decided = []

    async def go():
        d, fakes = make({"run the tests": [text("They're running in the background; I'll report back."), done()]},
                        autopilot="autonomous", decide=decide, on_pilot=decided.append)
        await d.start("run the tests")
        await settle(lambda: d.state == BACKGROUND)
        assert d.detail == "Claude is waiting on the test run" and fakes[0].sent == ["run the tests"]
        assert decided == []  # not an answer, nor one left for you
        # The run finishing starts Claude's next turn, which carries on the work: it reads that one.
        await fakes[0]._play([text("All 40 pass. Shall I push?"), finished({"kind": "task-notification"})])
        await settle(lambda: len(fakes[0].sent) == 2)
        assert fakes[0].sent[1] == by_autopilot("Yes, push it.")
        assert "All 40 pass. Shall I push?" in asked[1][2]
        await d.close()
    asyncio.run(go())


@pytest.mark.parametrize("origin", [{"kind": "peer", "from": "uds:x", "name": "Bug fixes"},
                                    {"kind": "task-notification", "subkind": "peer-send-message"},
                                    {"kind": "task-notification", "subkind": "scheduled-trigger"}])
def test_a_turn_another_sessions_message_or_a_schedule_started_is_yours(origin):
    decide, asked = fake_check(Decision("", "done"))

    async def go():
        d, fakes = make({"go": [text("Done with that."), done()]}, autopilot="autonomous", decide=decide)
        await d.start("go")
        await settle(lambda: d.state == WAITING and len(asked) == 1)
        await fakes[0]._play([text("Sure, here's the branch name."), finished(origin)])
        await settle(lambda: d.state == WAITING and d.events[-1].text.startswith("Sure"))
        await asyncio.sleep(0.1)
        assert len(asked) == 1 and d.detail == "turn finished"
        await d.close()
    asyncio.run(go())


def test_it_runs_in_a_folder_of_its_own_so_no_projects_notes_come_with_it(isolated_home, monkeypatch):
    ran = {}

    class Done:
        returncode, stderr = 0, ""
        stdout = result({"action": "wait", "message": "ignored", "reason": "Claude is waiting on the build"})

    def run(cmd, **kwargs):
        ran.update(kwargs)
        return Done()

    monkeypatch.setattr(autopilot.subprocess, "run", run)
    decision = autopilot.decide("autonomous", "Build", [("build it", False)], "Building…", cli_path="claude")
    assert decision == Decision("", "Claude is waiting on the build", waiting=True)
    assert ran["cwd"] == isolated_home / "autopilot" and ran["cwd"].is_dir() and not any(ran["cwd"].iterdir())


def test_your_setting_changing_starts_at_the_next_turn_not_the_one_waiting_on_you():
    decide, asked = fake_check(Decision("go ahead", "in scope"))

    async def go():
        d, fakes = make({"go": [text("Shall I go on?"), done()]}, decide=decide)
        await d.start("go")
        await settle(lambda: d.state == WAITING and d.events[-1].kind == "claude")
        d.set_autopilot("balanced", read_now=False)
        await asyncio.sleep(0.1)
        assert d.autopilot == "balanced" and asked == [] and fakes[0].sent == ["go"] and d.state == WAITING
        await d.close()
    asyncio.run(go())


def test_its_messages_show_as_its_own():
    events = [Event(datetime.now(timezone.utc), "you", "go ahead", "go ahead", tag=AUTOPILOT)]
    console = Console(width=80, record=True)
    console.print(conversation(events, Palette()))
    shown = console.export_text()
    assert "Autopilot" in shown and "for you, from Sonnet" in shown and "You" not in shown


def test_read_back_from_its_transcript_its_messages_are_still_its_own(projects):
    # As Claude Code writes a message sent through the SDK: plain text, no origin, the note after it.
    sent = {"type": "user", "timestamp": at(2), "message": {"role": "user", "content": by_autopilot("go ahead")}}
    path = write_jsonl(projects / "demo" / "sess-1.jsonl", common([
        human(0, "fix the null bugs"), said(1, "Fixed one. Go on?"), sent, said(3, "All fixed."),
        human(10, "thanks"), said(11, "Any time.")]))
    session, events = load_tail(path, 1_000_000)
    assert [(e.kind, e.detail, e.tag) for e in events["main"] if e.kind == "you"] == [
        ("you", "fix the null bugs", ""), ("you", "go ahead", AUTOPILOT), ("you", "thanks", "")]
    assert len(session.human_prompts) == 2  # yours, not its
    waits = [(s.kind, round(s.minutes)) for s in session.main.spans]
    assert waits[:3] == [(MODEL, 1), (MODEL, 1), (MODEL, 1)]  # the minute before its message wasn't you
    assert (WAITING_ON_YOU, 7) in waits


def test_what_it_sent_while_it_was_called_panopsis_is_still_its_own():
    sent_then = "go ahead\n\n" + NOTE.replace("agentlens's", "panopsis's")
    assert autopilot.said_by_it(sent_then) == "go ahead"
    assert autopilot.said_by_it("go ahead") is None


# In the app --------------------------------------------------------------------------------

def test_p_gives_a_pane_its_own_autopilot_until_it_comes_round_to_your_setting(projects, tmp_path, isolated_home):
    app, folder, _ = app_with_session(projects, tmp_path, {"go": [text("Working."), done()]})
    app.decide, _ = fake_check(Decision("", "done"))

    def saved():
        return [(d["session_id"], d["own_autopilot"])
                for d in config_mod.load(isolated_home / "config.yaml").driven_sessions]

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            driven = await started(app, pilot, folder, "go")
            assert (driven.autopilot, driven.own_autopilot) == ("off", False) and "autopilot" not in border(app, driven)
            app._focus_key(driven.key)
            await pilot.press("p")
            assert (driven.autopilot, driven.own_autopilot) == ("balanced", True)
            await pilot.press("p")
            assert (driven.autopilot, driven.own_autopilot) == ("autonomous", True)
            await until(pilot, lambda: driven.remembered)
            assert saved() == [("sess-42", "autonomous")]
            await pilot.pause(0.1)
            assert "autopilot autonomous, this pane" in border(app, driven)
            assert "autopilot autonomous for this pane, p changes it" in placeholder(driven)
            await pilot.press("p")  # round to your setting's, off: it follows your setting again
            assert (driven.autopilot, driven.own_autopilot) == ("off", False) and saved() == [("sess-42", "")]
            await pilot.pause(0.1)
            assert "autopilot" not in border(app, driven)

    asyncio.run(go())


def test_your_setting_is_for_every_session_here_but_those_with_their_own(projects, tmp_path):
    app, folder, _ = app_with_session(projects, tmp_path, {"go": [text("Shall I go on?"), done()]})
    app.decide, asked = fake_check(Decision("go ahead", "in scope"))

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            driven = await started(app, pilot, folder, "go")
            await until(pilot, lambda: driven.state == WAITING)
            own = app.open_driven(str(folder), resume="sess-7", start=False, own_autopilot="off", force=True)
            await pilot.pause(0.2)  # its pane mounted
            app.change_setting("autopilot", "autonomous")
            assert (driven.autopilot, own.autopilot) == ("autonomous", "off")
            await pilot.pause(0.2)
            assert asked == [] and driven.state == WAITING  # the turn already waiting on you stays yours
            assert border(app, driven).endswith(" autopilot autonomous")  # yours, not this pane's own
            assert "autopilot off, this pane" in border(app, own)
            new = await started(app, pilot, folder)
            assert (new.autopilot, new.own_autopilot) == ("autonomous", False)

    asyncio.run(go())


def test_a_session_keeps_its_own_autopilot_through_claude_code_and_a_failed_detach(
        projects, tmp_path, isolated_home, monkeypatch):
    import contextlib
    import subprocess

    app, folder, _ = app_with_session(projects, tmp_path)
    app.decide, _ = fake_check(Decision("", "done"))
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(tui_mod.terminal, "set_title", lambda *a, **kw: None)
    monkeypatch.setattr(tui_mod, "background_resume", lambda *a, **kw: (False, "Workspace not trusted."))

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            monkeypatch.setattr(app, "suspend", contextlib.nullcontext)
            app.config.cli_path = "claude"
            app.config.autopilot = "autonomous"
            driven = await started(app, pilot, folder)
            app._focus_key(driven.key)
            await pilot.press("p")  # from your setting's autonomous to this one's own, off
            await app._handoff_driven(driven)  # c, then back from Claude Code
            await pilot.pause(0.1)
            [driven] = app.driven.values()
            assert (driven.autopilot, driven.own_autopilot) == ("off", True)
            await until(pilot, lambda: driven.remembered)
            await app._detach(driven, "keep going")  # Claude Code won't take it, so it stays here
            await pilot.pause(0.1)
            [driven] = app.driven.values()
            assert (driven.autopilot, driven.own_autopilot) == ("off", True)
            await until(pilot, lambda: driven.remembered)
            saved = config_mod.load(isolated_home / "config.yaml").driven_sessions
            assert [(d["session_id"], d["own_autopilot"]) for d in saved] == [("sess-42", "off")]

    asyncio.run(go())


def test_d_lists_what_it_answered_and_enter_opens_that_session(projects, tmp_path, isolated_home):
    app, folder, _ = app_with_session(projects, tmp_path, {
        "go": [text("Fixed parser.py. Should I go on with cli.py?"), done()],
        by_autopilot("Yes, go on with cli.py."): [text("All three fixed."), done()]})
    app.decide, _ = fake_check(Decision("Yes, go on with cli.py.", "Claude asked whether to go on with cli.py"),
                               Decision("", "Claude fixed all three"))

    def summary():
        return app.query_one("#summary").content.plain

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            app.config.autopilot = "balanced"
            driven = await started(app, pilot, folder, "go")
            await until(pilot, lambda: len(app.pilot_log) == 2)
            await pilot.pause(0.1)
            assert "autopilot answered 1" in summary()
            assert len(autopilot.load()) == 2  # kept for the next launch too
            await pilot.press("D")
            await pilot.pause(0.2)
            screen = app.screen
            assert isinstance(screen, tui_mod.AutopilotScreen)
            head = screen.query_one("#pilot-head").content.plain
            assert "Today it answered 1 and left 1 for you" in head
            answers = screen.query_one("#pilot-answers")
            [shown] = [answers.get_option_at_index(i).prompt.plain for i in range(answers.option_count)]
            assert "Claude asked whether to go on with cli.py" in shown and "→ Yes, go on with cli.py." in shown
            assert shown.startswith("● ") and "go" in shown  # new, in the session it was in
            assert app.pilot_new() == 0 and "autopilot answered" not in summary()  # seen now
            assert autopilot.load_seen() == app.pilot_seen
            await pilot.press("enter")
            await until(pilot, lambda: isinstance(app.screen, tui_mod.ZoomScreen))
            assert app.screen.session_id == driven.key

    asyncio.run(go())


def test_the_setting_is_saved_and_a_wrong_one_is_off(isolated_home):
    path = isolated_home / "config.yaml"
    cfg = config_mod.Config(path=path, autopilot="autonomous")
    cfg.remember_driven("s1", "D:/w", "Parser", "default", "balanced")
    config_mod.save(cfg)
    loaded = config_mod.load(path)
    assert loaded.autopilot == "autonomous" and loaded.driven_sessions[0]["own_autopilot"] == "balanced"
    # Saved before the setting was for every session, its level was the setting's: it follows yours.
    path.write_text(path.read_text(encoding="utf-8").replace("own_autopilot: balanced", "autopilot: balanced"),
                    encoding="utf-8")
    assert config_mod.load(path).driven_sessions[0]["own_autopilot"] == ""
    path.write_text(path.read_text(encoding="utf-8").replace("autopilot: autonomous", "autopilot: always"),
                    encoding="utf-8")
    assert config_mod.load(path).autopilot == "off"
