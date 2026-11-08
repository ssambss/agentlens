"""Where a task session's turns can leave a goal stuck: a pane, the lead's or a task's, in a state that nothing
moves on and that doesn't call you. A task's turn that fails, that you stop, that Claude Code exiting cuts off,
or that something other than a message from agentlens starts; a task closed, finished on a prompt, or brought
back by a restart.

In the app, with fake Claude Code clients. Where the end of a turn matters, its script stops short of it and
the test plays the rest (the fake's _play), as Claude Code would send it. A case that gets stuck is an xfail
written for what should happen: strict, so the fix that makes it pass says so. Its scene is checked with
reach() and given(), which raise Setup, not an AssertionError: a scene that didn't come about fails the test
rather than passing for the failure the xfail expects.

What these take Claude Code to do. Checked by hand against Claude Code 2.1.318 (haiku, no tools or settings):
- A turn you stop (x) ends at once with "[Request interrupted by user]" and a ResultMessage that's an error
  (error_during_execution): stopped(), played here.
- Killed mid-turn, it sends no ResultMessage, and reading it fails (ProcessError, exit code 1): "breaks" here.
  Its stream ending ("exits") is played too.
- Resumed, it's the same session, starts nothing by itself, and has none of the reply it was cut off in.
Not checked:
- A turn it starts itself (a background task ending, another session's message) ends with a ResultMessage
  whose origin says what began it: a background task's as test_driver's notified() gives it, a peer's here
  the same as the peer's message.
- A message sent to it mid-turn is read at a step of that turn (one ResultMessage), or as the next turn (one
  each): both are played here.
"""

import asyncio

import pytest
from claude_agent_sdk import ResultMessage, UserMessage
from test_coordinator import Prefixes, said, tasks
from test_driver import app_with_session, background_command, done, notified, task_done, text, until

from agentlens import config as config_mod, coordinator
from agentlens.coordinator import RUNNING
from agentlens.driver import APPROVAL, BACKGROUND, PAUSED, PERMISSION, QUESTION, WAITING, WORKING
from agentlens.tui_driven import ChoiceScreen

SCRIPTS = {
    "Split the parser": [text("I'll plan it."), done()],  # your goal, to the lead
    "Do Parser.": [text("Split it. Shall I add tests?"), done()],  # the lead's brief, to its session
    "Parser (task 1) ended a turn": [text("Telling it to add tests.")],  # agentlens, to the lead: the test ends it
    "Add tests.": [text("Adding them.")],  # the lead's answer, to its session: the test ends that turn too
}
OWN_WORK = {"kind": "task-notification", "producer": "session-task"}  # a turn a background task's end began


class Setup(Exception):
    """The scene a test sets didn't come about: a failure of the test, never the one an xfail expects."""


async def reach(pilot, condition, what):
    try:
        await until(pilot, condition)
    except AssertionError:
        raise Setup(f"never got there: {what}") from None


def given(condition, what):
    if not condition:
        raise Setup(f"not so: {what}")


def app_for(projects, tmp_path, scripts=None):
    app, folder, fakes = app_with_session(projects, tmp_path, Prefixes({**SCRIPTS, **(scripts or {})}))
    app.config.coordinator, app.config.autopilot = True, "autonomous"
    return app, folder, fakes


def fake_of(fakes, d):
    """The fake Claude Code a session runs now: the last one started for it."""
    return [f for f in fakes if f.options.can_use_tool.__self__ is d][-1]


def sent_to(fakes, d):
    """What agentlens has sent the session's Claude Code, since it last started; nothing if it hasn't."""
    started = [f for f in fakes if f.options.can_use_tool.__self__ is d]
    return started[-1].sent if started else []


def rings(app, d):
    """Its pane, as the wall shows it now, waits on you and calls you."""
    s = app.sessions.get(d.key)
    return s is not None and s.state == d.state and app.needs_you(s)


def ended(origin=None, error=""):
    """A turn's end, as Claude Code says it: what began it, and its error if it failed."""
    return ResultMessage("error_during_execution" if error else "success", 10, 10, bool(error), 1, "sess-42",
                         total_cost_usd=0.01, errors=[error] if error else None, origin=origin)


def stopped():
    """What Claude Code sends as a turn you stopped ends, as Claude Code 2.1.318 does."""
    return [UserMessage("[Request interrupted by user]"),
            ended(error="[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=null")]


