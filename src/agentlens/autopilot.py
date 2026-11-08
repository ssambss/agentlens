"""Experimental: autopilot, for the sessions agentlens runs. When one finishes a turn, Sonnet reads
Claude's reply with the session before it and says what you would. Balanced only gives the
go-aheads. Autonomous runs the session for you, through checking and delivering the work, and leaves
you the big decisions. Anything it doesn't answer it leaves for you, with a line on what it needs.

Through your own `claude`, with no tools, so it can't read or change anything: it only answers. It
runs in an empty folder of its own, so no project's CLAUDE.md or memory comes with what it reads.
When Claude is only waiting on work it started in the session, such as a test run in the
background, it answers nothing and the session doesn't call you: the work brings Claude back by
itself. The session checks that some of that work is still going (agentlens.background): with none,
the turn is yours, and so is one where nothing comes back for a while once the work has ended.
A CI run, a deploy or another session's work doesn't bring Claude back, so it never waits on those.
Questions, permission prompts and plans come in the middle of a turn, as requests the pane
answers, so it never sees them: those are always yours.

What it decided is kept in ~/.agentlens/autopilot.jsonl, for the list of what it answered for you
(D), with when you last opened that list in autopilot-seen.json.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from agentlens.model import APPROVED, AUTOPILOT, DECLINED, SENT_BACK, Event
from agentlens.preferences import home
from agentlens.terminal import claude_env

# off, then what each level answers for you, as the settings and the p key say it.
LEVELS = {
    "off": "Off",
    "balanced": "Balanced: it gives the go-aheads",
    "autonomous": "Autonomous: all but the big decisions",
}
SESSION_NAME = "agentlens autopilot"  # how its runs show in `claude agents`, so the wall leaves them off
# Sonnet at medium effort: on sample turns it was faster than Haiku (3 seconds a check against 8,
# the slowest 5 against 39), and steadier in what it said.
MODEL = "sonnet"
EFFORT = "medium"
# Its answers in a row before one is yours anyway, in case a session is going round in circles.
# Autonomous runs whole tasks, which can take many turns.
IN_A_ROW = {"balanced": 5, "autonomous": 12}
TIMEOUT = 120
CHARS = 2000  # of each of your messages
REPLY_CHARS = 8000  # of Claude's reply this turn, from its end
# The session before this turn: its first turn, which says what you set out to do, then as many of the
# newest as fit. With only your last few messages, a session that only talked things over read as work
# done and not yet delivered.
EARLIER_CHARS = 24_000
EARLIER_REPLY_CHARS = 1500  # of what Claude said in each earlier turn, its start and its end
TOOLS = 15  # of the tools Claude ran in a turn, the last ones: what it changed, committed or pushed
TOOL_CHARS = 120  # of each
KEPT = 500  # decisions the log keeps, the newest
TRIM_BYTES = 400_000  # past this, the log is cut back to KEPT

# With its message, so Claude knows you didn't type it. The transcript keeps it, which is how
# agentlens tells its messages from yours there (said_by_it): keep this wording.
NOTE = ("(agentlens's autopilot sent this for the user, who didn't type it. If something needs the user's own "
        "decision, say so and stop: they'll read it.)")
# The notes it sent with before, which transcripts still have: while it was called panopsis.
EARLIER_NOTES = (NOTE.replace("agentlens's", "panopsis's", 1),)

SYSTEM = ("You stand in for a developer who runs several Claude Code sessions at once. One of them has just "
          "finished its turn. Decide whether the developer needs to read it, or whether you can answer it "
          "for them, and with what.")

RULES = {
    "balanced": (
        "Continue only when the reply stops to ask whether to go on with what the developer already asked "
        "for, or stops partway through it, and going on needs no choice from them. Send a go-ahead."),
    "autonomous": (
        "You run this session for the developer. Keep the work they asked for moving until it's done, checked "
        "and delivered, and leave them only the big decisions. Continue when the reply asks whether to go on, "
        "stops partway, or asks something you can answer. Where it gives options, take the one Claude "
        "recommends, or the sensible default, if it fits what the developer asked for and can be changed "
        "later. Say yes to small follow-ups Claude proposes that serve what they asked for, and to small "
        "changes it made along the way that it thinks are right and that fit what they asked for. Say yes to "
        "cleanups of things the work made that can be redone, like temporary files, build output or leftover "
        "folders and worktrees.\n"
        "When the reply asks the developer to try, look at or test something, and doesn't say it has checked "
        "that itself, have it check what it can first. It can run the code or its tests, or drive the app "
        "headlessly and read the output as text, never as screenshots or images, which break the session. "
        "Then it should say what's left that only the developer can judge. Ask this only once: if you've "
        "already asked, it's theirs.\n"
        "When code changes they asked for are done and checked but not yet delivered, and their messages "
        "don't say otherwise, have it deliver them with a plain go-ahead and no verdict on the work: the "
        "repo's own skills and rules tell Claude how. Where changes need someone's review (a pull request, a Gerrit change), sending them for "
        "review is as far as you go: approving, submitting or merging them is the developer's."),
}

ALWAYS_YOURS = {
    "balanced": """Always leave it for the developer when:
