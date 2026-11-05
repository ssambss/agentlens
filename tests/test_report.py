"""The report: scripted runs left out, findings grouped by cause, the past week against the one before."""

from datetime import datetime, timedelta, timezone

from conftest import plain
from rich.console import Group

from agentlens import profile as profile_mod, trend as trend_mod
from agentlens.metrics import all_entities, baselines, scripted_runs
from agentlens.model import MODEL, AgentRun, Session, Span
from agentlens.preferences import Preferences
from agentlens.report import findings_parts, report_parts
from agentlens.rules import builtin_pack_dir, evaluate, group_findings, load_rules

T = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)


def run(run_id: str, start: datetime, minutes: float = 1.0, agent_type: str = "main") -> AgentRun:
    end = start + timedelta(minutes=minutes)
    return AgentRun(id=run_id, agent_type=agent_type, description=run_id, background=False, start=start, end=end,
                    spans=[Span(MODEL, start, end)])


def session(sid: str, start: datetime, prompts: int = 1, cwd: str = "C:\\work\\app",
            subagents: list[AgentRun] | None = None) -> Session:
    return Session(id=sid, project=cwd.rsplit("\\", 1)[-1], title=sid, entrypoint="cli", version="2.1.300",
                   main=run(sid, start), subagents=subagents or [], human_prompts=[start] * prompts, cwd=cwd)


def burst(prefix: str, n: int, cwd=lambda i: "C:\\work\\batch", every: float = 3.0) -> list[Session]:
    return [session(f"{prefix}{i}", T + timedelta(minutes=every * i), cwd=cwd(i)) for i in range(n)]


def evaluated(sessions, rules=None):
    entities = all_entities(sessions)
    rules = rules or load_rules([builtin_pack_dir()])
    findings, _ = evaluate(rules, entities, baselines(entities), Preferences())
    return entities, findings


def test_a_burst_of_one_prompt_sessions_is_a_script():
    sessions = burst("s", 10)
    assert scripted_runs(sessions) == {s.id for s in sessions}
    assert scripted_runs(sessions[:9]) == set()  # nine aren't enough
    spread = burst("s", 10, every=11)  # each over ten minutes after the one before
    assert scripted_runs(spread) == set()


def test_a_session_you_talk_to_is_never_a_script():
    sessions = burst("s", 10)
    sessions[4] = session("talked", sessions[4].start, prompts=2, cwd="C:\\work\\batch")
    assert "talked" not in scripted_runs(sessions)


def test_runs_in_folders_of_their_own_burst_in_the_parent():
    runs = burst("r", 12, cwd=lambda i: f"C:\\tmp\\ab\\runs\\case-{i % 6}")
    assert scripted_runs(runs) == {s.id for s in runs}


def test_your_own_question_in_a_project_beside_a_batch_is_kept():
    # The batch's folder and your repo share a parent; your repo has history of its own, so the
    # parent's burst doesn't take your question in.
    batch = burst("b", 10, cwd=lambda i: "C:\\Users\\me\\Desktop\\AcmeStack")
    yours = [session(f"y{i}", T - timedelta(days=i + 1), prompts=3, cwd="C:\\Users\\me\\Desktop\\agentlens")
             for i in range(10)]
    question = session("q", T + timedelta(minutes=10), cwd="c:\\Users\\me\\Desktop\\agentlens")  # VS Code's c:
    assert scripted_runs([*batch, *yours, question]) == {s.id for s in batch}


def test_scripted_runs_stay_out_of_findings_baselines_and_the_profile():
    long_ones = [run(f"sub{i}", T, minutes=30, agent_type="worker") for i in range(5)]
    sessions = [*burst("s", 9), session("s9", T + timedelta(minutes=27), cwd="C:\\work\\batch", subagents=long_ones)]
    mine = session("mine", T - timedelta(days=1), prompts=4, cwd="C:\\work\\app")
    entities, findings = evaluated([*sessions, mine])
    assert {r["id"] for r in entities["session"] if r["scripted"]} == {s.id for s in sessions}
    assert all(r["scripted"] for r in entities["subagent"])
    assert all(not f.entity.get("scripted") for f in findings)
    assert baselines(entities)["session"]["human_prompts"] == [4.0]
    prof = profile_mod.build([*sessions, mine], entities, Preferences())
    assert prof.sessions == 1 and prof.subagents == 0 and prof.scripted == {"batch": 10}


