"""Experimental: the coordinator, for a goal that takes more than one session.

You give a goal to a lead Claude, a session agentlens runs in a pane of its own. The lead reads
the repository, breaks the goal into tasks, and starts a session for each. When one of those
ends a turn, agentlens sends its reply to the lead, which answers it, marks it done, or brings
it to you. Autopilot is about one session; the coordinator works across them.

The lead does no work itself: it has Read, Grep and Glob, and agentlens's own tools for the
board (below). The limits are in code, not in its instructions: how many sessions run at once
and per goal, how much of your five-hour window is used before it stops starting them, and how
many times in a row it may answer one session. How much it decides itself follows your
autopilot level.

Its messages to the sessions are ordinary messages, with a note saying it sent them, as
autopilot's are. The goals are kept in ~/.agentlens/goals.json.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentlens import autopilot
from agentlens.preferences import home

AT_ONCE = 3  # its sessions running at once
PER_GOAL = 6  # sessions it starts for one goal
USAGE_STOP = 0.8  # of your five-hour window, past which it starts no more
REPLY_CHARS = 6000  # of a session's reply that it reads, from the end
SERVER = "agentlens"  # its tools are mcp__agentlens__<name>
TOOLS = ("board", "add_tasks", "start_task", "message_task", "finish_task", "ask_you", "goal_done")
TOOL_NAMES = [f"mcp__{SERVER}__{name}" for name in TOOLS]
ASK_YOU_TOOL = f"mcp__{SERVER}__ask_you"  # its question shows in the lead's conversation, as Claude's own do
ASKED = "ask_you ends your turn: the question went to the user."  # the lead reads it with their answer
LEAD_TOOLS = ["Read", "Grep", "Glob"]  # Claude Code's own: it reads the repository, and changes nothing

# Tags of message events, as autopilot's are (model.AUTOPILOT): who sent it, when you didn't.
COORDINATOR = "coordinator"  # the lead's message to one of its sessions
BOARD = "board"  # agentlens telling the lead what one of its sessions said
HANDOVER = "handover"  # a lead's first message: the goal and plan another session handed over

# With its messages, so Claude knows you didn't type them. The transcript keeps them, which is how
# agentlens tells them from yours there (said_by_it): keep this wording.
NOTE = ("(agentlens's coordinator sent this for the user, who didn't type it. It runs a goal across several "
        "sessions for them. If something needs the user's own decision, say so and stop: the coordinator "
        "brings it to them.)")
BOARD_NOTE = "(agentlens sent this to you, the coordinator, about one of the goal's sessions: the user didn't type it.)"
HANDOVER_NOTE = ("(Another Claude session handed this goal to you, the coordinator, when the user asked it to: they "
                 "planned it there with that Claude, and didn't type this themselves.)")

# The one tool a session agentlens runs has while the coordinator is on: the work planned in it, handed to
# a goal whose lead starts in a pane of its own, as with G.
DELEGATE = "delegate_to_coordinator"
DELEGATE_TOOL = f"mcp__{SERVER}__{DELEGATE}"
DELEGATE_ABOUT = (
    "Hand the work planned in this session to agentlens's coordinator. It runs the work across several Claude "
    "Code sessions in panes of their own, where the user can follow it. Use this only when the user asks you "
    "to delegate the work, or hand it, to a coordinator. Give the goal in a line, and the plan as agreed here, "
    "for a lead that hasn't seen this conversation: what to do and where, how to check it, what's out of "
    "scope, and what the user decided.")

PLANNED, RUNNING, DONE = "planned", "running", "done"


def said_by_it(text: str) -> tuple[str, str] | None:
    """A message in a transcript as the coordinator or agentlens sent it: (its tag, the message without
    its note). None if you sent it."""
    body = text.rstrip()
    for note, tag in ((NOTE, COORDINATOR), (BOARD_NOTE, BOARD), (HANDOVER_NOTE, HANDOVER)):
        if body.endswith(note):
            return tag, body[:-len(note)].rstrip()
    return None


@dataclass
class Task:
    id: int
    title: str
    brief: str  # what its session is told to do, on its own
    after: list[int] = field(default_factory=list)  # tasks it waits for
    status: str = PLANNED
    session_id: str = ""  # its session's, once Claude Code gives it one
    report: str = ""  # what the lead said of it when it was done
    in_a_row: int = 0  # the lead's messages to it since yours
    waiting_on_lead: bool = False  # its turn ended and went to the lead, which hasn't answered it yet


@dataclass
class Goal:
    id: str
    cwd: str
    space: str  # the space you started it in: its sessions go there
    mode: str  # the permission mode its sessions start in
    lead_session_id: str = ""
    tasks: list[Task] = field(default_factory=list)
    started: int = 0  # sessions started for it
    planned_at: str = ""  # when the plan last changed: on the careful levels, you OK it after that
    done: bool = False
    asking: str = ""  # what the lead asked you this turn, for the line under its title
    finished: str = ""  # its summary, when it said the goal is done
    title: str = ""  # what its panes call it: your goal's first words, then its lead's title

    def task(self, task_id: Any) -> Task | None:
        try:
            wanted = int(task_id)
        except (TypeError, ValueError):
            return None
        return next((t for t in self.tasks if t.id == wanted), None)


def path() -> Path:
    return home() / "goals.json"


def load(file: Path | None = None) -> dict[str, Goal]:
    try:
        data = json.loads((file or path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    goals = {}
    for raw in data if isinstance(data, list) else []:
        try:
            tasks = [Task(**t) for t in raw.pop("tasks", [])]
            goal = Goal(**raw, tasks=tasks)
        except TypeError:
            continue  # written by another version
        goals[goal.id] = goal
    return goals


def save(goals: dict[str, Goal], file: Path | None = None) -> None:
    file = file or path()
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps([asdict(g) for g in goals.values()], indent=1), encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def short(text: str, chars: int = 40) -> str:
    """A goal's first words, for its panes until its lead has a title: its first line, cut at a word."""
    line = " ".join(next((line for line in str(text or "").splitlines() if line.strip()), "").split())
    if len(line) <= chars:
        return line
    cut = line[:chars].rsplit(" ", 1)[0] or line[:chars]
    return cut.rstrip(" ,.;:") + "…"


