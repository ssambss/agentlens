"""The coordinator: a goal's board, the lead's tools and limits, and how its sessions' turns reach the lead.
No real CLI: the lead's tool calls are made here, as it would make them."""

import asyncio
from datetime import datetime, timedelta, timezone

from rich.text import Text
from test_driver import app_with_session, done, text, until

from agentlens import coordinator, tui as tui_mod
from agentlens.coordinator import BOARD, BOARD_NOTE, COORDINATOR, DONE, NOTE, PLANNED, RUNNING, Coordinator
from agentlens.driver import BACKGROUND, WAITING
from agentlens.tui_driven import ComposerInput


class Panes:
    """What the coordinator needs from the app, as a list of what it did."""

    def __init__(self, level="autonomous", used=None):
        self.level, self.used = level, used
        self.opened, self.sent, self.closed = [], [], set()
        self.closes: list[tuple[str, str]] = []  # what the coordinator closed, and why
        self.yours: dict[str, datetime] = {}

    def close(self, key, why):
        self.closes.append((key, why))
        self.closed.add(key)

    def open_task(self, goal, task, first):
        key = f"k{task.id}"
        self.opened.append((task.id, first))
        return key

    def send(self, key, text, note, by):
        if not key or key in self.closed:
            return False
        self.sent.append((key, text, note, by))
        return True

    def coordinator(self, tmp_path):
        c = Coordinator(open_task=self.open_task, send=self.send, alive=lambda key: bool(key) and key not in self.closed,
                        yours_since=lambda key, when: key in self.yours and (
                            not when or self.yours[key] > datetime.fromisoformat(when)),
                        level=lambda: self.level, usage=lambda: self.used, close=self.close,
                        file=tmp_path / "goals.json")
        goal = c.new_goal("D:/work/demo", "demo", "acceptEdits", "Split the parser")
        c.link("lead", goal)
        return c, goal


def tasks(*titles, after=None):
    return [{"title": t, "brief": f"Do {t}.", "after": (after or {}).get(t, [])} for t in titles]


def said(result):
    return result["content"][0]["text"]


def test_a_plan_runs_as_sessions_and_each_turn_goes_to_the_lead(tmp_path):
    panes = Panes()
    c, goal = panes.coordinator(tmp_path)
    assert "No tasks yet" in c.board(goal)
    c.add_tasks(goal, tasks("Parser", "CLI", after={"CLI": [1]}))
    assert [(t.id, t.title, t.after, t.status) for t in goal.tasks] == [(1, "Parser", [], PLANNED), (2, "CLI", [1], PLANNED)]
    assert "waits for 1" in said(c.start_task(goal, 2))  # its task before it isn't done
    assert "Started task 1" in said(c.start_task(goal, 1))
    c.link("k1", goal, goal.task(1))
    assert panes.opened[0][0] == 1 and panes.opened[0][1].startswith("Do Parser.") and "worktree" in panes.opened[0][1]
    assert goal.task(1).status == RUNNING and goal.started == 1

    # Its turn ending goes to the lead, quietly, with what it said.
    assert c.turn_ended("k1", False, "Split the parser. Shall I add tests?") == (True, "the coordinator is reading this")
    key, sent, note, by = panes.sent[-1]
    assert (key, note, by) == ("lead", BOARD_NOTE, BOARD)
    assert "Parser (task 1) ended a turn" in sent and "Shall I add tests?" in sent and "autonomous" in sent
    # The lead's own turn, started by that, waits quietly too.
    assert c.turn_ended("lead", False, "") == (True, "coordinating: 1 running, 0 of 2 done")

    assert said(c.message_task(goal, 1, "Yes, add tests.")) == "Sent to task 1."
    assert panes.sent[-1] == ("k1", "Yes, add tests.", NOTE, COORDINATOR)
    assert "Ready to start: 2. CLI" in said(c.finish_task(goal, 1, "split, with tests"))
    assert goal.task(1).status == DONE and "Done: split, with tests" in c.board(goal)


