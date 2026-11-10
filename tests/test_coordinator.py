"""The coordinator: a goal's board, the lead's tools and limits, and how its sessions' turns reach the lead.
No real CLI: the lead's tool calls are made here, as it would make them."""

import asyncio
import re
from datetime import datetime, timedelta, timezone

import pytest
from rich.text import Text
from test_driver import app_with_session, done, text, until
from textual.color import Color

from agentlens import coordinator, tui as tui_mod
from agentlens.coordinator import BOARD, BOARD_NOTE, COORDINATOR, DONE, NOTE, PLANNED, RUNNING, Coordinator
from agentlens.driver import BACKGROUND, WAITING, WORKING
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
    brief = panes.opened[0][1]
    assert panes.opened[0][0] == 1 and brief.startswith("Do Parser.") and "keep your changes apart" in brief
    # How it keeps them apart, delivers or cleans up is the repository's and the user's skills', not agentlens's.
    assert "worktree" not in brief and "push the branch" not in coordinator.INSTRUCTIONS
    # Where it stops is agentlens's: a change that needs review is sent for it, never approved or submitted.
    assert "sent for review counts as delivered" in coordinator.INSTRUCTIONS
    assert "approving, submitting or merging them is the user's" in coordinator.INSTRUCTIONS  # autonomous's, carried
    assert "merge and push" not in coordinator.INSTRUCTIONS
    assert goal.task(1).status == RUNNING and goal.started == 1

    # Its turn ending goes to the lead, quietly, with what it said.
    assert c.turn_ended("k1", False, "Split the parser. Shall I add tests?") == (True, "the coordinator is reading this")
    key, sent, note, by = panes.sent[-1]
    assert (key, note, by) == ("lead", BOARD_NOTE, BOARD)
    assert "Parser (task 1) ended a turn" in sent and "Shall I add tests?" in sent and "autonomous" in sent
    # The lead's own turn, started by that, answers it: then it waits quietly too, while that works.
    assert said(c.message_task(goal, 1, "Yes, add tests.")) == "Sent to task 1."
    assert panes.sent[-1] == ("k1", "Yes, add tests.", NOTE, COORDINATOR)
    assert c.turn_ended("lead", False, "Told it to add tests.") == (True, "coordinating: 1 running, 0 of 2 done")
    assert "Ready to start: 2. CLI" in said(c.finish_task(goal, 1, "split, with tests"))
    assert goal.task(1).status == DONE and "Done: split, with tests" in c.board(goal)


def test_a_leads_turn_with_none_of_its_sessions_at_work_is_for_you(tmp_path):
    """Its sessions report and wait on it. If it ends its turn without answering them, starting one or
    asking you, nothing brings it back: what it said is for you, even without ask_you."""
    panes = Panes()
    c, goal = panes.coordinator(tmp_path)
    c.add_tasks(goal, tasks("DJ rework", "Docs"))
    for n in (1, 2):
        c.start_task(goal, n)
        c.link(f"k{n}", goal, goal.task(n))
    c.turn_ended("k1", False, "Built and tested. Nothing pushed.")
    # One still at work brings it back: quiet.
    assert c.turn_ended("lead", False, "Waiting on the docs.") == (True, "coordinating: 2 running, 0 of 2 done")
    c.turn_ended("k2", False, "Docs updated.")
    reply = "**The DJ rework is finished** and tested on the PC. It's two commits, and nothing is pushed.\n\nMore."
    assert c.turn_ended("lead", False, reply) == (False, "The DJ rework is finished and tested on the PC.")
    assert goal.task(1).waiting_on_lead and goal.task(2).waiting_on_lead
    # Answered, a session is at work again; one you wrote to is yours, not the lead's.
    c.message_task(goal, 1, "Deliver it.")
    assert not goal.task(1).waiting_on_lead
    c.turn_ended("k2", True, "Done, as you said.")
    assert not goal.task(2).waiting_on_lead


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


