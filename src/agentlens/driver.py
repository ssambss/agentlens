"""Sessions agentlens runs itself, so you can type into them in its panes.

Each session runs your own installed, unmodified `claude` in headless mode (through the
Agent SDK, which speaks Claude Code's stream-json protocol), with the Claude Code
system prompt, tools and your user/project/local settings, so it behaves like the
sessions you run elsewhere. agentlens never reads or passes credentials: `claude`
uses whatever sign-in you completed through Anthropic's own flow.

Policy note (see README): agentlens never signs anyone in or handles a login. Each person's
own unmodified `claude` does, which Anthropic allows for Agent SDK use on Claude plans.
Keep it that way: no sign-in flow, token handling or shared logins here.

The session's messages become the same Event feed the monitored panes show. Permission
prompts, questions (AskUserQuestion) and plan approvals (ExitPlanMode) become a
PendingRequest the UI answers; the turn waits until it does. With autopilot on, a turn that
ends is read by it first, and it may answer for you (see agentlens.autopilot).
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentlens import autopilot as autopilot_mod
from agentlens import usage
from agentlens.adapters.claude_transcripts import (
    KEEP_PLANNING,
    PLAN_TOOL,
    QUESTION_TOOL,
    answer_event,
    ask_you_event,
    image_marks,
    plan_answer_event,
    plan_event,
    question_event,
    questions_of,
    tool_summary,
)
from agentlens.background import FINISHED, BackgroundWork
from agentlens.clipboard import PastedImage
from agentlens.coordinator import ASK_YOU_TOOL
from agentlens.commands import Catalog
from agentlens.config import repo_name
from agentlens.model import AUTOPILOT, Event
from agentlens.terminal import CLAUDE_ENV

# Live states for driven sessions, matching agentlens.live's names where they overlap.
STARTING = "starting"
WORKING = "working"
WAITING = "waiting on you"
BACKGROUND = "waiting on agents"  # live.BACKGROUND: its turn ended, and work it started brings it back
PERMISSION = "approve tool"
QUESTION = "question"
APPROVAL = "approve plan"
ENDED = "ended"
FAILED = "failed"
PAUSED = "paused"  # not running Claude Code (restored, or it stopped); connects when you next type

# The longest single message read from Claude Code. The SDK's default is 1 MiB, but a tool
# result that reads an image carries its base64 twice, so one screenshot can exceed it.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024

PERMISSION_MODES = ("default", "acceptEdits", "plan", "auto")

# How long a session autopilot left waiting on Claude's own work stays waiting on agents once none of
# that work is left and Claude Code has sent nothing, before it's yours after all. Work that ends
# starts Claude's next turn within seconds, and a retried model call shows in the stream meanwhile,
# so five quiet minutes means no turn is coming; any longer only keeps you from a turn that's yours.
QUIET_SECONDS = 5 * 60

# What Claude Code last wrote to stderr, kept to explain a failure. Its terminal codes are
# dropped: one it writes on exit is "show the cursor".
STDERR_LINES = 20
TERMINAL_CODES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|[\x00-\x08\x0b-\x1f\x7f]")


@dataclass
class PendingRequest:
    kind: str  # permission | question | plan
    tool_name: str
    tool_input: dict
    summary: str
    suggestions: list = field(default_factory=list)
    future: asyncio.Future | None = None
    answers: dict[str, str] = field(default_factory=dict)  # question text -> chosen label(s)

    @property
    def questions(self) -> list[dict]:
        return [q for q in self.tool_input.get("questions") or [] if isinstance(q, dict)]

    @property
    def current_question(self) -> dict | None:
        return next((q for q in self.questions if q.get("question") not in self.answers), None)


async def _with_images(text: str, images: list[PastedImage]):
    """Your message as Claude Code's stream-json takes one with images: the images first, then
    the text, as its VS Code extension sends a pasted image."""
    content: list[dict] = [{"type": "image", "source": {"type": "base64", "media_type": image.media_type,
                                                         "data": base64.b64encode(image.data).decode("ascii")}}
                           for image in images]
    if text:
        content.append({"type": "text", "text": text})
    yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}


def running(tool: str, summary: str) -> str:
    """What a session running a tool is doing, as its pane says: `running Bash: npm test`."""
    return f"running {tool}: {summary}" if summary else f"running {tool}"


def _started_by_its_own_work(result) -> bool:
    """Whether this turn began when a background task Claude had started, such as a test run, finished.
    Claude Code says what began each turn: a scheduled prompt or another session's message has a
    subkind, or another kind."""
    origin = getattr(result, "origin", None)
    return isinstance(origin, dict) and origin.get("kind") == "task-notification" and not origin.get("subkind")


def _from_another_session(message) -> tuple[str, str] | None:
    """A message another of your sessions sent this one, as Claude Code passes it on: who sent it and
    what it said. A background subagent reporting back is the session's own, not another's."""
    origin = getattr(message, "origin", None)
    if not isinstance(origin, dict):
        return None
    peer = origin.get("kind") == "peer" and not origin.get("senderTaskId")
    relayed = origin.get("kind") == "task-notification" and origin.get("subkind") == "peer-send-message"
    if not (peer or relayed):
        return None
    body = origin.get("body")
    if not isinstance(body, str):
        content = getattr(message, "content", None)
        body = content if isinstance(content, str) else "\n".join(
            getattr(b, "text", "") for b in content or [] if getattr(b, "text", None))
    return str(origin.get("name") or "another session"), body.strip()


def find_claude() -> str | None:
    """The user's own installed Claude Code. None lets the SDK use its bundled copy."""
    return shutil.which("claude")


