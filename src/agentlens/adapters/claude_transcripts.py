"""Adapter for Claude Code session transcripts under ~/.claude/projects.

Layout (Claude Code 2.1.x):
    <projects>/<project-slug>/<session-id>.jsonl                       main thread
    <projects>/<project-slug>/<session-id>/subagents/agent-<id>.jsonl  one per subagent
    <projects>/<project-slug>/<session-id>/subagents/agent-<id>.meta.json

The transcript format is internal to Claude Code and changes between releases
(https://code.claude.com/docs/en/sessions.md), so every field access here is
defensive: a record we don't understand is skipped, never fatal.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentlens.model import (
    AWAY,
    DELEGATED,
    DORMANT,
    MODEL,
    TOOL,
    WAITING_ON_BACKGROUND,
    WAITING_ON_YOU,
    AgentRun,
    Event,
    FileChange,
    Session,
    Span,
)

DEFAULT_ROOT = Path.home() / ".claude" / "projects"

# Tools whose result can't arrive until a human answers.
HUMAN_ANSWERED_TOOLS = {"AskUserQuestion", "ExitPlanMode"}
DELEGATING_TOOLS = {"Agent", "Task"}
READ_TOOLS = {"Read"}
EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
HANDBACK_TOOLS = {"SubagentHandback"}  # background subagents report through this, not a final text
MAX_MODEL_GAP = timedelta(minutes=30)
# Gaps of these kinds beyond the away threshold mean nobody was there. That holds for
# background waits too: in real history every one over 2h was a watcher parked overnight
# or an idle session that another session's message woke days later. Dormant subagents
# are excluded: they legitimately idle for hours until their parent messages them.
SUSPENDABLE = {TOOL, DELEGATED, WAITING_ON_YOU, WAITING_ON_BACKGROUND}
# On resume, Claude Code reports the background tasks the previous process left behind
# with a notification under this task id. The gap before it, the session was closed.
ORPHAN_NOTICE = "<task-id>__orphan_summary__"
EVENT_LIMIT = 300  # feed lines kept per thread for the live views
SLASH_COMMAND = re.compile(r"<command-name>\s*(/[^<\s]+)\s*</command-name>")
DETAIL_CHARS = 4000


def load_sessions(
    root: Path = DEFAULT_ROOT,
    since: datetime | None = None,
    away_after: timedelta = timedelta(hours=2),
    cache: SessionCache | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> Iterator[Session]:
    """Sessions from every transcript; `progress` hears how many of them it has been through."""
    paths = sorted(root.glob("*/*.jsonl"))
    for done, path in enumerate(paths, 1):
        if not since or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) >= since:
            session = cache.get(path, away_after) if cache is not None else load_session(path, away_after)
            if session and (since is None or session.end >= since):
                yield session
        if progress is not None:
            progress(done, len(paths))
    if cache is not None:
        yield from cache.gone(root, since)


def recent_transcripts(root: Path = DEFAULT_ROOT, since: datetime | None = None) -> list[Path]:
    """The transcripts written to since `since`, or all of them, the most recently written first."""
    written = []
    for path in root.glob("*/*.jsonl"):
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if since is None or datetime.fromtimestamp(modified, timezone.utc) >= since:
            written.append((modified, path))
    return [path for _, path in sorted(written, reverse=True)]


class SessionCache:
    """Sessions read from whole transcripts, kept until the transcript or one of its subagents'
    changes. Reading them is the slow part of History and the report: this way History opens
    again at once, and the report then reads only what History hasn't. With a store (see
    agentlens.store), they're kept between launches too, and after Claude Code deletes them."""

    def __init__(self, store=None) -> None:
        self.store = store
        self._sessions: dict[Path, tuple[list, Session | None]] = {}

    def get(self, path: Path, away_after: timedelta = timedelta(hours=2)) -> Session | None:
        subagents = sorted((path.parent / path.stem / "subagents").glob("*"))
        # As JSON has it, for the store: what each file was like, and how away time was counted.
        key = [_listed(_stamp(path)), [[p.name, _listed(_stamp(p))] for p in subagents], away_after.total_seconds()]
        hit = self._sessions.get(path)
        if hit is not None and hit[0] == key:
            return hit[1]
        session = self.store.load(path, key) if self.store is not None else None
        if session is None:
            session = load_session(path, away_after)
            if session is not None and self.store is not None:
                self.store.save(path, key, session)
        self._sessions[path] = (key, session)
        return session

    def gone(self, root: Path, since: datetime | None = None) -> list[Session]:
        """Sessions the store kept whose transcripts Claude Code has deleted, active since `since`."""
        return list(self.store.gone(root, since)) if self.store is not None else []


