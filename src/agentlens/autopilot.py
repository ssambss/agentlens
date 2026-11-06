"""Experimental: autopilot, for the sessions agentlens runs. When one finishes a turn, Sonnet reads
Claude's reply with your last few messages and says what you would. Balanced only gives the
go-aheads. Autonomous runs the session for you, through checking and delivering the work, and leaves
you the big decisions. Anything it doesn't answer it leaves for you, with a line on what it needs.

Through your own `claude`, with no tools, so it can't read or change anything: it only answers. It
runs in an empty folder of its own, so no project's CLAUDE.md or memory comes with what it reads.
When Claude is only waiting on work it started, such as a test run, it answers nothing and the
session doesn't call you: the work brings Claude back by itself.
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
SAID = 4  # your last messages it reads
CHARS = 2000  # of each of them
REPLY_CHARS = 8000  # of Claude's reply, from its end
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
        "don't say otherwise, tell it to deliver them the way this repo does it. Where changes need someone's "
        "review (a pull request, a Gerrit change), that means sending them for review and stopping there, "
        "never approving, submitting or merging them itself."),
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
deliver); or the reply only answers their question
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
    # Nothing to answer, and nothing for you yet: Claude is waiting on work it started, which brings
    # it back by itself.
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


def prompt(level: str, title: str, said: list[tuple[str, bool]], reply: str) -> str:
    """`said`: your last messages, oldest first, each with whether autopilot sent it."""
    messages = "\n---\n".join(("(autopilot) " if by_autopilot else "") + text[:CHARS]
                              for text, by_autopilot in said[-SAID:]) or "(none)"
    return f"""Session: {title or "(untitled)"}

{RULES[level]}

{ALWAYS_YOURS[level]}

The developer's last messages to it, oldest first ("(autopilot)" marks the ones you sent):
---
{messages}
---

Claude's reply this turn:
---
{reply[-REPLY_CHARS:] or "(no text)"}
---

Answer with action "continue" and the message to send it, short and in the developer's own words, like \
their messages above: an answer, a go-ahead or a pick that moves the work on, never only thanks, an \
acknowledgement or small talk (if that's all you'd say, "ask"). When the reply asks more than one thing, \
continue only if you can answer every one of them: if any is the developer's, "ask", and don't answer the \
rest. Say only what their messages and the reply show. Never say you've looked at or tried something (no \
"looks good": you haven't seen the work), never promise what the developer will do, never state \
facts about anything outside this conversation, never add requirements of your own, and never tell Claude \
to skip a check or a precaution it chose. Or with action "ask" and an empty message. Or with action "wait" \
and an empty message when the reply asks nothing and only says Claude is waiting on something it already \
started, like a test run, a build or a background task, that brings it back by itself when it's done: then \
there's nothing to answer, and the developer isn't called yet.
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


def decide(level: str, title: str, said: list[tuple[str, bool]], reply: str, cli_path: str | None = None,
           timeout: int = TIMEOUT) -> Decision:
    """Ask your own `claude` (Sonnet, no tools) what you'd say to this reply. Blocking: run it in a thread."""
    claude = cli_path or shutil.which("claude")
    if not claude:
        raise RuntimeError("`claude` CLI not found on PATH")
    try:
        done = subprocess.run(command(claude), input=prompt(level, title, said, reply), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout, env=claude_env(),
                              cwd=workdir())
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"no answer in {timeout} seconds") from None
    if not done.stdout.strip():
        raise RuntimeError(done.stderr.strip()[-300:] or f"claude exited with {done.returncode}")
    return parse(done.stdout)
