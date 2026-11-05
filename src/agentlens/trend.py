"""The past 7 days against the 7 before them: where the time went and how often each rule
fired, so a change to how you work shows up the week after.

A session counts in the week it started, a finding in the week its session, subagent or
day did. Scripted runs are left out, as everywhere else.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from agentlens.metrics import unscripted
from agentlens.model import Session
from agentlens.profile import time_split
from agentlens.rules import Finding

WEEK = timedelta(days=7)


@dataclass
class Week:
    sessions: int
    time_split: dict[str, float]  # minutes, as in the profile
    findings: Counter  # rule id -> how many times it fired


@dataclass
class Trend:
    recent: Week  # the past 7 days
    before: Week  # the 7 days before them


def build(sessions: list[Session], entities: dict[str, list[dict]], findings: list[Finding],
          now: datetime | None = None) -> Trend | None:
    """None unless both weeks have sessions of yours: there's nothing to compare."""
    now = now or datetime.now(timezone.utc)
    yours = unscripted(sessions, entities)
    weeks = []
    for start in (now - WEEK, now - 2 * WEEK):
        end = start + WEEK
        these = [s for s in yours if start <= s.start < end]
        fired = Counter(f.rule.id for f in findings if (at := _when(f.entity)) and start <= at < end)
        weeks.append(Week(len(these), time_split(these), fired))
    if not all(w.sessions for w in weeks):
        return None
    return Trend(*weeks)


def _when(entity: dict) -> datetime | None:
    if isinstance(entity.get("start"), datetime):
        return entity["start"]
    try:  # a day: its local midnight
        return datetime.fromisoformat(str(entity["date"])).astimezone()
    except (KeyError, ValueError):
        return None