def test_a_session_closes_once_it_has_served_its_purpose(tmp_path):
    panes = Panes()
    c, goal = panes.coordinator(tmp_path)
    c.add_tasks(goal, tasks("Parser", "CLI", "Docs"))
    for n in (1, 2, 3):
        c.start_task(goal, n)
        c.link(f"k{n}", goal, goal.task(n))

    # Its task done, its session closes.
    c.finish_task(goal, 1, "split, with tests")
    assert panes.closes == [("k1", "task done: split, with tests")]
    assert "1. Parser [done] Done: split, with tests" in c.board(goal)  # done, not "closed by the user"
    # Finished while it was mid-turn: it closes when that turn ends, without going to the lead.
    c.finish_task(goal, 2, "")
    panes.closed.discard("k2")  # as the app leaves it open while it works
    sent = len(panes.sent)
    assert c.turn_ended("k2", False, "Pushed.") == (True, "task done") and len(panes.sent) == sent
    assert panes.closes[-1] == ("k2", "task done")
    # The goal done, what's still open of it closes too, and the lead stays.
    c.goal_done(goal, "Parser split")
    assert panes.closes[-1] == ("k3", "goal done") and "lead" not in panes.closed


def test_a_goals_panes_are_named_after_it(tmp_path):
    panes = Panes()
    c, goal = panes.coordinator(tmp_path)
    assert goal.title == "Split the parser" and c.name("lead") == "Split the parser"
    c.add_tasks(goal, tasks("Parser"))
    c.start_task(goal, 1)
    c.link("k1", goal, goal.task(1))
    assert c.name("k1") == "Split the parser" and c.name("someone else's") == ""
    c.got_title("k1", "Parser")  # a task's title doesn't name the goal
    c.got_title("lead", "(untitled)")
    assert goal.title == "Split the parser"
    c.got_title("lead", "Parser split into modules")  # its lead's title, once Claude Code gives it one
    assert c.name("k1") == "Parser split into modules"
    assert coordinator.load(tmp_path / "goals.json")[goal.id].title == "Parser split into modules"
    assert coordinator.short("Split the parser into a module per format, with a test for each\nmore") == \
        "Split the parser into a module per…"
    assert coordinator.short("\n  Fix   it \n") == "Fix it"


def test_what_the_lead_asks_you_or_finishing_the_goal_calls_you(tmp_path):
    c, goal = Panes().coordinator(tmp_path)
    c.ask_you(goal, "Should the parser keep the old API?")
    assert c.turn_ended("lead", False, "") == (False, "Should the parser keep the old API?")
    assert c.turn_ended("lead", False, "") == (True, "coordinating: planning")  # asked once
    assert c.turn_ended("lead", True, "") is None  # it answered you: as any session would
    c.goal_done(goal, "Parser split and merged")
    assert goal.done and c.turn_ended("lead", False, "") == (False, "Goal done: Parser split and merged")


def test_your_message_to_one_of_its_sessions_gets_its_reply_to_you(tmp_path):
    panes = Panes()
    c, goal = panes.coordinator(tmp_path)
    c.add_tasks(goal, tasks("Parser"))
    c.start_task(goal, 1)
    c.link("k1", goal, goal.task(1))
    goal.task(1).in_a_row = 4
    assert c.turn_ended("k1", True, "Done, as you said.") is None and panes.sent == []
    assert goal.task(1).in_a_row == 0  # and the lead counts again from yours


def test_on_the_careful_levels_you_see_the_plan_before_any_session_starts(tmp_path):
    panes = Panes(level="balanced")
    c, goal = panes.coordinator(tmp_path)
    c.add_tasks(goal, tasks("Parser"))
    assert "show them the plan with ask_you first" in said(c.start_task(goal, 1)) and panes.opened == []
    planned = datetime.fromisoformat(goal.planned_at)
    panes.yours["lead"] = planned + timedelta(milliseconds=1)  # you answered the lead after the plan
    assert "Started task 1" in said(c.start_task(goal, 1))
    c.add_tasks(goal, tasks("CLI"))  # a changed plan: you see it again
    goal.planned_at = (planned + timedelta(seconds=1)).isoformat()
    assert "ask_you first" in said(c.start_task(goal, 2))


