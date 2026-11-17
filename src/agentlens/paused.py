"""The tools a session agentlens runs has for your sessions paused here.

Claude finds your other sessions through Claude Code, which lists only the ones running: those are
the ones it can message. When you reopen agentlens, the sessions from last time come back paused,
without Claude Code running for them until you type into one, so another session's Claude couldn't
find one unless you named it with @. With these it can find them itself: list them, read what one
said without starting it, and start one, under its name, so it can message it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agentlens import coordinator
from agentlens.model import APPROVED, AUTOPILOT, DECLINED, PROGRAM, SENT_BACK, Event

SERVER = coordinator.SERVER  # one agentlens server for a session: these tools, and the delegate tool beside them
LIST, READ, START = "paused_sessions", "read_paused_session", "start_paused_session"
TOOL_NAMES = [f"mcp__{SERVER}__{name}" for name in (LIST, READ, START)]
MESSAGES = 10  # of a paused session's conversation that read_paused_session gives, unless Claude asks for more
MOST_MESSAGES = 40
MESSAGE_CHARS = 4000  # of one message, from its end, as the coordinator reads a reply
READ_CHARS = 16000  # of all it gives: the newest messages, as many as fit

# What Claude Code puts in the session's system prompt with the server, as any MCP server's: the tools' names
# alone, which is all Claude sees of them until it looks one up, don't say when to reach for them.
INSTRUCTIONS = (
    "The user runs this session in agentlens, beside their other Claude Code sessions. Those agentlens has "
    "paused (it pauses every session when it reopens) aren't running, so ListAgents doesn't list them and "
    "SendMessage can't reach them. When the user refers to another session you don't find in ListAgents, look "
    f"for it with {LIST}. For what it did, said or decided, {READ} reads its conversation without starting it. "
    f"Only to ask it something or tell it something, {START} starts it so you can message it.")

LIST_ABOUT = ("The user's Claude Code sessions that agentlens has paused, which ListAgents doesn't list as they "
              "aren't running: each one's title, folder, and when it was last at work.")
READ_ABOUT = ("Read the end of a paused session's conversation, the user's messages and Claude's replies, without "
              "starting it.")
START_ABOUT = ("Start a paused session, without a message, so you can message it with SendMessage. It takes "
               "memory while it runs: if what you need is what it said or decided, read it instead.")
SESSION = {"type": "string",
           "description": "The session's title as paused_sessions lists it, or its id where it gives one"}

# Who a message in its conversation was from, when it wasn't the user (an event's tag) or wasn't a message.
FROM = {AUTOPILOT: "agentlens's autopilot, for the user", PROGRAM: "a program running the session",
        coordinator.COORDINATOR: "agentlens's coordinator", coordinator.BOARD: "agentlens, to the coordinator",
        coordinator.HANDOVER: "another session, handing a goal over",
        coordinator.DEBATE: "the sessions it debated, passed on by agentlens"}
SAID = {"claude": "Claude", "question": "Claude asked", "plan": "Claude's plan"}
ANSWERED = {APPROVED: "The user approved the plan", SENT_BACK: "The user sent the plan back",
            DECLINED: "The user didn't answer"}


@dataclass(frozen=True)
class Paused:
    """One of your sessions paused here."""

    key: str  # its pane's, which also tells apart two with one title
    name: str  # what Claude Code lists it by once it runs: what SendMessage takes
    title: str  # what the wall calls it
    cwd: str
    worktree: str = ""
    last: datetime | None = None  # when it was last at work


def when(at: datetime) -> str:
    """'Sat 2026-11-15 14:02', in your time: Claude matches "the one from Friday" by it."""
    return at.astimezone().strftime("%a %Y-%m-%d %H:%M")


def _plain(text: Any) -> str:
    """A session as Claude names it, to match on: without the @ and quotes of a mention, any case."""
    return " ".join(str(text or "").strip().lstrip("@").strip("\"'").split()).casefold()


def matching(sessions: Sequence[Paused], wanted: Any) -> list[Paused]:
    """The sessions `wanted` names: by its pane's key, its title or its name, or failing that, those whose title
    or name has it in."""
    q = _plain(wanted)
    if not q:
        return []
    exact = [s for s in sessions if q in (s.key.casefold(), _plain(s.title), _plain(s.name))]
    return exact or [s for s in sessions if q in _plain(s.title) or q in _plain(s.name)]


def _shared(sessions: Sequence[Paused]) -> set[str]:
    """Titles more than one has: those are told apart by their keys."""
    return {t for t, n in Counter(_plain(s.title) for s in sessions).items() if n > 1}


def _called(s: Paused, shared: set[str]) -> str:
    return f'"{s.title}" (id {s.key})' if _plain(s.title) in shared else f'"{s.title}"'


def listing(sessions: Sequence[Paused]) -> str:
    """The paused sessions, the last at work first."""
    if not sessions:
        return ("None of the user's sessions is paused in agentlens now. ListAgents lists those running, which "
                "you can message.")
    shared = _shared(sessions)
    lines = [f"The user's sessions paused in agentlens, the last at work first. Give {READ} or {START} one's "
             "title as it's written here."]
    for s in sorted(sessions, key=lambda s: s.last.timestamp() if s.last else 0.0, reverse=True):
        about = [s.cwd + (f", worktree {s.worktree}" if s.worktree else "")]
        if s.name != s.title:
            about.append(f"ListAgents lists it as {s.name} once it runs")
        if s.last is not None:
            about.append(f"last at work {when(s.last)}")
        lines.append(f"- {_called(s, shared)}: {'; '.join(about)}")
    return "\n".join(lines)


def _speaker(e: Event) -> str:
    if e.kind == "you":
        return FROM.get(e.tag, "The user")  # "queued" too: yours, sent while Claude worked
    if e.kind == "answer":
        return ANSWERED.get(e.tag, "The user answered")
    return SAID[e.kind]


def _cut(text: str) -> str:
    text = text.strip()
    return text if len(text) <= MESSAGE_CHARS else "…" + text[-MESSAGE_CHARS:].lstrip()


def conversation(session: Paused, events: Sequence[Event], messages: Any = None) -> str:
    """The end of its conversation, oldest first: its last `messages`, as many of those as fit."""
    try:
        wanted = max(1, min(MOST_MESSAGES, int(messages)))
    except (TypeError, ValueError):
        wanted = MESSAGES
    said = [e for e in events if e.kind in ("you", "answer", *SAID)]
    where = f'"{session.title}", paused in agentlens, in {session.cwd}'
    if not said:
        return f"{where}: agentlens has none of its conversation to read."
    blocks: list[str] = []
    total = 0
    for e in reversed(said[-wanted:]):
        body = _cut(e.detail or e.text)
        block = f"[{when(e.at)}] {_speaker(e)}:" + (f"\n{body}" if body else "")
        if blocks and total + len(block) > READ_CHARS:
            break
        blocks.append(block)
        total += len(block)
    more = len(said) - len(blocks)
    shown = "its last message" if len(blocks) == 1 else f"its last {len(blocks)} messages"
    earlier = f" ({more} earlier not shown)" if more else ""
    head = f"{where}: {shown}, oldest first{earlier}."
    return "\n\n".join([head, *reversed(blocks)])


def _text(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], **({"is_error": True} if error else {})}


def _one(sessions: Sequence[Paused], wanted: Any) -> Paused | dict:
    """The session `wanted` names, or what to tell Claude when it names none, or more than one."""
    found = matching(sessions, wanted)
    if len(found) == 1:
        return found[0]
    if not found:
        paused = ", ".join(_called(s, _shared(sessions)) for s in sessions)
        return _text(f'No session paused in agentlens goes by "{" ".join(str(wanted or "").split())}". '
                     + (f"Paused now: {paused}. " if paused else "None is paused now. ")
                     + "One that's running is in ListAgents.", error=True)
    shared = _shared(found)
    return _text("More than one paused session goes by that: "
                 + "; ".join(f"{_called(s, shared)}, in {s.cwd}" for s in found)
                 + ". Give one's whole title" + (", or its id" if shared else "") + ".", error=True)


def tools(paused: Callable[[], list[Paused]], conversation_of: Callable[[str], Sequence[Event]],
          start: Callable[[str], Awaitable[tuple[bool, str]]]) -> list:
    """The tools, for the session the app gives these for. `paused()`: your other sessions paused now.
    `conversation_of(key)`: one's conversation, as its pane has it. `start(key)` starts one, and says whether
    it could: with the name it runs under, or why not."""
    from claude_agent_sdk import tool

    async def list_them(args: dict) -> dict:
        return _text(listing(paused()))

    async def read(args: dict) -> dict:
        found = _one(paused(), args.get("session"))
        if isinstance(found, dict):
            return found
        return _text(conversation(found, conversation_of(found.key), args.get("messages")))

    async def start_it(args: dict) -> dict:
        found = _one(paused(), args.get("session"))
        if isinstance(found, dict):
            return found
        ok, said = await start(found.key)
        if not ok:
            return _text(f'agentlens couldn\'t start "{found.title}": {said}', error=True)
        return _text(f'"{found.title}" is running now, with nothing to do until it hears from you: message it with '
                     f'SendMessage, to "{said}".')

    messages = {"type": "integer", "minimum": 1, "maximum": MOST_MESSAGES,
                "description": f"How many of its last messages to read (default {MESSAGES})"}
    return [
        tool(LIST, LIST_ABOUT, {"type": "object", "properties": {}})(list_them),
        tool(READ, READ_ABOUT, {"type": "object", "required": ["session"],
                                "properties": {"session": SESSION, "messages": messages}})(read),
        tool(START, START_ABOUT, {"type": "object", "required": ["session"], "properties": {"session": SESSION}})(
            start_it),
    ]


def options(paused: Callable[[], list[Paused]], conversation_of: Callable[[str], Sequence[Event]],
            start: Callable[[str], Awaitable[tuple[bool, str]]],
            hand_over: Callable[[str, str], tuple[bool, str]] | None = None) -> dict:
    """What a session agentlens runs gets on top of Claude Code's own: these tools, allowed without asking, and
    with `hand_over`, the coordinator's delegate tool beside them (see coordinator.delegate_tool)."""
    from claude_agent_sdk import create_sdk_mcp_server

    made = tools(paused, conversation_of, start)
    allowed = list(TOOL_NAMES)
    if hand_over is not None:
        made.append(coordinator.delegate_tool(hand_over))
        allowed.append(coordinator.DELEGATE_TOOL)
    server = create_sdk_mcp_server(SERVER, tools=made)
    server["instance"].instructions = INSTRUCTIONS  # the SDK's helper takes none; its MCP server does
    return {"mcp_servers": {SERVER: server}, "allowed_tools": allowed}