def _listed(stamp: tuple[int, int] | None) -> list[int] | None:
    return list(stamp) if stamp is not None else None


def load_tail(path: Path, tail_bytes: int, event_limit: int = EVENT_LIMIT,
              subagent_cache: dict | None = None) -> tuple[Session | None, dict[str, list[Event]]]:
    """The end of a session, plus activity feeds keyed by "main" and by subagent id. Pass the same
    subagent_cache each time to re-read only the subagents whose files changed."""
    events: dict[str, list[Event]] = {}
    session = load_session(path, tail_bytes=tail_bytes, events=events, event_limit=event_limit,
                           subagent_cache=subagent_cache)
    return session, events


def load_session(
    path: Path,
    away_after: timedelta = timedelta(hours=2),
    tail_bytes: int | None = None,
    events: dict[str, list[Event]] | None = None,
    event_limit: int = EVENT_LIMIT,
    subagent_cache: dict | None = None,
) -> Session | None:
    """Load a session. With tail_bytes, only the end of each transcript is read: enough
    for the live views' current state, and fast even on multi-hundred-MB transcripts.
    Totals (tool calls, tokens, prompts) then cover only that tail, and subagents last
    written before it begins are left out. Pass `events` to also collect activity feeds
    into it, and a subagent_cache to keep subagents whose files haven't changed from the
    last load that was given it."""
    records = _read(path, tail_bytes)
    if not records:
        return None
    if events is not None:
        events["main"] = extract_events(records, is_main=True)[-event_limit:]
    first = records[0]
    title = next((r["aiTitle"] for r in reversed(records) if r.get("aiTitle")), "(untitled)")
    session = Session(
        id=first.get("sessionId") or path.stem,
        project=_project_name(records, path),
        title=title,
        entrypoint=next((r["entrypoint"] for r in records if r.get("entrypoint")), "unknown"),
        version=next((r["version"] for r in reversed(records) if r.get("version")), "unknown"),
        main=_build_run(records, path.stem, "main", title, False, is_main=True, away_after=away_after),
        cwd=next((r["cwd"] for r in records if r.get("cwd")), ""),
        worktree=worktree_name(next((r["cwd"] for r in reversed(records) if r.get("cwd")), "")),
    )
    summaries = 0
    for r in records:
        if _is_human_prompt(r):
            session.human_prompts.append(r["_ts"])
            if r.get("permissionMode"):
                session.permission_modes[str(r["permissionMode"])] += 1
        if command := _slash_command(r):
            session.commands[command] += 1
        if r.get("isCompactSummary"):
            summaries += 1
        if r.get("subtype") == "compact_boundary":
            meta = r.get("compactMetadata") or {}
            session.compactions += 1
            if isinstance(meta.get("preTokens"), int):
                session.compaction_pre_tokens.append(meta["preTokens"])
            if meta.get("trigger") == "manual":
                session.manual_compactions += 1
        for block in _tool_uses(r):
            if block.get("name") == "Skill":
                session.skills[str((block.get("input") or {}).get("skill", "?"))] += 1
    # Older transcripts have no boundary records; the summary messages are the fallback.
    session.compactions = session.compactions or summaries

    # A long session can have hundreds of subagents that finished before the tail read here
    # begins; parsing them all would be most of the time a live view takes to show it.
    since = records[0]["_ts"] if tail_bytes is not None and _size(path) > tail_bytes else None
    for meta_path in sorted((path.parent / path.stem / "subagents").glob("*.meta.json")):
        run = _load_subagent(meta_path, away_after, tail_bytes, events, event_limit, since, subagent_cache)
        if run:
            session.subagents.append(run)
    return session