def test_it_starts_no_more_than_its_limits_allow(tmp_path, monkeypatch):
    monkeypatch.setattr(coordinator, "AT_ONCE", 2)
    monkeypatch.setattr(coordinator, "PER_GOAL", 3)
    panes = Panes()
    c, goal = panes.coordinator(tmp_path)
    c.add_tasks(goal, tasks("A", "B", "C", "D"))
    for n in (1, 2):
        c.start_task(goal, n)
        c.link(f"k{n}", goal, goal.task(n))
    assert "2 sessions are running already" in said(c.start_task(goal, 3))
    panes.closed.add("k1")  # you closed one: it isn't running any more
    assert "Started task 3" in said(c.start_task(goal, 3))
    c.link("k3", goal, goal.task(3))
    c.finish_task(goal, 2, "")
    assert "3 sessions have been started for this goal" in said(c.start_task(goal, 4))
    assert "closed by the user" in c.board(goal)


def test_it_starts_none_once_most_of_your_five_hour_window_is_used(tmp_path):
    panes = Panes(used=0.85)
    c, goal = panes.coordinator(tmp_path)
    c.add_tasks(goal, tasks("A"))
    assert "85% of their five-hour window" in said(c.start_task(goal, 1)) and panes.opened == []


def test_it_answers_one_session_only_so_many_times_in_a_row(tmp_path):
    panes = Panes(level="balanced")
    c, goal = panes.coordinator(tmp_path)
    c.add_tasks(goal, tasks("A"))
    panes.yours["lead"] = datetime.now(timezone.utc) + timedelta(seconds=1)
    c.start_task(goal, 1)
    c.link("k1", goal, goal.task(1))
    for _ in range(5):
        assert said(c.message_task(goal, 1, "go on")) == "Sent to task 1."
    assert "5 times in a row" in said(c.message_task(goal, 1, "go on"))


def test_with_its_lead_gone_a_sessions_turn_is_yours(tmp_path):
    panes = Panes()
    c, goal = panes.coordinator(tmp_path)
    c.add_tasks(goal, tasks("A"))
    c.start_task(goal, 1)
    c.link("k1", goal, goal.task(1))
    panes.closed.add("lead")
    assert c.turn_ended("k1", False, "Done.") is None


def test_goals_are_kept_and_their_sessions_known_again_after_a_restart(tmp_path):
    panes = Panes()
    c, goal = panes.coordinator(tmp_path)
    c.add_tasks(goal, tasks("A"))
    c.start_task(goal, 1)
    c.link("k1", goal, goal.task(1))
    c.got_id("lead", "lead-session")
    c.got_id("k1", "task-session")
    again = panes.coordinator(tmp_path)[0]  # a fresh one reads the same file (and adds a goal of its own)
    assert again.goals[goal.id].tasks[0].session_id == "task-session"
    assert again.adopt("new-lead-key", "lead-session")[1] is None
    found = again.adopt("new-task-key", "task-session")
    assert found is not None and found[1].title == "A" and again.label("new-task-key") == "task 1 of 1 · coordinator"


def test_its_messages_are_told_from_yours_in_a_transcript():
    assert coordinator.said_by_it(f"Add tests.\n\n{NOTE}") == (COORDINATOR, "Add tests.")
    assert coordinator.said_by_it(f"Parser ended a turn.\n\n{BOARD_NOTE}") == (BOARD, "Parser ended a turn.")
    assert coordinator.said_by_it("Add tests.") is None


def test_the_lead_reads_the_repository_and_has_the_boards_tools_but_nothing_that_changes_files(tmp_path):
    c, goal = Panes().coordinator(tmp_path)
    options = c.lead_options(goal)
    assert options["tools"] == ["Read", "Grep", "Glob"]
    assert set(options["allowed_tools"]) == {"Read", "Grep", "Glob", *coordinator.TOOL_NAMES}
    assert options["system_prompt"]["append"] == coordinator.INSTRUCTIONS
    assert options["mcp_servers"][coordinator.SERVER]["type"] == "sdk"
    assert "the developer" not in coordinator.INSTRUCTIONS  # it speaks of the user, as autopilot's rules don't