def _first_sentence(text: str, chars: int = 160) -> str:
    """Where a reply starts, for the line under a pane's title: its first sentence, without markdown."""
    line = " ".join(next((line for line in str(text or "").splitlines() if line.strip()), "").split())
    sentence = re.split(r"(?<=[.!?])\s", line.replace("**", "").lstrip("#>*- "), maxsplit=1)[0]
    return short(sentence, chars)


def handover(goal: str, plan: str, by: str) -> str:
    """A lead's first message, for a goal another session hands over: the goal, then the plan made there."""
    return f"{goal}\n\nThe plan, as the user made it with Claude in the session \"{by}\":\n\n{plan}"


def delegate_tool(hand_over: Callable[[str, str], tuple[bool, str]]):
    """The delegate tool. `hand_over(goal, plan)` starts the goal, and says whether it could and what to
    tell Claude."""
    from claude_agent_sdk import tool

    async def handler(args: dict) -> dict:
        ok, said = hand_over(" ".join(str(args.get("goal") or "").split()), str(args.get("plan") or "").strip())
        return _text(said, error=not ok)

    schema = {"type": "object", "required": ["goal", "plan"],
              "properties": {"goal": {"type": "string", "description": "The goal, in a line"},
                             "plan": {"type": "string", "description": "The plan, as agreed in this session"}}}
    return tool(DELEGATE, DELEGATE_ABOUT, schema)(handler)


def delegate_options(hand_over: Callable[[str, str], tuple[bool, str]]) -> dict:
    """What a session agentlens runs gets while the coordinator is on: the delegate tool, allowed without
    asking."""
    from claude_agent_sdk import create_sdk_mcp_server

    server = create_sdk_mcp_server(SERVER, tools=[delegate_tool(hand_over)])
    return {"mcp_servers": {SERVER: server}, "allowed_tools": [DELEGATE_TOOL]}


def _text(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], **({"is_error": True} if error else {})}