def test_new_work_on_a_done_goal_opens_it_again(tmp_path):
    """The goal done, you ask for one more thing, and the lead adds a task and starts it: the goal is open
    again, so that task's reply reaches the lead, and its pane reads as any open goal's."""
    panes = Panes()
    c, goal = panes.coordinator(tmp_path)
    c.add_tasks(goal, tasks("Parser"))
    c.start_task(goal, 1)
    c.link("k1", goal, goal.task(1))
    c.finish_task(goal, 1, "split")
    c.goal_done(goal, "Parser split")
    assert c.turn_ended("lead", False, "") == (False, "Goal done: Parser split")
    assert c.label("lead") == "coordinator · goal done"

    assert "The goal is open again" in said(c.add_tasks(goal, tasks("Check")))
    assert not goal.done and coordinator.load(tmp_path / "goals.json")[goal.id].done is False
    assert "Started task 2" in said(c.start_task(goal, 2))
    c.link("k2", goal, goal.task(2))
    assert c.label("lead") == "coordinator · 1 running · 1/2 done"
    assert c.turn_ended("lead", True, "Started a check.") is None  # it answered you, as for any goal
    assert c.turn_ended("k2", False, "Checked: all good.") == (True, "the coordinator is reading this")
    assert panes.sent[-1][0] == "lead" and "Check (task 2) ended a turn" in panes.sent[-1][1]
    assert ("k2", "goal done") not in panes.closes


def test_starting_a_planned_task_opens_a_done_goal_again_and_its_summary_is_not_said_twice(tmp_path):
    panes = Panes()
    c, goal = panes.coordinator(tmp_path)
    c.add_tasks(goal, tasks("Parser", "Docs"))
    c.goal_done(goal, "Parser split")  # before its turn ended, so its summary isn't out yet
    assert goal.done and goal.finished
    assert "Started task 2" in said(c.start_task(goal, 2))
    assert not goal.done and not goal.finished
    c.link("k2", goal, goal.task(2))
    assert c.turn_ended("lead", False, "Started the docs.") == (True, "coordinating: 1 running, 0 of 2 done")
    # A start it refuses opens nothing.
    c.goal_done(goal, "Docs too")
    assert "There's no task 9" in said(c.start_task(goal, 9)) and goal.done


def test_a_task_still_running_in_a_done_goal_is_heard_unless_the_goal_closed_it(tmp_path):
    """Its reply reaches the lead, whatever the goal says, unless finishing the goal closed its session
    while it was mid-turn: then it closes as that turn ends."""
    panes = Panes()
    c, goal = panes.coordinator(tmp_path)
    c.add_tasks(goal, tasks("Parser", "Check"))
    for n in (1, 2):
        c.start_task(goal, n)
        c.link(f"k{n}", goal, goal.task(n))
    goal.done = True  # as goals.json kept it from before new work opened a done goal again
    assert c.turn_ended("k2", False, "Checked.") == (True, "the coordinator is reading this")
    assert "Check (task 2) ended a turn" in panes.sent[-1][1] and not panes.closes
    c.message_task(goal, 2, "Check the docs too.")

    # Finishing the goal closes them; one mid-turn closes as that ends, without going to the lead.
    c.goal_done(goal, "Parser split")
    assert panes.closes == [("k1", "goal done"), ("k2", "goal done")]
    panes.closed -= {"k1", "k2"}  # as the app leaves them open while they work
    sent = len(panes.sent)
    assert c.turn_ended("k1", False, "Pushed.") == (True, "goal done") and len(panes.sent) == sent
    assert panes.closes[-1] == ("k1", "goal done")
    # Opened again for more work before the other's turn ended, that one is the lead's again.
    c.add_tasks(goal, tasks("Docs"))
    assert c.turn_ended("k2", False, "The docs are fine.") == (True, "the coordinator is reading this")
    assert len(panes.sent) == sent + 1 and panes.closes[-1] == ("k1", "goal done")


def test_a_goals_panes_are_named_after_it(tmp_path):
    panes = Panes()
    c, goal = panes.coordinator(tmp_path)
    assert goal.title == "Split the parser" and c.name("lead") == "Split the parser"
    c.add_tasks(goal, tasks("Parser"))
    c.start_task(goal, 1)
    c.link("k1", goal, goal.task(1))
    assert c.name("k1") == "Split the parser" and c.name("someone else's") == ""
    c.got_title("k1", "Parser")  # a task's title doesn't name the goal
    # Nor does its lead's, once it has a name: Claude Code titles a lead from what agentlens tells it.
    c.got_title("lead", "No question presented")
    assert goal.title == "Split the parser" and c.name("k1") == "Split the parser"
    goal.title = ""  # one from before goals had names
    c.got_title("lead", "Parser split into modules")
    assert coordinator.load(tmp_path / "goals.json")[goal.id].title == "Parser split into modules"
    assert coordinator.short("Split the parser into a module per format, with a test for each\nmore") == \
        "Split the parser into a module per…"
    assert coordinator.short("\n  Fix   it \n") == "Fix it"