def _load_subagent(
    meta_path: Path,
    away_after: timedelta,
    tail_bytes: int | None = None,
    events: dict[str, list[Event]] | None = None,
    event_limit: int = EVENT_LIMIT,
    written_since: datetime | None = None,
    cache: dict | None = None,
) -> AgentRun | None:
    """A subagent's run, or None if it has no records, or wasn't written to since written_since.
    With a cache, one whose files haven't changed since it was last read comes from there: a
    working session's transcript changes every few seconds, its finished subagents don't."""
    path = meta_path.with_name(meta_path.name.removesuffix(".meta.json") + ".jsonl")
    try:
        st = path.stat()
    except OSError:
        return None  # gone
    if written_since is not None and datetime.fromtimestamp(st.st_mtime, timezone.utc) < written_since:
        return None  # finished before the part of the session that was read
    key = ((st.st_size, st.st_mtime_ns), _stamp(meta_path), tail_bytes, event_limit, away_after, events is not None)
    if cache is not None and (hit := cache.get(path)) is not None and hit[0] == key:
        _, run, run_events = hit
        if run is not None and events is not None:
            events[run.id] = run_events
        return run
    run = _read_subagent(meta_path, path, away_after, tail_bytes, events, event_limit)
    if cache is not None:
        cache[path] = (key, run, events.get(run.id) if run is not None and events is not None else None)
    return run


def _read_subagent(
    meta_path: Path,
    path: Path,
    away_after: timedelta,
    tail_bytes: int | None,
    events: dict[str, list[Event]] | None,
    event_limit: int,
) -> AgentRun | None:
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    records = _read(path, tail_bytes)
    if not records:
        return None
    run = _build_run(
        records,
        run_id=meta_path.name.removesuffix(".meta.json").removeprefix("agent-"),
        agent_type=meta.get("agentType") or "unknown",
        description=meta.get("description") or "",
        background=meta.get("requestShape") == "background",
        is_main=False,
        away_after=away_after,
    )
    run.launched_by = meta.get("toolUseId")
    if tail_bytes is not None and _size(path) > tail_bytes:
        # Only the end was read, so its spans start late; when it began still says who it ran alongside.
        run.start = _first_timestamp(path) or run.start
    if events is not None:
        events[run.id] = extract_events(records, is_main=False)[-event_limit:]
    return run


def _build_run(
    records: list[dict],
    run_id: str,
    agent_type: str,
    description: str,
    background: bool,
    *,
    is_main: bool,
    away_after: timedelta,
) -> AgentRun:
    # Only messages mark real transitions. Bookkeeping records (attachments,
    # queue-operation, api-request, mode changes) are written alongside them and
    # would otherwise split a wait on you into a misclassified sliver.
    messages = [r for r in records if r.get("type") in ("user", "assistant") and r["_timed"]]
    timeline = messages or [r for r in records if r["_timed"]] or records
    run = AgentRun(
        id=run_id,
        agent_type=agent_type,
        description=description,
        background=background,
        start=timeline[0]["_ts"],
        end=timeline[-1]["_ts"],
    )
    tool_names: dict[str, str] = {}
    open_tools: dict[str, tuple[str, datetime]] = {}  # tool calls with no result yet
    edit_targets: dict[str, str] = {}  # edit calls with no result yet: the file each one changes
    seen_messages: set[str] = set()
    last_text = ""
    handback_chars = 0
    prev = None
    for r in timeline:
        if prev is not None and r["_ts"] > prev:
            kind = _span_kind(r, tool_names, is_main)
            gap = r["_ts"] - prev
            # No single model call takes this long; the session was suspended (laptop
            # closed, VS Code quit mid-request) and resumed later.
            if (kind == MODEL and gap > MAX_MODEL_GAP) or (kind in SUSPENDABLE and gap > away_after):
                kind = AWAY
            run.spans.append(Span(kind, prev, r["_ts"]))
        prev = r["_ts"]

        if r.get("type") != "assistant":
            for block in _content(r):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    open_tools.pop(block.get("tool_use_id", ""), None)
                    target = edit_targets.pop(block.get("tool_use_id", ""), None)
                    if target and not block.get("is_error"):  # a failed or denied edit changed nothing
                        _record_change(run, target, r)
            continue
        message = r.get("message") or {}
        # One API message is written as several records (one per content block),
        # each repeating the same usage, so count usage once per message id.
        message_id = message.get("id") or r.get("uuid")
        if message_id not in seen_messages:
            seen_messages.add(message_id)
            run.output_tokens += (message.get("usage") or {}).get("output_tokens") or 0
            # "<synthetic>" marks messages Claude Code writes itself (an interrupt notice, an API error).
            if message.get("model") and message["model"] != "<synthetic>":
                run.models.add(message["model"])
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                last_text = block["text"]
            elif block.get("type") == "tool_use":
                name = block.get("name", "?")
                tool_input = block.get("input") or {}
                tool_names[block.get("id", "")] = name
                open_tools[block.get("id", "")] = (name, r["_ts"])
                run.tool_calls[name] += 1
                run.last_tool = name
                if name in DELEGATING_TOOLS:
                    run.agent_calls[block.get("id", "")] = False
                if name in READ_TOOLS and tool_input.get("file_path"):
                    run.files_read.add(_normalize_path(tool_input["file_path"]))
                elif name in EDIT_TOOLS:
                    target = tool_input.get("file_path") or tool_input.get("notebook_path")
                    if isinstance(target, str) and target:
                        edit_targets[block.get("id", "")] = target
                elif name in HANDBACK_TOOLS and isinstance(tool_input.get("message"), str):
                    handback_chars += len(tool_input["message"])
    run.final_chars = handback_chars or len(last_text)
    run.pending_tools = list(open_tools.values())
    run.agent_calls = {call: call not in open_tools for call in run.agent_calls}
    return run


