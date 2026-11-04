"""Autopilot: what it asks Haiku, and how a session panopsis runs acts on the answer. No real CLI."""

import asyncio
import json
from datetime import datetime, timezone

import pytest
from rich.console import Console
from conftest import at, common, human, text as said, write_jsonl
from test_driver import app_with_session, done, make, settle, started, text, until

from panopsis import autopilot, config as config_mod
from panopsis.adapters.claude_transcripts import load_tail
from panopsis.autopilot import NOTE, Decision
from panopsis.driver import WAITING, WORKING
from panopsis.feed import Palette, conversation
from panopsis.model import AUTOPILOT, MODEL, WAITING_ON_YOU, Event


def fake_haiku(*answers):
    """Autopilot's Haiku call, giving `answers` in turn (the last one again after that), and what it was asked."""
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


# What it asks Haiku ------------------------------------------------------------------------

def test_the_prompt_has_the_levels_rules_your_messages_and_the_end_of_the_reply():
    said = [("fix the null handling", False), ("go ahead", True)]
    reply = "x" * 9000 + "Should I update the call sites too?"
    asked = autopilot.prompt("balanced", "Null handling", said, reply)
    assert autopilot.RULES["balanced"] in asked and autopilot.RULES["autonomous"] not in asked
    assert autopilot.ALWAYS_YOURS in asked and "Session: Null handling" in asked
    assert "fix the null handling\n---\n(autopilot) go ahead" in asked
    assert "Should I update the call sites too?" in asked and "x" * 9000 not in asked  # the reply's end
    assert autopilot.RULES["autonomous"] in autopilot.prompt("autonomous", "", said, reply)


def test_it_runs_your_claude_on_haiku_with_no_tools_and_leaves_no_session():
    cmd = autopilot.command("claude")
    assert cmd[:2] == ["claude", "-p"] and "--no-session-persistence" in cmd
    assert cmd[cmd.index("--tools") + 1] == "" and cmd[cmd.index("--model") + 1] == "haiku"
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
    from panopsis import live

    listed = [{"sessionId": "a", "name": autopilot.SESSION_NAME}, {"sessionId": "b", "name": "Parser work"}]

    class Done:
        returncode, stdout, stderr = 0, json.dumps(listed), ""

    monkeypatch.setattr(live.shutil, "which", lambda name: "claude")
    monkeypatch.setattr(live.subprocess, "run", lambda *a, **k: Done())
    assert [e["sessionId"] for e in live.open_sessions()] == ["b"]


# A session acting on it --------------------------------------------------------------------

def test_balanced_gives_the_go_ahead_then_leaves_the_finished_work_for_you():
    decide, asked = fake_haiku(Decision("go ahead", "in scope"), Decision("", "done: all three fixed"))
    scripts = {"fix the null bugs": [text("Fixed parser.py. Should I go on with cli.py?"), done()],
               by_autopilot("go ahead"): [text("All three fixed."), done()]}

    async def go():
        d, fakes = make(scripts, autopilot="balanced", decide=decide)
        await d.start("fix the null bugs")
        await settle(lambda: d.state == WAITING and d.detail == "done: all three fixed")
        assert fakes[0].sent == ["fix the null bugs", by_autopilot("go ahead")]
        assert [(e.kind, e.text, e.tag) for e in d.events] == [
            ("you", "fix the null bugs", ""), ("claude", "Fixed parser.py. Should I go on with cli.py?", ""),
            ("you", "go ahead", AUTOPILOT), ("claude", "All three fixed.", ""),
            ("notice", "Autopilot left this for you: done: all three fixed", "")]
        assert asked == [("balanced", [("fix the null bugs", False)], "Fixed parser.py. Should I go on with cli.py?"),
                         ("balanced", [("fix the null bugs", False), ("go ahead", True)], "All three fixed.")]
        assert d.title == "fix the null bugs"  # its message doesn't name the session
        await d.close()
    asyncio.run(go())


def test_off_it_asks_nothing():
    decide, asked = fake_haiku(Decision("go ahead", ""))

    async def go():
        d, fakes = make({"hi": [text("Shall I go on?"), done()]}, decide=decide)
        await d.start("hi")
        await settle(lambda: d.state == WAITING and d.events[-1].kind == "claude")
        assert asked == [] and fakes[0].sent == ["hi"] and d.detail == "turn finished"
        await d.close()
    asyncio.run(go())


def test_after_a_few_go_aheads_in_a_row_one_is_yours(monkeypatch):
    monkeypatch.setattr(autopilot, "IN_A_ROW", 2)
    decide, asked = fake_haiku(Decision("yes", "more of the same"))
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
    decide, asked = fake_haiku(Decision("go on", ""))

    async def go():
        d, _ = make({"go": [("pause", 0.2), text("Half done."), done()]}, autopilot="balanced", decide=decide)
        await d.start("go")
        await d.interrupt()  # the fake goes on to the end of its turn, as Claude Code does after an interrupt
        await settle(lambda: d.state == WAITING)
        assert asked == [] and d.detail == "turn finished"
        await d.close()
    asyncio.run(go())


def test_when_it_cant_decide_the_turn_is_yours():
    decide, _ = fake_haiku(RuntimeError("out of credit"))

    async def go():
        d, fakes = make({"go": [text("Next?"), done()]}, autopilot="balanced", decide=decide)
        await d.start("go")
        await settle(lambda: d.state == WAITING and d.events[-1].kind == "notice")
        assert d.events[-1].text == "Autopilot couldn't decide (out of credit), so this one is yours."
        assert fakes[0].sent == ["go"] and d.detail == "turn finished"
        await d.close()
    asyncio.run(go())


