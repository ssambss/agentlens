"""Turn sessions into flat metric dicts. Rules, reports and the profile all read these.

Three entity kinds: "session", "subagent" and "day" (cross-session load on one date).
Adding a metric here makes it available to every rule pack without other code changes.

Scripted runs, a script starting `claude -p` over a list, are flagged `scripted` and left
out of days, baselines, findings and the profile: they're not how you work with Claude.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from panopsis.model import (
    ACTIVE_KINDS,
    AWAY,
    DELEGATED,
    DORMANT,
    WAITING_ON_BACKGROUND,
    WAITING_ON_YOU,
    AgentRun,
    Session,
)

# A scripted run has one prompt and starts in a burst: at least BURST_RUNS such sessions in one
# folder, each started within BURST_GAP of the one before. A script that gives each run a folder
# of its own (runs/a-1, runs/a-2) bursts in their parent folder instead. Someone asking one-off
# questions in new chats doesn't start ten that close together.
BURST_RUNS = 10
BURST_GAP = timedelta(minutes=10)
TYPE_MIN_RUNS = 5  # your runs of a subagent type before one of them can be compared with the rest


def scripted_runs(sessions: list[Session]) -> set[str]:
    """Ids of the sessions a script started rather than you."""
    folders = Counter(_folder(s) for s in sessions)
    by_folder: dict[str, list[Session]] = defaultdict(list)
    for session in sessions:
        if len(session.human_prompts) != 1:
            continue
        folder = _folder(session)
        by_folder[folder].append(session)
        # A folder with only a few sessions may be one a script made for a run. Not a project
        # you work in: a parent like your Desktop holds those, and its burst would take them in.
        if folders[folder] < BURST_RUNS and "/" in folder:
            by_folder[folder.rsplit("/", 1)[0] + "/*"].append(session)
    out: set[str] = set()
    for runs in by_folder.values():
        runs.sort(key=lambda s: s.start)
        burst = runs[:1]
        for prev, cur in zip(runs, runs[1:]):
            if cur.start - prev.start > BURST_GAP:
                if len(burst) >= BURST_RUNS:
                    out.update(s.id for s in burst)
                burst = []
            burst.append(cur)
        if len(burst) >= BURST_RUNS:
            out.update(s.id for s in burst)
    return out


def _folder(session: Session) -> str:
    """Where it ran, written one way: VS Code says c:\\ where the CLI says C:\\."""
    if not session.cwd:
        return f"project:{session.project}"
    return session.cwd.replace("\\", "/").rstrip("/").casefold()


def unscripted(sessions: list[Session], entities: dict[str, list[dict]]) -> list[Session]:
    """The sessions you ran yourself, by the flag `all_entities` set."""
    scripted = {row["id"] for row in entities.get("session", []) if row.get("scripted")}
    return [s for s in sessions if s.id not in scripted]


def session_metrics(session: Session) -> dict:
    main = session.main
    active = main.minutes(*ACTIVE_KINDS)
    waiting = main.minutes(WAITING_ON_YOU)
    subs = session.subagents
    return {
        "kind": "session",
        "id": session.id,
        "project": session.project,
        "title": session.title,
        "entrypoint": session.entrypoint,
        "version": session.version,
        "start": session.start,
        "wall_min": _minutes(session.start, session.end),
        "active_min": active,
        "waiting_on_you_min": waiting,
        "waiting_on_background_min": main.minutes(WAITING_ON_BACKGROUND),
        "delegated_min": main.minutes(DELEGATED),
        "away_min": main.minutes(AWAY),
        "waiting_share": waiting / (active + waiting) if active + waiting else 0.0,
        "human_prompts": len(session.human_prompts),
        "active_per_prompt_min": _median_active_per_prompt(session),
        "subagents": len(subs),
        "background_subagents": sum(1 for s in subs if s.background),
        "foreground_subagents": sum(1 for s in subs if not s.background),
        "max_parallel_subagents": _peak_overlap([(s.start, s.end) for s in subs]),
        **_delegation(session, main),
        "compactions": session.compactions,
        "compacted_at_ktokens": (
            statistics.median(session.compaction_pre_tokens) / 1000 if session.compaction_pre_tokens else 0.0
        ),
        "skill_calls": sum(session.skills.values()),
        "tool_calls": sum(main.tool_calls.values()),
        "output_tokens": main.output_tokens + sum(s.output_tokens for s in subs),
    }


def subagent_metrics(session: Session, run: AgentRun) -> dict:
    return {
        "kind": "subagent",
        "id": run.id,
        "session_id": session.id,
        "project": session.project,
        "session_title": session.title,
        "entrypoint": session.entrypoint,
        "version": session.version,
        "description": run.description or "(no description)",
        "agent_type": run.agent_type,
        "background": run.background,
        "start": run.start,
        "wall_min": _minutes(run.start, run.end),
        "active_min": run.minutes(*ACTIVE_KINDS),
        "dormant_min": run.minutes(DORMANT),
        "delegated_min": run.minutes(DELEGATED),
        "tool_calls": sum(run.tool_calls.values()),
        "output_tokens": run.output_tokens,
        "final_chars": run.final_chars,
        "files_read": len(run.files_read),
        "shared_reads": len(run.files_read & session.main.files_read),
        **_delegation(session, run),
    }


def _delegation(session: Session, parent: AgentRun) -> dict:
    """The subagents a thread launched itself and waited on: how many, in how many rounds, of what types.
    A round is a stretch where at least one of them ran; two launched together share one."""
    children = [run for run in session.subagents if run.launched_by and run.launched_by in parent.agent_calls]
    waited = [run for run in children if not run.background]
    types = Counter(run.agent_type for run in waited)
    return {
        "foreground_children": len(waited),
        "foreground_waves": len(_merge([(run.start, run.end) for run in waited])),
        "child_types": ", ".join(f"{name} ×{n}" for name, n in types.most_common(3)),
        # Where they come from: the plugin (acme-core for acme-core:verifier), else the type
        # itself. One plugin's pipeline is one cause, whichever of its steps a session ran.
        "child_sources": ", ".join(sorted({name.split(":")[0] for name in types})),
    }


def day_metrics(sessions: list[Session]) -> list[dict]:
    """Cross-session load per local date: how many sessions ran, and waited on you, at once."""
    working: dict = defaultdict(list)
    waiting: dict = defaultdict(list)
    for session in sessions:
        runs = [session.main, *session.subagents]
        busy = _merge([(s.start, s.end) for run in runs for s in run.spans if s.kind in ACTIVE_KINDS])
        idle = _merge([(s.start, s.end) for s in session.main.spans if s.kind == WAITING_ON_YOU])
        for start, end in busy:
            working[_local_date(start)].append((start, end))
        for start, end in idle:
            waiting[_local_date(start)].append((start, end))
    days = []
    for date in sorted(set(working) | set(waiting)):
        days.append(
            {
                "kind": "day",
                "id": date,
                "date": date,
                "peak_parallel_working": _peak_overlap(working[date]),
                "peak_waiting_on_you": _peak_overlap(waiting[date]),
                "minutes_2plus_waiting": _minutes_at_or_above(waiting[date], 2),
            }
        )
    return days


def all_entities(sessions: list[Session]) -> dict[str, list[dict]]:
    scripted = scripted_runs(sessions)
    subagents = [{**subagent_metrics(s, run), "scripted": s.id in scripted} for s in sessions for run in s.subagents]
    _compare_with_type(subagents)
    return {
        "session": [{**session_metrics(s), "scripted": s.id in scripted} for s in sessions],
        "subagent": subagents,
        "day": day_metrics([s for s in sessions if s.id not in scripted]),
    }


def _compare_with_type(rows: list[dict]) -> None:
    """How long each subagent worked against your usual run of its type. A planner and a
    reviewer take different times, so one is long only next to others of its kind."""
    by_type: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if not row["scripted"]:
            by_type[row["agent_type"]].append(row["active_min"])
    for row in rows:
        values = by_type.get(row["agent_type"], [])
        median = statistics.median(values) if len(values) >= TYPE_MIN_RUNS else 0.0  # too few to say
        row["type_runs"] = len(values)
        row["type_median_min"] = median
        row["active_vs_type_median"] = row["active_min"] / median if median else 0.0


def baselines(entities: dict[str, list[dict]]) -> dict[str, dict[str, list[float]]]:
    """Sorted values of every numeric metric, per entity kind: the user's own history."""
    out: dict[str, dict[str, list[float]]] = {}
    for kind, rows in entities.items():
        columns: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            if row.get("scripted"):
                continue
            for key, value in row.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    columns[key].append(float(value))
        out[kind] = {key: sorted(values) for key, values in columns.items()}
    return out