def test_what_the_lead_asks_you_or_finishing_the_goal_calls_you(tmp_path):
    c, goal = Panes().coordinator(tmp_path)
    c.ask_you(goal, "Should the parser keep the old API?")
    assert c.turn_ended("lead", True, "") == (False, "Should the parser keep the old API?")  # in your turn too
    # Until you answer, a turn something else starts (a session's report) still calls you with it.
    assert c.turn_ended("lead", False, "Docs are written.") == (False, "Should the parser keep the old API?")
    assert coordinator.load(tmp_path / "goals.json")[goal.id].asking  # kept, for after a restart
    assert c.turn_ended("lead", True, "") is None  # you answered it: as any session would
    assert c.turn_ended("lead", False, "") == (False, "the coordinator is waiting on you")  # asked once
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


def test_what_the_lead_asks_you_shows_in_its_conversation(projects):
    """Its ask_you question, options and all, reads in its pane as Claude's own questions do, live and from
    its transcript: the line under its title only has room for the start of it."""
    from claude_agent_sdk import AssistantMessage, ToolUseBlock
    from conftest import common, human, tool_use, write_jsonl
    from test_driver import make

    from agentlens.adapters.claude_transcripts import load_session

    question = ("For the DJ rework: how often should the DJ talk? (1) At the start, then when the music changes "
                "direction. (2) Before every set, as now. I recommend (1).")
    d, _ = make({})
    d.handle(AssistantMessage([ToolUseBlock("a1", "mcp__agentlens__ask_you", {"question": question})],
                              "claude-opus-5-5"))
    asked = d.events[-1]
    assert (asked.kind, asked.detail) == ("question", question)
    assert asked.text.startswith("For the DJ rework: how often should the DJ talk?")

    events: dict = {}
    path = write_jsonl(projects / "demo" / "lead.jsonl", common(
        [human(0, "Split the parser"), tool_use(1, "mcp__agentlens__ask_you", "a1", question=question)], "lead"))
    load_session(path, events=events)
    assert [(e.kind, e.detail) for e in events["main"] if e.kind != "you"] == [("question", question)]


def test_its_messages_are_told_from_yours_in_a_transcript():
    assert coordinator.said_by_it(f"Add tests.\n\n{NOTE}") == (COORDINATOR, "Add tests.")
    assert coordinator.said_by_it(f"Parser ended a turn.\n\n{BOARD_NOTE}") == (BOARD, "Parser ended a turn.")
    handed = coordinator.handover("Split the parser", "1. One module per format.", "Parser plan")
    assert coordinator.said_by_it(f"{handed}\n\n{coordinator.HANDOVER_NOTE}") == (coordinator.HANDOVER, handed)
    assert coordinator.said_by_it("Add tests.") is None


def test_the_lead_reads_the_repository_and_has_the_boards_tools_but_nothing_that_changes_files(tmp_path):
    c, goal = Panes().coordinator(tmp_path)
    options = c.lead_options(goal)
    assert options["tools"] == ["Read", "Grep", "Glob"]
    assert set(options["allowed_tools"]) == {"Read", "Grep", "Glob", *coordinator.TOOL_NAMES}
    assert options["system_prompt"]["append"] == coordinator.INSTRUCTIONS
    assert options["mcp_servers"][coordinator.SERVER]["type"] == "sdk"
    assert "the developer" not in coordinator.INSTRUCTIONS  # it speaks of the user, as autopilot's rules don't


def test_asking_you_ends_the_leads_turn_so_it_cant_say_it_again_under_the_question(tmp_path):
    import asyncio

    c, goal = Panes().coordinator(tmp_path)
    (matcher,) = c.lead_options(goal)["hooks"]["PostToolUse"]
    assert matcher.matcher == coordinator.ASK_YOU_TOOL
    (hook,) = matcher.hooks
    stop = asyncio.run(hook({"hook_event_name": "PostToolUse", "tool_name": coordinator.ASK_YOU_TOOL}, "t1", None))
    assert stop["continue_"] is False and stop["stopReason"] == coordinator.ASKED


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
            # Claude Code titles a lead from what agentlens tells it: it keeps its goal's name.
            from agentlens.live import LiveSession
            app._adopt_names([LiveSession("lead", lead.session_id, "repo", "No question presented", "interactive",
                                          WAITING, None, "")])
            assert lead.title == "Split the parser"
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

            # Its reply went to the lead. The lead's turn on it answered nothing (this fake calls no tools),
            # and nothing else of the goal is at work, so none would bring it back: it calls you, with what
            # it said, while its session waits on it quietly.
            await until(pilot, lambda: len(fakes[0].sent) == 2 and lead.state == WAITING)
            assert "Shall I add tests?" in fakes[0].sent[1] and lead.events[-2].tag == BOARD
            assert lead.detail == "Telling it to add tests."
            assert app.coordinator.label(lead.key) == "coordinator · 1 running · 0/1 done"
            assert app.coordinator.label(worker.key) == "task 1 of 1 · coordinator"
            await until(pilot, lambda: app.sessions[worker.key].state == BACKGROUND)
            assert app.needs_you(app.sessions[lead.key]) and not app.needs_you(app.sessions[worker.key])

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


