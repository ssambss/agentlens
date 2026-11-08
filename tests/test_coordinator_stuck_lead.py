"""Where the lead's side of the coordinator can leave a goal stuck: the lead, or a task waiting on it, in a state
that nothing moves on and that doesn't call you, or a question the lead asked you lost. Reports arriving while
the lead works, several at once, or while it's stopped; its own turn failing; its pane closed; its question
overtaken by a report; the goal done; a restart; your own message to it.

As test_coordinator_stuck_tasks, whose helpers these use: in the app with fake Claude Code clients, the end of a
turn played by the test, and a stuck case a strict xfail for what should happen, its scene checked with reach()
and given(). The cases where the lead goes quiet and then each of its tasks leaves without reporting (you stop
one, its Claude Code stops, you close it or write to it) are there.

What these take Claude Code to do, as well as what that file says:
- A message sent to the lead while it works is read at a step of that turn (one ResultMessage), or as the next
  turn (one each): both are played here. Several sent at once are all read, in one turn or one each.
- ask_you's PostToolUse hook stopping the turn ends it with a ResultMessage, as any turn's end.
"""

import asyncio

import pytest
from claude_agent_sdk import AssistantMessage, ToolUseBlock
from test_coordinator import Prefixes, said
from test_coordinator_stuck_tasks import (
    back, close_with_w, ended, fake_of, given, goal_with, lead_hears_or_calls_you, reach, restored, rings, sent_to)
from test_driver import app_with_session, done, text, until

from agentlens.coordinator import ASK_YOU_TOOL
from agentlens.driver import BACKGROUND, FAILED, PAUSED, WORKING

SCRIPTS = {
    "Split the parser": [text("I'll plan it."), done()],  # your goal, to the lead
    # The lead's briefs, to its sessions: each is mid-turn on it until the test plays the rest.
    "Do Parser.": [text("Splitting it.")],
    "Do Docs.": [text("Writing the docs.")],
    "Do CLI.": [text("Adding the flags.")],
    # agentlens, to the lead, and you: the test plays what the lead's Claude Code makes of each.
    "Parser (task 1) ended a turn": [],
    "Docs (task 2) ended a turn": [],
    "CLI (task 3) ended a turn": [],
    "How is it going?": [],
}
QUESTION = "Should the parser keep the old API? I'd keep it for one release."


def app_for(projects, tmp_path):
    app, folder, fakes = app_with_session(projects, tmp_path, Prefixes(SCRIPTS))
    app.config.coordinator, app.config.autopilot = True, "autonomous"
    return app, folder, fakes


async def goal_at_work(app, pilot, folder, *titles):
    """A goal's lead after its first turn, and a session for each task, mid-turn on its brief."""
    lead, goal, workers = await goal_with(app, pilot, folder, *titles)
    await reach(pilot, lambda: all(w.state == WORKING and w.events[-1].kind == "claude" for w in workers),
                "its tasks at work")
    return lead, goal, workers


async def reports(fakes, *workers):
    """Their turns end together: what each said goes to the lead."""
    await asyncio.gather(*(fake_of(fakes, w)._play([text(f"{w.title}: done."), done()]) for w in workers))


async def lead_says(fakes, lead, reply, *, error=""):
    """The lead's turn ends, saying `reply`."""
    await fake_of(fakes, lead)._play([text(reply), ended(error=error) if error else done()])


async def lead_quiet(app, pilot, folder, fakes, *titles):
    """The first task reported and the lead's turn on it ended quietly, as the others are at work."""
    lead, goal, workers = await goal_at_work(app, pilot, folder, *titles)
    await reports(fakes, workers[0])
    await reach(pilot, lambda: len(sent_to(fakes, lead)) == 2, "the first task's reply sent to the lead")
    await lead_says(fakes, lead, "The parser is split. The rest are at work.")
    await reach(pilot, lambda: lead.state == BACKGROUND, "the lead's turn ended quietly")
    given(lead.detail == f"coordinating: {len(titles)} running, 0 of {len(titles)} done", "the lead waits on them")
    return lead, goal, workers