def test_a_message_from_another_session_shows_in_the_pane_and_a_subagents_report_doesnt():
    from claude_agent_sdk import UserMessage

    from test_driver import make

    peer = UserMessage("<peer message>", origin={"kind": "peer", "from": "uds:x", "name": "Bug fixes",
                                                 "body": "Can you check the parser?\nIt fails on empty input."})
    relayed = UserMessage("Is main green?", origin={"kind": "task-notification", "subkind": "peer-send-message",
                                                    "name": "Release"})
    subagent = UserMessage("Found 3 callers.", origin={"kind": "peer", "senderTaskId": "t1", "name": "Explore"})

    async def go():
        d, fakes = make({"hi": [text("Hello."), done()]})
        await d.start("hi")
        await until_state(d)
        await fakes[0]._play([peer, relayed, subagent, text("Checked it."), done()])
        await until_state(d, lambda: d.events[-1].text == "Checked it.")
        notices = [(e.text, e.detail) for e in d.events if e.kind == "notice"]
        assert notices == [("From Bug fixes: Can you check the parser?",
                            "Can you check the parser?\nIt fails on empty input."),
                           ("From Release: Is main green?", "Is main green?")]
        await d.close()
    asyncio.run(go())


async def until_state(d, condition=lambda: True):
    for _ in range(200):
        if d.state == WAITING and condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("it never got there")


# In the app ---------------------------------------------------------------------------------

class Prefixes(dict):
    """A fake Claude Code's script, by how a message starts: the coordinator's carry notes and briefs."""

    def get(self, prompt, default=None):
        return next((steps for start, steps in self.items() if prompt.startswith(start)), default)


def test_g_starts_a_goal_whose_sessions_report_to_its_lead(projects, tmp_path):
    scripts = Prefixes({
        "Split the parser": [text("I'll plan it."), done()],  # your goal, to the lead
        "Do Parser.": [text("Split it. Shall I add tests?"), done()],  # the lead's brief, to its session
        "Parser (task 1) ended a turn": [text("Telling it to add tests."), done()],  # agentlens, to the lead
    })
    app, folder, fakes = app_with_session(projects, tmp_path, scripts)
    app.config.coordinator, app.config.autopilot = True, "autonomous"

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead = app.start_goal(str(folder), "Split the parser", "acceptEdits", "")
            await until(pilot, lambda: lead.state == WAITING and lead.events[-1].kind == "claude")
            assert lead.detail == "turn finished"  # its first turn answers you
            options = fakes[0].options
            assert options.tools == ["Read", "Grep", "Glob"] and coordinator.SERVER in options.mcp_servers
            assert options.permission_mode == "default"  # it changes nothing; its sessions get your mode

            goal = app.coordinator.role(lead.key)[0]
            app.coordinator.add_tasks(goal, tasks("Parser"))  # as the lead's tool calls would
            assert "Started task 1" in said(app.coordinator.start_task(goal, 1))
            worker = next(d for d in app.driven.values() if d is not lead)
            await until(pilot, lambda: worker.state == BACKGROUND)
            assert worker.title == "Parser" and worker.permission_mode == "acceptEdits"
            assert worker.events[0].tag == COORDINATOR and fakes[1].sent[0].endswith(NOTE)
            assert fakes[1].options.tools == {"type": "preset", "preset": "claude_code"}  # a full session
            assert worker.detail == "the coordinator is reading this"

            # Its reply went to the lead, whose turn on it ends quietly: nothing calls you.
            await until(pilot, lambda: len(fakes[0].sent) == 2 and lead.state == BACKGROUND)
            assert "Shall I add tests?" in fakes[0].sent[1] and lead.events[-2].tag == BOARD
            assert lead.detail == "coordinating: 1 running, 0 of 1 done"
            assert app.coordinator.label(lead.key) == "coordinator · 1 running · 0/1 done"
            assert app.coordinator.label(worker.key) == "task 1 of 1 · coordinator"
            await until(pilot, lambda: app.sessions[lead.key].state == BACKGROUND == app.sessions[worker.key].state)
            assert not app.needs_you(app.sessions[lead.key]) and not app.needs_you(app.sessions[worker.key])

            # A goal's panes stand out: double lines, and the goal's name first, on the lead and its sessions.
            for d in (lead, worker):
                pane = app.query_one(f"#s-{d.key}", tui_mod.SessionPane)
                title = Text.from_markup(pane.border_title).plain
                assert pane.has_class("coordinated") and title.startswith("◆ Split the parser · ")

            # Finished, its session has served its purpose: closed as w closes one, without calling you,
            # and not brought back at the next launch.
            app.coordinator.finish_task(goal, 1, "split, with tests")
            await until(pilot, lambda: worker.key not in app.driven and not app.query(f"#s-{worker.key}"))
            assert worker.session_id not in {d["session_id"] for d in app.config.driven_sessions}
            assert any(n.title == "The coordinator closed Parser" and "Task done: split, with tests" in n.message
                       for n in app._notifications)
            assert lead.key in app.driven and app.coordinator.label(lead.key) == "coordinator · 0 running · 1/1 done"

    asyncio.run(go())


