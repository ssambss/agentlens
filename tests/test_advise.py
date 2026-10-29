import shutil
import subprocess
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
from conftest import T0, StreamingClaude, claude_result, common, human, text, write_jsonl

from panopsis import advise, cli, features
from panopsis import profile as profile_mod
from panopsis.adapters.claude_transcripts import load_session
from panopsis.metrics import all_entities, day_metrics
from panopsis.model import AgentRun, Session
from panopsis.preferences import Preferences
from panopsis.rules import Finding, Rule

DAY = 24 * 60  # minutes


def bare(session_id: str, **fields) -> Session:
    main = AgentRun("main", "main", "", False, T0, T0 + timedelta(minutes=5))
    return Session(session_id, "demo", "t", "cli", "1", main, **fields)


def session_on(projects, session_id: str, day: int, mode: str = "auto") -> Session:
    prompt = human(day * DAY)
    prompt["permissionMode"] = mode
    path = write_jsonl(projects / "demo" / f"{session_id}.jsonl",
                       common([prompt, text(day * DAY + 5)], session_id))
    return load_session(path)


def rule(rule_id: str, applies_to: str = "session") -> Rule:
    return Rule(id=rule_id, applies_to=applies_to, title=f"Title of {rule_id}", when={}, message="", advice="",
                source="", reviewed=None, pack="test")


def test_features_count_sessions_and_name_what_was_never_used():
    fork = AgentRun("a1", "fork", "check it", True, T0, T0 + timedelta(minutes=2))
    planned = bare("s1", permission_modes=Counter({"plan": 2}), subagents=[fork])
    planned.main.tool_calls.update({"mcp__claude_ai_Slack__send_message": 2, "WebFetch": 1})
    plain = bare("s2", commands=Counter({"/model": 1}))
    usage = features.build([planned, plain])
    assert usage.features["plan mode"] == 1 and usage.features["forked subagents"] == 1
    assert usage.features["subagents"] == 0  # a fork isn't a fresh subagent
    assert "worktrees" in usage.never_used() and "/context" in usage.never_used()
    lines = features.describe(usage)
    assert "- plan mode: 1 of 2 sessions" in lines and "- /context: never" in lines
    assert "- prompts by permission mode: plan 2" in lines
    assert "- MCP servers called: claude_ai_Slack 2" in lines
    assert "- subagents by type, with the model they ran on: fork 1 (model not recorded)" in lines


def test_advice_is_kept_and_the_latest_is_the_last_one_written(isolated_home):
    assert advise.latest() is None
    isolated_home.mkdir(parents=True)
    (isolated_home / "advice.md").write_text("from before the history folder\n", encoding="utf-8")
    assert advise.latest().text == "from before the history folder"
    advise.save("older", at=datetime(2026, 10, 20, 9, 0))
    advise.save("newer", at=datetime(2026, 10, 25, 15, 30))
    last = advise.latest()
    assert last.text == "newer" and last.path.name == "2026-10-25-153000.md"
    assert last.at == datetime(2026, 10, 25, 15, 30).astimezone()
    assert len(list(advise.history_dir().glob("*.md"))) == 2  # both kept


def test_fresh_advice_follows_up_on_the_last(projects):
    sessions = [session_on(projects, "b1", 0), session_on(projects, "b2", 1),
                session_on(projects, "s1", 3, mode="plan"), session_on(projects, "s2", 4), session_on(projects, "s3", 5)]
    previous = advise.Advice(T0 + timedelta(days=2, hours=3), "1. Plan first for the renderer.\n\n2. Fork instead.",
                             advise.history_dir() / "x.md")
    days = day_metrics(sessions)
    findings = [Finding(rule("r"), {"kind": "session", "start": s.start}, "") for s in sessions[:2]]
    findings.append(Finding(rule("d", "day"), days[0], ""))
    entities = all_entities(sessions)
    prof = profile_mod.build(sessions, entities, Preferences())

    prompt = advise.build_prompt(prof, Preferences(goals=["learning"]), findings, [], sessions, previous=previous)
    assert f"## Their last advice, from {previous.at:%Y-%m-%d} (" in prompt
    assert "> 1. Plan first for the renderer.\n>\n> 2. Fork instead." in prompt
    assert "## Since then: 3 sessions, against 2 before" in prompt
    assert "- [r] Title of r: 2 of 2 sessions before (100%), 0 of 3 since (0%)" in prompt
    assert "- [d] Title of d: 1 of 2 days before (50%), 0 of 3 since (0%)" in prompt
    assert "- first tried since: plan mode (1 of 3 sessions)" in prompt
    assert "- steering: " in prompt and "- share of time waiting on you: " in prompt
    assert 'Start with "Since last time"' in prompt and "under 450 words" in prompt
    assert "learning is one of their goals" in prompt

    first = advise.build_prompt(prof, Preferences(), findings, [], sessions)
    assert "Since last time" not in first and "## Their last advice" not in first and "under 350 words" in first