async def goal_with(app, pilot, folder, *titles):
    """A goal's lead, its first turn over, and a session started for each task, as its tools would."""
    lead = app.start_goal(str(folder), "Split the parser", "acceptEdits", "")
    await reach(pilot, lambda: lead.state == WAITING and lead.events[-1].kind == "claude", "the lead's first turn")
    goal = app.coordinator.role(lead.key)[0]
    app.coordinator.add_tasks(goal, tasks(*titles))
    for n in range(1, len(titles) + 1):
        given(said(app.coordinator.start_task(goal, n)).startswith("Started"), f"task {n} started")
    return lead, goal, [d for d in app.driven.values() if d is not lead]


async def reading_its_reply(app, pilot, folder):
    """The task's first turn ended and went to the lead, which is mid-turn on it: the task waits on the lead."""
    lead, goal, [worker] = await goal_with(app, pilot, folder, "Parser")
    await reach(pilot, lambda: lead.events[-1].text == "Telling it to add tests." and goal.task(1).waiting_on_lead
                and worker.state == BACKGROUND, "the lead reading the task's reply")
    return lead, goal, worker


async def lead_quiet_on(app, pilot, folder, fakes):
    """The lead answered the task's first reply and ended its turn quietly, as the task is at work on that answer:
    only the task's turn ending brings the lead back."""
    lead, goal, worker = await reading_its_reply(app, pilot, folder)
    given(said(app.coordinator.message_task(goal, 1, "Add tests.")) == "Sent to task 1.", "the lead answered it")
    await reach(pilot, lambda: worker.events[-1].text == "Adding them.", "the task at work on the answer")
    await fake_of(fakes, lead)._play([done()])
    await reach(pilot, lambda: lead.state == BACKGROUND, "the lead's turn ended quietly")
    given(lead.detail == "coordinating: 1 running, 0 of 1 done", "the lead waits on the task")
    await reach(pilot, lambda: app.query(f"#s-{worker.key}"), "the task's pane on the wall")
    return lead, goal, worker


async def close_with_w(app, pilot, d):
    app._focus_key(d.key)
    await pilot.pause(0.1)
    await pilot.press("w")  # it always asks first
    await reach(pilot, lambda: isinstance(app.screen, ChoiceScreen), "asked whether to close it")
    await pilot.press("w")
    await reach(pilot, lambda: d.key not in app.driven, "the task's session closed")


# A turn's end, and what began it -------------------------------------------------------------

def test_a_tasks_turn_that_fails_reaches_the_lead_with_its_error(projects, tmp_path):
    app, folder, fakes = app_for(projects, tmp_path, {
        "Do Parser.": [text("Splitting it."), ended(error="API Error: 529 Overloaded")],
        "Parser (task 1) ended a turn": [text("It hit an API error. Shall I start it again?"), done()],
    })

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, goal, [worker] = await goal_with(app, pilot, folder, "Parser")
            await until(pilot, lambda: len(sent_to(fakes, lead)) == 2)
            told = sent_to(fakes, lead)[1]
            assert told.startswith("Parser (task 1) ended a turn, with an error: API Error: 529 Overloaded.")
            assert "Splitting it." in told
            assert any(e.kind == "error" and "529" in e.text for e in worker.events)  # in its pane too
            # It waits on the lead; the lead, with nothing at work, calls you with what it made of it.
            await until(pilot, lambda: rings(app, lead) and worker.state == BACKGROUND)
            assert lead.detail == "It hit an API error." and goal.task(1).waiting_on_lead
            assert worker.detail == "the coordinator is reading this" and not rings(app, worker)

    asyncio.run(go())


BEGUN = {  # what starts a turn that no message from agentlens did: what Claude Code sends first, and its origin
    "by-its-own-work": ([task_done("bt1"), notified("bt1")], OWN_WORK),
    "by-a-peer": ([UserMessage("<peer message>", origin={"kind": "peer", "from": "uds:x", "name": "Bug fixes",
                                                          "body": "Does the parser take empty input?"})],
                  {"kind": "peer"}),
    "by-a-relayed-message": ([UserMessage("Does the parser take empty input?", origin={
        "kind": "task-notification", "subkind": "peer-send-message", "name": "Bug fixes"})],
        {"kind": "task-notification", "subkind": "peer-send-message"}),
}


