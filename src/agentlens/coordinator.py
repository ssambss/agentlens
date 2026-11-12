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
from agentlens.preferences import home, write_whole
from agentlens.report import counted

AT_ONCE = 3  # its sessions running at once
PER_GOAL = 10  # sessions it starts for one goal
USAGE_STOP = 0.8  # of your five-hour window, past which it starts no more
REPLY_CHARS = 6000  # of a session's reply that it reads, from the end
DEBATE_ROUNDS = 3  # the most rounds two of its sessions debate for, after their openings
SERVER = "agentlens"  # its tools are mcp__agentlens__<name>
TOOLS = ("board", "add_tasks", "start_task", "message_task", "debate", "finish_task", "ask_you", "goal_done")
TOOL_NAMES = [f"mcp__{SERVER}__{name}" for name in TOOLS]
ASK_YOU_TOOL = f"mcp__{SERVER}__ask_you"  # its question shows in the lead's conversation, as Claude's own do
ASKED = "ask_you ends your turn: the question went to the user."  # the lead reads it with their answer
LEAD_TOOLS = ["Read", "Grep", "Glob"]  # Claude Code's own: it reads the repository, and changes nothing

# Tags of message events, as autopilot's are (model.AUTOPILOT): who sent it, when you didn't.
COORDINATOR = "coordinator"  # the lead's message to one of its sessions
BOARD = "board"  # agentlens telling the lead what one of its sessions said
HANDOVER = "handover"  # a lead's first message: the goal and plan another session handed over
DEBATE = "debate"  # what the sessions one of its sessions debates said, passed on to it as it is

# With its messages, so Claude knows you didn't type them. The transcript keeps them, which is how
# agentlens tells them from yours there (said_by_it): keep this wording.
NOTE = ("(agentlens's coordinator sent this for the user, who didn't type it. It runs a goal across several "
        "sessions for them. If something needs the user's own decision, say so and stop: the coordinator "
        "brings it to them.)")
BOARD_NOTE = "(agentlens sent this to you, the coordinator, about one of the goal's sessions: the user didn't type it.)"
HANDOVER_NOTE = ("(Another Claude session handed this goal to you, the coordinator, when the user asked it to: they "
                 "planned it there with that Claude, and didn't type this themselves.)")
DEBATE_NOTE = ("(agentlens passed this on to you, word for word, from the goal's sessions you're debating: neither "
               "the user nor the coordinator wrote it.)")
AGREED = re.compile(r"^\W*agreed\b", re.IGNORECASE)  # a debating session's reply that says another convinced it

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
    for note, tag in ((NOTE, COORDINATOR), (BOARD_NOTE, BOARD), (HANDOVER_NOTE, HANDOVER), (DEBATE_NOTE, DEBATE)):
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
    debate: int = 0  # the debate its session is in, if it is: its replies go to the others in it, not the lead
    rounds_left: int = 0  # of that debate, after the round under way
    held: str = ""  # its reply in the round under way, held until the others' are in
    agreed: bool = False  # it said another side convinced it: out of the arguing, its reply kept for the lead


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
    asking: str = ""  # what the lead asked you, until you answer: the line under its title, after a restart too
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
    write_whole(file, json.dumps([asdict(g) for g in goals.values()], indent=1))


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