INSTRUCTIONS = """You are agentlens's coordinator for one goal the user gives you, in this repository.

You don't do the work yourself: you have no Bash, Edit or Write. You read the repository (Read, Grep, \
Glob) to plan, and run the work through sessions agentlens starts for you, one per task. Each is a full \
Claude Code session in this repository, working beside the others, and knows only what you tell it.

- A goal another session handed you comes with the plan the user made there: keep to what they \
decided, and bring what it leaves open to them.
- add_tasks: break the goal into tasks. Each has a title, a brief that a fresh session can act on \
without this conversation (what to do and where, how to check it, what's out of scope), and the tasks \
it waits for. Prefer a few tasks that can run side by side without changing the same files.
- start_task starts a task's session. agentlens refuses past its limits (sessions at once, per goal, \
and the user's usage); then wait for one to finish.
- When one of your sessions ends a turn, agentlens sends you its reply, marked as coming from \
agentlens. Answer it with message_task, mark it done with finish_task, or bring it to the user with \
ask_you. When its work is delivered or it has nothing left to do, finish it. Finishing closes its \
session, so finish one only when you need nothing more from it.
- Deliver one at a time: when tasks are done and checked, have one delivered the way this repository \
does it, and the next only once that one has, updated with what came before. Where changes need \
someone's review (a pull request, a Gerrit change), sent for review counts as delivered: approving and \
submitting are the reviewers' job, never yours or a session's.
- Use ask_you whenever you need the user: to decide, to look at the work, or before it's delivered. \
A turn you end without it waits quietly while your sessions work.
- ask_you brings a question to the user: one at a time, written to them as "you", with your \
recommendation. It's what they read, and your turn ends with it, so put in it everything they need \
to answer, as briefly as you can. Their answer comes as their next message.
- When every task is done, call goal_done with a short summary for the user.
- Never tell a session to release, deploy, publish, force-push, or delete what can't easily be got \
back: those are always the user's.

How much you decide yourself follows the user's autopilot level, which each message from agentlens \
gives:
- off: bring every reply that needs an answer to the user with ask_you, with your suggestion. Before \
starting tasks, show the user the plan with ask_you.
- balanced: give the go-aheads yourself, and bring anything else to the user. Before starting tasks, \
show the user the plan with ask_you.
- autonomous: start the tasks yourself, within the limits, and answer the sessions as this says:
""" + (autopilot.RULES["autonomous"] + "\n" + autopilot.ALWAYS_YOURS["autonomous"]).replace(
    "Leave it for the developer only when", "Bring it to the user only when").replace(
    "You run this session", "You run each session").replace("the developer", "the user")