def test_a_slash_command_is_not_a_turn_for_it():
    decide, asked = fake_haiku(Decision("go on", ""))

    async def go():
        d, _ = make({"/context": [text("Context: 12k tokens"), done()]}, autopilot="balanced", decide=decide)
        await d.start("/context")
        await settle(lambda: d.state == WAITING and d.events[-1].kind == "claude")
        assert asked == []
        await d.close()
    asyncio.run(go())


def test_turned_on_while_a_session_waits_it_reads_that_turn():
    decide, asked = fake_haiku(Decision("go ahead", "in scope"))

    async def go():
        d, fakes = make({"go": [text("Shall I go on?"), done()]}, decide=decide)
        await d.start("go")
        await settle(lambda: d.state == WAITING and d.events[-1].kind == "claude")
        d.set_autopilot("balanced")
        await settle(lambda: len(fakes[0].sent) == 2)
        assert fakes[0].sent[1] == by_autopilot("go ahead") and asked[0][0] == "balanced"
        await d.close()
    asyncio.run(go())


def test_its_messages_show_as_its_own():
    events = [Event(datetime.now(timezone.utc), "you", "go ahead", "go ahead", tag=AUTOPILOT)]
    console = Console(width=80, record=True)
    console.print(conversation(events, Palette()))
    shown = console.export_text()
    assert "Autopilot" in shown and "for you, from Haiku" in shown and "You" not in shown


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


# In the app --------------------------------------------------------------------------------

def test_p_turns_a_sessions_autopilot_round_and_it_comes_back_so(projects, tmp_path, isolated_home):
    app, folder, _ = app_with_session(projects, tmp_path, {"go": [text("Working."), done()]})
    app.decide, _ = fake_haiku(Decision("", "done"))

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            driven = await started(app, pilot, folder, "go")
            assert driven.autopilot == "off"
            app._focus_key(driven.key)
            await pilot.press("p")
            assert driven.autopilot == "balanced"
            await pilot.press("p")
            assert driven.autopilot == "autonomous"
            await until(pilot, lambda: driven.remembered)
            saved = config_mod.load(isolated_home / "config.yaml").driven_sessions
            assert [(d["session_id"], d["autopilot"]) for d in saved] == [("sess-42", "autonomous")]
            await pilot.press("p")
            assert driven.autopilot == "off"

    asyncio.run(go())


def test_new_sessions_start_with_the_settings_autopilot(projects, tmp_path):
    app, folder, _ = app_with_session(projects, tmp_path)
    app.decide, _ = fake_haiku(Decision("", "done"))

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            app.config.autopilot = "balanced"
            driven = await started(app, pilot, folder)
            assert driven.autopilot == "balanced"
            restored = app.open_driven(str(folder), resume="sess-7", start=False, autopilot="autonomous", force=True)
            assert restored.autopilot == "autonomous"
            await pilot.pause(0.2)  # its pane mounted

    asyncio.run(go())



def test_a_session_keeps_its_own_autopilot_through_claude_code_and_a_failed_detach(
        projects, tmp_path, isolated_home, monkeypatch):
    import contextlib
    import subprocess

    from panopsis import tui as tui_mod

    app, folder, _ = app_with_session(projects, tmp_path)
    app.decide, _ = fake_haiku(Decision("", "done"))
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: None)
    monkeypatch.setattr(tui_mod.terminal, "set_title", lambda *a, **kw: None)
    monkeypatch.setattr(tui_mod, "background_resume", lambda *a, **kw: (False, "Workspace not trusted."))

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            monkeypatch.setattr(app, "suspend", contextlib.nullcontext)
            app.config.cli_path = "claude"
            app.config.autopilot = "autonomous"
            driven = await started(app, pilot, folder)
            driven.set_autopilot("off")  # p on this one, against the setting
            await app._handoff_driven(driven)  # c, then back from Claude Code
            await pilot.pause(0.1)
            [driven] = app.driven.values()
            assert driven.autopilot == "off"
            await until(pilot, lambda: driven.remembered)
            await app._detach(driven, "keep going")  # Claude Code won't take it, so it stays here
            await pilot.pause(0.1)
            [driven] = app.driven.values()
            assert driven.autopilot == "off"
            await until(pilot, lambda: driven.remembered)
            saved = config_mod.load(isolated_home / "config.yaml").driven_sessions
            assert [(d["session_id"], d["autopilot"]) for d in saved] == [("sess-42", "off")]

    asyncio.run(go())

def test_the_setting_is_saved_and_a_wrong_one_is_off(isolated_home):
    path = isolated_home / "config.yaml"
    cfg = config_mod.Config(path=path, autopilot="autonomous")
    cfg.remember_driven("s1", "D:/w", "Parser", "default", "balanced")
    config_mod.save(cfg)
    loaded = config_mod.load(path)
    assert loaded.autopilot == "autonomous" and loaded.driven_sessions[0]["autopilot"] == "balanced"
    path.write_text(path.read_text(encoding="utf-8").replace("autopilot: autonomous", "autopilot: always"),
                    encoding="utf-8")
    assert config_mod.load(path).autopilot == "off"