def _listed(items: list[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    return f"{', '.join(items[:-1])} and {items[-1]}" if len(items) > 1 else "".join(items)


def _tasks(tasks: list[Task]) -> str:
    """'task 2', 'tasks 2 and 3'."""
    return ("task " if len(tasks) == 1 else "tasks ") + _listed([str(t.id) for t in tasks])


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
Glob) only as far as you need to split the goal into tasks, and run the work through sessions agentlens \
starts for you, one per task. Each is a full Claude Code session in this repository, working beside the \
others, and knows only what you tell it. Working out how to do a task, or why something went wrong, is \
its session's job, not yours: it reads the code and runs things, so it soon knows more of its task than \
you do. What you hold is what it can't know: the goal, what the user decided, and what the other sessions \
are doing.

- A goal another session handed you comes with the plan the user made there: keep to what they \
decided, and bring what it leaves open to them.
- add_tasks: break the goal into tasks. Each has a title, a brief that a fresh session can act on \
without this conversation, and the tasks it waits for. The brief says what's wanted and why, where to \
start if you know, what's out of scope, what the user decided, and how it will be judged done. Say \
what, not how: no steps, and no guesses at a cause for it to check. Prefer a few tasks that can run \
side by side without changing the same files.
- start_task starts a task's session. agentlens refuses past its limits (sessions at once, per goal, \
and the user's usage); then wait for one to finish.
- When one of your sessions ends a turn, agentlens sends you its reply, marked as coming from \
agentlens. Answer it with message_task, mark it done with finish_task, or bring it to the user with \
ask_you. When its work is delivered or it has nothing left to do, finish it. Finishing closes its \
session, so finish one only when you need nothing more from it.
- message_task: write as the user would, short. When you pass on something the user said, give it in \
their words, and add only what the session can't know: what the user decided, what other sessions found \
or are changing, what's out of scope. Don't diagnose it for the session or list steps. If you need \
something back, ask for it in a line, the answer you need, not an account of everything it did.
- debate: when the goal turns on views that differ (designs to choose from, or a cause one session \
found and another doubts), have two or more of your sessions argue it out with each other instead of \
through you. Each must have ended its turn and be waiting on you. Give the question in a line or two, \
and each side's position if it has none yet. agentlens passes each one's replies to the others, word \
for word, for the rounds you give, and one the others convince drops out. Then you get each side's \
last reply: weigh them yourself, or bring them to the user with ask_you. Messaging or finishing one \
still arguing ends the debate.
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
    purpose (its task is done, or its goal), saying why, and says False if it keeps it open instead (you've
    written to it, so it's yours)."""

    def __init__(self, *, open_task: Callable[[Goal, Task, str], str | None],
                 send: Callable[[str, str, str, str], bool], alive: Callable[[str], bool],
                 yours_since: Callable[[str, str], bool], level: Callable[[], str],
                 usage: Callable[[], float | None],
                 close: Callable[[str, str], bool | None] = lambda key, why: None,
                 at_work: Callable[[str], bool] | None = None,
                 call_you: Callable[[str, str], None] = lambda key, why: None, file: Path | None = None) -> None:
        """`at_work`: whether an open session is running Claude Code, not paused (as alive, if not given).
        `call_you`: has the session at `key` call you, saying why, if it waits quietly or is paused: one
        mid-turn is left as it is, as that turn's end says what it says."""
        self.open_task, self.send, self.alive, self.yours_since = open_task, send, alive, yours_since
        self.level, self.usage, self.close, self.file = level, usage, close, file
        self.at_work, self.call_you = at_work or alive, call_you
        self.goals = load(file)
        self.keys: dict[str, tuple[str, int | None]] = {}  # pane key -> (goal, task); no task: the lead
        self.lead_keys: dict[str, str] = {}  # goal -> its lead's pane key
        # goal -> the tasks whose sessions goal_done closed: one that was mid-turn closes as that ends
        self.goal_closed: dict[str, set[int]] = {}
        self._asked: set[str] = set()  # goals whose lead asked you in the turn under way
        # goal -> when its lead last started or messaged one of its sessions: after your message, it passed that on
        self._passed_on: dict[str, str] = {}
        # (goal, task): those whose last turn was yours, as you wrote to it or stopped it. They wait on you, not
        # at work for the goal, until the lead writes to them or a turn of theirs reaches it.
        self._yours: set[tuple[str, int]] = set()

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
        others = [t for t in self.sides(goal, task) if t is not task]
        debating = f" · debating {_tasks(others)}" if others else ""
        return f"task {task.id} of {len(goal.tasks)}{' done' if task.status == DONE else ''}{debating} · coordinator"

    def summary(self, goal: Goal) -> str:
        running = self.running(goal)
        done = sum(1 for t in goal.tasks if t.status == DONE)
        if not goal.tasks:
            return "planning"
        if running:
            return f"{len(running)} running, {done} of {len(goal.tasks)} done"
        return f"{done} of {len(goal.tasks)} done"

    # When a turn ends: (quiet, the line under its title), or None as without it ---------------

    def turn_ended(self, key: str, yours: bool, reply: str, failed: str = "", interrupted: bool = False,
                   cut_off: bool = False) -> tuple[bool, str] | None:
        """`yours`: you sent a message in that turn. `interrupted`: you stopped it, which makes it yours too.
        `cut_off`: Claude Code stopped under it, with `failed` saying how, and the session is paused. Quiet,
        the session waits without calling you."""
        found = self.role(key)
        if found is None:
            return None
        goal, task = found
        if task is None:
            if cut_off:
                return self._lead_cut_off(goal, failed)
            return self._lead_turn_ended(goal, yours, reply, interrupted, failed)
        return self._task_turn_ended(goal, task, yours or interrupted, reply, failed, cut_off)

    def _lead_cut_off(self, goal: Goal, why: str) -> tuple[bool, str]:
        """Its Claude Code stopped mid-turn: it calls you, whatever its sessions are doing, as the turn didn't end.
        What it asked you, or the goal done, is still what it says."""
        self._asked.discard(goal.id)
        if goal.finished:
            finished, goal.finished = goal.finished, ""
            self.save()
            return False, f"Goal done: {finished}"
        if goal.asking:
            return False, goal.asking
        return False, f"stopped mid-turn ({why.rstrip('.')}) · type to carry on"

    def left(self, key: str, why: str) -> None:
        """The task session at `key` stopped being at work for its goal without its turn reaching the lead, as
        `why` says: it's yours now (you wrote to it or stopped it), you closed it, or its Claude Code stopped
        between turns. With none of the goal's sessions at work then, none will bring the lead back: it calls
        you. Not for the coordinator's own closes (finish_task, goal_done), which its turn is under way for."""
        found = self.role(key)
        if found is None or found[1] is None:
            return
        goal, task = found
        if task.debate and task.agreed:
            self._leave(goal, task)  # out of the arguing already: the debate goes on without it
        elif task.debate and self._debate_over(goal, task, f"task {task.id} {why}") is not None:
            return  # the lead hears of it, with the other sides' replies
        if goal.done or self.under_way(goal):
            return
        self.call_you(self.lead_keys.get(goal.id, ""), f"{task.title} {why}; none of its sessions is at work"[:200])

    def lead_closed(self, key: str) -> None:
        """You closed a goal's lead: the tasks waiting on it to answer what they said are yours now."""
        found = self.role(key)
        if found is None or found[1] is not None:
            return
        goal = found[0]
        for task in goal.tasks:
            task_key = self.key_of(goal, task)
            if task.waiting_on_lead and self.alive(task_key):
                task.waiting_on_lead = False
                self._yours.add((goal.id, task.id))
                self.call_you(task_key, "its coordinator was closed: this one is yours now")
        self.save()

    def under_way(self, goal: Goal) -> list[Task]:
        """Its sessions at work: running Claude Code, and waiting neither on the lead to answer what they last
        said nor on you (you wrote to it or stopped it). Only these bring the lead back by themselves."""
        return [t for t in self.running(goal) if not t.waiting_on_lead and (goal.id, t.id) not in self._yours
                and self.at_work(self.key_of(goal, t))]

    def _lead_turn_ended(self, goal: Goal, yours: bool, reply: str = "", interrupted: bool = False,
                         failed: str = "") -> tuple[bool, str] | None:
        if goal.finished:
            finished, goal.finished = goal.finished, ""
            self.save()
            return False, f"Goal done: {finished}"
        if goal.id in self._asked:  # it asked you in this turn
            self._asked.discard(goal.id)
            return False, goal.asking
        if goal.asking:
            if not yours:
                # Not answered yet: a turn something else started (a session's report) leaves it yours to answer.
                return False, goal.asking
            goal.asking = ""  # your message answers it
            self.save()
        if interrupted or yours and not self._passed_on_since_yours(goal):
            # It answered you, or you stopped it: yours, as any session's. One that passed what you said on to its
            # sessions waits on them, as any turn of its own does.
            return None
        if failed:
            # Its turn failed (an API error, say): what it set out to do, a report's answer, may not be done, and
            # one of its sessions at work bringing it back later wouldn't say so. It's yours.
            return False, f"its turn failed: {failed}"[:200]
        if not self.under_way(goal):
            # None of its sessions is at work, so none will bring it back: what it said is for you, even
            # without ask_you.
            return False, _first_sentence(reply) or "the coordinator is waiting on you"
        return True, f"coordinating: {self.summary(goal)}"

    def _passed_on_since_yours(self, goal: Goal) -> bool:
        """Whether its lead has started or messaged one of its sessions since your last message to it."""
        when = self._passed_on.get(goal.id)
        return when is not None and not self.yours_since(self.lead_keys.get(goal.id, ""), when)

    def _task_turn_ended(self, goal: Goal, task: Task, yours: bool, reply: str, failed: str,
                         cut_off: bool = False) -> tuple[bool, str] | None:
        if yours:
            task.in_a_row = 0  # you spoke to it: its reply is for you, and the lead counts again from here
            task.waiting_on_lead = False
            self._yours.add((goal.id, task.id))  # it waits on you now, not at work for the goal
            self.save()
            if task.status == RUNNING and task.debate and not task.agreed:
                self._debate_over(goal, task, f"the user wrote to task {task.id} or stopped it, so its replies "
                                              "are theirs now")
            elif task.status == RUNNING:
                self.left(self.key_of(goal, task), "is yours now")
            return None
        self._yours.discard((goal.id, task.id))
        with_goal = task.id in self.goal_closed.get(goal.id, ())
        if with_goal or task.status == DONE:
            # Finished while it was mid-turn: it has served its purpose, now that the turn is over. One still
            # running in a goal that says it's done otherwise (kept so from before goals opened again for new
            # work) is the lead's.
            why = "goal done" if with_goal else "task done"
            if self.close(self.key_of(goal, task), why) is False:
                return None  # kept open, as you've written to it: its turns are yours, as any session's
            return True, why
        if task.debate and task.agreed:
            self._leave(goal, task)  # a turn of one out of the arguing (it shouldn't have one) is the lead's
        if task.debate:
            return self._debate_turn_ended(goal, task, reply, failed, cut_off)
        lead = self.lead_keys.get(goal.id, "")
        if cut_off:
            ended = (f"stopped mid-turn: {failed.rstrip('.')}. Its session is paused, and message_task starts it "
                     "again on its conversation")
        else:
            ended = f"ended a turn, with an error: {failed}" if failed else "ended a turn"
        text = (f"{task.title} (task {task.id}) {ended}. The user's autopilot level: {self.level()}.\n\n"
                f"Its reply{', the end of it' if len(reply) > REPLY_CHARS else ''}:\n---\n"
                f"{reply[-REPLY_CHARS:] or '(no text)'}\n---")
        if not self.send(lead, text, BOARD_NOTE, BOARD):
            return None  # its lead is gone: this one's yours
        task.waiting_on_lead = True  # until the lead answers it
        self.save()
        return True, "the coordinator is reading this"

    # Its sessions debating ---------------------------------------------------------------------

    def sides(self, goal: Goal, task: Task) -> list[Task]:
        """The sessions in the debate `task` is in, it too: none if it's in none."""
        return [t for t in goal.tasks if task.debate and t.debate == task.debate]

    def _debate_turn_ended(self, goal: Goal, task: Task, reply: str, failed: str,
                           cut_off: bool) -> tuple[bool, str] | None:
        """Its reply is held until every side still arguing has replied. Then each of those gets the others',
        word for word. One that says it's convinced is out of the arguing from then on, its reply kept for the
        lead. The rounds done, fewer than two sides left arguing, or the user's usage past the limit, the lead
        gets every side's last reply."""
        task.held = reply[-REPLY_CHARS:] or "(no text)"
        if cut_off:
            return self._debate_over(goal, task, f"task {task.id} stopped mid-turn ({failed.rstrip('.')}), and its "
                                                 "session is paused: message_task starts it again")
        if failed:
            return self._debate_over(goal, task, f"task {task.id} ended a turn with an error: {failed.rstrip('.')}")
        arguing = [t for t in self.sides(goal, task) if not t.agreed]
        waiting = [t for t in arguing if not t.held]
        if waiting:
            self.save()
            return True, f"debating: waiting on {_tasks(waiting)}"
        for t in arguing:
            t.agreed = bool(AGREED.match(t.held))
        still = [t for t in arguing if not t.agreed]
        agreed = [t for t in arguing if t.agreed]
        closed = [t for t in still if not self.alive(self.key_of(goal, t))]
        used = self.usage()
        why = ""
        if len(still) < 2:
            why = (f"{_tasks(agreed)} agreed, so only task {still[0].id} is left arguing" if still else
                   "every side agreed")
        elif task.rounds_left <= 0:
            why = "every side has given its last reply"
        elif used is not None and used >= USAGE_STOP:
            why = f"the user has used {used:.0%} of their five-hour window, so agentlens stopped it"
        elif closed:
            why = f"{_tasks(closed)}'s session is closed"
        if why:
            return self._debate_over(goal, task, why)
        for t in self.sides(goal, task):
            t.rounds_left -= 1
        for t in still:
            others = "\n\n".join(
                f"Task {o.id}, {o.title}, {'agreed, and is out of the debate now' if o.agreed else 'answered'}:\n"
                f"---\n{o.held}\n---" for o in arguing if o is not t)
            then = ("Your reply to this is your last in the debate: the coordinator reads it next, with the others' "
                    "last replies." if t.rounds_left == 0 else
                    f"{counted(t.rounds_left, 'more round')} after your reply to this.")
            self.send(self.key_of(goal, t), f"{others}\n\n{then}", DEBATE_NOTE, DEBATE)
        for t in still:
            t.held = ""  # one that agreed keeps its reply, for the lead
        self.save()
        if task.agreed:
            return True, f"agreed: {_tasks(still)} debate on"
        return True, f"debating {_tasks([t for t in still if t is not task])}"

    def _end_debate(self, goal: Goal, task: Task, why: str) -> str:
        """Ends the debate `task` is in, as `why` says: what the lead is told of it, with each side's reply held
        for this round (or kept from when it agreed), or that it's still on its turn (its reply goes to the lead
        when it ends). A side whose reply is in waits on the lead now, unless it's yours."""
        sides = self.sides(goal, task)
        told = [f"The debate between {_listed([f'task {s.id} ({s.title})' for s in sides])} is over: {why}."]
        for side in sides:
            if side.held:
                told.append(f"Task {side.id}'s last reply{', agreeing' if side.agreed else ''}:\n---\n{side.held}\n---")
                side.waiting_on_lead = (goal.id, side.id) not in self._yours
            elif side is not task:
                told.append(f"Task {side.id} is still on its turn: its reply comes to you when it ends.")
        for side in sides:
            side.debate, side.rounds_left, side.held, side.agreed = 0, 0, "", False
        self.save()
        return "\n\n".join(told)

    def _leave(self, goal: Goal, task: Task) -> str:
        """A side that agreed, out of the arguing already, leaves its debate, which goes on without it: what
        the lead is told of that, with its last reply there."""
        others = [t for t in self.sides(goal, task) if t is not task]
        told = (f"Task {task.id} had agreed in its debate with {_tasks(others)}, which goes on without it. Its last "
                f"reply there:\n---\n{task.held}\n---")
        task.debate, task.rounds_left, task.held, task.agreed = 0, 0, "", False
        self.save()
        return told

    def _step_in(self, goal: Goal, task: Task, why: str) -> str:
        """The lead messages or finishes one of a debate's sessions: one still arguing ends the debate, and one
        that agreed leaves it. What the lead is told of it, to go before what its tool says."""
        if not task.debate:
            return ""
        return (self._leave(goal, task) if task.agreed else self._end_debate(goal, task, why)) + "\n\n"

    def _debate_over(self, goal: Goal, task: Task, why: str) -> tuple[bool, str] | None:
        """Ends it from one of its sessions (a turn of theirs, or one that left), and tells the lead. None if
        its lead is gone."""
        told = self._end_debate(goal, task, why)
        head, _, rest = told.partition("\n\n")
        if not self.send(self.lead_keys.get(goal.id, ""), f"{head} The user's autopilot level: {self.level()}."
                         f"\n\n{rest}", BOARD_NOTE, BOARD):
            return None
        return True, "the coordinator is reading this"

    def debate(self, goal: Goal, task_ids: Any, rounds: Any, question: str) -> dict:
        try:
            ids = list(dict.fromkeys(int(i) for i in task_ids or []))
        except (TypeError, ValueError):
            ids = []
        sides = [goal.task(i) for i in ids]
        if len(sides) < 2 or None in sides:
            return _text("Give two or more of the goal's tasks, by their numbers.", error=True)
        running = {t.id for t in self.running(goal)}
        cap = autopilot.IN_A_ROW.get(self.level(), autopilot.IN_A_ROW["balanced"])
        for t in sides:
            if t.id not in running:
                return _text(f"Task {t.id} isn't running.", error=True)
            if t.debate:
                return _text(f"Task {t.id} is in a debate already.", error=True)
            if (goal.id, t.id) in self._yours:
                return _text(f"Task {t.id} is the user's: they wrote to it, and its replies go to them.", error=True)
            if not self.at_work(self.key_of(goal, t)):
                return _text(f"Task {t.id} is paused: message_task starts it again.", error=True)
            if not t.waiting_on_lead:
                return _text(f"Task {t.id} is still on its turn: start the debate once its reply is in.", error=True)
            if t.in_a_row >= cap:
                return _text(f"You've answered task {t.id} {cap} times in a row: bring it to the user with ask_you.",
                             error=True)
        try:
            rounds = int(rounds)
        except (TypeError, ValueError):
            rounds = 0
        if not 1 <= rounds <= DEBATE_ROUNDS:
            return _text(f"Give 1 to {DEBATE_ROUNDS} rounds.", error=True)
        question = str(question or "").strip()
        if not question:
            return _text("Say what they're to debate.", error=True)
        used = self.usage()
        if used is not None and used >= USAGE_STOP:
            return _text(f"The user has used {used:.0%} of their five-hour window: no debate now. Tell them with "
                         "ask_you.", error=True)
        number = max(t.debate for t in goal.tasks) + 1
        for t in sides:
            others = [o for o in sides if o is not t]
            one = len(others) == 1
            whom = "another of the goal's sessions" if one else f"{len(others)} other sessions of the goal"
            brief = (f"{question}\n\nYou're debating this with {whom}: "
                     f"{_listed([f'task {o.id} ({o.title})' for o in others])}. Your reply to this is your opening. "
                     f"agentlens passes each of your replies to {'it' if one else 'them'} word for word, and "
                     f"{'its' if one else 'theirs'} to you, for {counted(rounds, 'round')} after your openings; then "
                     "the coordinator reads each side's last reply. Make your case from what you find in the "
                     "repository, and change nothing in it while you debate. "
                     f"Answer {'its' if one else 'their'} points, and keep to them. If {'it' if one else 'one of them'} convinces you, start your reply with "
                     "\"Agreed:\" and say what changed your mind: that takes you out of the debate.")
            if not self.send(self.key_of(goal, t), brief, NOTE, COORDINATOR):
                told = [o for o in sides if o.debate == number]
                for o in told:
                    o.debate, o.rounds_left = 0, 0
                self.save()
                after = (f" {_tasks(told).capitalize()} got the brief already: what "
                         f"{'it replies' if len(told) == 1 else 'they reply'} comes to you." if told else "")
                return _text(f"Task {t.id}'s session is closed: no debate.{after}", error=True)
            t.debate, t.rounds_left, t.held, t.agreed = number, rounds, "", False
            t.waiting_on_lead = False
            t.in_a_row += 1
        self._passed_on[goal.id] = _now()
        self.save()
        return _text(f"{_tasks(sides).capitalize()} are debating it, for {counted(rounds, 'round')} after their "
                     "openings. Each side's last reply comes to you when it's over.")

    # The lead's tools -------------------------------------------------------------------------

    def board(self, goal: Goal) -> str:
        lines = [f"Limits: {AT_ONCE} sessions at once, {PER_GOAL} per goal ({goal.started} started so far), "
                 f"none started past {USAGE_STOP:.0%} of the user's five-hour usage. "
                 f"The user's autopilot level: {self.level()}."]
        running = {t.id for t in self.running(goal)}
        for t in goal.tasks:
            status = t.status if t.status != RUNNING or t.id in running else "closed by the user"
            if t.id in running and not self.at_work(self.key_of(goal, t)):
                status = "running, paused: its Claude Code isn't running, and message_task starts it again"
            elif t.id in running and (goal.id, t.id) in self._yours:
                status = "running, the user's: they wrote to it or stopped it, and its replies go to them"
            elif t.id in running and t.debate:
                others = _tasks([o for o in self.sides(goal, t) if o is not t])
                status = (f"running, agreed in its debate with {others}: out of it, and waits for it to end"
                          if t.agreed else f"running, debating {others}: {counted(t.rounds_left, 'round')} left "
                          "after this one, and its replies go to them")
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
        self._passed_on[goal.id] = _now()
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
        ended = self._step_in(goal, task, f"you messaged task {task.id}")
        if not self.send(self.key_of(goal, task), message, NOTE, COORDINATOR):
            return _text(f"{ended}Task {task.id}'s session is closed.", error=True)
        task.in_a_row += 1
        task.waiting_on_lead = False
        self._yours.discard((goal.id, task.id))  # at work for the goal again
        self._passed_on[goal.id] = _now()
        self.save()
        return _text(f"{ended}Sent to task {task.id}.")

    def finish_task(self, goal: Goal, task_id: Any, report: str) -> dict:
        task = goal.task(task_id)
        if task is None:
            return _text(f"There's no task {task_id}.", error=True)
        ended = self._step_in(goal, task, f"you finished task {task.id}")
        task.status, task.report = DONE, " ".join(str(report or "").split())[:300]
        self.save()
        self.close(self.key_of(goal, task), f"task done: {task.report}" if task.report else "task done")
        ready = [t for t in goal.tasks if t.status == PLANNED and all(
            (goal.task(a) or t).status == DONE for a in t.after)]
        then = (" Ready to start: " + "; ".join(f"{t.id}. {t.title}" for t in ready)) if ready else ""
        return _text(f"{ended}Task {task.id} is done.{then}")

    def ask_you(self, goal: Goal, question: str) -> dict:
        goal.asking = " ".join(str(question or "").split())[:200] or "The coordinator wants you"
        self._asked.add(goal.id)
        self.save()
        return _text("The user has your question. Their answer comes as their next message.")

    def goal_done(self, goal: Goal, summary: str) -> dict:
        goal.done = True
        goal.finished = " ".join(str(summary or "").split())[:200] or "every task is done"
        goal.asking = ""  # nothing left to ask about
        self._asked.discard(goal.id)
        for task in goal.tasks:  # a debate under way ends with its goal
            task.debate, task.rounds_left, task.held, task.agreed = 0, 0, "", False
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
                                   "brief": {"type": "string",
                                             "description": "What its session is to get done, and why: what, not how"},
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
            bound("debate", "Have two or more of the goal's sessions debate a question with each other: each "
                  "one's replies go to the others, word for word, then each side's last reply to you.", {
                "type": "object", "required": ["tasks", "rounds", "question"],
                "properties": {"tasks": {"type": "array", "items": {"type": "integer"}, "minItems": 2,
                                         "description": "The tasks that debate it, two or more, by their numbers"},
                               "rounds": {"type": "integer", "minimum": 1, "maximum": DEBATE_ROUNDS,
                                          "description": "How many times each answers the other, after their "
                                                         "openings"},
                               "question": {"type": "string",
                                            "description": "What they debate, and each side's position if it has "
                                                           "none yet"}}},
                  lambda g, a: self.debate(g, a.get("tasks"), a.get("rounds"), a.get("question", ""))),
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