- the work they asked for is done, or the reply reports results for them to look at
- it asks them to try, test or check something by hand
- it's stuck, keeps failing, or isn't sure what they meant
- going on would push, merge, release, deploy or delete, or do anything others see or that can't be undone
- it asks about them: how something went for them, what they think or prefer, or how they want things \
done from now on; or they're talking ideas over rather than having work done
- the reply is cut off, or you aren't sure""",
    "autonomous": """Leave it for the developer only when:
- it needs a big design decision, one that's hard to undo later: how the system is built, what's stored \
and in what format, interfaces others rely on; or a change that goes against what they asked for, or \
takes the work well beyond it
- it asks about them: how something went for them, what they think or prefer, or how they want things \
done from now on; or they're talking ideas over rather than having work done
- going on would release, deploy, publish a package, force-push or rewrite history others have, approve \
or submit a change that needs someone's review, or delete or overwrite what can't easily be got back: \
their own files, uncommitted work, branches with unmerged commits, data
- only they can do it or decide it: their own money, purchases or plans; something outside the session, \
like restarting an app, signing in or a device in their hands; or how something looks or feels, once \
Claude has checked what it can
- the work they asked for is done, checked and delivered: pushed, or sent for review (or has nothing to \
deliver)
- they asked a question rather than for work, and the reply answers it: if it offers to do more, that's \
theirs to take up
- it's stuck: going round in circles, or still failing after you've answered it
- it isn't sure what they meant, or you'd be guessing at what they want
- the reply is cut off""",
}

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["continue", "ask", "wait"]},
        "message": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["action", "message", "reason"],
}


@dataclass(frozen=True)
class Decision:
    send: str  # what to send the session; empty leaves it for you
    reason: str  # one line: what it told the session and why, or what the session needs from you
    # Nothing to answer, and nothing for you yet: Claude says it's waiting on work it started, which
    # brings it back by itself. The session waits only while some of that work is still going.
    waiting: bool = False


@dataclass(frozen=True)
class Entry:
    """A turn it read, for the list of what it answered for you."""

    at: datetime
    session_id: str
    title: str
    where: str  # its folder, and its worktree
    asked: str  # what Claude asked, or where it stopped; in one it left you, what it needs from you
    said: str = ""  # what it sent for you; empty, it left the turn to you


def log_path() -> Path:
    return home() / "autopilot.jsonl"


def seen_path() -> Path:
    return home() / "autopilot-seen.json"


def workdir() -> Path:
    """An empty folder of its own to run `claude` in. In the folder agentlens was started from, it got
    that project's CLAUDE.md and memory, and answered for a session elsewhere from what those said."""
    path = home() / "autopilot"
    path.mkdir(parents=True, exist_ok=True)
    return path


def record(entry: Entry, path: Path | None = None) -> None:
    """Add a decision to the log; past TRIM_BYTES, keep only the newest KEPT."""
    path = path or log_path()
    line = json.dumps({**asdict(entry), "at": entry.at.isoformat()}, ensure_ascii=False)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if path.stat().st_size > TRIM_BYTES:
            kept = path.read_text(encoding="utf-8").splitlines()[-KEPT:]
            path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except OSError:
        pass  # only the list of its answers goes without it


def load(path: Path | None = None) -> list[Entry]:
    """The newest KEPT decisions, oldest first. A line it can't read is left out."""
    path = path or log_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines[-KEPT:]:
        try:
            data = json.loads(line)
            entries.append(Entry(datetime.fromisoformat(data["at"]), str(data.get("session_id") or ""),
                                 str(data.get("title") or ""), str(data.get("where") or ""),
                                 str(data.get("asked") or ""), str(data.get("said") or "")))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return entries


