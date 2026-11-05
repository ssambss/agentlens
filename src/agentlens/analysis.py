"""Everything the report-style views need, computed once from full session history.

Reading full history is the slow part (seconds on large transcripts), so the app builds
this in the background once and re-evaluates rules against it when preferences change.
History builds one from just the sessions it lists, for their details.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from agentlens import preferences
from agentlens import profile as profile_mod, trend as trend_mod
from agentlens.adapters.claude_transcripts import DEFAULT_ROOT, SessionCache, load_sessions
from agentlens.metrics import all_entities, baselines
from agentlens.model import Session
from agentlens.preferences import Preferences
from agentlens.profile import Profile
from agentlens.rules import (ContextEntry, Finding, Muted, Rule, evaluate, load_context, load_rules, pack_dirs,
                             stale_rules, unknown_metrics)
from agentlens.trend import Trend


@dataclass
class Analysis:
    sessions: list[Session]  # all of them, scripted runs too: the metrics flag those, and the rest leaves them out
    entities: dict[str, list[dict]]
    history: dict
    rules: list[Rule]
    prefs: Preferences
    profile: Profile | None
    findings: list[Finding]
    muted: list[Muted]
    stale: list[tuple[Rule, int | None]]
    broken: list[tuple[Rule, list[str]]]  # rules naming metrics that don't exist
    loaded_at: datetime
    trend: Trend | None = None  # the past 7 days against the 7 before
    context: list[ContextEntry] = field(default_factory=list)  # what team packs have written down

    def session(self, session_id: str) -> Session | None:
        return next((s for s in self.sessions if s.id == session_id), None)

    def findings_for(self, session_id: str) -> list[Finding]:
        return [
            f for f in self.findings
            if f.entity.get("id") == session_id or f.entity.get("session_id") == session_id
        ]


def build(root: Path = DEFAULT_ROOT, extra_rules: list[Path] | None = None,
          cache: SessionCache | None = None, progress: Callable[[int, int], None] | None = None) -> Analysis:
    """`progress` hears how many transcripts it has read, of how many."""
    return from_sessions(list(load_sessions(root=root, cache=cache, progress=progress)), extra_rules)


def from_sessions(sessions: list[Session], extra_rules: list[Path] | None = None) -> Analysis:
    """The analysis of these sessions: History's, for the ones it has read so far."""
    prefs = preferences.load()
    entities = all_entities(sessions)
    history = baselines(entities)
    dirs = pack_dirs(extra_rules, prefs)
    rules = load_rules(dirs)
    findings, muted = evaluate(rules, entities, history, prefs)
    return Analysis(
        sessions=sessions,
        entities=entities,
        history=history,
        rules=rules,
        prefs=prefs,
        profile=profile_mod.build(sessions, entities, prefs) if sessions else None,
        findings=findings,
        muted=muted,
        stale=stale_rules(rules),
        broken=unknown_metrics(rules, entities),
        loaded_at=datetime.now(timezone.utc),
        trend=trend_mod.build(sessions, entities, findings),
        context=load_context(dirs),
    )


def reevaluate(analysis: Analysis) -> Analysis:
    """Re-apply rules after preferences change (a mute, a new goal) without re-reading history."""
    prefs = preferences.load()
    findings, muted = evaluate(analysis.rules, analysis.entities, analysis.history, prefs)
    prof = profile_mod.build(analysis.sessions, analysis.entities, prefs) if analysis.sessions else None
    trend = trend_mod.build(analysis.sessions, analysis.entities, findings)
    return replace(analysis, prefs=prefs, findings=findings, muted=muted, profile=prof, trend=trend)
