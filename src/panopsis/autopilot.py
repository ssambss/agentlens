"""Experimental: autopilot, for the sessions panopsis runs. When one finishes a turn, Haiku reads
Claude's reply with your last few messages and says what you would: go on (balanced), or also the
routine answer (autonomous). Anything else it leaves for you, with a line on what it needs.

Through your own `claude`, with no tools, so it can't read or change anything: it only answers.
Questions, permission prompts and plans come in the middle of a turn, as requests the pane
answers, so it never sees them: those are always yours.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass

from panopsis.terminal import claude_env

# off, then what each level answers for you, as the settings and the p key say it.
LEVELS = {
    "off": "Off",
    "balanced": "Balanced: it gives the go-aheads",
    "autonomous": "Autonomous: routine questions too",
}
SESSION_NAME = "panopsis autopilot"  # how its runs show in `claude agents`, so the wall leaves them off
MODEL = "haiku"
IN_A_ROW = 5  # go-aheads in a row before one is yours anyway: a session going round in circles
TIMEOUT = 120
SAID = 4  # your last messages it reads
CHARS = 2000  # of each of them
REPLY_CHARS = 8000  # of Claude's reply, from its end

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
their messages above; or with action "ask" and an empty message. reason: one short line for the developer \
saying what you told it and why, or what it needs from them, like "wants a pick: Redis or in-memory" or \
"done: fixed the parser, asks whether to commit"."""


def command(claude: str) -> list[str]:
    return [claude, "-p", "--no-session-persistence", "-n", SESSION_NAME, "--model", MODEL, "--tools", "",
            "--strict-mcp-config", "--system-prompt", SYSTEM, "--output-format", "json",
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
    """Ask your own `claude` (Haiku, no tools) what you'd say to this reply. Blocking: run it in a thread."""
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