def load_seen(path: Path | None = None) -> datetime | None:
    """When you last had the list of its answers open; None if never."""
    try:
        return datetime.fromisoformat(json.loads((path or seen_path()).read_text(encoding="utf-8"))["seen"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def save_seen(at: datetime, path: Path | None = None) -> None:
    path = path or seen_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"seen": at.isoformat()}), encoding="utf-8")
    except OSError:
        pass  # only so a restart remembers


def said_by_it(text: str) -> str | None:
    """A message in a transcript as autopilot sent it, without its note; None if you sent it."""
    body = text.rstrip()
    note = next((note for note in (NOTE, *EARLIER_NOTES) if body.endswith(note)), None)
    return body[:-len(note)].rstrip() if note else None


def _clip(text: str, chars: int) -> str:
    """`text`, or its start and its end with what's between left out."""
    text = text.strip()
    if len(text) <= chars:
        return text
    head = chars // 3
    return f"{text[:head].rstrip()} [...] {text[len(text) - (chars - head):].lstrip()}"


def _turns(events: list[Event]) -> list[list[Event]]:
    """The session in turns, each from one of your messages to the next. What came before the first is one too."""
    turns: list[list[Event]] = []
    for e in events:
        if e.kind == "you" or not turns:
            turns.append([])
        turns[-1].append(e)
    return turns


def _you(e: Event) -> str:
    return ("You (autopilot): " if e.tag == AUTOPILOT else "You: ") + _clip(e.detail or e.text, CHARS)


def _ran(turn: list[Event]) -> str:
    tools = [e.text[:TOOL_CHARS] for e in turn if e.kind == "tool"]
    if not tools:
        return ""
    before = f"({len(tools) - TOOLS} more before) " if len(tools) > TOOLS else ""
    return "Claude ran: " + before + "; ".join(tools[-TOOLS:])


def _answered(e: Event) -> str:
    said = _clip(e.detail or e.text, 600)
    if e.tag == APPROVED:
        return "You approved the plan"
    if e.tag == SENT_BACK:
        return f"You sent the plan back: {said}"
    if e.tag == DECLINED:
        return "You didn't answer" + (f": {said}" if said else "")
    return f"You answered: {said}"


def _turn(turn: list[Event]) -> str:
    """An earlier turn as autopilot reads it: your message, the tools Claude ran, what it asked you and
    what you answered, and what it said."""
    lines = [_you(turn[0])] if turn[0].kind == "you" else []
    if ran := _ran(turn):
        lines.append(ran)
    for e in turn:
        if e.kind == "question":
            lines.append("Claude asked you: " + _clip(e.detail or e.text, 600))
        elif e.kind == "plan":
            lines.append("Claude's plan: " + _clip(e.detail or e.text, 1000))
        elif e.kind == "answer":
            lines.append(_answered(e))
    said = "\n\n".join(e.detail or e.text for e in turn if e.kind == "claude" and e.tag != "command")
    if said.strip():
        lines.append("Claude: " + _clip(said, EARLIER_REPLY_CHARS))
    return "\n".join(lines)


def earlier(events: list[Event]) -> str:
    """The session before this turn, oldest first: its first turn, then as many of the newest as fit in
    EARLIER_CHARS, saying how many were left out between."""
    turns = [text for text in map(_turn, _turns(events)) if text]
    if not turns:
        return ""
    first, rest = turns[0], turns[1:]
    kept: list[str] = []
    size = len(first)
    for text in reversed(rest):
        if size + len(text) > EARLIER_CHARS:
            break
        kept.append(text)
        size += len(text)
    gap = [f"({len(rest) - len(kept)} turns left out)"] if len(kept) < len(rest) else []
    return "\n\n".join([first, *gap, *reversed(kept)])