def test_a_subagent_is_long_next_to_others_of_its_type():
    usual = [run(f"rev{i}", T, minutes=4, agent_type="reviewer") for i in range(5)]
    slow = run("slow", T, minutes=20, agent_type="reviewer")
    planners = [run(f"plan{i}", T, minutes=40, agent_type="planner") for i in range(4)]  # too few to compare
    sessions = [session("a", T, prompts=3, subagents=[*usual, slow, *planners])]
    _, findings = evaluated(sessions)
    long = [f for f in findings if f.rule.id == "subagent-unusually-long"]
    assert [f.entity["id"] for f in long] == ["slow"]
    assert "5× the 4 min your reviewer runs usually take" in long[0].message


def test_findings_group_by_cause_with_the_biggest_first():
    runs = [run(f"impl{i}", T, minutes=m, agent_type="kit:implementer") for i, m in enumerate((13, 19, 15))]
    runs.append(run("lone", T, minutes=12, agent_type="general-purpose"))
    rules = load_rules([builtin_pack_dir()])
    long_rule = next(r for r in rules if r.id == "subagent-unusually-long")
    assert long_rule.group_by == "agent_type"  # a subagent rule's, unless it says
    entities = {"subagent": [
        {"kind": "subagent", "id": r.id, "description": r.id, "agent_type": r.agent_type, "tool_calls": 50,
         "active_min": r.minutes(MODEL), "active_vs_type_median": 5.0, "type_median_min": 2.0, "start": T}
        for r in runs]}
    findings, _ = evaluate([long_rule], entities, {}, Preferences())
    [groups] = group_findings(findings).values()
    assert [(g.cause, len(g.findings)) for g in groups] == [("kit:implementer", 3), ("general-purpose", 1)]
    assert [f.entity["id"] for f in groups[0].findings] == ["impl1", "impl2", "impl0"]  # by active_min
    assert groups[0].total == "47 min working"
    text = plain(Group(*findings_parts(findings, [])))
    assert "kit:implementer: 3 runs, 47 min working" in text
    assert 'biggest: "impl1" worked 19 active min' in text
    assert '• "lone" worked 12 active min' in text  # one case is just that
    assert "open-ended brief" not in text  # why it matters is the Rules screen's


def test_days_make_one_group():
    rules = load_rules([builtin_pack_dir()])
    rule = next(r for r in rules if r.id == "many-waiting-at-once")
    days = {"day": [{"kind": "day", "id": d, "date": d, "peak_waiting_on_you": 3, "minutes_2plus_waiting": m}
                    for d, m in (("2026-10-01", 40), ("2026-10-02", 90))]}
    findings, _ = evaluate([rule], days, {}, Preferences())
    [[group]] = group_findings(findings).values()
    assert group.cause == "" and group.total == "130 min with 2 or more waiting"
    assert "2026-10-02" in group.findings[0].message


def test_the_past_week_against_the_one_before():
    now = T + timedelta(days=14)
    recent = session("recent", now - timedelta(days=2), prompts=3)
    before = [session(f"before{i}", now - timedelta(days=9, minutes=i), prompts=3) for i in range(2)]
    sessions = [recent, *before]
    entities, findings = evaluated(sessions)
    trend = trend_mod.build(sessions, entities, findings, now=now)
    assert (trend.recent.sessions, trend.before.sessions) == (1, 2)
    assert trend.recent.time_split["working"] == 1.0
    assert trend_mod.build([recent], entities, findings, now=now) is None  # nothing to compare with
    prof = profile_mod.build(sessions, entities, Preferences())
    text = plain(Group(*report_parts(sessions, prof, findings, [], [], [], hints=False, trend=trend)))
    assert "The past 7 days against the 7 before" in text


def test_the_header_says_what_it_left_out():
    sessions = [*burst("s", 10), session("mine", T - timedelta(days=1), prompts=4)]
    entities, findings = evaluated(sessions)
    prof = profile_mod.build(sessions, entities, Preferences())
    text = plain(Group(*report_parts(sessions, prof, findings, [], [], [], hints=False)))
    assert "1 session · 0 subagents · 1 project" in text
    assert "Left out: 10 scripted runs (batch 10), one prompt each, started in bursts" in text