async def asked(app, pilot, fakes, lead, goal):
    """The lead, mid-turn, asks you with ask_you, whose hook ends its turn."""
    app.coordinator.ask_you(goal, QUESTION)  # as its tool call does
    await fake_of(fakes, lead)._play([AssistantMessage([ToolUseBlock("q1", ASK_YOU_TOOL, {"question": QUESTION})],
                                                       "claude-opus-5-5"), done()])
    await reach(pilot, lambda: rings(app, lead) and lead.detail == QUESTION, "the lead calling you with its question")


def told(fakes, lead):
    """What agentlens has sent the lead, in every Claude Code it has run."""
    return [m for f in fakes if f.options.can_use_tool.__self__ is lead for m in f.sent]


# Reports while the lead works, or several at once -------------------------------------------

@pytest.mark.parametrize("read", ["in-that-turn", "as-the-next-turn"])
def test_a_report_that_comes_while_the_lead_works_is_read_and_the_lead_then_calls_you(projects, tmp_path, read):
    """Docs reports while the lead reads Parser's. It goes in at once (send's busy is False for agentlens's
    messages), and the lead reads it in that turn or the next. Either way the turn that read it is the one that
    calls you. Read as the next turn, the first turn's end calls you too, a moment early: not asserted here."""
    app, folder, fakes = app_for(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, goal, [parser, docs] = await goal_at_work(app, pilot, folder, "Parser", "Docs")
            await reports(fakes, parser)
            await until(pilot, lambda: len(sent_to(fakes, lead)) == 2)
            await fake_of(fakes, lead)._play([text("Reading Parser's reply.")])
            await reports(fakes, docs)
            await until(pilot, lambda: len(sent_to(fakes, lead)) == 3)
            assert lead.state == WORKING and sent_to(fakes, lead)[2].startswith("Docs (task 2) ended a turn.")
            if read == "in-that-turn":
                await lead_says(fakes, lead, "Both are done. Shall I deliver them?")
            else:
                await fake_of(fakes, lead)._play([done()])
                await lead_says(fakes, lead, "Both are done. Shall I deliver them?")
            await until(pilot, lambda: rings(app, lead) and lead.events[-1].text.startswith("Both are done."))
            assert lead.detail == "Both are done."
            assert goal.task(1).waiting_on_lead and goal.task(2).waiting_on_lead
            assert parser.state == docs.state == BACKGROUND

    asyncio.run(go())


@pytest.mark.parametrize("read", ["in-one-turn", "one-turn-each"])
def test_reports_that_come_together_all_reach_the_lead_and_none_is_left_unread(projects, tmp_path, read):
    app, folder, fakes = app_for(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, goal, workers = await goal_at_work(app, pilot, folder, "Parser", "Docs", "CLI")
            await reports(fakes, *workers)
            await until(pilot, lambda: len(sent_to(fakes, lead)) == 4)
            for n, title in enumerate(("Parser", "Docs", "CLI"), 1):
                [report] = [m for m in sent_to(fakes, lead) if m.startswith(f"{title} (task {n}) ended a turn.")]
                assert f"{title}: done." in report
            assert [e.tag for e in lead.events if e.kind == "you"][1:] == ["board"] * 3
            for _ in range(1 if read == "in-one-turn" else 3):
                await lead_says(fakes, lead, "Read it. Waiting on your call.")
            await until(pilot, lambda: rings(app, lead) and lead.events[-1].kind == "claude"
                        and len([e for e in lead.events if e.kind == "claude"]) == (2 if read == "in-one-turn" else 4))
            assert all(t.waiting_on_lead for t in goal.tasks) and app.coordinator.under_way(goal) == []
            assert all(w.state == BACKGROUND and not rings(app, w) for w in workers)

    asyncio.run(go())


# The lead's own turn: failed, stopped, cut off -----------------------------------------------

@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "hides a failure, not stuck: Coordinator.turn_ended passes the error to _task_turn_ended only, and "
    "_lead_turn_ended goes quiet while a task is at work; the error sits in the lead's feed, and the report the "
    "failed turn was on waits until another task's wakes the lead"))