def test_the_day_advice_was_written_counts_neither_before_nor_since(projects):
    sessions = [session_on(projects, "b1", 0), session_on(projects, "s1", 2)]
    previous = advise.Advice(sessions[1].start - timedelta(minutes=1), "tip", advise.history_dir() / "x.md")
    days = day_metrics(sessions)
    findings = [Finding(rule("d", "day"), day, "") for day in days]
    lines = advise.followup(previous, sessions, findings)
    assert "- [d] Title of d: 1 of 1 day before (100%), none since yet" in lines


@pytest.mark.usefixtures("findings_and_advice")
def test_advise_saves_and_the_next_run_follows_up(projects, monkeypatch, capsys):
    for i in range(3):
        session_on(projects, f"s{i}", i)
    prompts = []
    monkeypatch.setattr(advise, "run", lambda prompt, model=None, **kw: prompts.append(prompt) or "1. Try plan mode.")
    assert cli.main(["advise", "--root", str(projects)]) == 0
    assert "the next advice follows up on it" in " ".join(capsys.readouterr().out.split())  # wrapped
    assert advise.latest().text == "1. Try plan mode."
    assert "## Their last advice" not in prompts[0]
    assert cli.main(["advise", "--root", str(projects), "--dry-run"]) == 0
    assert "> 1. Try plan mode." in capsys.readouterr().out
    assert len(prompts) == 1  # a dry run asks nothing


@pytest.mark.usefixtures("findings_and_advice")
def test_a_team_pack_reaches_advice_and_the_rules_list(projects, tmp_path, capsys):
    session_on(projects, "s0", 0)
    team = tmp_path / "acme.yaml"
    team.write_text("""
pack: acme
context:
  - id: reads-free-writes-wait
    title: Reads run without asking; writes wait for the person
    for: anyone giving Claude access to a live service
    reviewed: 2026-10-28
    practice:
      status: current
      now: Reads go through a script that can only read.
      sources:
        - {url: "https://example.com/c/2", says: so reads can be pre-approved}
""", encoding="utf-8")
    assert cli.main(["advise", "--root", str(projects), "--rules", str(team), "--dry-run"]) == 0
    assert "### [reads-free-writes-wait] Reads run without asking; writes wait for the person (acme pack)" \
        in capsys.readouterr().out
    assert cli.main(["rules", "--root", str(projects), "--rules", str(team)]) == 0
    out = " ".join(capsys.readouterr().out.split())  # wrapped
    assert "Written down by teams" in out and "reads-free-writes-wait acme · reviewed 2026-10-28" in out
    assert "for anyone giving Claude access to a live service" in out


def test_followup_without_sessions_since_says_so(projects):
    sessions = [session_on(projects, "b1", 0)]
    previous = advise.Advice(datetime.now(timezone.utc), "tip", advise.history_dir() / "x.md")
    lines = advise.followup(previous, sessions, [])
    assert "## Since then: 0 sessions, against 1 before" in lines
    assert not any(line.startswith("- steering") for line in lines)


def test_advice_streams_in_saying_what_claude_is_on(monkeypatch):
    claude = StreamingClaude([
        {"type": "system", "subtype": "init", "tools": ["WebFetch", "WebSearch"]},
        {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "thinking"}}},
        {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 50},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "WebSearch", "input": {"query": "claude code hooks"}}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "WebFetch", "input": {"url": "https://code.claude.com/docs/en/hooks.md#events"}}]}},
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour",
                                                         "utilization": 0.4, "resetsAt": 1793194800}},
        {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "text"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta"}}},
        "not a message",
        claude_result("1. Try plan mode."),
    ])
    monkeypatch.setattr(subprocess, "Popen", claude)
    monkeypatch.setattr(shutil, "which", lambda name: "claude")
    steps, limits = [], []
    text = advise.run("advice, please", on_step=steps.append, on_rate_limit=limits.extend)
    assert text == "1. Try plan mode."
    assert steps == ["thinking", "searching the web for claude code hooks", "reading code.claude.com/docs/en/hooks",
                     "writing"]
    assert [(w.key, w.used) for w in limits] == [("five_hour", 0.4)]
    run = claude.runs[0]
    assert run["prompt"] == "advice, please"
    # The web and nothing else: the other tools aren't there at all.
    cmd = " ".join(run["cmd"])
    assert "--tools WebFetch,WebSearch" in cmd and "--output-format stream-json" in cmd


def test_advice_that_fails_says_why(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "claude")
    monkeypatch.setattr(subprocess, "Popen", StreamingClaude([claude_result("Credit balance is too low", True)]))
    with pytest.raises(RuntimeError, match="Credit balance is too low"):
        advise.run("advice, please")
    monkeypatch.setattr(subprocess, "Popen", StreamingClaude([], returncode=1, stderr="not logged in"))
    with pytest.raises(RuntimeError, match="not logged in"):
        advise.run("advice, please")