def _record_change(run: AgentRun, path: str, result_record: dict) -> None:
    details = result_record.get("toolUseResult")
    line = None
    # A record holding several tool results has one set of details; use them only if they're this file's.
    if isinstance(details, dict) and _normalize_path(str(details.get("filePath", ""))) == _normalize_path(path):
        line = _first_changed_line(details)
    key = _normalize_path(path)
    change = run.files_changed.get(key)
    if change is None:
        run.files_changed[key] = FileChange(path, result_record["_ts"], line)
    else:
        change.at, change.edits = result_record["_ts"], change.edits + 1
        change.line = line or change.line


def _first_changed_line(details: dict) -> int | None:
    """Where a change starts, from the patch Claude Code records with an edit's result."""
    patch = details.get("structuredPatch")
    if isinstance(patch, list) and patch and isinstance(patch[0], dict):
        hunk = patch[0]
        start = hunk.get("newStart")
        if isinstance(start, int):
            lines = hunk.get("lines") or []
            context = next((i for i, ln in enumerate(lines) if isinstance(ln, str) and ln[:1] in "+-"), 0)
            return start + context  # skip the unchanged lines the hunk opens with
    if details.get("type") == "create":
        return 1
    return None


def _span_kind(record: dict, tool_names: dict[str, str], is_main: bool) -> str:
    """Classify the time leading up to `record` by what `record` is."""
    if record.get("type") == "assistant":
        return MODEL
    if is_main and _is_human_prompt(record):
        return WAITING_ON_YOU
    kind = _origin_kind(record)
    if kind == "coordinator":
        return DORMANT
    if kind in ("task-notification", "peer"):
        return AWAY if ORPHAN_NOTICE in _text_of(record) else WAITING_ON_BACKGROUND
    results = [b for b in _content(record) if isinstance(b, dict) and b.get("type") == "tool_result"]
    if results:
        names = {tool_names.get(b.get("tool_use_id", ""), "") for b in results}
        decision = record.get("permissionDecision")
        if names & HUMAN_ANSWERED_TOOLS:
            return WAITING_ON_YOU
        if isinstance(decision, dict) and str(decision.get("source", "")).startswith("user"):
            return WAITING_ON_YOU  # a permission prompt was open
        if names & DELEGATING_TOOLS:
            return DELEGATED
        return TOOL
    return MODEL