def test_a_lead_turn_that_fails_while_a_task_is_at_work_calls_you(projects, tmp_path):
    app, folder, fakes = app_for(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, _, [parser, _] = await goal_at_work(app, pilot, folder, "Parser", "Docs")
            await reports(fakes, parser)
            await reach(pilot, lambda: len(sent_to(fakes, lead)) == 2, "Parser's reply sent to the lead")
            await lead_says(fakes, lead, "Telling Parser to add tests.", error="API Error: 500 Internal server error")
            await reach(pilot, lambda: lead.state != WORKING, "the lead's turn ended")
            given(any(e.kind == "error" and "API Error: 500" in e.text for e in lead.events), "the error in its feed")
            try:
                await until(pilot, lambda: rings(app, lead), steps=40)
            except AssertionError:
                raise AssertionError(f"the lead's failed turn left it {lead.state!r} ({lead.detail!r})") from None

    asyncio.run(go())


def test_a_report_after_you_stop_the_lead_starts_its_next_turn_which_the_coordinator_takes(projects, tmp_path):
    """You stop the lead mid-turn (x): that turn is yours. A task reporting then starts the lead's next turn, and
    that one's end is the coordinator's again."""
    app, folder, fakes = app_for(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, _, [parser, docs] = await goal_at_work(app, pilot, folder, "Parser", "Docs")
            await reports(fakes, parser)
            await until(pilot, lambda: len(sent_to(fakes, lead)) == 2)
            await fake_of(fakes, lead)._play([text("Reading Parser's reply.")])
            await until(pilot, lambda: lead.events[-1].text == "Reading Parser's reply.")
            app._focus_key(lead.key)
            await pilot.pause(0.1)
            await pilot.press("x")
            await until(pilot, lambda: lead._interrupted)
            await fake_of(fakes, lead)._play([done()])  # Claude Code ends the stopped turn
            await until(pilot, lambda: rings(app, lead) and lead.detail == "turn finished")
            await reports(fakes, docs)
            await until(pilot, lambda: len(sent_to(fakes, lead)) == 3 and lead.state == WORKING)
            await lead_says(fakes, lead, "Both are done. Shall I deliver them?")
            await until(pilot, lambda: rings(app, lead) and lead.detail == "Both are done.")

    asyncio.run(go())


def test_reports_after_the_leads_claude_code_stopped_start_it_again_and_reach_it(projects, tmp_path):
    """Its Claude Code exits while it waits quietly: paused, it starts again for the next report, and two that come
    together, the second while it's starting, both reach it."""
    app, folder, fakes = app_for(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, _, [_, docs, cli] = await lead_quiet(app, pilot, folder, fakes, "Parser", "Docs", "CLI")
            stopped = fake_of(fakes, lead)
            await stopped.queue.put(None)
            await until(pilot, lambda: lead.state == PAUSED)
            await reports(fakes, docs, cli)
            await until(pilot, lambda: fake_of(fakes, lead) is not stopped and len(sent_to(fakes, lead)) == 2)
            assert fake_of(fakes, lead).options.resume == "sess-42"
            assert sorted(m.split(" (task")[0] for m in sent_to(fakes, lead)) == ["CLI", "Docs"]
            await lead_says(fakes, lead, "All three are done. Shall I deliver them?")
            await until(pilot, lambda: rings(app, lead) and lead.detail == "All three are done.")

    asyncio.run(go())


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "stuck: DrivenSession._stopped parks the lead (PAUSED, which doesn't call you) and never ends its turn; with "
    "every task waiting on it, no report will start it again"))
def test_the_leads_claude_code_stopping_mid_turn_with_every_task_waiting_on_it_calls_you(projects, tmp_path):
    app, folder, fakes = app_for(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, goal, [parser] = await goal_at_work(app, pilot, folder, "Parser")
            await reports(fakes, parser)
            await reach(pilot, lambda: len(sent_to(fakes, lead)) == 2 and goal.task(1).waiting_on_lead,
                        "Parser's reply sent to the lead")
            await fake_of(fakes, lead)._play([text("Reading Parser's reply.")])
            await fake_of(fakes, lead).queue.put(None)  # its Claude Code exits: no ResultMessage
            await reach(pilot, lambda: lead.state == PAUSED, "the lead paused")
            try:
                await until(pilot, lambda: rings(app, lead), steps=40)
            except AssertionError:
                raise AssertionError(f"the lead was left {lead.state!r} ({lead.detail!r})") from None

    asyncio.run(go())


def test_a_report_while_the_leads_claude_code_shuts_down_isnt_lost(projects, tmp_path):
    """Claude Code takes a moment to exit (the SDK's disconnect waits for it): a task reporting then waits for that,
    and starts the lead's Claude Code again, as a report to a paused lead does. It had gone nowhere."""
    app, folder, fakes = app_for(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, goal, [_, docs] = await lead_quiet(app, pilot, folder, fakes, "Parser", "Docs")
            stopping = fake_of(fakes, lead)

            async def slow_disconnect():
                await asyncio.sleep(1.0)
                stopping.closed = True

            stopping.disconnect = slow_disconnect
            await stopping.queue.put(None)
            await until(pilot, lambda: lead._client is None and not lead.parked)  # shutting down
            await reports(fakes, docs)
            await until(pilot, lambda: fake_of(fakes, lead) is not stopping and len(sent_to(fakes, lead)) == 1)
            assert stopping.closed  # the new one started once the old one had exited
            assert sent_to(fakes, lead)[0].startswith("Docs (task 2) ended a turn") and goal.task(2).waiting_on_lead
            assert told(fakes, lead).count(sent_to(fakes, lead)[0]) == 1

    asyncio.run(go())


def test_a_message_to_a_task_whose_claude_code_never_started_is_refused_not_dropped(projects, tmp_path):
    app, folder, fakes = app_for(projects, tmp_path)
    fake = app.client_factory

    def none_for_tasks(options):
        if fakes:  # the lead's started: its tasks' can't
            raise RuntimeError("claude not found")
        return fake(options)

    app.client_factory = none_for_tasks

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            _, goal, [parser] = await goal_with(app, pilot, folder, "Parser")
            await until(pilot, lambda: parser.state == FAILED)
            assert said(app.coordinator.message_task(goal, 1, "Add tests.")) == "Task 1's session is closed."

    asyncio.run(go())


# The lead's pane closed ------------------------------------------------------------------------

def test_with_the_leads_pane_closed_a_tasks_next_turn_is_yours(projects, tmp_path):
    app, folder, fakes = app_for(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, goal, [parser] = await goal_at_work(app, pilot, folder, "Parser")
            await close_with_w(app, pilot, lead)
            await reports(fakes, parser)
            await until(pilot, lambda: rings(app, parser))
            assert parser.detail == "turn finished" and not goal.task(1).waiting_on_lead
            assert len(sent_to(fakes, lead)) == 1

    asyncio.run(go())


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "stuck: closing the lead (TUI._end_driven) tells the coordinator nothing, so a task already waiting on it "
    "keeps waiting quietly ('the coordinator is reading this') with no lead to answer it"))
def test_closing_the_leads_pane_leaves_a_task_waiting_on_it_to_you(projects, tmp_path):
    app, folder, fakes = app_for(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, _, [parser] = await goal_at_work(app, pilot, folder, "Parser")
            await reports(fakes, parser)
            await reach(pilot, lambda: len(sent_to(fakes, lead)) == 2, "Parser's reply sent to the lead")
            await lead_says(fakes, lead, "The parser is split. Shall it add tests?")
            await reach(pilot, lambda: rings(app, lead) and parser.state == BACKGROUND, "the lead calling you")
            await close_with_w(app, pilot, lead)
            try:
                await until(pilot, lambda: rings(app, parser), steps=40)
            except AssertionError:
                raise AssertionError(f"Parser was left {parser.state!r} ({parser.detail!r})") from None

    asyncio.run(go())


# What the lead asked you -----------------------------------------------------------------------

def test_the_leads_question_still_calls_you_after_a_report_comes_before_your_answer(projects, tmp_path):
    """It asks you while reading Parser's reply; Docs reports before you answer, with CLI still at work. The
    question stays until you answer: it had gone, with the lead waiting quietly on CLI."""
    app, folder, fakes = app_for(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, goal, [parser, docs, _] = await goal_at_work(app, pilot, folder, "Parser", "Docs", "CLI")
            await reports(fakes, parser)
            await reach(pilot, lambda: len(sent_to(fakes, lead)) == 2, "Parser's reply sent to the lead")
            await asked(app, pilot, fakes, lead, goal)
            await reports(fakes, docs)
            await reach(pilot, lambda: len(sent_to(fakes, lead)) == 3, "Docs' reply sent to the lead")
            await lead_says(fakes, lead, "The docs are written. Still waiting on your answer.")
            await until(pilot, lambda: lead.events[-1].text.startswith("The docs are written.") and rings(app, lead))
            assert lead.detail == QUESTION
            # Your answer is the end of it: the lead's turn on it is yours, as any.
            await lead.send("Keep it for one release.")
            await lead_says(fakes, lead, "Telling Parser to keep it.")
            await until(pilot, lambda: lead.events[-1].text == "Telling Parser to keep it." and rings(app, lead))
            assert lead.detail == "turn finished" and not goal.asking

    asyncio.run(go())


def test_with_nothing_at_work_a_report_before_your_answer_still_calls_you(projects, tmp_path):
    """As the one before, with no task at work when Docs reports: the lead's next turn calls you, still with the
    question, which is in its conversation too."""
    app, folder, fakes = app_for(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, goal, [parser, docs] = await goal_at_work(app, pilot, folder, "Parser", "Docs")
            await reports(fakes, parser)
            await until(pilot, lambda: len(sent_to(fakes, lead)) == 2)
            await asked(app, pilot, fakes, lead, goal)
            await reports(fakes, docs)
            await until(pilot, lambda: len(sent_to(fakes, lead)) == 3)
            await lead_says(fakes, lead, "The docs are written. Still waiting on your answer.")
            await until(pilot, lambda: rings(app, lead) and lead.events[-1].text.startswith("The docs are written."))
            assert lead.detail == QUESTION
            assert any(e.kind == "question" and e.detail == QUESTION for e in lead.events)

    asyncio.run(go())


# The goal done ---------------------------------------------------------------------------------

def test_goal_done_closes_its_tasks_and_one_mid_turn_closes_as_that_turn_ends(projects, tmp_path):
    """The lead says the goal is done while reading Parser's reply (Parser waits on it) and Docs is mid-turn: Parser
    closes at once, Docs once its turn ends, without its reply going to the lead, and the lead calls you."""
    app, folder, fakes = app_for(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, goal, [parser, docs] = await goal_at_work(app, pilot, folder, "Parser", "Docs")
            await reports(fakes, parser)
            await until(pilot, lambda: len(sent_to(fakes, lead)) == 2 and parser.state == BACKGROUND)
            assert "End your turn" in said(app.coordinator.goal_done(goal, "Parser split, docs under way"))
            await until(pilot, lambda: parser.key not in app.driven)
            assert docs.key in app.driven and docs.state == WORKING
            await lead_says(fakes, lead, "The goal is done.")
            await until(pilot, lambda: rings(app, lead))
            assert lead.detail == "Goal done: Parser split, docs under way"
            assert app.coordinator.label(lead.key) == "coordinator · goal done"
            await reports(fakes, docs)
            await until(pilot, lambda: docs.key not in app.driven)
            assert len(sent_to(fakes, lead)) == 2 and lead.key in app.driven
            closed = {n.title for n in app._notifications}
            assert {"The coordinator closed Parser", "The coordinator closed Docs"} <= closed

    asyncio.run(go())


# After a restart -------------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "stuck: last time's sessions come back paused (PAUSED, which doesn't call you), the tasks that were at work "
    "included, so none will report: nothing wakes the lead or calls you until you type in one"))
def test_after_a_restart_a_goal_left_at_work_calls_you_or_carries_on(projects, tmp_path, isolated_home):
    """Parser was at work and Docs waiting on the lead when you quit: the lead was waiting quietly on Parser."""
    restored(tmp_path, isolated_home)
    app, _, fakes = app_for(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            _, lead, _, _ = await back(app, pilot)
            await lead_hears_or_calls_you(app, pilot, lead, fakes, 0)

    asyncio.run(go())


def test_after_a_restart_the_question_the_lead_asked_still_calls_you(projects, tmp_path):
    """It asks you to look at the plan; you quit before answering, and open agentlens again: it's back paused,
    calling you with the question, where it had come back saying nothing of it."""
    app, folder, fakes = app_for(projects, tmp_path)

    async def before_quitting():
        async with app.run_test(size=(160, 44)) as pilot:
            lead = app.start_goal(str(folder), "Plan the parser split", "acceptEdits", "")
            await reach(pilot, lambda: lead.state == WORKING and lead.events, "the lead at work on your goal")
            goal = app.coordinator.role(lead.key)[0]
            await asked(app, pilot, fakes, lead, goal)
            await reach(pilot, lambda: lead.remembered and goal.lead_session_id and not app._listing,
                        "the lead saved to come back")

    asyncio.run(before_quitting())
    again, _, _ = app_for(projects, tmp_path)

    async def after():
        async with again.run_test(size=(160, 44)) as pilot:
            await reach(pilot, lambda: len(again.driven) == 1, "the lead back")
            [lead] = again.driven.values()
            assert again.coordinator.role(lead.key) is not None  # known as the goal's lead
            await until(pilot, lambda: rings(again, lead))
            assert lead.detail == QUESTION and lead.parked and not lead.connected  # Claude Code starts as you answer
            await until(pilot, lambda: not again._listing)  # the listing that brought it back done, before closing

    asyncio.run(after())


# Your own message to the lead ------------------------------------------------------------------

@pytest.mark.parametrize("when", ["while-it-waits-quietly", "mid-turn-read-in-that-turn"])
def test_your_message_to_the_lead_gets_its_answer_to_you(projects, tmp_path, when):
    """While it waits quietly on tasks at work, or while it reads a report (Claude Code reading yours in that turn):
    the turn that answers you is yours, and calls you though a task is at work."""
    app, folder, fakes = app_for(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            if when == "while-it-waits-quietly":
                lead, goal, _ = await lead_quiet(app, pilot, folder, fakes, "Parser", "Docs")
                await lead.send("How is it going?")
            else:
                lead, goal, [parser, _] = await goal_at_work(app, pilot, folder, "Parser", "Docs")
                await reports(fakes, parser)
                await until(pilot, lambda: len(sent_to(fakes, lead)) == 2)
                await lead.send("How is it going?")
                assert lead.events[-1].tag == "queued"
            await until(pilot, lambda: sent_to(fakes, lead)[-1] == "How is it going?")
            await lead_says(fakes, lead, "The parser is split and the docs are under way.")
            await until(pilot, lambda: rings(app, lead))
            assert lead.detail == "turn finished" and app.coordinator.under_way(goal) == [goal.task(2)]

    asyncio.run(go())


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "misrouted, not stuck: DrivenSession clears _yours_sent at the first turn's end after your message, so when "
    "Claude Code reads it as the next turn, the report's turn is taken for yours, and the one answering you is "
    "the coordinator's, quiet while a task is at work"))
def test_your_message_to_the_lead_mid_turn_read_as_its_next_turn_gets_its_answer_to_you(projects, tmp_path):
    app, folder, fakes = app_for(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead, _, [parser, _] = await goal_at_work(app, pilot, folder, "Parser", "Docs")
            await reports(fakes, parser)
            await reach(pilot, lambda: len(sent_to(fakes, lead)) == 2, "Parser's reply sent to the lead")
            await lead.send("How is it going?")
            given(lead.events[-1].tag == "queued", "yours, sent while it works")
            await lead_says(fakes, lead, "The parser is split.")  # the report's turn
            await lead_says(fakes, lead, "Parser is split and the docs are under way.")  # yours, read next
            await reach(pilot, lambda: lead.events[-1].text.startswith("Parser is split and") and lead.state != WORKING,
                        "the turn answering you ended")
            try:
                await until(pilot, lambda: rings(app, lead), steps=40)
            except AssertionError:
                raise AssertionError(f"your answer came quietly: {lead.state!r} ({lead.detail!r})") from None

    asyncio.run(go())