@pytest.mark.parametrize("begun", list(BEGUN))
def test_a_task_turn_that_no_message_from_agentlens_started_reaches_the_lead(projects, tmp_path, begun):
    """The background tests it left running finish, or another session writes to it: Claude Code starts a turn
    by itself, and what it says then goes to the lead as any of its turns does."""
    app, folder, fakes = app_for(projects, tmp_path, {
        "Do Parser.": [*background_command(), text("Split it. The tests run in the background."), done()],
        "Parser (task 1) ended a turn": [text("Noted."), done()],
    })

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, goal, [worker] = await goal_with(app, pilot, folder, "Parser")
            await until(pilot, lambda: len(sent_to(fakes, lead)) == 2 and lead.events[-1].text == "Noted."
                        and rings(app, lead))
            first, origin = BEGUN[begun]
            await fake_of(fakes, worker)._play([*first, text("Told them: it does now."), ended(origin)])
            await until(pilot, lambda: len(sent_to(fakes, lead)) == 3)
            told = sent_to(fakes, lead)[2]
            assert told.startswith("Parser (task 1) ended a turn.") and "Told them: it does now." in told
            await until(pilot, lambda: worker.state == BACKGROUND)
            assert worker.detail == "the coordinator is reading this" and goal.task(1).waiting_on_lead

    asyncio.run(go())


def test_a_task_that_reports_twice_before_the_lead_answers_still_waits_on_it(projects, tmp_path):
    """It reports, and its background tests finish while the lead still reads that: both go to the lead, the task
    waits on the lead throughout, and the lead's turn, answering neither, is yours."""
    app, folder, fakes = app_for(projects, tmp_path, {
        "Do Parser.": [*background_command(), text("Split it. The tests run in the background."), done()],
        "Parser (task 1) ended a turn": [text("Reading it.")],  # the test ends its turn
    })

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, goal, [worker] = await goal_with(app, pilot, folder, "Parser")
            await until(pilot, lambda: lead.events[-1].text == "Reading it." and goal.task(1).waiting_on_lead)
            await fake_of(fakes, worker)._play([task_done("bt1"), notified("bt1"), text("The tests pass."),
                                                ended(OWN_WORK)])
            await until(pilot, lambda: len(sent_to(fakes, lead)) == 3)
            assert "The tests pass." in sent_to(fakes, lead)[2]
            assert goal.task(1).waiting_on_lead and app.coordinator.under_way(goal) == []
            # The lead reads both in the turn it's in (assumed: see the top), and answers neither.
            await fake_of(fakes, lead)._play([done()])
            await until(pilot, lambda: rings(app, lead))
            assert goal.task(1).waiting_on_lead and worker.state == BACKGROUND
            assert said(app.coordinator.message_task(goal, 1, "Add a test for empty input.")) == "Sent to task 1."
            assert not goal.task(1).waiting_on_lead

    asyncio.run(go())


def test_a_task_the_lead_leaves_unanswered_waits_quietly_while_the_lead_calls_you(projects, tmp_path):
    """The lead's turn on its reply ends without answering it: with nothing else at work, the lead calls you, and
    the task waits quietly until the lead or you write to it. Meanwhile its line still says "the coordinator is
    reading this", which is no longer so: not asserted here, as it's not what it should say."""
    app, folder, fakes = app_for(projects, tmp_path, {
        "Parser (task 1) ended a turn": [text("The parser is split. Shall it add tests?"), done()]})

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, goal, [worker] = await goal_with(app, pilot, folder, "Parser")
            await until(pilot, lambda: len(sent_to(fakes, lead)) == 2 and rings(app, lead))
            assert lead.detail == "The parser is split."
            await pilot.pause(0.3)
            assert worker.state == BACKGROUND and not rings(app, worker) and goal.task(1).waiting_on_lead
            # A message moves it: here the lead's, once you've answered the lead.
            app.coordinator.message_task(goal, 1, "Add tests.")
            await until(pilot, lambda: worker.events[-1].text == "Adding them." and worker.state == WORKING)

    asyncio.run(go())


