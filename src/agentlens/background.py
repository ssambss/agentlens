"""What a session agentlens runs has left going that brings Claude back by itself: a command or a
subagent running in the background, a monitor, a wakeup or a cron it scheduled, a wait for another
session to go idle. Autopilot lets a session wait on that without calling you only while some of
it is still to come (see agentlens.driver).

Read from the Agent SDK's stream. Claude Code reports each task it starts (task_started) and its
end (task_notification, or a task_updated to a finished status), by the task id that the tool's
result and the notification Claude gets carry too. Schedules and idle subscriptions aren't tasks:
they're read from the tool calls and their results, and end at their time, when they're cancelled,
or with the notice they send.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

# Tasks whose end Claude Code tells the session about, which starts its next turn: commands and
# monitors (local_bash), subagents, workflows, remote agents, MCP monitors. Its other tasks (memory
# upkeep, teammates) don't bring it back, so they aren't counted.
WAKING_TASKS = frozenset({"local_bash", "local_agent", "local_workflow", "remote_agent", "monitor_mcp", "monitor_ws"})
FINISHED = frozenset({"completed", "failed", "stopped", "killed"})  # as the SDK's TERMINAL_TASK_STATUSES
# Claude Code sends one notice for each idle subscription, but the notice can arrive inside a turn
# agentlens started, where the stream may not show it. Past this, one counts as answered, so a
# notice missed can't keep a session waiting for good.
SUBSCRIPTION_LIFE = 60 * 60
# The calls whose results say what they started or ended.
TOOLS = frozenset({"Bash", "Monitor", "Agent", "Task", "ScheduleWakeup", "CronCreate", "CronDelete", "SendMessage",
                   "TaskStop", "KillShell", "KillBash"})

TASK_ID = re.compile(r"<task-id>\s*([^<\s]+)\s*</task-id>")
STATUS = re.compile(r"<status>\s*([a-z_]+)\s*</status>")
# The notice an idle subscription ends with, and the session it names: idle now, or exited. Or, worded
# otherwise, that none will come: the subscription expired, or the session isn't holding it.
IDLE_NOTICE = re.compile(r'\[Cross-session idle notice\]\s*(?:"([^"]+)")?')
IDLE_OVER = re.compile(r"idle subscription expired|is not holding (?:your|the) idle subscription")


@dataclass
class _Subscription:
    to: str
    since: float


class BackgroundWork:
    def __init__(self) -> None:
        self.clear()

    def clear(self) -> None:
        """Nothing outstanding: Claude Code stopped, and what it had going went with it."""
        self.tasks: dict[str, str] = {}  # task id -> what it is
        self._ended: set[str] = set()  # tasks over, so a result read after their end doesn't count them again
        self._calls: dict[str, tuple[str, dict]] = {}  # tool calls whose results are still to come
        self._by_call: dict[str, str] = {}  # the task each call started, by the call
        self._schedules: dict[str, tuple[float | None, bool]] = {}  # "wakeup", "cron:<id>" -> when, recurring
        self._subscriptions: dict[str, _Subscription] = {}  # by the SendMessage call that made one
        self._noticed = False  # an idle notice came in the turn under way

    def outstanding(self, now: float | None = None) -> list[str]:
        """What is still to bring Claude back, as a line each; empty if nothing."""
        now = time.time() if now is None else now
        self._lapse(now)
        return [*self.tasks.values(),
                *(f"waiting for {s.to} to be idle" for s in self._subscriptions.values()),
                *("a wakeup" if key == "wakeup" else "a scheduled prompt" for key in self._schedules)]

    def lapses_in(self, now: float | None = None) -> float | None:
        """Seconds until all that's outstanding is over by its time alone; None if some of it has no time
        to be over by (a task, a recurring cron), so only its end can say. 0 if nothing is outstanding."""
        now = time.time() if now is None else now
        self._lapse(now)
        if self.tasks or any(at is None or recurring for at, recurring in self._schedules.values()):
            return None
        ends = [s.since + SUBSCRIPTION_LIFE for s in self._subscriptions.values()]
        ends += [at for at, _ in self._schedules.values() if at is not None]
        return max([end - now for end in ends], default=0.0)

    # From the stream ----------------------------------------------------------------------

    def started(self, task_id: str | None, task_type: str | None, description: str = "",
                call_id: str | None = None) -> None:
        """A task_started: Claude Code began a task, for the tool call `call_id`. One with no type is from an
        older Claude Code."""
        if task_id and task_id not in self._ended and (task_type is None or task_type in WAKING_TASKS):
            self.tasks[task_id] = description or task_type or "a background task"
            if call_id:
                self._by_call[call_id] = task_id

    def ended(self, task_id: str | None) -> None:
        if task_id:
            self.tasks.pop(task_id, None)
            self._ended.add(task_id)

    def called(self, call_id: str, tool: str, tool_input: dict) -> None:
        """A tool call in the main conversation."""
        if tool in TOOLS:
            self._calls[call_id] = (tool, tool_input if isinstance(tool_input, dict) else {})

    def answered(self, call_id: str, is_error: bool, result, text: str = "") -> None:
        """A call's result: `result` is its structured output (tool_use_result), `text` what Claude read."""
        tool, given = self._calls.pop(call_id, ("", {}))
        result = result if isinstance(result, dict) else {}
        # A subagent or command that ran to its end in the call, or failed, is over, whatever its task said.
        if is_error or (tool == "Bash" and not result.get("backgroundTaskId")) or (
                tool in ("Agent", "Task") and not result.get("isAsync")):
            self.ended(self._by_call.pop(call_id, None))
        if not tool or is_error:
            return  # a call that failed started nothing, and ended nothing
        if tool == "Bash":  # in the background as asked, or moved there while it ran
            self._task(result.get("backgroundTaskId"), given.get("description") or "a background command")
        elif tool == "Monitor":
            self._task(result.get("taskId"), given.get("description") or "a monitor")
        elif tool in ("Agent", "Task") and result.get("isAsync"):
            self._task(result.get("agentId"), given.get("description") or "a background subagent")
        elif tool in ("TaskStop", "KillShell", "KillBash"):
            self.ended(given.get("task_id") or given.get("shell_id"))
        elif tool == "ScheduleWakeup":  # one at a time: a new one replaces it, and stop cancels it
            when = result.get("scheduledFor")
            if result.get("stopped") or given.get("stop"):
                self._schedules.pop("wakeup", None)
            elif isinstance(when, (int, float)) and when > 0:
                self._schedules["wakeup"] = (when / 1000, False)
        elif tool == "CronCreate" and result.get("id"):
            when = result.get("nextFireAt")
            self._schedules[f"cron:{result['id']}"] = (when / 1000 if isinstance(when, (int, float)) else None,
                                                       bool(result.get("recurring", given.get("recurring", True))))
        elif tool == "CronDelete" and given.get("id"):
            self._schedules.pop(f"cron:{given['id']}", None)
        elif tool == "SendMessage" and given.get("notify_when_idle") and result.get("success") is not False:
            said = str(result.get("message") or text)
            if "no idle notice will come" not in said:
                self._subscriptions[call_id] = _Subscription(str(given.get("to") or "another session"), time.time())

    def told(self, text: str) -> None:
        """What Claude Code told Claude in a message of its own: a task's notification, an idle notice."""
        for notification in text.split("<task-notification>")[1:]:  # several can come in one message
            task, status = TASK_ID.search(notification), STATUS.search(notification)
            if task and status and status[1] in FINISHED:  # a monitor's events say no status: it goes on
                self.ended(task[1])
        names = [m[1] or "" for m in IDLE_NOTICE.finditer(text)] or ["" for _ in IDLE_OVER.finditer(text)]
        for name in names:
            self._noticed = True
            self._drop_subscription(name)

    def turn_ended(self, origin, ours: bool, now: float | None = None) -> None:
        """A turn's end. `origin`: what Claude Code says started it. `ours`: agentlens sent a message in it."""
        now = time.time() if now is None else now
        kind = origin.get("kind") if isinstance(origin, dict) else None
        subkind = origin.get("subkind") if isinstance(origin, dict) else None
        if kind == "task-notification" and subkind == "scheduled-trigger":
            for key, (at, recurring) in list(self._schedules.items()):
                if not recurring and (at is None or at <= now):
                    del self._schedules[key]  # fired
        elif not ours and kind in (None, "unclassified") and not self._noticed:
            # Nothing it says began it, and the idle notice that likely did didn't show: one is answered.
            self._drop_subscription("")
        self._noticed = False

    # Internals ----------------------------------------------------------------------------

    def _task(self, task_id, what: str) -> None:
        if isinstance(task_id, str) and task_id and task_id not in self._ended:
            self.tasks.setdefault(task_id, what)

    def _drop_subscription(self, name: str) -> None:
        """The one waiting for `name`, or else the oldest."""
        key = next((k for k, s in self._subscriptions.items() if name and s.to == name), None)
        key = key or next(iter(self._subscriptions), None)
        if key is not None:
            del self._subscriptions[key]

    def _lapse(self, now: float) -> None:
        """Wakeups and one-off prompts whose time has come, and subscriptions past their life."""
        for key, (at, recurring) in list(self._schedules.items()):
            if at is not None and not recurring and at <= now:
                del self._schedules[key]
        for key, sub in list(self._subscriptions.items()):
            if now - sub.since >= SUBSCRIPTION_LIFE:
                del self._subscriptions[key]