class Coordinator:
    """The goals and their boards, and the lead's tools. The app gives it what it needs from the
    panes: `open_task` starts a task's session and gives its key, `send` sends a session a message
    and says whether it could, `alive` says whether a session is still open, `yours_since` whether
    you've messaged one since a time, `level` your autopilot setting, `usage` how much of your
    five-hour window is used (None if it isn't known), `close` closes a session that has served its
    purpose (its task is done, or its goal), saying why."""

    def __init__(self, *, open_task: Callable[[Goal, Task, str], str | None],
                 send: Callable[[str, str, str, str], bool], alive: Callable[[str], bool],
                 yours_since: Callable[[str, str], bool], level: Callable[[], str],
                 usage: Callable[[], float | None], close: Callable[[str, str], None] = lambda key, why: None,
                 file: Path | None = None) -> None:
        self.open_task, self.send, self.alive, self.yours_since = open_task, send, alive, yours_since
        self.level, self.usage, self.close, self.file = level, usage, close, file
        self.goals = load(file)
        self.keys: dict[str, tuple[str, int | None]] = {}  # pane key -> (goal, task); no task: the lead
        self.lead_keys: dict[str, str] = {}  # goal -> its lead's pane key
        # goal -> the tasks whose sessions goal_done closed: one that was mid-turn closes as that ends
        self.goal_closed: dict[str, set[int]] = {}

    def save(self) -> None:
        save(self.goals, self.file)

    # Goals and the sessions they run --------------------------------------------------------

    def new_goal(self, cwd: str, space: str, mode: str, text: str = "") -> Goal:
        """`text`: your goal, whose first words name it until its lead has a title."""
        goal = Goal(uuid.uuid4().hex[:8], cwd, space, mode, title=short(text))
        self.goals[goal.id] = goal
        self.save()
        return goal

    def link(self, key: str, goal: Goal, task: Task | None = None) -> None:
        """The pane `key` is this goal's lead, or its session for `task`."""
        self.keys[key] = (goal.id, task.id if task else None)
        if task is None:
            self.lead_keys[goal.id] = key

    def role(self, key: str) -> tuple[Goal, Task | None] | None:
        goal_id, task_id = self.keys.get(key, ("", None))
        goal = self.goals.get(goal_id)
        if goal is None:
            return None
        return goal, (goal.task(task_id) if task_id is not None else None)

    def adopt(self, key: str, session_id: str) -> tuple[Goal, Task | None] | None:
        """A session coming back (at launch): the goal it's the lead of, or a task of. Linked if so."""
        for goal in self.goals.values():
            if goal.lead_session_id == session_id:
                self.link(key, goal)
                return goal, None
            for task in goal.tasks:
                if task.session_id == session_id:
                    self.link(key, goal, task)
                    return goal, task
        return None

    def got_id(self, key: str, session_id: str) -> None:
        """Claude Code gave the session at `key` its id: keep it, so it's known after a restart."""
        found = self.role(key)
        if found is None or not session_id:
            return
        goal, task = found
        if task is None and goal.lead_session_id != session_id:
            goal.lead_session_id = session_id
            self.save()
        elif task is not None and task.session_id != session_id:
            task.session_id = session_id
            self.save()

    def got_title(self, key: str, title: str) -> None:
        """The session at `key` has this title now: a lead's names its goal, if it has no name yet. A goal
        keeps the name it started with: Claude Code titles a lead from what agentlens tells it, which
        can make one like "No question presented"."""
        found = self.role(key)
        title = " ".join(str(title or "").split())[:60]
        if found is None or found[1] is not None or not title or title == "(untitled)" or found[0].title:
            return
        found[0].title = title
        self.save()

    def name(self, key: str) -> str:
        """The name of the goal the session at `key` is part of, for its pane; empty if it's none's."""
        found = self.role(key)
        if found is None:
            return ""
        return found[0].title or "a goal"

    def running(self, goal: Goal) -> list[Task]:
        """Its tasks whose sessions are under way: started, not done, and still open."""
        keys = {task_id: key for key, (goal_id, task_id) in self.keys.items() if goal_id == goal.id}
        return [t for t in goal.tasks if t.status == RUNNING and self.alive(keys.get(t.id, ""))]

    def key_of(self, goal: Goal, task: Task) -> str:
        return next((key for key, (g, t) in self.keys.items() if g == goal.id and t == task.id), "")

    def label(self, key: str) -> str:
        """For the pane's border: what it is in its goal."""
        found = self.role(key)
        if found is None:
            return ""
        goal, task = found
        if task is None:
            done = sum(1 for t in goal.tasks if t.status == DONE)
            return ("coordinator · goal done" if goal.done else
                    f"coordinator · {len(self.running(goal))} running · {done}/{len(goal.tasks)} done")
        return f"task {task.id} of {len(goal.tasks)}{' done' if task.status == DONE else ''} · coordinator"

    def summary(self, goal: Goal) -> str:
        running = self.running(goal)
        done = sum(1 for t in goal.tasks if t.status == DONE)
        if not goal.tasks:
            return "planning"
        if running:
            return f"{len(running)} running, {done} of {len(goal.tasks)} done"
        return f"{done} of {len(goal.tasks)} done"

    # When a turn ends: (quiet, the line under its title), or None as without it ---------------

    def turn_ended(self, key: str, yours: bool, reply: str, failed: str = "") -> tuple[bool, str] | None:
        """`yours`: you sent a message in that turn. Quiet, the session waits without calling you."""
        found = self.role(key)
        if found is None:
            return None
        goal, task = found
        return self._lead_turn_ended(goal, yours, reply) if task is None else self._task_turn_ended(
            goal, task, yours, reply, failed)

    def under_way(self, goal: Goal) -> list[Task]:
        """Its sessions at work: running, and not waiting on the lead to answer what they last said. Only
        these bring the lead back by themselves."""
        return [t for t in self.running(goal) if not t.waiting_on_lead]

    def _lead_turn_ended(self, goal: Goal, yours: bool, reply: str = "") -> tuple[bool, str] | None:
        if goal.finished:
            finished, goal.finished = goal.finished, ""
            self.save()
            return False, f"Goal done: {finished}"
        if goal.asking:
            asking, goal.asking = goal.asking, ""
            self.save()
            return False, asking
        if yours:
            return None  # it answered you
        if not self.under_way(goal):
            # None of its sessions is at work, so none will bring it back: what it said is for you, even
            # without ask_you.
            return False, _first_sentence(reply) or "the coordinator is waiting on you"
        return True, f"coordinating: {self.summary(goal)}"

    def _task_turn_ended(self, goal: Goal, task: Task, yours: bool, reply: str, failed: str) -> tuple[bool, str] | None:
        if yours:
            task.in_a_row = 0  # you spoke to it: its reply is for you, and the lead counts again from here
            task.waiting_on_lead = False
            self.save()
            return None
        with_goal = task.id in self.goal_closed.get(goal.id, ())
        if with_goal or task.status == DONE:
            # Finished while it was mid-turn: it has served its purpose, now that the turn is over. One still
            # running in a goal that says it's done otherwise (kept so from before goals opened again for new
            # work) is the lead's.
            why = "goal done" if with_goal else "task done"
            self.close(self.key_of(goal, task), why)
            return True, why
        lead = self.lead_keys.get(goal.id, "")
        ended = f"ended a turn, with an error: {failed}" if failed else "ended a turn"
        text = (f"{task.title} (task {task.id}) {ended}. The user's autopilot level: {self.level()}.\n\n"
                f"Its reply{', the end of it' if len(reply) > REPLY_CHARS else ''}:\n---\n"
                f"{reply[-REPLY_CHARS:] or '(no text)'}\n---")
        if not self.send(lead, text, BOARD_NOTE, BOARD):
            return None  # its lead is gone: this one's yours
        task.waiting_on_lead = True  # until the lead answers it
        self.save()
        return True, "the coordinator is reading this"

    # The lead's tools -------------------------------------------------------------------------

    def board(self, goal: Goal) -> str:
        lines = [f"Limits: {AT_ONCE} sessions at once, {PER_GOAL} per goal ({goal.started} started so far), "
                 f"none started past {USAGE_STOP:.0%} of the user's five-hour usage. "
                 f"The user's autopilot level: {self.level()}."]
        running = {t.id for t in self.running(goal)}
        for t in goal.tasks:
            status = t.status if t.status != RUNNING or t.id in running else "closed by the user"
            after = f", after {', '.join(map(str, t.after))}" if t.after else ""
            lines.append(f"{t.id}. {t.title} [{status}{after}]" + (f" Done: {t.report}" if t.report else ""))
        if not goal.tasks:
            lines.append("No tasks yet.")
        return "\n".join(lines)

    def add_tasks(self, goal: Goal, tasks: list[dict]) -> dict:
        if not isinstance(tasks, list) or not tasks:
            return _text("Give at least one task, each with a title and a brief.", error=True)
        added = []
        for raw in tasks:
            title = str(raw.get("title") or "").strip() if isinstance(raw, dict) else ""
            brief = str(raw.get("brief") or "").strip() if isinstance(raw, dict) else ""
            if not title or not brief:
                return _text("Each task needs a title and a brief.", error=True)
            after = [int(a) for a in raw.get("after") or [] if str(a).isdigit() and goal.task(a)]
            task = Task(max((t.id for t in goal.tasks), default=0) + 1, title[:80], brief, after)
            goal.tasks.append(task)
            added.append(task)
        goal.planned_at = _now()
        again = " The goal is open again: call goal_done once these are done too." if self._reopen(goal) else ""
        self.save()
        return _text("Added: " + "; ".join(f"{t.id}. {t.title}" for t in added) + again + "\n\n" + self.board(goal))

    def _reopen(self, goal: Goal) -> bool:
        """New work on a goal that was done opens it again: its lead and panes go on as for any open goal.
        Whether it was done."""
        if not goal.done:
            return False
        goal.done, goal.finished = False, ""  # its summary, if its lead's turn hasn't ended yet, would say it's done
        self.goal_closed.pop(goal.id, None)
        return True

    def start_task(self, goal: Goal, task_id: Any) -> dict:
        task = goal.task(task_id)
        if task is None:
            return _text(f"There's no task {task_id}.", error=True)
        if task.status != PLANNED:
            return _text(f"Task {task.id} is {task.status} already.", error=True)
        waiting = [a for a in task.after if (goal.task(a) or task).status != DONE]
        if waiting:
            return _text(f"Task {task.id} waits for {', '.join(map(str, waiting))}: finish those first.", error=True)
        level = self.level()
        lead = self.lead_keys.get(goal.id, "")
        if level != "autonomous" and not self.yours_since(lead, goal.planned_at):
            return _text(f"The user's autopilot level is {level}: show them the plan with ask_you first, and start "
                         "tasks once they've answered.", error=True)
        if len(self.running(goal)) >= AT_ONCE:
            return _text(f"{AT_ONCE} sessions are running already: wait for one to finish.", error=True)
        if goal.started >= PER_GOAL:
            return _text(f"{PER_GOAL} sessions have been started for this goal, the most there can be. Ask the "
                         "user with ask_you how to go on.", error=True)
        used = self.usage()
        if used is not None and used >= USAGE_STOP:
            return _text(f"The user has used {used:.0%} of their five-hour window: start no more sessions now, "
                         "and tell them with ask_you.", error=True)
        first = (f"{task.brief}\n\nThis is one task of a goal that agentlens's coordinator runs for the user, "
                 f"with other sessions working on the rest at the same time: keep your changes apart from theirs, "
                 f"the way this repository does it. When you're done, say what you did and how you checked it.")
        key = self.open_task(goal, task, first)
        if not key:
            return _text(f"agentlens couldn't start a session for task {task.id}.", error=True)
        task.status = RUNNING
        goal.started += 1
        again = " The goal is open again: call goal_done once it's done too." if self._reopen(goal) else ""
        self.save()
        return _text(f"Started task {task.id}, {task.title}. Its replies come to you as it ends its turns.{again}")

    def message_task(self, goal: Goal, task_id: Any, message: str) -> dict:
        task = goal.task(task_id)
        if task is None or task.status != RUNNING:
            return _text(f"Task {task_id} isn't running.", error=True)
        message = str(message or "").strip()
        if not message:
            return _text("Say what to send it.", error=True)
        cap = autopilot.IN_A_ROW.get(self.level(), autopilot.IN_A_ROW["balanced"])
        if task.in_a_row >= cap:
            return _text(f"You've answered task {task.id} {cap} times in a row: bring it to the user with ask_you.",
                         error=True)
        if not self.send(self.key_of(goal, task), message, NOTE, COORDINATOR):
            return _text(f"Task {task.id}'s session is closed.", error=True)
        task.in_a_row += 1
        task.waiting_on_lead = False
        self.save()
        return _text(f"Sent to task {task.id}.")

    def finish_task(self, goal: Goal, task_id: Any, report: str) -> dict:
        task = goal.task(task_id)
        if task is None:
            return _text(f"There's no task {task_id}.", error=True)
        task.status, task.report = DONE, " ".join(str(report or "").split())[:300]
        self.save()
        self.close(self.key_of(goal, task), f"task done: {task.report}" if task.report else "task done")
        ready = [t for t in goal.tasks if t.status == PLANNED and all(
            (goal.task(a) or t).status == DONE for a in t.after)]
        then = (" Ready to start: " + "; ".join(f"{t.id}. {t.title}" for t in ready)) if ready else ""
        return _text(f"Task {task.id} is done.{then}")

    def ask_you(self, goal: Goal, question: str) -> dict:
        goal.asking = " ".join(str(question or "").split())[:200] or "The coordinator wants you"
        self.save()
        return _text("The user has your question. Their answer comes as their next message.")

    def goal_done(self, goal: Goal, summary: str) -> dict:
        goal.done = True
        goal.finished = " ".join(str(summary or "").split())[:200] or "every task is done"
        self.save()
        still_open = [t for t in goal.tasks if self.alive(self.key_of(goal, t))]
        self.goal_closed[goal.id] = {t.id for t in still_open}
        for task in still_open:  # their purpose was the goal: any still open close too
            self.close(self.key_of(goal, task), "goal done")
        return _text("Done. End your turn: the user will see it.")

    def lead_options(self, goal: Goal) -> dict:
        """What the lead's session runs with: Claude Code's prompt with these instructions, the tools
        to read the repository, and the board's."""
        from claude_agent_sdk import HookMatcher, create_sdk_mcp_server, tool

        def bound(name: str, description: str, schema: dict, run: Callable[[dict], dict]):
            async def handler(args: dict) -> dict:
                current = self.goals.get(goal.id)
                if current is None:
                    return _text("This goal is gone.", error=True)
                return run(current, args)
            return tool(name, description, schema)(handler)

        task_id = {"type": "integer", "description": "The task's number, as the board gives it"}
        tools = [
            bound("board", "The goal's tasks, what each is doing, and the limits.", {"type": "object", "properties": {}},
                  lambda g, a: _text(self.board(g))),
            bound("add_tasks", "Add tasks to the goal's plan.", {
                "type": "object", "required": ["tasks"],
                "properties": {"tasks": {"type": "array", "items": {
                    "type": "object", "required": ["title", "brief"],
                    "properties": {"title": {"type": "string"},
                                   "brief": {"type": "string", "description": "What its session is told to do"},
                                   "after": {"type": "array", "items": {"type": "integer"},
                                             "description": "Tasks it waits for"}}}}}},
                  lambda g, a: self.add_tasks(g, a.get("tasks"))),
            bound("start_task", "Start a session for a planned task.",
                  {"type": "object", "required": ["task"], "properties": {"task": task_id}},
                  lambda g, a: self.start_task(g, a.get("task"))),
            bound("message_task", "Send one of the goal's sessions a message, as the user would.", {
                "type": "object", "required": ["task", "message"],
                "properties": {"task": task_id, "message": {"type": "string"}}},
                  lambda g, a: self.message_task(g, a.get("task"), a.get("message", ""))),
            bound("finish_task", "Mark a task done.", {
                "type": "object", "required": ["task", "report"],
                "properties": {"task": task_id, "report": {"type": "string", "description": "One line on what it did"}}},
                  lambda g, a: self.finish_task(g, a.get("task"), a.get("report", ""))),
            bound("ask_you", "Bring a question to the user, with everything they need to answer it. Your turn ends "
                  "with it.",
                  {"type": "object", "required": ["question"], "properties": {"question": {"type": "string"}}},
                  lambda g, a: self.ask_you(g, a.get("question", ""))),
            bound("goal_done", "Say the goal is done, with a short summary for the user.",
                  {"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}},
                  lambda g, a: self.goal_done(g, a.get("summary", ""))),
        ]
        return {
            "system_prompt": {"type": "preset", "preset": "claude_code", "append": INSTRUCTIONS},
            "tools": LEAD_TOOLS,
            "allowed_tools": [*LEAD_TOOLS, *TOOL_NAMES],
            "mcp_servers": {SERVER: create_sdk_mcp_server(SERVER, tools=tools)},
            "hooks": {"PostToolUse": [HookMatcher(matcher=ASK_YOU_TOOL, hooks=[end_turn_after_asking])]},
        }


async def end_turn_after_asking(hook_input: dict, tool_use_id: str | None, context: Any) -> dict:
    """ask_you ends the lead's turn, as Claude Code's own questions do. Told to stop there, it still
    wrote a line after every question in its logged turns (and in replays), which read as a second
    message saying the same."""
    return {"continue_": False, "stopReason": ASKED}
