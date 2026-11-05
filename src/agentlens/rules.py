"""Rule packs: best practices as data.

A pack is a YAML file of rules. Each rule names the entity kind it applies to, a
condition over that entity's metrics, the advice to show, where the advice comes
from, and the date someone last checked it still holds. Changing practice means
editing YAML in a shared repo, not releasing a new version of the tool.

Thresholds can be absolute (`value: 5`) or relative to the user's own history
(`value: p90`), so the same pack adapts to each person.

Findings are grouped by what they have in common (`group_by`: a subagent's type, a
session's project), so one cause shows once however often it fired.

A pack can also hold `context:` entries: what a team has written down about how it works
that no metric can detect, such as where a skill's knowledge belongs. Each says who it's
`for` and carries a practice with its sources; advice gets them all and uses the ones
that fit the person's work.
"""

from __future__ import annotations

import operator
import os
import string
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from importlib import resources
from pathlib import Path

import yaml

from agentlens.metrics import percentile
from agentlens.preferences import Preferences

OPS = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}
SEVERITY_ORDER = {"warn": 0, "info": 1}
MIN_HISTORY = 5  # percentile thresholds need at least this many of your own data points
# Where a practice stands: still the way to do it, changed by a newer feature or default,
# replaced by something else, or new and not yet proven.
PRACTICE_STATUSES = ("current", "changed", "superseded", "emerging")
PRACTICE_TEXT = ("was", "now", "still_fits", "available_from")
# What a rule's findings are grouped by unless it says: a subagent's type, a session's
# project. Days have nothing in common but the rule, so they make one group.
DEFAULT_GROUP_BY = {"subagent": "agent_type", "session": "project"}


class RuleError(ValueError):
    pass


@dataclass
class Rule:
    id: str
    applies_to: str
    title: str
    when: dict
    message: str
    advice: str
    source: str
    reviewed: date | None
    pack: str
    severity: str = "info"
    goals: list[str] = field(default_factory=list)
    unless_preference: dict = field(default_factory=dict)
    max_age_days: int = 90
    practice: Practice | None = None
    group_by: str = ""  # the metric findings are grouped by; "" puts them all in one group
    total: str = ""  # a line like `message` over a group's summed metrics: "{delegated_min:.0f} min blocked"


@dataclass
class Source:
    url: str
    says: str  # the passage the practice rests on, quoted
    date: date | None = None


@dataclass
class Practice:
    """How the way of working a rule detects has moved: what it was, what to do now, and the
    sources that say so. Advice states these, so it doesn't have to recall them."""

    status: str
    was: str = ""
    now: str = ""
    still_fits: str = ""  # when the old way is still the right one
    available_from: str = ""  # the Claude Code version from which `now` works by default
    sources: list[Source] = field(default_factory=list)
    # What someone can change in their own setup to work the new way: where (a CLAUDE.md, a
    # subagent's definition, settings, a hook, how they prompt) and what. Advice fits these to
    # the person's setup; they're the part of a practice someone acts on.
    actions: list[str] = field(default_factory=list)


@dataclass
class ContextEntry:
    """Something a team has written down about how it works, which no metric can detect: a
    pack's `context:` entry. It reaches advice by who it's for, not through a finding."""

    id: str
    title: str
    audience: str  # the entry's `for:`, who it fits: "anyone writing a skill about a service"
    practice: Practice
    pack: str
    reviewed: date | None = None
    max_age_days: int = 90


@dataclass
class Finding:
    rule: Rule
    entity: dict
    message: str
    notes: list[str] = field(default_factory=list)  # how personal thresholds resolved


@dataclass
class FindingGroup:
    """A rule's findings that share a cause, the biggest first."""

    rule: Rule
    cause: str  # what they have in common: "acme-core:implementer", a project; "" when the rule doesn't group
    findings: list[Finding]
    total: str  # the rule's `total` over all of them, "" without one


@dataclass
class Muted:
    rule: Rule
    reason: str


def builtin_pack_dir() -> Path:
    return Path(str(resources.files("agentlens") / "rules"))


def pack_dirs(extra: list[Path] | None = None, prefs: Preferences | None = None) -> list[Path]:
    """Built-in pack first, then team/personal packs; later packs override earlier ones by rule id."""
    dirs = [builtin_pack_dir()]
    dirs += [Path(p) for p in os.environ.get("AGENTLENS_RULES", "").split(os.pathsep) if p]
    if prefs:
        dirs += [Path(p).expanduser() for p in prefs.rule_packs]
    dirs += extra or []
    return dirs


def load_rules(dirs: list[Path]) -> list[Rule]:
    return _load_all(dirs, _load_pack)


def load_context(dirs: list[Path]) -> list[ContextEntry]:
    """The packs' `context:` entries. Later packs override earlier ones by id, as with rules."""
    return _load_all(dirs, _load_context)


def _load_all(dirs: list[Path], load) -> list:
    loaded: dict = {}
    for directory in dirs:
        files = sorted(directory.glob("*.y*ml")) if directory.is_dir() else [directory]
        for path in files:
            for item_id, item in load(path):
                if item is None:
                    loaded.pop(item_id, None)
                else:
                    loaded[item_id] = item
    return list(loaded.values())