def prompt(level: str, title: str, events: list[Event], reply: str) -> str:
    """`events`: the session's, oldest first, to the end of this turn; `reply`: what Claude said in it."""
    last = max((i for i, e in enumerate(events) if e.kind == "you"), default=len(events))
    this_turn = events[last:]
    ran = _ran(this_turn)
    # The rules come after the session, which can be long, so they're what it reads last.
    return f"""Session: {title or "(untitled)"}

The session before this turn, oldest first. "You" is the developer, "You (autopilot)" a message you sent \
for them, and "Claude ran" the tools Claude used:
---
{earlier(events[:last]) or "(none: this is its first turn)"}
---

This turn, the developer's message:
---
{_you(this_turn[0]) if this_turn else "(none)"}
---
{ran + chr(10) if ran else ""}
Claude's reply this turn:
---
{reply[-REPLY_CHARS:] or "(no text)"}
---

Decide on this turn with the whole session in mind: what the developer came to it for, and what Claude has \
done in it.

{RULES[level]}

{ALWAYS_YOURS[level]}

Answer with action "continue" and the message to send it, short and in the developer's own words, like \
the ones they typed above. Those marked "You (autopilot)" were yours: don't reuse their wording. Send an \
answer, a go-ahead or a pick that moves the work on, never only thanks, an \
acknowledgement or small talk (if that's all you'd say, "ask"). Write only what they would type, often a \
word or two, like "yes", "go on" or "sure, both". Don't repeat Claude's question back, or anything from \
these instructions, and don't say how to do it: Claude knows its repo, its skills and its rules. When the reply asks more than one thing, \
continue only if you can answer every one of them: if any is the developer's, "ask", and don't answer the \
rest. Say only what the session shows. Never say you've looked at or tried something (no \
"looks good" or "looks done": you haven't seen the work), never promise what the developer will do, never state \
facts about anything outside this conversation, never add requirements of your own, and never tell Claude \
to skip a check or a precaution it chose. Or with action "ask" and an empty message. Or with action "wait" \
and an empty message when the reply asks nothing and only says Claude is waiting on work it started itself, \
with its own tools in this session: a command or a build it runs in the background, a background subagent, a \
monitor, or a wakeup it scheduled. That work brings it back by itself when it's done: then there's nothing to \
answer, and the developer isn't called yet. Never "wait" for anything else, like a CI run, a deploy, another \
session's work or a person: those don't bring Claude back, so "ask".
reason: one short line the developer reads on the wall, under the session's title. Write it to them, as \
"you", the way a colleague would: what Claude's reply needs from them, or else what it says, like \
"Claude wants you to pick: Redis or in-memory", "Claude fixed the parser and asks if you want it committed" or \
"Claude answered you: the setting only applies to new sessions". Never call them "the developer", and don't \
explain why you decided. With "continue", reason is only what Claude asked, or where it stopped, like "Claude \
asked whether to add a test for --json": they read it beside your message, in the list of what you answered for \
them, so don't say who answered or what. With "wait", it's what Claude is waiting on, like "Claude is waiting \
on the test run"."""


def command(claude: str) -> list[str]:
    return [claude, "-p", "--no-session-persistence", "-n", SESSION_NAME, "--model", MODEL, "--effort", EFFORT,
            "--tools", "", "--strict-mcp-config", "--system-prompt", SYSTEM, "--output-format", "json",
            "--json-schema", json.dumps(SCHEMA)]


def parse(output: str) -> Decision:
    result = json.loads(output)
    if not isinstance(result, dict) or result.get("is_error") or result.get("subtype") != "success":
        detail = result.get("result") if isinstance(result, dict) else None
        raise RuntimeError(str(detail or "claude didn't answer"))
    answer = result.get("structured_output")
    if not isinstance(answer, dict):
        raise RuntimeError("claude's answer wasn't the shape asked for")
    reason = " ".join(str(answer.get("reason") or "").split())
    message = str(answer.get("message") or "").strip() if answer.get("action") == "continue" else ""
    return Decision(message, reason, waiting=answer.get("action") == "wait")


def decide(level: str, title: str, events: list[Event], reply: str, cli_path: str | None = None,
           timeout: int = TIMEOUT) -> Decision:
    """Ask your own `claude` (Sonnet, no tools) what you'd say to this reply. Blocking: run it in a thread."""
    claude = cli_path or shutil.which("claude")
    if not claude:
        raise RuntimeError("`claude` CLI not found on PATH")
    try:
        done = subprocess.run(command(claude), input=prompt(level, title, events, reply), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout, env=claude_env(),
                              cwd=workdir())
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"no answer in {timeout} seconds") from None
    if not done.stdout.strip():
        raise RuntimeError(done.stderr.strip()[-300:] or f"claude exited with {done.returncode}")
    return parse(done.stdout)
