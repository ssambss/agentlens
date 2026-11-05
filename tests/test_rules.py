from datetime import date
from pathlib import Path

import pytest

from agentlens.metrics import baselines
from agentlens.preferences import Preferences
from agentlens.rules import (RuleError, builtin_pack_dir, evaluate, load_context, load_rules, stale_rules,
                             unknown_metrics)


def pack(tmp_path: Path, body: str, name: str = "p.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


RULE = """
pack: test
rules:
  - id: long-sub
    applies_to: subagent
    title: Long subagent
    goals: [cost]
    when: {all: [{metric: active_min, op: ">", value: %s}]}
    message: "{description} took {active_min:.0f} min"
    advice: shorter briefs
    source: https://example.com
    reviewed: 2026-10-01
"""


def subs(*minutes):
    return {"subagent": [{"kind": "subagent", "id": str(i), "description": f"s{i}", "active_min": m}
                         for i, m in enumerate(minutes)]}


def run(rules, entities, prefs=None):
    return evaluate(rules, entities, baselines(entities), prefs or Preferences())


def test_absolute_threshold_and_message(tmp_path):
    rules = load_rules([pack(tmp_path, RULE % 10)])
    findings, _ = run(rules, subs(5, 30))
    assert [f.message for f in findings] == ["s1 took 30 min"]


def test_percentile_threshold_uses_own_history(tmp_path):
    rules = load_rules([pack(tmp_path, RULE % "p80")])
    findings, _ = run(rules, subs(1, 2, 3, 4, 5, 50))
    assert [f.entity["id"] for f in findings] == ["5"]
    assert "your own p80" in findings[0].notes[0]


def test_percentile_threshold_needs_enough_history(tmp_path):
    rules = load_rules([pack(tmp_path, RULE % "p80")])
    findings, _ = run(rules, subs(1, 50))
    assert findings == []


def test_mute_and_stated_style(tmp_path):
    body = RULE % 10 + "    unless_preference: {steering: close}\n"
    rules = load_rules([pack(tmp_path, body)])
    findings, muted = run(rules, subs(30), Preferences(style={"steering": "close"}))
    assert findings == [] and "steering: close" in muted[0].reason
    findings, muted = run(rules, subs(30), Preferences(mute=["long-sub"]))
    assert findings == [] and muted[0].reason == "you muted it"


def test_threshold_override(tmp_path):
    rules = load_rules([pack(tmp_path, RULE % 10)])
    findings, _ = run(rules, subs(30), Preferences(thresholds={"long-sub": {"active_min": 60}}))
    assert findings == []


def test_goals_rank_findings(tmp_path):
    other = RULE.replace("long-sub", "other").replace("[cost]", "[quality]")
    rules = load_rules([pack(tmp_path, RULE % 10), pack(tmp_path, other % 10, "q.yaml")])
    findings, _ = run(rules, subs(30), Preferences(goals=["quality"]))
    assert [f.rule.id for f in findings] == ["other", "long-sub"]


def test_later_pack_overrides_or_disables(tmp_path):
    base = pack(tmp_path, RULE % 10)
    team = tmp_path / "team"
    team.mkdir()
    pack(team, "rules:\n  - {id: long-sub, disabled: true}\n")
    assert load_rules([base, team]) == []


CONTEXT = """
context:
  - id: one-place
    title: Each skill lives in one place
    for: anyone adding
      a skill
    reviewed: 2026-10-01
    practice:
      status: current
      now: One job, one skill.
      actions: [Check the team plugin first.]
      sources:
        - {url: "https://example.com/decisions", date: 2026-09-28, says: Copies would drift apart.}
"""


def test_context_entries_load_beside_rules_and_override_by_id(tmp_path):
    both = pack(tmp_path, RULE % 10 + CONTEXT)
    assert [r.id for r in load_rules([both])] == ["long-sub"]  # each loader reads only its own part
    entry, = load_context([both])
    assert (entry.title, entry.audience, entry.pack) == ("Each skill lives in one place", "anyone adding a skill", "test")
    assert entry.practice.sources[0].says == "Copies would drift apart."
    assert stale_rules([entry], today=date(2027, 1, 30)) == [(entry, 121)]
    team = tmp_path / "team"
    team.mkdir()
    pack(team, CONTEXT.replace("Each skill lives in one place", "One place per skill"))
    entry, = load_context([both, team])
    assert (entry.title, entry.pack) == ("One place per skill", "p")
    pack(team, "context:\n  - {id: one-place, disabled: true}\n", "z.yaml")
    assert load_context([both, team]) == []


def test_a_context_entry_needs_who_its_for_and_a_source(tmp_path):
    with pytest.raises(RuleError, match="one-place.*for"):
        load_context([pack(tmp_path, CONTEXT.replace("    for: anyone adding\n      a skill\n", ""))])
    with pytest.raises(RuleError, match="at least one source"):  # no metric backs it, so a source must
        load_context([pack(tmp_path, CONTEXT.split("      sources:")[0], "q.yaml")])


def test_stale_rules(tmp_path):
    rules = load_rules([pack(tmp_path, RULE % 10)])
    assert stale_rules(rules, today=date(2026, 11, 1)) == []
    [(rule, age)] = stale_rules(rules, today=date(2027, 3, 1))
    assert rule.id == "long-sub" and age == 151


def test_invalid_rule_reports_file_and_id(tmp_path):
    with pytest.raises(RuleError, match="long-sub.*unknown op"):
        load_rules([pack(tmp_path, (RULE % 10).replace('">"', '"~"'))])


def test_core_pack_only_uses_metrics_that_exist(tmp_path):
    """Catches typos in rule packs: every metric a rule names must be produced by metrics.py."""
    from conftest import common, human, text, tool_result, tool_use, write_jsonl

    from agentlens.adapters.claude_transcripts import load_session
    from agentlens.metrics import all_entities

    projects = tmp_path / "projects"
    path = write_jsonl(projects / "demo" / "s.jsonl", common([human(0), text(1)]))
    sub = write_jsonl(projects / "demo" / "s" / "subagents" / "agent-a.jsonl", common([
        tool_use(1, "Bash", "x"), tool_result(2, "x")]))
    (sub.parent / "agent-a.meta.json").write_text("{}", encoding="utf-8")
    entities = all_entities([load_session(path)])

    def metrics_in(cond):
        if "all" in cond or "any" in cond:
            return set().union(*(metrics_in(c) for c in cond.get("all") or cond.get("any")))
        return {cond["metric"]}

    for rule in load_rules([builtin_pack_dir()]):
        entity = entities[rule.applies_to][0]
        missing = metrics_in(rule.when) - set(entity)
        assert not missing, f"{rule.id} uses unknown metrics {missing}"
        rule.message.format_map(entity)  # raises on a misspelled placeholder or bad format spec
        rule.total.format_map(entity)
        assert not rule.group_by or rule.group_by in entity, f"{rule.id} groups by unknown metric {rule.group_by}"


def test_rule_naming_an_unknown_metric_is_reported(tmp_path):
    rules = load_rules([pack(tmp_path, (RULE % 10).replace("metric: active_min", "metric: activ_min"))])
    assert [(r.id, metrics) for r, metrics in unknown_metrics(rules, subs(30))] == [("long-sub", ["activ_min"])]
    assert unknown_metrics(rules, {}) == []  # nothing to check against without data of that kind