def extract_events(records: list[dict], is_main: bool) -> list[Event]:
    """Turn records into a readable activity feed: prompts, replies, tool calls, errors."""
    events: list[Event] = []
    for r in records:
        t = r["_ts"]
        if r.get("subtype") == "compact_boundary":
            pre = (r.get("compactMetadata") or {}).get("preTokens")
            events.append(Event(t, "compact", "context compacted" + (f" at {pre // 1000}K tokens" if pre else "")))
            continue
        if r.get("type") == "assistant":
            for block in _content(r):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text", "").strip():
                    events.append(Event(t, "claude", _one_line(block["text"]), block["text"][:DETAIL_CHARS]))
                elif block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    tool_input = block.get("input") or {}
                    report = tool_input.get("message")
                    if name in HANDBACK_TOOLS and isinstance(report, str) and report.strip():
                        # A background subagent's final report is its reply, not a tool call.
                        events.append(Event(t, "claude", _one_line(report), report[:DETAIL_CHARS]))
                        continue
                    summary = tool_summary(tool_input, r.get("cwd"))
                    events.append(Event(t, "tool", f"{name}: {summary}" if summary else name))
            continue
        if r.get("type") != "user" or r.get("isMeta") or r.get("isCompactSummary"):
            continue
        if is_main and _is_human_prompt(r):
            images = sum(1 for b in _content(r) if isinstance(b, dict) and b.get("type") == "image")
            body = f"{image_marks(images)} {_text_of(r)}".strip()
            events.append(Event(t, "you", _one_line(body), body[:DETAIL_CHARS]))
        elif _origin_kind(r) in ("task-notification", "peer", "coordinator"):
            body = _text_of(r)
            match = re.search(r"<summary>(.*?)</summary>", body, re.S)
            label = {"peer": "message from another session", "coordinator": "message from parent session"}
            text = _one_line(match[1]) if match else label.get(_origin_kind(r), "background task update")
            events.append(Event(t, "notice", text, body[:DETAIL_CHARS]))
        else:
            for block in _content(r):
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    body = _result_text(block)
                    events.append(Event(t, "error", _one_line(body) or "tool error", body[:DETAIL_CHARS]))
    return events


def tool_summary(tool_input: dict, cwd: str | None = None) -> str:
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description", "skill", "prompt"):
        value = tool_input.get(key)
        if not (isinstance(value, str) and value.strip()):
            continue
        if key in ("file_path", "path"):
            return short_path(value, cwd)
        if key == "command":
            return _one_line(CD_PREFIX.sub("", value.strip()))
        return _one_line(value)
    value = next((v for v in tool_input.values() if isinstance(v, str) and v.strip()), "")
    return _one_line(value)


CD_PREFIX = re.compile(r'^cd\s+("[^"]*"|\'[^\']*\'|\S+)\s*(&&|;)\s*')


def short_path(path: str, cwd: str | None) -> str:
    """Relative to the session's directory when inside it, else the last three parts."""
    norm = path.replace("\\", "/")
    if cwd:
        base = cwd.replace("\\", "/").rstrip("/") + "/"
        if norm.lower().startswith(base.lower()):
            return norm[len(base):]
    parts = [p for p in norm.split("/") if p]
    return norm if len(parts) <= 3 else "…/" + "/".join(parts[-3:])


def image_marks(count: int) -> str:
    """How a message shows the images that went with it: "[Image #1] [Image #2]"."""
    return " ".join(f"[Image #{n}]" for n in range(1, count + 1))


def _text_of(record: dict) -> str:
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in _content(record) if isinstance(b, dict) and b.get("type") == "text")


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def _one_line(text: str, limit: int = 240) -> str:
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _is_human_prompt(record: dict) -> bool:
    if record.get("type") != "user" or record.get("isMeta") or record.get("isCompactSummary"):
        return False
    kind = _origin_kind(record)
    if kind is not None:
        return kind == "human"
    # Older transcripts have no origin field: fall back to "plain text, not a system injection".
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return not content.lstrip().startswith("<")
    return False


def _slash_command(record: dict) -> str | None:
    """The slash command you typed, from the record Claude Code writes for it: "/model"."""
    if record.get("type") != "user":
        return None
    content = (record.get("message") or {}).get("content")
    match = SLASH_COMMAND.match(content.lstrip()) if isinstance(content, str) else None
    return match[1] if match else None