@pytest.mark.parametrize("read", ["in-that-turn", "as-the-next-turn"])
def test_the_leads_message_to_a_task_mid_turn_is_answered_to_the_lead(projects, tmp_path, read):
    """The lead, woken by another task say, writes to this one while it works. Claude Code reads it at a step of
    that turn, or as its next (assumed: either, see the top): what the task says goes to the lead each time."""
    app, folder, fakes = app_for(projects, tmp_path, {
        "Do Parser.": [text("Splitting it.")],  # the test plays the rest of the turn
        "Keep the old names.": [],  # and what Claude Code makes of this
        "Parser (task 1) ended a turn": [text("Good."), done()],
    })

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, goal, [worker] = await goal_with(app, pilot, folder, "Parser")
            await until(pilot, lambda: worker.events and worker.events[-1].text == "Splitting it.")
            assert said(app.coordinator.message_task(goal, 1, "Keep the old names.")) == "Sent to task 1."
            task = fake_of(fakes, worker)
            await until(pilot, lambda: task.sent[-1].startswith("Keep the old names."))
            assert worker.state == WORKING
            if read == "in-that-turn":
                await task._play([text("Split it, and kept the old names."), done()])
                replies = ["Split it, and kept the old names."]
            else:
                await task._play([text("Split it."), done()])
                await until(pilot, lambda: len(sent_to(fakes, lead)) == 2)
                await task._play([text("Kept the old names."), done()])
                replies = ["Split it.", "Kept the old names."]
            await until(pilot, lambda: len(sent_to(fakes, lead)) == 1 + len(replies))
            for told, reply in zip(sent_to(fakes, lead)[1:], replies):
                assert told.startswith("Parser (task 1) ended a turn.") and reply in told
            await until(pilot, lambda: worker.state == BACKGROUND and rings(app, lead))
            assert goal.task(1).waiting_on_lead and worker.detail == "the coordinator is reading this"

    asyncio.run(go())


# Stopped, cut off, closed ---------------------------------------------------------------------