def test_a_task_session_you_wrote_to_stays_when_its_task_is_done(projects, tmp_path):
    scripts = Prefixes({
        "Split the parser": [text("I'll plan it."), done()],
        "Do Parser.": [text("Split it."), done()],
        "Parser (task 1) ended a turn": [text("Good."), done()],
        "keep the old names": [text("Kept them."), done()],  # yours, to its session
    })
    app, folder, _ = app_with_session(projects, tmp_path, scripts)
    app.config.coordinator, app.config.autopilot = True, "autonomous"

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead = app.start_goal(str(folder), "Split the parser", "acceptEdits", "")
            await until(pilot, lambda: lead.state == WAITING)
            goal = app.coordinator.role(lead.key)[0]
            app.coordinator.add_tasks(goal, tasks("Parser"))
            app.coordinator.start_task(goal, 1)
            worker = next(d for d in app.driven.values() if d is not lead)
            await until(pilot, lambda: worker.state == BACKGROUND)
            await worker.send("keep the old names")
            await until(pilot, lambda: worker.events[-1].text == "Kept them." and worker.state == WAITING)
            app._notifications.clear()
            app.coordinator.finish_task(goal, 1, "split")
            await pilot.pause(0.2)
            assert worker.key in app.driven and app.coordinator.label(worker.key) == "task 1 of 1 done · coordinator"
            [shown] = [n for n in app._notifications if n.title == "Parser: task done: split"]
            assert "stays open" in shown.message

    asyncio.run(go())


def test_a_goals_panes_offer_no_autopilot_as_the_coordinator_answers_there(projects, tmp_path):
    scripts = Prefixes({
        "Split the parser": [text("I'll plan it."), done()],
        "Do Parser.": [text("Split it."), done()],
        "Parser (task 1) ended a turn": [text("Good."), done()],
    })
    app, folder, _ = app_with_session(projects, tmp_path, scripts)
    app.config.coordinator, app.config.autopilot = True, "autonomous"  # your other panes would show it

    def box(d):
        return app.query_one(f"#s-{d.key}", tui_mod.SessionPane).query_one(ComposerInput).placeholder

    async def pressing_p_on(pilot, d):
        before = (d.autopilot, d.own_autopilot)
        app._notifications.clear()
        app._focus_key(d.key)
        await pilot.press("p")
        assert (d.autopilot, d.own_autopilot) == before
        [shown] = app._notifications
        assert shown.title == "No autopilot on this pane"
        assert "The coordinator answers" in shown.message and "your autopilot setting, autonomous" in shown.message

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead = app.start_goal(str(folder), "Split the parser", "acceptEdits", "")
            await until(pilot, lambda: lead.state == WAITING and box(lead).startswith("Message Claude…"))
            assert "autopilot" not in box(lead)
            await pressing_p_on(pilot, lead)

            goal = app.coordinator.role(lead.key)[0]
            app.coordinator.add_tasks(goal, tasks("Parser"))
            app.coordinator.start_task(goal, 1)
            worker = next(d for d in app.driven.values() if d is not lead)
            await until(pilot, lambda: worker.state == BACKGROUND and box(worker).startswith("Message Claude…"))
            assert "autopilot" not in box(worker)
            await pressing_p_on(pilot, worker)

    asyncio.run(go())


def test_g_needs_the_coordinator_turned_on(projects, tmp_path):
    app, _, _ = app_with_session(projects, tmp_path)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.press("G")
            await pilot.pause(0.1)
            assert not app.driven and not app.screen_stack[1:]
            app.config.coordinator = True
            await pilot.press("G")
            await pilot.pause(0.1)
            assert type(app.screen).__name__ == "NewSessionScreen" and app.screen.goal

    asyncio.run(go())