def percentile(sorted_values: list[float], pct: float) -> float | None:
    if not sorted_values:
        return None
    index = (len(sorted_values) - 1) * pct / 100
    low = int(index)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (index - low)


def _median_active_per_prompt(session: Session) -> float:
    """Median minutes of agent work between one of your prompts and the next."""
    prompts = sorted(session.human_prompts)
    if not prompts:
        return 0.0
    bounds = [*prompts, session.main.end]
    per_prompt = []
    for start, end in zip(bounds, bounds[1:]):
        per_prompt.append(
            sum(s.minutes for s in session.main.spans if s.kind in ACTIVE_KINDS and start <= s.start < end)
        )
    return statistics.median(per_prompt) if per_prompt else 0.0


def _merge(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[list[datetime]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def _events(intervals):
    # Ends sort before starts at the same instant, so touching intervals don't count as overlapping.
    return sorted([(s, 1) for s, _ in intervals] + [(e, -1) for _, e in intervals], key=lambda x: (x[0], x[1]))


def _peak_overlap(intervals: list[tuple[datetime, datetime]]) -> int:
    peak = current = 0
    for _, delta in _events(intervals):
        current += delta
        peak = max(peak, current)
    return peak


def _minutes_at_or_above(intervals: list[tuple[datetime, datetime]], level: int) -> float:
    total = 0.0
    current = 0
    since = None
    for t, delta in _events(intervals):
        if current >= level and since is not None:
            total += (t - since).total_seconds() / 60
        current += delta
        since = t
    return total


def _minutes(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 60


def _local_date(t: datetime) -> str:
    return t.astimezone().date().isoformat()
