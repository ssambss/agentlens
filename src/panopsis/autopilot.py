"""Experimental: autopilot, for the sessions panopsis runs. When one finishes a turn, Sonnet reads
Claude's reply with your last few messages and says what you would: go on (balanced), or also the
routine answer (autonomous). Anything else it leaves for you, with a line on what it needs.

Through your own `claude`, with no tools, so it can't read or change anything: it only answers.
Questions, permission prompts and plans come in the middle of a turn, as requests the pane
answers, so it never sees them: those are always yours.

What it decided is kept in ~/.panopsis/autopilot.jsonl, for the list of what it answered for you
(D), with when you last opened that list in autopilot-seen.json.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from panopsis.preferences import home
from panopsis.terminal import claude_env

# off, then what each level answers for you, as the settings and the p key say it.
LEVELS = {
    "off": "Off",
    "balanced": "Balanced: it gives the go-aheads",
    "autonomous": "Autonomous: routine questions too",
}
SESSION_NAME = "panopsis autopilot"  # how its runs show in `claude agents`, so the wall leaves them off
# Sonnet at medium effort: on sample turns it was faster than Haiku (3 seconds a check against 8,
# the slowest 5 against 39), and steadier in what it said.
MODEL = "sonnet"
EFFORT = "medium"
IN_A_ROW = 5  # go-aheads in a row before one is yours anyway: a session going round in circles
TIMEOUT = 120
SAID = 4  # your last messages it reads
CHARS = 2000  # of each of them
REPLY_CHARS = 8000  # of Claude's reply, from its end
KEPT = 500  # decisions the log keeps, the newest
TRIM_BYTES = 400_000  # past this, the log is cut back to KEPT

# With its message, so Claude knows you didn't type it. The transcript keeps it, which is how
# panopsis tells its messages from yours there (said_by_it): keep this wording.
NOTE = ("(panopsis's autopilot sent this for the user, who didn't type it. If something needs the user's own "
        "decision, say so and stop: they'll read it.)")

SYSTEM = ("You stand in for a developer who runs several Claude Code sessions at once. One of them has just "
          "finished its turn. Decide whether the developer needs to read it, or whether you can answer it "
          "for them, and with what.")

RULES = {
    "balanced": (
        "Continue only when the reply stops to ask whether to go on with what the developer already asked "
        "for, or stops partway through it, and going on needs no choice from them. Send a go-ahead."),
    "autonomous": (
        "Continue when the reply asks whether to go on with what the developer already asked for, or stops "
        "partway through it. Also answer routine questions whose answer is clear from the developer's "
        "messages, or that have a sensible default that's easy to change later: naming, small implementation "
        "details, the order to do things in, whether to run or add tests. Leave design choices with lasting "
        "consequences, changes of scope, and anything you'd be guessing at for the developer."),
}

ALWAYS_YOURS = """Always leave it for the developer when:
- the work they asked for is done, or the reply reports results for them to look at
- it asks them to try, test or check something by hand
- it's stuck, keeps failing, or isn't sure what they meant
- going on would push, merge, release, deploy or delete, or do anything others see or that can't be undone
- the reply is cut off, or you aren't sure"""

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["continue", "ask"]},
        "message": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["action", "message", "reason"],
}


@dataclass(frozen=True)
class Decision:
    send: str  # what to send the session; empty leaves it for you
    reason: str  # one line: what it told the session and why, or what the session needs from you


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
    return body[:-len(NOTE)].rstrip() if body.endswith(NOTE) else None


def prompt(level: str, title: str, said: list[tuple[str, bool]], reply: str) -> str:
    """`said`: your last messages, oldest first, each with whether autopilot sent it."""
    messages = "\n---\n".join(("(autopilot) " if by_autopilot else "") + text[:CHARS]
                              for text, by_autopilot in said[-SAID:]) or "(none)"
    return f"""Session: {title or "(untitled)"}

{RULES[level]}

{ALWAYS_YOURS}

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
acknowledgement or small talk (if that's all you'd say, "ask"). Or with action "ask" and an empty message. \
reason: one short line the developer reads on the wall, under the session's title. Write it to them, as \
"you", the way a colleague would: what Claude's reply needs from them, or else what it says, like \
"Claude wants you to pick: Redis or in-memory", "Claude fixed the parser and asks if you want it committed" or \
"Claude answered you: the setting only applies to new sessions". Never call them "the developer", and don't \
explain why you decided. With "continue", reason is only what Claude asked, or where it stopped, like "Claude \
asked whether to add a test for --json": they read it beside your message, in the list of what you answered for \
them, so don't say who answered or what."""


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
    return Decision(message, reason)


def decide(level: str, title: str, said: list[tuple[str, bool]], reply: str, cli_path: str | None = None,
           timeout: int = TIMEOUT) -> Decision:
    """Ask your own `claude` (Sonnet, no tools) what you'd say to this reply. Blocking: run it in a thread."""
    claude = cli_path or shutil.which("claude")
    if not claude:
        raise RuntimeError("`claude` CLI not found on PATH")
    try:
        done = subprocess.run(command(claude), input=prompt(level, title, said, reply), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout, env=claude_env())
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"no answer in {timeout} seconds") from None
    if not done.stdout.strip():
        raise RuntimeError(done.stderr.strip()[-300:] or f"claude exited with {done.returncode}")
    return parse(done.stdout)