def is_home(cwd: str) -> bool:
    return _norm(cwd) == _norm(str(Path.home()))


def folder_trust(cwd: str, config_file: Path | None = None) -> bool | None:
    """Whether terminal Claude Code trusts this folder (or a parent): True, False, or None if
    it has never been asked. Background sessions only start in trusted folders. Read-only,
    from Claude Code's own config file, whose format is internal: any surprise means None."""
    path = config_file or Path.home() / ".claude.json"
    try:
        projects = json.loads(path.read_text(encoding="utf-8")).get("projects") or {}
    except (OSError, ValueError, AttributeError):
        return None
    trust = {_norm(k): v.get("hasTrustDialogAccepted") for k, v in projects.items() if isinstance(v, dict)}
    folder = Path(cwd)
    for candidate in (folder, *folder.parents):
        value = trust.get(_norm(str(candidate)))
        if value is True:
            return True
        if value is False and candidate == folder:
            return False
    return None


def _norm(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").lower()


def default_client_factory(options):
    from claude_agent_sdk import ClaudeSDKClient

    return ClaudeSDKClient(options=options)


async def generate_title(client, about: str) -> str:
    """Claude Code's title for a session, saved to its transcript. Its control protocol has the
    request (as the TypeScript SDK's generateSessionTitle); the Python SDK has no call for it."""
    response = await client._query._send_control_request(
        {"subtype": "generate_session_title", "description": about[:1000], "persist": True})
    return str(response.get("title") or "").strip()


async def rename(client, name: str, session_id: str | None = None) -> None:
    """Name the session in Claude Code, as /rename does: the name your other sessions list it under
    and message it by. Unnamed, it's its folder's with two characters after it ("tubeless-d4"),
    alike for every session there. As the TypeScript SDK's renameSession; the Python SDK has no
    call for it. Given the session, Claude Code refuses it if it has moved on to another (/clear)."""
    request = {"subtype": "rename_session", "title": name, "source": "host"}  # host: the app it runs in
    if session_id:
        request["session_id"] = session_id
    await client._query._send_control_request(request)


class DrivenSession:
    def __init__(
        self,
        cwd: str,
        *,
        resume: str | None = None,
        permission_mode: str = "default",
        model: str | None = None,
        name: str | None = None,
        on_change: Callable[[DrivenSession], None] = lambda s: None,
        on_rate_limit: Callable[[list[usage.Window]], None] = lambda windows: None,
        client_factory: Callable[[Any], Any] = default_client_factory,
        cli_path: str | None = None,
        autopilot: str = "off",
        decide: Callable[..., Awaitable[autopilot_mod.Decision]] | None = None,
        on_pilot: Callable[[autopilot_mod.Entry], None] = lambda entry: None,
    ) -> None:
        self.key = "d-" + uuid.uuid4().hex[:10]  # stable id for the pane, before Claude Code assigns one
        self.cwd = cwd
        self.resume = resume
        self.session_id: str | None = resume
        self.permission_mode = permission_mode
        self.model = model
        self.name = name or f"new-{self.key[2:6]}"
        self.title = ""  # your first message until Claude Code titles the session (see _name)
        self.titled = False  # Claude Code has given it a title
        self.known_as = ""  # the name Claude Code has saved for it, which it takes back when resumed
        self.worktree = ""  # the Claude Code worktree it moved into, read from its transcript
        self.on_change = on_change
        self.on_rate_limit = on_rate_limit
        self.on_pilot = on_pilot  # what autopilot decided about a turn: answered it, or left it to you
        self.client_factory = client_factory
        self.cli_path = cli_path if cli_path is not None else find_claude()
        self.events: list[Event] = []
        self.partial = ""  # reply text streaming in right now
        self.state = STARTING
        self.detail = "starting Claude Code…"
        self.since = datetime.now(timezone.utc)
        self.started = self.since
        # The turn under way, for the line above the box: when it began (your message, or Claude
        # starting by itself), and the output tokens of the replies it has finished in it.
        self.turn_started: datetime | None = None
        self.turn_tokens = 0
        self._streamed: dict[str | None, int] = {}  # characters of each reply still streaming, by subagent
        self.pending: PendingRequest | None = None
        self._asked: dict[str, list[dict]] = {}  # questions Claude asked, by call, until they're answered
        self._plans: dict[str, bool] = {}  # plan calls, by id, until answered: whether the plan is shown
        self.cost_usd = 0.0
        self.error: str | None = None
        self.parked = False  # restored from last time: connects to Claude Code when you next type
        self.remembered = False  # saved so agentlens restores it on the next launch
        self.cleared: list[str] = []  # conversations /clear replaced, for the app to stop restoring
        self.catalog: Catalog | None = None  # its slash commands, once Claude Code has started
        self.images: list[PastedImage] = []  # pasted into the box, to go with your next message
        self._client = None
        self._reader: asyncio.Task | None = None
        self._naming: asyncio.Task | None = None  # asking Claude Code for its title
        self._renaming: asyncio.Task | None = None  # a resumed session taking its name back
        self._connecting: asyncio.Future | None = None  # a paused session starting Claude Code
        self._stderr: deque[str] = deque(maxlen=STDERR_LINES)
        self.autopilot = autopilot  # one of autopilot.LEVELS
        self.own_autopilot = False  # its level is this session's own, set with p, rather than your setting's
        self.piloted = 0  # autopilot's messages since yours
        self.decide = decide or self._decide
        self._piloting: asyncio.Task | None = None  # autopilot reading the turn that just ended
        self._interrupted = False  # you stopped the turn under way: autopilot leaves it to you
        # What Claude has left going that brings it back by itself (a command or subagent in the
        # background, a monitor, a wakeup), and whether it waits on agents because autopilot said it waits
        # on that, with the timer that makes it yours if nothing comes (QUIET_SECONDS).
        self.work = BackgroundWork()
        self._awaiting = False
        self._quiet: asyncio.TimerHandle | None = None
        # Whether agentlens sent a message (yours or autopilot's) since the last turn ended, and so
        # whether that turn was ours. Claude Code starts turns of its own too: another session's
        # message, a background task finishing. Your messages don't say what another session's are
        # about, so autopilot leaves those to you. A turn that Claude's own background work started,
        # such as a test run it left going, carries on the work and counts as ours.
        self._sent = False
        self._turn_ours = False
        self._yours_sent = False  # a message of yours, not autopilot's or the coordinator's, since the last turn ended
        self.last_yours: datetime | None = None  # when you last sent it one
        # The coordinator's, for its lead and the sessions it runs: what the session runs with on top of
        # Claude Code's own (the lead's instructions and tools), and what a turn ending does instead of
        # autopilot. That's given whether you sent a message in the turn, Claude's reply, and its error if
        # it failed, and says whether the session waits quietly and the line under its title, or None for
        # what it would do without it.
        self.options_extra: dict = {}
        self.on_turn_end: Callable[[DrivenSession, bool, str, str], tuple[bool, str] | None] | None = None

    # Lifecycle --------------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """Claude Code is running for it, so closing waits for that to exit."""
        return self._client is not None

    @property
    def tokens(self) -> int:
        """Output tokens so far this turn: the finished replies' counts, plus an estimate for the
        ones still streaming (about four characters a token) until Claude Code gives theirs."""
        return self.turn_tokens + sum(self._streamed.values()) // 4

    def options(self):
        from claude_agent_sdk import ClaudeAgentOptions

        return ClaudeAgentOptions(**{
            "cwd": self.cwd,
            "resume": self.resume,
            "permission_mode": self.permission_mode,
            "model": self.model,
            "include_partial_messages": True,
            "can_use_tool": self._can_use_tool,
            # Behave like Claude Code: its system prompt and tools, and the same settings,
            # CLAUDE.md, skills and permission rules your other sessions load.
            "system_prompt": {"type": "preset", "preset": "claude_code"},
            "tools": {"type": "preset", "preset": "claude_code"},
            "setting_sources": ["user", "project", "local"],
            "cli_path": self.cli_path,
            "max_buffer_size": MAX_MESSAGE_BYTES,
            # Piped, not shared with agentlens's terminal: written there, it lands on the screen,
            # and the "show the cursor" Claude Code sends on exit leaves a stray cursor showing.
            "stderr": self._on_stderr,
            "env": dict(CLAUDE_ENV),
            **self.options_extra,
        })

    def _on_stderr(self, line: str) -> None:
        if line := TERMINAL_CODES.sub("", line).strip():
            self._stderr.append(line)

    async def start(self, first_prompt: str | None = None, images: list[PastedImage] | None = None,
                    note: str = "", by: str = "") -> None:
        """`note` and `by`: as send's, for the first message."""
        try:
            self._client = self.client_factory(self.options())
            await self._client.connect()
        except Exception as e:  # noqa: BLE001 - shown in the pane, never fatal to the app
            await self._stopped(self._client, f"Couldn't start Claude Code: {e}")
            return
        try:
            info = await self._client.get_server_info() if hasattr(self._client, "get_server_info") else None
        except Exception:  # noqa: BLE001 - the command menu just keeps the list it had
            info = None
        if info:
            self.catalog = Catalog.from_server_info(info)
        self.error = None
        self._reader = asyncio.ensure_future(self._read(self._client))
        self._set(WAITING, "ready")
        if self.resume and self.title and not self.known_as:
            # One from before agentlens named its sessions, or one Claude Code never titled until now:
            # a named one takes its name back by itself, and a name you gave it stays.
            self._renaming = asyncio.ensure_future(self._give_name(self._client))
        if first_prompt or images:
            self.images = [*(images or []), *self.images]  # the first message's, then any pasted since
            await self.send(first_prompt or "", note, by=by)

    def park(self, events: list[Event]) -> None:
        """Show a resumed session's past conversation without starting Claude Code yet."""
        self.parked = True
        self.events = list(events)
        if events:
            self.since = events[-1].at
        self.state, self.detail = PAUSED, "from last time · type to continue"
        self.on_change(self)

    async def wake(self, why: str = "ready") -> None:
        """Start Claude Code for a paused session without a message, under its name: another session
        can message it then. Paused, Claude Code doesn't list it, so nothing could. `why`: what the
        pane says while it waits."""
        if self.parked:
            self.parked = False
            self._set(STARTING, "starting Claude Code…")
            self._connecting = asyncio.ensure_future(self.start())
        if self._connecting is not None:
            await asyncio.shield(self._connecting)
        if self._renaming is not None:
            await asyncio.shield(self._renaming)  # a message for it finds it by that name
        if self._client is not None and (self.state, self.detail) == (WAITING, "ready"):
            self._set(WAITING, why)

    async def send(self, text: str, note: str = "", first: Awaitable | None = None, by_autopilot: bool = False,
                   by: str = "") -> None:
        """Your message, and a note for Claude with it that the pane doesn't show as yours: what the
        sessions it names are. `first`: what Claude reading it waits for, those sessions starting.
        `by_autopilot`: autopilot's message, which the pane shows as its own. `by`: another sender's
        tag, the coordinator's (coordinator.COORDINATOR, coordinator.BOARD), shown as theirs."""
        text = text.strip()
        starting = self._connecting is not None and not self._connecting.done()
        images = [] if text.startswith("/") else self.images  # a command leaves them for the next message
        if not (text or images) or (self._client is None and not self.parked and not starting):
            return
        if images:
            self.images = []
        by = AUTOPILOT if by_autopilot else by
        now = datetime.now(timezone.utc)
        if not by:
            self.piloted = 0
            self._yours_sent, self.last_yours = True, now
            if self._cancel_pilot() and self.state == WORKING:
                self.state, self.turn_started = WAITING, None  # yours is the next turn, not one it was reading
        busy = self.state == WORKING and not by
        shown = f"{image_marks(len(images))} {text}".strip()
        tag = by or ("queued" if busy else "")
        self.events.append(Event(now, "you", shown.splitlines()[0][:240], shown, tag=tag))
        self._sent = True
        if not self.title and text and not text.startswith("/") and not by:
            self.title = text.splitlines()[0][:80]
        if self._client is None or starting:
            # Show the message right away: starting Claude Code takes a few seconds.
            if self.parked:
                self.parked = False
                self._connecting = asyncio.ensure_future(self.start())
            if not busy:
                self._set(WORKING, "starting Claude Code…")
            else:
                self.on_change(self)
            await asyncio.shield(self._connecting)
            if self._client is None:
                return  # start() has shown why
        if first is not None:
            if not busy:
                self._set(WORKING, "starting the sessions it names…")
            await first
            if self._client is None:
                return  # closed meanwhile
        if not busy:
            self._set(WORKING, "thinking")
        else:
            self.on_change(self)
        client = self._client
        said = f"{text}\n\n{note}" if note and not text.startswith("/") else text
        try:
            await client.query(_with_images(said, images) if images else said)
        except Exception as e:  # noqa: BLE001
            await self._stopped(client, f"Couldn't send that to Claude Code ({e}), so agentlens stopped it.")
            return
        if not self.titled and self._naming is None and text and not text.startswith("/") and not by:
            # What it's about: your first message, which it's called by until now, and this one.
            about = text if text.startswith(self.title) else f"{self.title}\n{text}"
            self._naming = asyncio.ensure_future(self._name(client, about))

    async def _name(self, client, about: str) -> None:
        """Have Claude Code title the session. It does that by itself in a terminal, but not for
        the SDK, so its sessions stayed called by your whole first message. It saves the title to
        the transcript, where History and /resume find it, and keeps the one it has, if any.
        Too little to go on ("test") gets no title: the next message asks again."""
        try:
            title = await generate_title(client, about)
        except Exception:  # noqa: BLE001 - it keeps your first message as its title
            title = ""
        finally:
            self._naming = None
        if title and client is self._client:
            self.title, self.titled = title[:80], True
            self.on_change(self)
            await self._give_name(client)

    async def _give_name(self, client) -> None:
        """Name the session after its title in Claude Code, so your other sessions, and Claude in
        them, can tell it from the others in its folder. Once: Claude Code keeps the name, and gives
        it back when the session is resumed. An older Claude Code keeps its own name."""
        if self.known_as:
            return
        title = self.title
        try:
            await rename(client, title, self.session_id)
        except Exception:  # noqa: BLE001 - it's still the session it was, under Claude Code's name
            return
        self.known_as = title

    async def interrupt(self) -> None:
        if self._cancel_pilot():
            self._note("Autopilot stopped reading the reply: this one is yours.")
            self._set(WAITING, "turn finished")
            return
        if self._client is not None and self.state in (WORKING, PERMISSION, QUESTION, APPROVAL):
            self._interrupted = True
            if self.pending and self.pending.future and not self.pending.future.done():
                self._resolve(self._deny("Interrupted by the user.", interrupt=True))
            try:
                await self._client.interrupt()
            except Exception as e:  # noqa: BLE001
                self._note(f"interrupt failed: {e}", kind="error")

    async def close(self) -> None:
        self._stop_waiting()
        for task in (self._reader, self._naming, self._renaming, self._piloting):
            if task is not None:
                task.cancel()
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001 - closing anyway
                pass
        self._client = None
        if self.state not in (FAILED,):
            self._set(ENDED, "closed")

    # Answering requests -----------------------------------------------------------------

    def allow(self, always: bool = False) -> None:
        p = self.pending
        if p is None or p.kind != "permission":
            return
        self._resolve(self._allow(p.tool_input, p.suggestions if always else None))

    def deny(self, reason: str = "") -> None:
        p = self.pending
        if p is None:
            return
        if p.kind == "plan":
            message = reason or KEEP_PLANNING
        elif p.kind == "question":
            message = reason or "The user declined to answer."
        else:
            message = reason or "The user denied this."
        self._resolve(self._deny(message))

    def approve_plan(self) -> None:
        p = self.pending
        if p is not None and p.kind == "plan":
            self._resolve(self._allow(p.tool_input, None))

    def choose(self, number: int) -> None:
        """Pick option `number` (1-based) for the current question."""
        p = self.pending
        q = p.current_question if p and p.kind == "question" else None
        options = [o for o in (q or {}).get("options") or [] if isinstance(o, dict)]
        if q is None or not 1 <= number <= len(options):
            return
        self.answer(str(options[number - 1].get("label", "")))

    def answer(self, text: str) -> None:
        """Answer the current question with an option label or free text ("Other")."""
        p = self.pending
        q = p.current_question if p and p.kind == "question" else None
        if q is None or not text.strip():
            return
        p.answers[str(q.get("question"))] = text.strip()
        if p.current_question is None:
            # The SDK's documented shape: the original questions plus answers keyed by question text.
            self._resolve(self._allow({**p.tool_input, "answers": dict(p.answers)}, None))
        else:
            self.on_change(self)

    def say_quietly(self, detail: str) -> None:
        """Change the line under its title while it waits quietly (the coordinator's session, done),
        without calling you."""
        if self.state == BACKGROUND:
            self._set(BACKGROUND, detail[:200])

    # Autopilot --------------------------------------------------------------------------

    def set_autopilot(self, level: str, read_now: bool = True) -> None:
        """Turn autopilot to `level` (one of autopilot.LEVELS). Turned on while the session waits on
        you after a turn, it reads that turn now, unless `read_now` is False (your setting changing
        for every session: then it starts at the next turn). Turned off, it stops reading one."""
        self.autopilot, self.piloted = level, 0
        reading = self._cancel_pilot()
        if level != "off" and self.state in (WAITING, WORKING) and (reading or read_now and self.state == WAITING) \
                and self._turn_ours and self._client is not None and self.pending is None and self._reply():
            self._set(WORKING, "autopilot is reading the reply")
            self._piloting = asyncio.ensure_future(self._pilot())
        elif reading:
            self._set(WAITING, "turn finished")
        else:
            self.on_change(self)

    def _pilot_due(self, message) -> bool:
        return (self.autopilot != "off" and self._turn_ours and not self._interrupted
                and not getattr(message, "is_error", False) and self._client is not None and bool(self._reply()))

    def _cancel_pilot(self) -> bool:
        """Stop autopilot reading a turn. Whether it was."""
        task, self._piloting = self._piloting, None
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def _said(self) -> list[tuple[str, bool]]:
        """Your last messages, oldest first, each with whether autopilot sent it."""
        said = [(e.detail or e.text, e.tag == AUTOPILOT) for e in self.events if e.kind == "you"]
        return said[-autopilot_mod.SAID:]

    def _reply(self) -> str:
        """What Claude has said since your last message: the turn autopilot reads. Nothing after a
        slash command, whose output is Claude Code's."""
        texts = []
        for e in reversed(self.events):
            if e.kind == "you":
                if (e.detail or e.text).startswith("/"):
                    return ""
                break
            if e.kind == "claude" and e.tag != "command":
                texts.append(e.detail or e.text)
        return "\n\n".join(reversed(texts))

    async def _decide(self, level: str, title: str, said: list[tuple[str, bool]], reply: str):
        return await asyncio.to_thread(autopilot_mod.decide, level, title, said, reply, self.cli_path)

    async def _pilot(self) -> None:
        """Have autopilot answer the turn that has just ended, or leave it for you saying why."""
        try:
            decision = await self.decide(self.autopilot, self.title, self._said(), self._reply())
            failed = ""
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - it's yours, as it would be without autopilot
            decision, failed = None, str(e) or type(e).__name__
        if self._piloting is not asyncio.current_task() or self._client is None:
            return  # stopped meanwhile
        self._piloting = None
        in_a_row = autopilot_mod.IN_A_ROW.get(self.autopilot, 0)
        if decision is not None and decision.send and self.piloted < in_a_row:
            self.piloted += 1
            self.turn_started = None  # its message starts the next turn
            self.on_pilot(self._entry(decision.reason, decision.send))
            await self.send(decision.send, autopilot_mod.NOTE, by_autopilot=True)
            return
        if decision is not None and decision.waiting and self.work.outstanding():
            # Nothing to answer, nor yours yet: what Claude waits on starts its next turn. With none of its
            # work left going, nothing would (a CI run, another session's work): then the turn is yours.
            self._set(BACKGROUND, decision.reason[:200] or "Claude is waiting on work it started")
            self._awaiting = True
            self._time_the_quiet()
            return
        if decision is not None:
            self.on_pilot(self._entry(decision.reason))  # left to you
        if decision is None:
            note, detail = f"Autopilot couldn't decide ({failed[:200]}), so this one is yours.", "turn finished"
        elif decision.send:
            note = (f"Autopilot has answered {in_a_row} times in a row, so this one is yours. "
                    f"It would have said: {decision.send}")
            detail = f"autopilot: {in_a_row} answers in a row, over to you"
        else:
            # The reply above says it already; the line under its title is for the wall.
            note, detail = "", decision.reason or "turn finished"
        if note:
            self._note(note)
        self._set(WAITING, detail[:200])

    def _time_the_quiet(self) -> None:
        """While it waits on Claude's work, (re)start the wait that makes the turn yours: QUIET_SECONDS
        from now once none of that work is left, or from when what's left lapses by its time (a wakeup,
        say). Work that only its end can finish, like a background command, holds it off until then."""
        if self._quiet is not None:
            self._quiet.cancel()
            self._quiet = None
        lapses = self.work.lapses_in() if self._awaiting else None
        if lapses is None:
            return
        try:
            self._quiet = asyncio.get_running_loop().call_later(lapses + QUIET_SECONDS, self._quiet_too_long)
        except RuntimeError:
            pass  # no loop running: nothing would come either

    def _quiet_too_long(self) -> None:
        self._quiet = None
        if not self._awaiting or self.state != BACKGROUND:
            return
        if self.work.outstanding():
            self._time_the_quiet()  # it went on with more meanwhile
            return
        self._note("The work Claude was waiting on has ended and Claude hasn't carried on, so this one is yours.")
        self._set(WAITING, "Claude didn't carry on after the work it waited on")

    def _stop_waiting(self) -> None:
        self._awaiting = False
        if self._quiet is not None:
            self._quiet.cancel()
            self._quiet = None

    def _entry(self, asked: str, said: str = "") -> autopilot_mod.Entry:
        # As its pane says it: the worktree, then the repository ("linux-build · Tubeless"), not the
        # worktree's folder twice.
        where = " · ".join(dict.fromkeys(part for part in (self.worktree, repo_name(self.cwd)) if part))
        return autopilot_mod.Entry(datetime.now(timezone.utc), self.session_id or "", self.title, where, asked, said)

    # Internals --------------------------------------------------------------------------

    async def _can_use_tool(self, tool_name: str, tool_input: dict, context) -> Any:
        if tool_name == "AskUserQuestion":
            kind, state, detail = "question", QUESTION, "asked you a question"
        elif tool_name == PLAN_TOOL:
            kind, state, detail = "plan", APPROVAL, "plan waiting for your approval"
            self._show_plan(getattr(context, "tool_use_id", None), tool_input)
        else:
            kind, state, detail = "permission", PERMISSION, f"wants to use {tool_name}"
        summary = getattr(context, "title", None) or tool_summary(tool_input, self.cwd)
        future = asyncio.get_running_loop().create_future()
        self.pending = PendingRequest(kind, tool_name, tool_input, summary,
                                      list(getattr(context, "suggestions", None) or []), future)
        self._set(state, detail)
        try:
            return await future
        finally:
            self.pending = None
            if self.state in (PERMISSION, QUESTION, APPROVAL):
                self._set(WORKING, running(tool_name, tool_summary(tool_input, self.cwd)) if kind == "permission"
                          else "thinking")  # over your answer, or the plan you approved

    def _show_plan(self, call: str | None, tool_input: dict) -> None:
        """Put a plan in the conversation, once: its call and the request to approve it can each
        carry it, and either can come first. The call is noted either way, so its result reads as
        your answer."""
        if call and self._plans.setdefault(call, False):
            return
        plan = plan_event(datetime.now(timezone.utc), tool_input)
        if plan is None:
            return  # the other one has it
        if call:
            self._plans[call] = True
        self.events.append(plan)

    def _resolve(self, result) -> None:
        p = self.pending
        if p and p.future and not p.future.done():
            p.future.set_result(result)

    @staticmethod
    def _allow(tool_input: dict, permissions):
        from claude_agent_sdk import PermissionResultAllow

        return PermissionResultAllow(updated_input=tool_input, updated_permissions=permissions)

    @staticmethod
    def _deny(message: str, interrupt: bool = False):
        from claude_agent_sdk import PermissionResultDeny

        return PermissionResultDeny(message=message, interrupt=interrupt)

    async def _read(self, client) -> None:
        try:
            async for message in client.receive_messages():
                self.handle(message)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            if "maximum buffer size" in str(e):
                why = (f"Claude Code sent one message over {MAX_MESSAGE_BYTES // 2**20} MB (a large image or "
                       "file?), more than agentlens reads, so agentlens stopped it.")
            else:
                why = f"Claude Code stopped: {e}"
            await self._stopped(client, why)
            return
        await self._stopped(client, "Claude Code exited.")

    async def _stopped(self, client, why: str) -> None:
        """Claude Code stopped under this session: shut it down and pause the session, so your
        next message starts it again on the same conversation rather than leaving the pane dead."""
        if client is not self._client:
            return  # already handled, or a client this session has since replaced
        self._client = None
        self._cancel_pilot()
        self.work.clear()  # what it had going stopped with it
        if self.pending and self.pending.future and not self.pending.future.done():
            self.pending.future.cancel()
        self.pending = None
        self.partial = ""
        if client is not None:
            try:
                await client.disconnect()  # it may still be running: one client at a time on a session
            except Exception:  # noqa: BLE001 - stopping anyway
                pass
        if not self.session_id:
            self._fail(why)  # it never started a conversation to come back to
            return
        self.resume, self.parked, self.error = self.session_id, True, why
        self.events.append(Event(datetime.now(timezone.utc), "error", why[:240], why))
        self._set(PAUSED, "Claude Code stopped · type to continue")

    def handle(self, message) -> None:
        """Fold one SDK message into the feed and state. Public for tests."""
        self._fold(message)
        if self._awaiting:
            self._time_the_quiet()  # anything from Claude Code starts the quiet over

    def _fold(self, message) -> None:
        kind = type(message).__name__
        now = datetime.now(timezone.utc)
        if kind in ("StreamEvent", "AssistantMessage") and self.state in (WAITING, BACKGROUND):
            self._set(WORKING, "thinking")
        if kind == "StreamEvent":
            event = getattr(message, "event", {}) or {}
            stream = getattr(message, "parent_tool_use_id", None)  # None: the main conversation
            delta = event.get("delta") or {}
            if event.get("type") == "message_start":
                self._streamed[stream] = 0
            elif event.get("type") == "content_block_delta":
                streamed = delta.get("text") or delta.get("thinking") or delta.get("partial_json") or ""
                self._streamed[stream] = self._streamed.get(stream, 0) + len(streamed)
            elif event.get("type") == "message_delta" and "output_tokens" in (event.get("usage") or {}):
                self.turn_tokens += event["usage"]["output_tokens"] or 0  # the reply's own count, at its end
                self._streamed.pop(stream, None)
            if stream is not None:
                return
            if delta.get("type") == "text_delta":
                self.partial += delta.get("text", "")
                self.on_change(self)
            elif event.get("type") == "content_block_start":
                block = event.get("content_block") or {}
                doing = {"thinking": "thinking", "redacted_thinking": "thinking", "text": "writing",
                         "tool_use": f"preparing {block.get('name') or 'a tool call'}"}.get(block.get("type"))
                if doing and doing != self.detail:
                    self.detail = doing
                    self.on_change(self)
            return
        if kind == "AssistantMessage":
            if getattr(message, "parent_tool_use_id", None):
                self.detail = "a subagent is working"
                self.on_change(self)
                return
            # A slash command's output (/context, /model...) comes from Claude Code, not the model.
            tag = "command" if getattr(message, "model", "") == "<synthetic>" else ""
            for block in getattr(message, "content", None) or []:
                block_kind = type(block).__name__
                if block_kind == "TextBlock" and block.text.strip():
                    self.partial = ""
                    first = next((ln for ln in block.text.splitlines() if ln.strip()), "")
                    self.events.append(Event(now, "claude", first[:240], block.text, tag=tag))
                elif block_kind == "ToolUseBlock":
                    self.work.called(block.id, block.name, block.input or {})
                    summary = tool_summary(block.input or {}, self.cwd)
                    if block.name == QUESTION_TOOL:  # what it asked you, not a tool call
                        self._asked[block.id] = questions_of(block.input or {})
                        self.events.append(question_event(now, block.input or {}))
                    elif block.name == ASK_YOU_TOOL and (lead_asked := ask_you_event(now, block.input or {})):
                        self.events.append(lead_asked)  # a coordinator's lead asking you: also not a tool call
                    elif block.name == PLAN_TOOL:  # the plan you're to approve, not a tool call
                        self._show_plan(block.id, block.input or {})
                    else:
                        self.events.append(Event(now, "tool", f"{block.name}: {summary}" if summary else block.name))
                    self.detail = running(block.name, summary)
            if getattr(message, "session_id", None):
                self.session_id = message.session_id
            self.on_change(self)
            return
        if kind == "UserMessage":
            if (said := _from_another_session(message)) is not None:
                name, body = said
                first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
                self.events.append(Event(now, "notice", f"From {name}: {first}"[:240], body[:4000]))
            elif getattr(message, "parent_tool_use_id", None) is None:
                # Claude Code's own messages to Claude: a task's notification, an idle notice.
                content = getattr(message, "content", None)
                self.work.told(content if isinstance(content, str) else "\n".join(
                    getattr(b, "text", "") for b in content or [] if isinstance(getattr(b, "text", None), str)))
            for block in getattr(message, "content", None) or []:
                if type(block).__name__ != "ToolResultBlock":
                    continue
                if getattr(message, "parent_tool_use_id", None) is None:
                    self.detail = "thinking"  # over what the tool gave back, until its next reply starts
                content = block.content if isinstance(block.content, str) else " ".join(
                    c.get("text", "") for c in block.content or [] if isinstance(c, dict))
                self.work.answered(block.tool_use_id, bool(getattr(block, "is_error", False)),
                                   getattr(message, "tool_use_result", None), content)
                questions = self._asked.pop(block.tool_use_id, None)
                if questions is not None:
                    self.events.append(answer_event(now, questions, getattr(message, "tool_use_result", None),
                                                    content, bool(getattr(block, "is_error", False))))
                elif self._plans.pop(block.tool_use_id, None) is not None:  # approved, or sent back
                    self.events.append(plan_answer_event(now, content, bool(getattr(block, "is_error", False))))
                elif getattr(block, "is_error", False):
                    first = next((ln for ln in content.splitlines() if ln.strip()), "tool error")
                    self.events.append(Event(now, "error", first[:240], content[:4000]))
            self.on_change(self)
            return
        if kind == "TaskNotificationMessage":
            self.work.ended(getattr(message, "task_id", None))
            self.events.append(Event(now, "notice", getattr(message, "summary", "") or "background task update"))
            self.on_change(self)
            return
        if kind == "TaskStartedMessage":
            self.work.started(getattr(message, "task_id", None), getattr(message, "task_type", None),
                              getattr(message, "description", "") or "", getattr(message, "tool_use_id", None))
            return
        if kind == "TaskUpdatedMessage":
            if getattr(message, "status", None) in FINISHED:  # some tasks end saying only this
                self.work.ended(getattr(message, "task_id", None))
            return
        if kind == "SystemMessage":
            data = getattr(message, "data", {}) or {}
            subtype = getattr(message, "subtype", "")
            if subtype == "init" and data.get("session_id"):
                self.session_id = data["session_id"]
            elif subtype == "compact_boundary":
                self.events.append(Event(now, "compact", "context compacted"))
            elif subtype == "session_title_changed" and data.get("title"):
                self.title, self.titled = str(data["title"])[:80], True  # /rename, or after /clear
                self.session_id = data.get("session_id") or self.session_id
            self.on_change(self)
            return
        if kind == "RateLimitEvent":
            windows = usage.from_rate_limit(getattr(message, "rate_limit_info", None))
            if windows:
                self.on_rate_limit(windows)
            return
        if kind == "ConversationResetMessage":
            # /clear: Claude Code starts a new conversation (a new session id, which the next
            # message carries). The old one stays in History.
            if self.session_id:
                self.cleared.append(self.session_id)
            self.session_id = None
            self.events.append(Event(now, "notice", "Context cleared: a new conversation starts here. "
                                                    "The earlier one is in History."))
            self.on_change(self)
            return
        if kind == "ResultMessage":
            self.partial = ""
            self.turn_started = None
            # A message of yours sent while Claude worked: you answered this turn yourself. It went in at a
            # step of the turn, or goes in now as the next one, and autopilot's answer would only follow it.
            queued = False
            for event in self.events:
                if event.tag == "queued":
                    event.tag, queued = "", True  # whatever was queued has been read by now
            self.session_id = getattr(message, "session_id", None) or self.session_id
            self.cost_usd += getattr(message, "total_cost_usd", None) or 0.0
            failed = ""
            if getattr(message, "is_error", False):
                errors = getattr(message, "errors", None) or [getattr(message, "result", "") or "the turn failed"]
                failed = str(errors[0])[:240]
                self.events.append(Event(now, "error", failed, "\n".join(map(str, errors))))
            self._cancel_pilot()  # another turn ended meanwhile (another session's message started it)
            self.work.turn_ended(getattr(message, "origin", None), ours=self._sent)
            self._turn_ours, self._sent = self._sent or _started_by_its_own_work(message), False
            yours, self._yours_sent = self._yours_sent, False
            instead = None
            if self.on_turn_end is not None and not self._interrupted:
                try:
                    instead = self.on_turn_end(self, yours, self._reply(), failed)
                except Exception as e:  # noqa: BLE001 - it's yours, as it would be without the coordinator
                    self.events.append(Event(now, "error", f"The coordinator couldn't take this turn: {e}"[:240]))
            if instead is not None:
                quiet, detail = instead
                self._set(BACKGROUND if quiet else WAITING, detail[:200])
            elif self.on_turn_end is None and not queued and self._pilot_due(message):
                self._set(WORKING, "autopilot is reading the reply")
                self._piloting = asyncio.ensure_future(self._pilot())
            else:
                self._set(WAITING, "turn finished")
            self._interrupted = False

    def _note(self, text: str, kind: str = "notice") -> None:
        self.events.append(Event(datetime.now(timezone.utc), kind, text[:240], text))
        self.on_change(self)

    def _set(self, state: str, detail: str) -> None:
        self._stop_waiting()  # a new turn, your message, closing or stopping: whatever it is, it's not that wait
        now = datetime.now(timezone.utc)
        if state != self.state:
            self.since = now
        if state == WORKING and self.turn_started is None:
            self.turn_started, self.turn_tokens, self._streamed = now, 0, {}
        elif state in (PAUSED, ENDED, FAILED):
            self.turn_started = None  # Claude Code stopped, maybe mid-turn
        self.state, self.detail = state, detail
        self.on_change(self)

    def _fail(self, message: str) -> None:
        self.error = message
        detail = message + ("\n\nClaude Code's last output:\n" + "\n".join(self._stderr) if self._stderr else "")
        self.events.append(Event(datetime.now(timezone.utc), "error", message[:240], detail))
        self._set(FAILED, message[:120])