def test_stopping_a_task_the_lead_waits_on_leaves_the_lead_to_you(projects, tmp_path):
    """The stopped turn is yours, so the task waits on you, and with nothing else at work the quiet lead calls
    you, not told (as you chose). It had waited on agents for good."""
    app, folder, fakes = app_for(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, _, worker = await lead_quiet_on(app, pilot, folder, fakes)
            before = len(sent_to(fakes, lead))
            app._focus_key(worker.key)
            await until(pilot, lambda: getattr(app.focused, "session_id", None) == worker.key)
            await pilot.press("x")
            await until(pilot, lambda: worker._interrupted)
            await fake_of(fakes, worker)._play(stopped())  # Claude Code ends the stopped turn (see the top)
            await until(pilot, lambda: worker.state == WAITING and rings(app, worker) and rings(app, lead))
            assert worker.detail == "turn finished"
            assert lead.detail == "Parser is yours now; none of its sessions is at work"
            assert len(sent_to(fakes, lead)) == before

    asyncio.run(go())


@pytest.mark.parametrize("how", ["exits", "breaks"])
def test_a_tasks_claude_code_stopping_mid_turn_leaves_the_lead_to_you(projects, tmp_path, how):
    """The turn it cut off goes to the lead, as a failed one, saying how to start it again: the lead had waited on
    it quietly, told nothing."""
    app, folder, fakes = app_for(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, goal, worker = await lead_quiet_on(app, pilot, folder, fakes)
            before = len(sent_to(fakes, lead))
            # No ResultMessage: its stream ends (Claude Code exited), or reading it fails.
            await fake_of(fakes, worker).queue.put(None if how == "exits" else RuntimeError("the pipe broke"))
            await until(pilot, lambda: worker.state == PAUSED and len(sent_to(fakes, lead)) == before + 1)
            why = "Claude Code exited" if how == "exits" else "Claude Code stopped: the pipe broke"
            told = sent_to(fakes, lead)[-1]
            assert told.startswith(f"Parser (task 1) stopped mid-turn: {why}. Its session is paused, and "
                                   "message_task starts it again on its conversation.")
            assert "Adding them." in told and goal.task(1).waiting_on_lead

    asyncio.run(go())


def test_a_task_whose_claude_code_stopped_mid_turn_isnt_taken_for_one_at_work(projects, tmp_path):
    """Paused, nothing will make it report: it isn't at work for the goal, and the lead's board says how it is,
    where it said "running" as of one at work."""
    app, folder, fakes = app_for(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            _, goal, worker = await lead_quiet_on(app, pilot, folder, fakes)
            await fake_of(fakes, worker).queue.put(None)
            await until(pilot, lambda: worker.state == PAUSED)
            assert goal.task(1) not in app.coordinator.under_way(goal)
            assert "1. Parser [running, paused: its Claude Code isn't running, and message_task starts it again]" \
                in app.coordinator.board(goal)

    asyncio.run(go())


def test_a_task_whose_claude_code_stopped_starts_again_when_the_lead_answers_it(projects, tmp_path):
    """Stopped between turns, while it waits on the lead: the lead's answer starts it again on its conversation,
    and its reply reaches the lead."""
    app, folder, fakes = app_for(projects, tmp_path, {"Add tests.": [text("Added them."), done()]})

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, goal, worker = await reading_its_reply(app, pilot, folder)
            stopped = fake_of(fakes, worker)
            await stopped.queue.put(None)
            await until(pilot, lambda: worker.state == PAUSED)
            assert said(app.coordinator.message_task(goal, 1, "Add tests.")) == "Sent to task 1."
            await until(pilot, lambda: len(sent_to(fakes, lead)) == 3)
            again = fake_of(fakes, worker)
            assert again is not stopped and again.options.resume == "sess-42"
            told = sent_to(fakes, lead)[2]
            assert told.startswith("Parser (task 1) ended a turn.") and "Added them." in told
            await until(pilot, lambda: worker.state == BACKGROUND and goal.task(1).waiting_on_lead)

    asyncio.run(go())


def test_closing_a_task_mid_turn_leaves_the_lead_to_you(projects, tmp_path):
    """With nothing else at work, the quiet lead calls you, not told (as you chose). It had waited on agents for
    good, its board alone saying you'd closed the task."""
    app, folder, fakes = app_for(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, goal, worker = await lead_quiet_on(app, pilot, folder, fakes)
            before = len(sent_to(fakes, lead))
            await close_with_w(app, pilot, worker)
            assert "1. Parser [closed by the user]" in app.coordinator.board(goal)
            await until(pilot, lambda: rings(app, lead))
            assert lead.detail == "Parser was closed; none of its sessions is at work"
            assert len(sent_to(fakes, lead)) == before

    asyncio.run(go())


def test_closing_a_task_that_waits_on_the_lead_leaves_the_leads_turn_to_you(projects, tmp_path):
    """Closed while the lead reads its reply: the lead's tools say it's closed, and with nothing else at work the
    lead's turn is yours. Mid-turn, the lead is left as it is until then."""
    app, folder, fakes = app_for(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, goal, worker = await reading_its_reply(app, pilot, folder)
            await close_with_w(app, pilot, worker)
            assert lead.state == WORKING and lead.detail != "Parser was closed; none of its sessions is at work"
            assert said(app.coordinator.message_task(goal, 1, "Add tests.")) == "Task 1's session is closed."
            assert "1. Parser [closed by the user]" in app.coordinator.board(goal)
            await fake_of(fakes, lead)._play([done()])
            await until(pilot, lambda: rings(app, lead))
            assert lead.detail == "Telling it to add tests."

    asyncio.run(go())


@pytest.mark.parametrize("when", ["mid-turn", "waiting-on-the-lead"])
def test_writing_to_a_task_the_lead_waits_on_leaves_the_lead_to_you(projects, tmp_path, when):
    """You write to a task while it works on the lead's answer (the lead quiet on it), or while it waits on the
    lead (the lead mid-turn on its reply, which it then leaves): its reply is yours, and it waits on you, not at
    work for the goal, so the lead calls you, not told (as you chose). It had counted as at work, and the lead
    waited on agents for good. Mid-turn, the lead is left as it is until its turn ends."""
    app, folder, fakes = app_for(projects, tmp_path, {"keep the old names": []})  # the test plays its answer

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            if when == "mid-turn":
                lead, _, worker = await lead_quiet_on(app, pilot, folder, fakes)
                before = len(sent_to(fakes, lead))
                await worker.send("keep the old names")
                assert worker.events[-1].tag == "queued"  # yours, sent while it works
                await fake_of(fakes, worker)._play([text("Added them, and kept the old names."), done()])
                await until(pilot, lambda: worker.state == WAITING and rings(app, worker) and rings(app, lead))
                assert lead.detail == "Parser is yours now; none of its sessions is at work"
            else:
                lead, _, worker = await reading_its_reply(app, pilot, folder)
                await worker.send("keep the old names")
                await fake_of(fakes, worker)._play([text("Kept them."), done()])
                await until(pilot, lambda: worker.state == WAITING and rings(app, worker))
                assert lead.state == WORKING  # left as it is, mid-turn
                before = len(sent_to(fakes, lead))
                await fake_of(fakes, lead)._play([done()])  # the lead's turn on its reply ends, answering nothing
                await until(pilot, lambda: lead.state != WORKING and rings(app, lead))
                assert lead.detail == "Telling it to add tests."  # what its turn said
            assert len(sent_to(fakes, lead)) == before

    asyncio.run(go())


# Finished -------------------------------------------------------------------------------------

PROMPTS = {  # what it asks you mid-turn, and your answer
    PERMISSION: (("ask", "Bash", {"command": "uv run pytest"}), lambda d: d.allow()),
    QUESTION: (("ask", "AskUserQuestion", {"questions": [
        {"question": "One module per format?", "header": "Layout", "multiSelect": False,
         "options": [{"label": "Yes", "description": ""}, {"label": "No", "description": ""}]}]}),
        lambda d: d.choose(1)),
    APPROVAL: (("ask", "ExitPlanMode", {"plan": "Split the parser into a module per format."}),
               lambda d: d.approve_plan()),
}


@pytest.mark.parametrize("state", list(PROMPTS), ids=["permission", "question", "plan"])
def test_a_task_finished_while_it_asks_you_something_closes_once_that_turn_ends(projects, tmp_path, state):
    """As one finished while it works (test_a_session_finished_mid_turn_closes_once_that_turn_ends): a turn on a
    permission prompt, a question or a plan is mid-turn too. It had closed at once, cutting the turn short and
    taking the prompt that called you off the wall."""
    ask, answer = PROMPTS[state]
    app, folder, fakes = app_for(projects, tmp_path, {"Do Parser.": [text("Splitting it."), ask, text("Split it."),
                                                                     done()]})

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, goal, [worker] = await goal_with(app, pilot, folder, "Parser")
            await reach(pilot, lambda: worker.state == state, "the task asking you")
            app.coordinator.finish_task(goal, 1, "split")
            await pilot.pause(0.2)
            assert worker.key in app.driven and worker.state == state and worker.pending is not None
            answer(worker)
            await until(pilot, lambda: worker.key not in app.driven)
            assert worker.events[-1].text == "Split it." and len(sent_to(fakes, lead)) == 1

    asyncio.run(go())


@pytest.mark.parametrize("finished", ["task", "goal"])
def test_a_done_task_you_wrote_to_calls_you_for_a_turn_it_starts_itself(projects, tmp_path, finished):
    """You've written to it, so it stays when its task or goal is done. Its background tests finishing then start
    a turn: that's yours, as the session is, and the coordinator says it stays only the once. It had waited
    quietly, "task done", with what it said reaching no one."""
    app, folder, fakes = app_for(projects, tmp_path, {
        "Do Parser.": [*background_command(), text("Split it. The tests run in the background."), done()],
        "Parser (task 1) ended a turn": [text("Good."), done()],
        "keep the old names": [text("Kept them."), done()],  # yours, to its session
    })

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, goal, [worker] = await goal_with(app, pilot, folder, "Parser")
            await reach(pilot, lambda: lead.events[-1].text == "Good." and lead.state == WAITING, "the lead read it")
            await worker.send("keep the old names")
            await reach(pilot, lambda: worker.events[-1].text == "Kept them." and worker.state == WAITING, "yours")
            if finished == "task":
                app.coordinator.finish_task(goal, 1, "split")
            else:
                app.coordinator.goal_done(goal, "Parser split")
            await pilot.pause(0.2)
            given(worker.key in app.driven, "it stays: you've written to it")
            await fake_of(fakes, worker)._play([task_done("bt1"), notified("bt1"), text("The tests pass."),
                                                ended(OWN_WORK)])
            await until(pilot, lambda: worker.events[-1].text == "The tests pass." and worker.state != WORKING)
            await until(pilot, lambda: rings(app, worker))
            assert worker.detail == "turn finished" and len(sent_to(fakes, lead)) == 2  # yours, not the lead's
            assert len([n for n in app._notifications if "stays open" in n.message]) == 1

    asyncio.run(go())


# After a restart ------------------------------------------------------------------------------

def restored(tmp_path, isolated_home):
    """What agentlens finds at launch: goals.json as it was left, Parser at work when you quit and Docs waiting on
    the lead, and the three sessions saved to come back."""
    folder = tmp_path / "repo"
    folder.mkdir(exist_ok=True)
    goal = coordinator.Goal("g1", str(folder), "", "acceptEdits", lead_session_id="lead-1", started=2,
                            title="Split the parser", tasks=[
                                coordinator.Task(1, "Parser", "Do Parser.", status=RUNNING, session_id="task-1"),
                                coordinator.Task(2, "Docs", "Do Docs.", status=RUNNING, session_id="task-2",
                                                 waiting_on_lead=True)])
    coordinator.save({goal.id: goal})
    cfg = config_mod.Config(path=isolated_home / "config.yaml")
    for session_id, title in (("lead-1", "Split the parser"), ("task-1", "Parser"), ("task-2", "Docs")):
        cfg.remember_driven(session_id, str(folder), title, "acceptEdits")
    config_mod.save(cfg)


async def back(app, pilot):
    # Parked: not running Claude Code until a message starts it. The listing that brought them back is done, so
    # none is still applying when a test that's done with them closes the app.
    await reach(pilot, lambda: len(app.driven) == 3 and all(d.parked for d in app.driven.values())
                and not app._listing, "last time's sessions back, parked")
    by_id = {d.resume: d for d in app.driven.values()}
    goal = app.coordinator.goals["g1"]
    lead, parser, docs = by_id["lead-1"], by_id["task-1"], by_id["task-2"]
    given(app.coordinator.role(lead.key) == (goal, None), "the lead known again")
    given([app.coordinator.role(d.key)[1].id for d in (parser, docs)] == [1, 2], "its tasks known again")
    given(lead.on_turn_end is not None and parser.on_turn_end is not None, "their turns go to the coordinator")
    return goal, lead, parser, docs


@pytest.mark.parametrize("n", [1, 2], ids=["at-work-when-you-quit", "waiting-on-the-lead"])
def test_after_a_restart_a_tasks_next_turn_reaches_the_lead(projects, tmp_path, isolated_home, n):
    """Both paused at launch, the lead's message starts a task's Claude Code again, and what it says goes to the
    lead, whose own starts again for it."""
    restored(tmp_path, isolated_home)
    app, _, fakes = app_for(projects, tmp_path, {"Go on.": [text("Going on.")]})

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            goal, lead, *workers = await back(app, pilot)
            worker = workers[n - 1]
            assert said(app.coordinator.message_task(goal, n, "Go on.")) == f"Sent to task {n}."
            await until(pilot, lambda: worker.events and worker.events[-1].text == "Going on.")
            assert fake_of(fakes, worker).options.resume == f"task-{n}"
            await fake_of(fakes, worker)._play([text("Done."), done()])
            await until(pilot, lambda: len(sent_to(fakes, lead)) == 1)
            told = sent_to(fakes, lead)[0]
            assert told.startswith(f"{goal.task(n).title} (task {n}) ended a turn.") and "Done." in told
            assert fake_of(fakes, lead).options.resume == "lead-1" and goal.task(n).waiting_on_lead

    asyncio.run(go())


def test_after_a_restart_the_lead_doesnt_wait_quietly_on_a_task_still_paused(projects, tmp_path, isolated_home):
    """The lead answers Docs, whose reply then wakes the lead: Parser, paused since the restart, won't end a turn
    by itself, so it isn't at work, and the lead's turn calls you. It had gone quiet, waiting on Parser."""
    restored(tmp_path, isolated_home)
    app, _, _ = app_for(projects, tmp_path, {"Go on.": [text("Docs done."), done()],
                                                 "Docs (task 2) ended a turn": [text("Good."), done()]})

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            goal, lead, parser, _ = await back(app, pilot)
            given(said(app.coordinator.message_task(goal, 2, "Go on.")) == "Sent to task 2.", "the lead answered Docs")
            await reach(pilot, lambda: lead.events and lead.events[-1].text == "Good." and lead.state != WORKING,
                        "the lead's turn on Docs' reply ended")
            assert parser.state == PAUSED and app.coordinator.under_way(goal) == []
            await until(pilot, lambda: rings(app, lead))
            assert lead.detail == "Good."

    asyncio.run(go())
