"""Experimental: sessions agentlens runs itself, so you can type into them in its panes.

Each session runs your own installed, unmodified `claude` in headless mode (through the
Agent SDK, which speaks Claude Code's stream-json protocol), with the Claude Code
system prompt, tools and your user/project/local settings, so it behaves like the
sessions you run in VS Code. agentlens never reads or passes credentials: `claude`
uses whatever sign-in you completed through Anthropic's own flow.

Policy note (see README): agentlens never signs anyone in or handles a login. Each person's
own unmodified `claude` does, which Anthropic allows for Agent SDK use on Claude plans.
Keep it that way: no sign-in flow, token handling or shared logins here.

The session's messages become the same Event feed the monitored panes show. Permission
prompts, questions (AskUserQuestion) and plan approvals (ExitPlanMode) become a
PendingRequest the UI answers; the turn waits until it does.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentlens import usage
from agentlens.adapters.claude_transcripts import image_marks, tool_summary
from agentlens.clipboard import PastedImage
from agentlens.commands import Catalog
from agentlens.model import Event
from agentlens.terminal import CLAUDE_ENV

# Live states for driven sessions, matching agentlens.live's names where they overlap.
STARTING = "starting"
WORKING = "working"
WAITING = "waiting on you"
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
        self.worktree = ""  # the Claude Code worktree it moved into, read from its transcript
        self.on_change = on_change
        self.on_rate_limit = on_rate_limit
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
        self._connecting: asyncio.Future | None = None  # a paused session starting Claude Code
        self._stderr: deque[str] = deque(maxlen=STDERR_LINES)

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

        return ClaudeAgentOptions(
            cwd=self.cwd,
            resume=self.resume,
            permission_mode=self.permission_mode,
            model=self.model,
            include_partial_messages=True,
            can_use_tool=self._can_use_tool,
            # Behave like Claude Code: its system prompt and tools, and the same settings,
            # CLAUDE.md, skills and permission rules your VS Code sessions load.
            system_prompt={"type": "preset", "preset": "claude_code"},
            tools={"type": "preset", "preset": "claude_code"},
            setting_sources=["user", "project", "local"],
            cli_path=self.cli_path,
            max_buffer_size=MAX_MESSAGE_BYTES,
            # Piped, not shared with agentlens's terminal: written there, it lands on the screen,
            # and the "show the cursor" Claude Code sends on exit leaves a stray cursor showing.
            stderr=self._on_stderr,
            env=dict(CLAUDE_ENV),
        )

    def _on_stderr(self, line: str) -> None:
        if line := TERMINAL_CODES.sub("", line).strip():
            self._stderr.append(line)

    async def start(self, first_prompt: str | None = None, images: list[PastedImage] | None = None) -> None:
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
        if first_prompt or images:
            self.images = [*(images or []), *self.images]  # the first message's, then any pasted since
            await self.send(first_prompt or "")

    def park(self, events: list[Event]) -> None:
        """Show a resumed session's past conversation without starting Claude Code yet."""
        self.parked = True
        self.events = list(events)
        if events:
            self.since = events[-1].at
        self.state, self.detail = PAUSED, "from last time · type to continue"
        self.on_change(self)

    async def send(self, text: str) -> None:
        text = text.strip()
        starting = self._connecting is not None and not self._connecting.done()
        images = [] if text.startswith("/") else self.images  # a command leaves them for the next message
        if not (text or images) or (self._client is None and not self.parked and not starting):
            return
        if images:
            self.images = []
        now = datetime.now(timezone.utc)
        busy = self.state == WORKING
        shown = f"{image_marks(len(images))} {text}".strip()
        self.events.append(Event(now, "you", shown.splitlines()[0][:240], shown, tag="queued" if busy else ""))
        if not self.title and text and not text.startswith("/"):
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
        if not busy:
            self._set(WORKING, "thinking")
        else:
            self.on_change(self)
        client = self._client
        try:
            await client.query(_with_images(text, images) if images else text)
        except Exception as e:  # noqa: BLE001
            await self._stopped(client, f"Couldn't send that to Claude Code ({e}), so agentlens stopped it.")
            return
        if not self.titled and self._naming is None and text and not text.startswith("/"):
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

    async def interrupt(self) -> None:
        if self._client is not None and self.state in (WORKING, PERMISSION, QUESTION, APPROVAL):
            if self.pending and self.pending.future and not self.pending.future.done():
                self._resolve(self._deny("Interrupted by the user.", interrupt=True))
            try:
                await self._client.interrupt()
            except Exception as e:  # noqa: BLE001
                self._note(f"interrupt failed: {e}", kind="error")

    async def close(self) -> None:
        for task in (self._reader, self._naming):
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
            message = reason or "Keep planning."
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

    # Internals --------------------------------------------------------------------------

    async def _can_use_tool(self, tool_name: str, tool_input: dict, context) -> Any:
        if tool_name == "AskUserQuestion":
            kind, state, detail = "question", QUESTION, "asked you a question"
        elif tool_name == "ExitPlanMode":
            kind, state, detail = "plan", APPROVAL, "plan waiting for your approval"
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
        kind = type(message).__name__
        now = datetime.now(timezone.utc)
        if kind in ("StreamEvent", "AssistantMessage") and self.state == WAITING:
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
                    summary = tool_summary(block.input or {}, self.cwd)
                    self.events.append(Event(now, "tool", f"{block.name}: {summary}" if summary else block.name))
                    self.detail = running(block.name, summary)
            if getattr(message, "session_id", None):
                self.session_id = message.session_id
            self.on_change(self)
            return
        if kind == "UserMessage":
            for block in getattr(message, "content", None) or []:
                if type(block).__name__ == "ToolResultBlock" and getattr(message, "parent_tool_use_id", None) is None:
                    self.detail = "thinking"  # over what the tool gave back, until its next reply starts
                if type(block).__name__ == "ToolResultBlock" and getattr(block, "is_error", False):
                    content = block.content if isinstance(block.content, str) else " ".join(
                        c.get("text", "") for c in block.content or [] if isinstance(c, dict))
                    first = next((ln for ln in content.splitlines() if ln.strip()), "tool error")
                    self.events.append(Event(now, "error", first[:240], content[:4000]))
            self.on_change(self)
            return
        if kind == "TaskNotificationMessage":
            self.events.append(Event(now, "notice", getattr(message, "summary", "") or "background task update"))
            self.on_change(self)
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
            for event in self.events:
                if event.tag == "queued":
                    event.tag = ""  # whatever was queued has been read by now
            self.session_id = getattr(message, "session_id", None) or self.session_id
            self.cost_usd += getattr(message, "total_cost_usd", None) or 0.0
            if getattr(message, "is_error", False):
                errors = getattr(message, "errors", None) or [getattr(message, "result", "") or "the turn failed"]
                self.events.append(Event(now, "error", str(errors[0])[:240], "\n".join(map(str, errors))))
            self._set(WAITING, "turn finished")

    def _note(self, text: str, kind: str = "notice") -> None:
        self.events.append(Event(datetime.now(timezone.utc), kind, text[:240], text))
        self.on_change(self)

    def _set(self, state: str, detail: str) -> None:
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
