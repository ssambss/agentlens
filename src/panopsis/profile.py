"""Derive a user's working style from their data, and compare it with what they said they want.

The derived style is descriptive, not a grade: close steering and long autonomous
runs are both valid. What's worth surfacing is a gap between how someone says they
work and how they actually do.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field

from panopsis.metrics import unscripted
from panopsis.model import ACTIVE_KINDS, AWAY, DELEGATED, WAITING_ON_BACKGROUND, WAITING_ON_YOU, Session
from panopsis.preferences import Preferences

# Median minutes of agent work per prompt, at or below which steering counts as close
# and at or above which it counts as autonomous.
CLOSE_STEERING_MAX = 1.5
AUTONOMOUS_MIN = 6.0
AWAY_LABEL = "away or idle"


@dataclass
class Style:
    dimension: str
    value: str
    evidence: str


@dataclass
class Profile:
    sessions: int
    projects: int
    styles: list[Style]
    time_split: dict[str, float]  # minutes, across all main threads
    top_tools: list[tuple[str, int]]
    skills: Counter
    surfaces: Counter
    median_session_min: float
    compaction_rate: float
    mismatches: list[str] = field(default_factory=list)
    subagents: int = 0
    scripted: Counter = field(default_factory=Counter)  # runs a script started, left out of the rest, by project

    def style(self, dimension: str) -> str | None:
        return next((s.value for s in self.styles if s.dimension == dimension), None)


def time_split(sessions: list[Session]) -> dict[str, float]:
    """Minutes of the sessions' main threads, by what they were spent on."""
    return {
        "working": sum(s.main.minutes(*ACTIVE_KINDS) - s.main.minutes(DELEGATED) for s in sessions),
        "blocked on subagents": sum(s.main.minutes(DELEGATED) for s in sessions),
        "waiting on you": sum(s.main.minutes(WAITING_ON_YOU) for s in sessions),
        "waiting on background work": sum(s.main.minutes(WAITING_ON_BACKGROUND) for s in sessions),
        AWAY_LABEL: sum(s.main.minutes(AWAY) for s in sessions),
    }


def build(sessions: list[Session], entities: dict[str, list[dict]], prefs: Preferences) -> Profile:
    yours = unscripted(sessions, entities)
    kept = {s.id for s in yours}
    scripted = Counter(s.project for s in sessions if s.id not in kept)
    sessions = yours
    rows = [r for r in entities["session"] if not r.get("scripted")]
    tools: Counter = Counter()
    skills: Counter = Counter()
    for s in sessions:
        tools.update(s.main.tool_calls)
        skills.update(s.skills)
    profile = Profile(
        sessions=len(sessions),
        projects=len({s.project for s in sessions}),
        styles=[_steering(rows), _parallelism(entities["day"]), _delegation(rows)],
        time_split=time_split(sessions),
        top_tools=tools.most_common(8),
        skills=skills,
        surfaces=Counter(s.entrypoint for s in sessions),
        median_session_min=statistics.median([r["wall_min"] for r in rows]) if rows else 0.0,
        compaction_rate=(sum(r["compactions"] for r in rows) / len(rows)) if rows else 0.0,
        subagents=sum(len(s.subagents) for s in sessions),
        scripted=scripted,
    )
    for style in profile.styles:
        stated = prefs.style.get(style.dimension)
        if stated and style.value not in ("unknown", stated):
            profile.mismatches.append(
                f"You said {style.dimension}: {stated}, but your data looks {style.value} ({style.evidence})."
            )
    return profile


def _steering(rows: list[dict]) -> Style:
    conversational = [r["active_per_prompt_min"] for r in rows if r["human_prompts"] >= 3]
    if not conversational:
        return Style("steering", "unknown", "not enough multi-prompt sessions yet")
    median = statistics.median(conversational)
    if median <= CLOSE_STEERING_MAX:
        value = "close"
    elif median >= AUTONOMOUS_MIN:
        value = "autonomous"
    else:
        value = "balanced"
    return Style("steering", value, f"median {median:.1f} min of agent work per prompt")


def _parallelism(days: list[dict]) -> Style:
    peaks = [d["peak_parallel_working"] for d in days if d["peak_parallel_working"] > 0]
    if not peaks:
        return Style("parallelism", "unknown", "no active days yet")
    median = statistics.median(peaks)
    value = "single" if median <= 1 else "few" if median <= 3 else "many"
    return Style("parallelism", value, f"typically {median:g} session{'' if median == 1 else 's'} working at once at peak, max {max(peaks)}")


def _delegation(rows: list[dict]) -> Style:
    if not rows:
        return Style("delegation", "unknown", "no sessions yet")
    total = sum(r["subagents"] for r in rows)
    rate = total / len(rows)
    background = sum(r["background_subagents"] for r in rows)
    if rate < 0.25:
        value = "minimal"
    elif rate >= 2:
        value = "heavy"
    else:
        value = "fan-out"
    share = f", {background / total:.0%} in the background" if total else ""
    return Style("delegation", value, f"{total} subagent{'' if total == 1 else 's'} over {len(rows)} session{'' if len(rows) == 1 else 's'}{share}")