def _read_pack(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise RuleError(f"{path}: {e}") from e


def _load_context(path: Path):
    data = _read_pack(path)
    pack = data.get("pack") or path.stem
    max_age = int(data.get("max_age_days", 90))
    for raw in data.get("context") or []:
        entry_id = raw.get("id")
        if not entry_id:
            raise RuleError(f"{path}: a context entry is missing its id")
        if raw.get("disabled"):
            yield entry_id, None
            continue
        try:
            practice = _practice(raw["practice"])
            if not practice.sources:  # with no metric behind it, the source is all it rests on
                raise ValueError("a context entry needs at least one source saying so")
            yield entry_id, ContextEntry(
                id=entry_id,
                title=raw["title"],
                audience=" ".join(str(raw["for"]).split()),
                practice=practice,
                pack=pack,
                reviewed=_as_date(raw.get("reviewed")),
                max_age_days=int(raw.get("max_age_days", max_age)),
            )
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise RuleError(f"{path}: context entry {entry_id!r}: {e}") from e


def _load_pack(path: Path):
    data = _read_pack(path)
    pack = data.get("pack") or path.stem
    max_age = int(data.get("max_age_days", 90))
    for raw in data.get("rules") or []:
        rule_id = raw.get("id")
        if not rule_id:
            raise RuleError(f"{path}: a rule is missing its id")
        if raw.get("disabled"):
            yield rule_id, None
            continue
        try:
            when = raw["when"]
            _validate(when)
            yield rule_id, Rule(
                id=rule_id,
                applies_to=raw["applies_to"],
                title=raw["title"],
                when=when,
                message=raw.get("message", raw["title"]),
                advice=(raw.get("advice") or "").strip(),
                source=raw.get("source", ""),
                reviewed=_as_date(raw.get("reviewed")),
                pack=pack,
                severity=raw.get("severity", "info"),
                goals=list(raw.get("goals") or []),
                unless_preference=dict(raw.get("unless_preference") or {}),
                max_age_days=int(raw.get("max_age_days", max_age)),
                practice=_practice(raw["practice"]) if raw.get("practice") else None,
                group_by=str(raw["group_by"] or "") if "group_by" in raw
                else DEFAULT_GROUP_BY.get(raw["applies_to"], ""),
                total=str(raw.get("total") or ""),
            )
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise RuleError(f"{path}: rule {rule_id!r}: {e}") from e


def _practice(raw: dict) -> Practice:
    status = raw.get("status")
    if status not in PRACTICE_STATUSES:
        raise ValueError(f"practice status {status!r}; use one of {', '.join(PRACTICE_STATUSES)}")
    sources = []
    for source in raw.get("sources") or []:
        if not source.get("url") or not source.get("says"):
            raise ValueError("each practice source needs a url and the passage it says")
        sources.append(Source(str(source["url"]), " ".join(str(source["says"]).split()), _as_date(source.get("date"))))
    if status != "current" and not sources:
        raise ValueError(f"a {status} practice needs at least one source saying so")
    actions = raw.get("actions") or []
    if not isinstance(actions, list) or not all(isinstance(a, str) and a.strip() for a in actions):
        raise ValueError("practice actions must be a list of things to do, each a line of text")
    text = {key: " ".join(str(raw.get(key) or "").split()) for key in PRACTICE_TEXT}
    return Practice(status=status, sources=sources, actions=[" ".join(a.split()) for a in actions], **text)


def version_tuple(version: str) -> tuple[int, ...] | None:
    """2.1.302 as (2, 1, 302), so versions compare as numbers. None for anything else."""
    parts = str(version or "").strip().split(".")
    return tuple(int(p) for p in parts) if all(p.isdigit() for p in parts) and parts != [""] else None


def _validate(cond: dict) -> None:
    if "all" in cond or "any" in cond:
        for child in cond.get("all") or cond.get("any"):
            _validate(child)
        return
    if cond.get("op") not in OPS:
        raise ValueError(f"unknown op {cond.get('op')!r}; use one of {', '.join(OPS)}")
    if "metric" not in cond or "value" not in cond:
        raise ValueError("a condition needs metric, op and value")


def evaluate(
    rules: list[Rule],
    entities: dict[str, list[dict]],
    history: dict[str, dict[str, list[float]]],
    prefs: Preferences,
) -> tuple[list[Finding], list[Muted]]:
    findings: list[Finding] = []
    muted: list[Muted] = []
    for rule in rules:
        reason = _mute_reason(rule, prefs)
        if reason:
            muted.append(Muted(rule, reason))
            continue
        when = _with_overrides(rule.when, prefs.thresholds.get(rule.id, {}))
        for entity in entities.get(rule.applies_to, []):
            if entity.get("scripted"):  # a script's runs, not how you work
                continue
            notes: list[str] = []
            if _matches(when, entity, history.get(rule.applies_to, {}), notes):
                findings.append(Finding(rule, entity, _format(rule.message, entity), notes))
    findings.sort(key=lambda f: _rank(f, prefs))
    return findings, muted


def cause(finding: Finding) -> str:
    """What this finding has in common with others of its rule, by the rule's `group_by`."""
    return str(finding.entity.get(finding.rule.group_by) or "") if finding.rule.group_by else ""


def group_findings(findings: list[Finding]) -> dict[str, list[FindingGroup]]:
    """Findings by rule, in the order they're ranked, each rule's grouped by cause: the
    group with the largest total first (the most findings without one), biggest finding first."""
    by_rule: dict[str, dict[str, list[Finding]]] = defaultdict(lambda: defaultdict(list))
    for f in findings:
        by_rule[f.rule.id][cause(f)].append(f)
    out: dict[str, list[FindingGroup]] = {}
    for rule_id, causes in by_rule.items():
        rule = next(iter(causes.values()))[0].rule
        weight = _first_field(rule.total)
        ranked = []
        for name, group in causes.items():
            if weight:
                group = sorted(group, key=lambda f: -_number(f.entity.get(weight)))
            sums = _sums([f.entity for f in group])
            total = _format(rule.total, sums) if rule.total else ""
            ranked.append((sums.get(weight, 0.0), FindingGroup(rule, name, group, total)))
        ranked.sort(key=lambda pair: (-pair[0], -len(pair[1].findings)))
        out[rule_id] = [g for _, g in ranked]
    return out


def _first_field(template: str) -> str:
    """The first metric a template names: what a group's findings are ranked by."""
    try:
        return next((name for _, name, _, _ in string.Formatter().parse(template) if name), "")
    except ValueError:
        return ""


def _sums(rows: list[dict]) -> dict[str, float]:
    sums: dict[str, float] = defaultdict(float)
    for row in rows:
        for key, value in row.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                sums[key] += value
    return dict(sums)


def _number(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def stale_rules(rules: list, today: date | None = None) -> list[tuple]:
    """Rules, or context entries, nobody has re-checked within their pack's max age. Practices
    move fast."""
    today = today or date.today()
    out = []
    for rule in rules:
        if rule.reviewed is None:
            out.append((rule, None))
        elif today - rule.reviewed > timedelta(days=rule.max_age_days):
            out.append((rule, (today - rule.reviewed).days))
    return out


def unknown_metrics(rules: list[Rule], entities: dict[str, list[dict]]) -> list[tuple[Rule, list[str]]]:
    """Rules naming a metric their entity kind doesn't have, usually a typo: they can never fire."""
    known = {kind: set(rows[0]) for kind, rows in entities.items() if rows}
    out = []
    for rule in rules:
        if rule.applies_to not in known:  # no data of that kind to check against
            continue
        missing = _metrics_in(rule.when) - known[rule.applies_to]
        if missing:
            out.append((rule, sorted(missing)))
    return out


def _metrics_in(cond: dict) -> set[str]:
    if "all" in cond or "any" in cond:
        return set().union(*(_metrics_in(c) for c in cond.get("all") or cond.get("any")))
    return {cond["metric"]}


def _mute_reason(rule: Rule, prefs: Preferences) -> str | None:
    if rule.id in prefs.mute:
        return "you muted it"
    if rule.unless_preference and all(prefs.style.get(k) == v for k, v in rule.unless_preference.items()):
        stated = ", ".join(f"{k}: {v}" for k, v in rule.unless_preference.items())
        return f"you said you prefer {stated}"
    return None


def _with_overrides(cond: dict, overrides: dict) -> dict:
    if not overrides:
        return cond
    if "all" in cond or "any" in cond:
        key = "all" if "all" in cond else "any"
        return {key: [_with_overrides(c, overrides) for c in cond[key]]}
    if cond["metric"] in overrides:
        return {**cond, "value": overrides[cond["metric"]]}
    return cond


def _matches(cond: dict, entity: dict, history: dict[str, list[float]], notes: list[str]) -> bool:
    if "all" in cond:
        return all(_matches(c, entity, history, notes) for c in cond["all"])
    if "any" in cond:
        return any(_matches(c, entity, history, notes) for c in cond["any"])
    value = entity.get(cond["metric"])
    if value is None:
        return False
    target = cond["value"]
    if isinstance(target, str) and target[:1] == "p" and target[1:].isdigit():
        values = history.get(cond["metric"], [])
        if len(values) < MIN_HISTORY:
            return False
        resolved = percentile(values, float(target[1:]))
        notes.append(f"{cond['metric']} {cond['op']} your own {target} ({resolved:.1f})")
        target = resolved
    return OPS[cond["op"]](value, target)


def _rank(finding: Finding, prefs: Preferences) -> tuple:
    goal_hits = len(set(finding.rule.goals) & set(prefs.goals))
    return (-goal_hits, SEVERITY_ORDER.get(finding.rule.severity, 9), finding.rule.id)


def _format(template: str, entity: dict) -> str:
    try:
        return template.format_map(entity)
    except (KeyError, ValueError, TypeError, IndexError):
        return template


def _as_date(value) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value))