def _origin_kind(record: dict) -> str | None:
    origin = record.get("origin")
    return origin.get("kind") if isinstance(origin, dict) else None


def _content(record: dict) -> list:
    content = (record.get("message") or {}).get("content")
    return content if isinstance(content, list) else []


def _tool_uses(record: dict) -> Iterator[dict]:
    if record.get("type") == "assistant":
        for block in _content(record):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield block


def _read(path: Path, tail_bytes: int | None = None) -> list[dict]:
    """Timestamped records in time order. Untimestamped ones (titles, mode changes) are
    kept, positioned at the time of the record before them."""
    records: list[dict] = []
    last_ts = None
    try:
        with path.open("rb") as f:
            size = f.seek(0, 2)
            start = max(0, size - tail_bytes) if tail_bytes else 0
            f.seek(start)
            if start > 0:
                f.readline()  # starts mid-record
            # A line at a time: reading a multi-hundred-MB transcript whole, and its decoded copy
            # and lines on top, took over twice the memory the records do.
            for line in f:
                try:
                    record = json.loads(line.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                try:
                    record["_ts"] = last_ts = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
                    record["_timed"] = True
                except (KeyError, TypeError, ValueError, AttributeError):
                    record["_ts"], record["_timed"] = last_ts, False
                records.append(record)
    except OSError:
        return []
    records = [r for r in records if r["_ts"] is not None]
    records.sort(key=lambda r: r["_ts"])
    return records


def _first_timestamp(path: Path, max_lines: int = 50) -> datetime | None:
    """When a transcript began, from its first timestamped record, without reading the rest."""
    try:
        with path.open("rb") as f:
            for _ in range(max_lines):
                line = f.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                    return datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
    except OSError:
        pass
    return None


def worked_in(root: Path, session_ids: Iterable[str]) -> Iterator[str]:
    """The folders these sessions worked in, the most recently active first, from the start of
    their transcripts. Ids can be the start of one, as a space keeps them."""
    return _folders([p for sid in session_ids if sid for p in root.glob(f"*/{sid}*.jsonl")])


def worked_in_named(root: Path, name: str) -> Iterator[str]:
    """The folders worked in by sessions that may be in a folder called `name` or under one, the
    most recently active first. Claude Code names a session's project folder after the folder it
    works in, with each character but a letter or digit as -, so only those transcripts are read."""
    named = re.sub(r"[^A-Za-z0-9]", "-", name).lower()
    try:
        projects = [d for d in root.iterdir() if named in d.name.lower()]
    except OSError:
        return iter(())
    return _folders([p for d in projects for p in d.glob("*.jsonl")])


def _folders(transcripts: list[Path]) -> Iterator[str]:
    transcripts.sort(key=lambda p: (_stamp(p) or (0, 0))[1], reverse=True)
    return (cwd for cwd in map(_first_cwd, transcripts) if cwd)


def _first_cwd(path: Path, max_lines: int = 50) -> str:
    try:
        with path.open("rb") as f:
            for _ in range(max_lines):
                line = f.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and isinstance(record.get("cwd"), str) and record["cwd"]:
                    return record["cwd"]
    except OSError:
        pass
    return ""


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _stamp(path: Path) -> tuple[int, int] | None:
    """A file's size and modification time, which change when it's written to."""
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def worktree_name(cwd: str) -> str:
    """The Claude Code worktree a folder is in: <repo>/.claude/worktrees/<name>/..., else ""."""
    parts = [p for p in cwd.replace("\\", "/").split("/") if p]
    for i in range(len(parts) - 2):
        if parts[i] == ".claude" and parts[i + 1] == "worktrees":
            return parts[i + 2]
    return ""


def _project_name(records: list[dict], path: Path) -> str:
    cwd = next((r["cwd"] for r in records if r.get("cwd")), None)
    if not cwd:
        return path.parent.name
    return Path(cwd.replace("\\", "/")).name or cwd


def _normalize_path(file_path: str) -> str:
    return file_path.replace("\\", "/").lower()