def test_a_done_goal_given_more_work_hears_from_its_new_session(projects, tmp_path):
    """The goal done, you ask for one more check, and the lead adds a task and starts it: that session's
    reply reaches the lead, where it had gone nowhere while its board said it was running."""
    scripts = Prefixes({
        "Split the parser": [text("I'll plan it."), done()],
        "Do Parser.": [text("Split it."), done()],
        "Parser (task 1) ended a turn": [text("Good."), done()],
        "Do Check.": [text("Checked: all good."), done()],
        "Check (task 2) ended a turn": [text("The check passed."), done()],
    })
    app, folder, fakes = app_with_session(projects, tmp_path, scripts)
    app.config.coordinator, app.config.autopilot = True, "autonomous"

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead = app.start_goal(str(folder), "Split the parser", "acceptEdits", "")
            await until(pilot, lambda: lead.state == WAITING)
            goal = app.coordinator.role(lead.key)[0]
            app.coordinator.add_tasks(goal, tasks("Parser"))
            app.coordinator.start_task(goal, 1)
            parser = next(d for d in app.driven.values() if d is not lead)
            await until(pilot, lambda: len(fakes[0].sent) == 2 and lead.state == WAITING)
            app.coordinator.finish_task(goal, 1, "split")
            app.coordinator.goal_done(goal, "Parser split")
            await until(pilot, lambda: parser.key not in app.driven)
            assert app.coordinator.label(lead.key) == "coordinator · goal done"

            app.coordinator.add_tasks(goal, tasks("Check"))
            app.coordinator.start_task(goal, 2)
            check = next(d for d in app.driven.values() if d is not lead)
            await until(pilot, lambda: len(fakes[0].sent) == 3 and lead.state == WAITING)
            assert "Check (task 2) ended a turn" in fakes[0].sent[2] and "Checked: all good." in fakes[0].sent[2]
            assert check.key in app.driven and check.detail == "the coordinator is reading this"
            assert app.coordinator.label(lead.key) == "coordinator · 1 running · 1/2 done"

    asyncio.run(go())


@pytest.mark.parametrize("finished", ["task", "goal"])
def test_a_session_finished_mid_turn_closes_once_that_turn_ends(projects, tmp_path, finished):
    """Its task or its goal done while it works, it isn't stopped: it closes as its turn ends, and what it
    said then doesn't go to the lead."""
    scripts = Prefixes({
        "Split the parser": [text("I'll plan it."), done()],
        "Do Parser.": [text("Splitting it."), ("pause", 0.8), text("Split it."), done()],
    })
    app, folder, fakes = app_with_session(projects, tmp_path, scripts)
    app.config.coordinator, app.config.autopilot = True, "autonomous"

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead = app.start_goal(str(folder), "Split the parser", "acceptEdits", "")
            await until(pilot, lambda: lead.state == WAITING)
            goal = app.coordinator.role(lead.key)[0]
            app.coordinator.add_tasks(goal, tasks("Parser"))
            app.coordinator.start_task(goal, 1)
            worker = next(d for d in app.driven.values() if d is not lead)
            await until(pilot, lambda: worker.events and worker.events[-1].text == "Splitting it.")
            if finished == "task":
                app.coordinator.finish_task(goal, 1, "split")
            else:
                app.coordinator.goal_done(goal, "Parser split")
            await pilot.pause(0.1)
            assert worker.key in app.driven and worker.state == WORKING  # its turn goes on
            await until(pilot, lambda: worker.key not in app.driven and not app.query(f"#s-{worker.key}"))
            assert worker.events[-1].text == "Split it." and len(fakes[0].sent) == 1
            assert worker.session_id not in {d["session_id"] for d in app.config.driven_sessions}
            assert any(n.title == "The coordinator closed Parser" for n in app._notifications)

    asyncio.run(go())


def test_a_task_session_you_wrote_to_stays_when_its_goal_is_done_mid_turn(projects, tmp_path):
    scripts = Prefixes({
        "Split the parser": [text("I'll plan it."), done()],
        "Do Parser.": [text("Split it."), done()],
        "Parser (task 1) ended a turn": [text("Good."), done()],
        "keep the old names": [text("Kept them."), done()],  # yours, to its session
        "Add tests.": [text("Adding them."), ("pause", 0.8), text("Added."), done()],  # the lead's
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
            app.coordinator.message_task(goal, 1, "Add tests.")
            await until(pilot, lambda: worker.events[-1].text == "Adding them.")
            app._notifications.clear()
            app.coordinator.goal_done(goal, "Parser split")
            await until(pilot, lambda: worker.events[-1].text == "Added." and worker.state != WORKING)
            await pilot.pause(0.2)
            assert worker.key in app.driven
            assert any(n.title == "Parser: goal done" and "stays open" in n.message for n in app._notifications)

    asyncio.run(go())


def test_a_session_you_planned_in_hands_the_work_to_the_coordinator_when_you_ask(projects, tmp_path):
    """Plan in an ordinary session, then say "delegate this to a coordinator": its Claude calls the tool
    it has for that, and a goal's lead starts in a pane of its own, as with G, with the plan."""
    import asyncio as _asyncio

    from test_driver import started

    scripts = Prefixes({
        "plan the parser split": [text("Plan: one module per format, a test for each."), done()],
        "Split the parser": [text("I'll make tasks of it."), done()],  # the handover, to the lead
    })
    app, folder, fakes = app_with_session(projects, tmp_path, scripts)
    app.config.coordinator, app.config.autopilot = True, "autonomous"

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            planner = await started(app, pilot, folder, prompt="plan the parser split")
            options = fakes[0].options
            assert coordinator.DELEGATE_TOOL in options.allowed_tools and coordinator.SERVER in options.mcp_servers

            # Its Claude calls the tool, as it would when you say to delegate.
            ok, said = app._hand_over(planner, "Split the parser", "1. One module per format.\n2. A test for each.")
            assert ok and "◆ Split the parser" in said and "Tell the user it's there" in said
            lead = next(d for d in app.driven.values() if d is not planner)
            await until(pilot, lambda: lead.events and lead.events[-1].kind == "claude")
            first = fakes[1].sent[0]
            assert first.startswith("Split the parser\n\nThe plan, as the user made it with Claude in the session")
            assert "A test for each." in first and first.endswith(coordinator.HANDOVER_NOTE)
            assert lead.events[0].tag == coordinator.HANDOVER  # not yours: its pane says so
            goal = app.coordinator.role(lead.key)[0]
            assert goal.title == "Split the parser" and goal.mode == planner.permission_mode
            assert app.coordinator.role(planner.key) is None and planner.key in app.driven  # it stays as it was
            assert coordinator.SERVER in fakes[1].options.mcp_servers  # the lead's own tools
            assert coordinator.DELEGATE_TOOL not in fakes[1].options.allowed_tools  # not the delegate tool
            assert any(n.title == "The coordinator took over: Split the parser" for n in app._notifications)
            # Turned off, there's nothing to hand over to, and the tool says how to turn it on.
            app.config.coordinator = False
            ok, said = app._hand_over(planner, "More", "Plan")
            assert not ok and "turn it on" in said

    asyncio.run(go())


def test_the_delegate_tool_passes_on_the_goal_and_plan_and_says_how_it_went():
    handed = []

    def hand_over(goal, plan):
        handed.append((goal, plan))
        return (True, "Handed over.") if plan else (False, "Give the plan.")

    tool = coordinator.delegate_tool(hand_over)
    assert tool.name == coordinator.DELEGATE and "only when the user asks" in tool.description
    result = asyncio.run(tool.handler({"goal": " Split  the\nparser ", "plan": "  1. One module per format.\n"}))
    assert handed == [("Split the parser", "1. One module per format.")] and said(result) == "Handed over."
    assert not result.get("is_error")
    assert asyncio.run(tool.handler({"goal": "Split it"}))["is_error"]


def test_with_the_coordinator_off_sessions_get_no_tool_for_it(projects, tmp_path):
    from test_driver import started

    app, folder, fakes = app_with_session(projects, tmp_path, {"hi": [text("Hello."), done()]})

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await started(app, pilot, folder, prompt="hi")
            assert not fakes[0].options.mcp_servers and coordinator.DELEGATE_TOOL not in fakes[0].options.allowed_tools

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


def test_each_coordinator_has_a_colour_and_an_entry_of_its_own_under_the_spaces(projects, tmp_path):
    scripts = Prefixes({
        "Split the parser": [text("I'll plan it."), done()],
        "Do Parser.": [text("Split it."), done()],
        "Parser (task 1) ended a turn": [text("Good."), done()],
        "Something else": [text("Hi."), done()],
    })
    app, folder, _ = app_with_session(projects, tmp_path, scripts)
    app.config.coordinator, app.config.autopilot = True, "autonomous"

    def pane(d):
        return app.query_one(f"#s-{d.key}", tui_mod.SessionPane)

    def rows():
        """The spaces list's lines, without how many need you: that comes and goes as the lead calls you."""
        return [re.sub(r"  ●\d+$", "", node.label.plain) for node in app.query_one(tui_mod.SpacesTree).root.children]

    def shown():
        return {p.session_id for p in app.query(tui_mod.SessionPane) if p.display}

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            other = app.open_driven(str(folder), prompt="Something else")
            lead = app.start_goal(str(folder), "Split the parser", "acceptEdits", "")
            await until(pilot, lambda: lead.state == WAITING and other.state == WAITING)
            goal = app.coordinator.role(lead.key)[0]
            app.coordinator.add_tasks(goal, tasks("Parser"))
            app.coordinator.start_task(goal, 1)
            worker = next(d for d in app.driven.values() if d not in (lead, other))
            # Its turn on what that session said answered nothing, so it calls you with what it said.
            await until(pilot, lambda: worker.state == BACKGROUND and lead.state == WAITING and lead.detail == "Good.")

            # Its lead's border has the coordinator's colour, its sessions' their state's, unless it needs you.
            variables = app.get_css_variables()
            app._focus_key(other.key)

            def read_and_shown(d, color: str) -> bool:
                """Once you've read them all (the lead may call you again meanwhile), `d`'s border is `color`."""
                for each in (lead, worker, other):
                    app.mark_read(app.sessions[each.key])
                return pane(d).styles.border_top[1] == Color.parse(variables[color])

            await until(pilot, lambda: read_and_shown(lead, "al-coordinator"))
            await until(pilot, lambda: read_and_shown(worker, "al-background"))
            assert pane(lead).has_class("lead") and not pane(worker).has_class("lead")
            # Needing you, it's in that colour, like any session.
            app.read._since.pop(lead.key, None)
            lead_pane = pane(lead)
            lead_pane.paint()
            await until(pilot, lambda: lead_pane.styles.border_top[1] == Color.parse(variables["al-waiting"]))
            # And the goal's name on both is in it.
            for d in (lead, worker):
                name = next(span for span in pane(d)._shown["title"].spans if span.start == 0)  # as painted
                assert name.style == app.coordinator_color == variables["al-coordinator"]

            # Under the spaces, one entry for each coordinator: its lead and its sessions, from wherever they are.
            assert rows() == ["▶ All sessions  3", "  repo  3", " ", "COORDINATORS", "  ◆ Split the parser  2"]
            await pilot.press("right_square_bracket", "right_square_bracket")
            assert app.current_space == f"{tui_mod.GOAL_VIEW}{goal.id}"
            assert shown() == {lead.key, worker.key}
            assert app.query_one(tui_mod.SpacesTree).cursor_line == 4
            assert app.query_one("#where").content.plain == "coordinator: ◆ Split the parser"
            assert rows()[4] == "▶ ◆ Split the parser  2"

            # A session you start from there shows where it goes.
            started = app.open_driven(str(folder), prompt="Something else")
            await pilot.pause(0.1)
            assert app.current_space == "repo" and started.key in shown()

            # Once its panes are closed, it's gone from the list.
            app.coordinator.finish_task(goal, 1, "split")
            await until(pilot, lambda: worker.key not in app.driven)
            await app._end_driven(lead, quiet=True)
            app.action_refresh()
            await until(pilot, lambda: "COORDINATORS" not in rows())
            assert app.goal_views() == []

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
