"""Practices: how a way of working has moved, detected in sessions, stated in advice, kept current."""

from collections import Counter
from datetime import timedelta

import pytest
from conftest import T0

from agentlens import advise, cli, curate, features, work
from agentlens import profile as profile_mod
from agentlens.metrics import all_entities, session_metrics, subagent_metrics
from agentlens.preferences import Preferences
from agentlens.model import AgentRun, Session
from agentlens.rules import Finding, RuleError, builtin_pack_dir, load_context, load_rules, version_tuple


def run(run_id: str, agent_type: str, start: float, end: float, background: bool = False,
        launched_by: str | None = None, calls: tuple[str, ...] = (), models: tuple[str, ...] = ()) -> AgentRun:
    r = AgentRun(run_id, agent_type, f"{agent_type} {run_id}", background,
                 T0 + timedelta(minutes=start), T0 + timedelta(minutes=end))
    r.launched_by = launched_by
    r.agent_calls = {call: True for call in calls}
    r.models = set(models)
    return r


def pipeline_session(version: str = "2.1.301") -> Session:
    """The main thread runs an implementer, which checks its work with three critics: the
    first two one after the other, the last two together. A background helper runs alongside."""
    main = run("main", "main", 0, 60, calls=("t-impl", "t-bg"))
    impl = run("impl", "implementer", 1, 50, launched_by="t-impl", calls=("t-c1", "t-c2", "t-c3"))
    helper = run("bg", "general-purpose", 2, 10, background=True, launched_by="t-bg")
    critics = [run("c1", "critic", 5, 8, launched_by="t-c1", models=("claude-haiku-4-5-20251001",)),
               run("c2", "critic", 20, 25, launched_by="t-c2", models=("claude-haiku-4-5-20251001",)),
               run("c3", "critic", 22, 26, launched_by="t-c3", models=("claude-haiku-4-5-20251001",))]
    return Session("s1", "demo", "Pipeline", "claude-vscode", version, main, subagents=[impl, helper, *critics])


def test_a_threads_own_foreground_subagents_are_counted_in_rounds():
    session = pipeline_session()
    impl = session.subagents[0]
    nested = subagent_metrics(session, impl)
    assert (nested["foreground_children"], nested["foreground_waves"]) == (3, 2)  # c2 and c3 ran together
    assert nested["child_types"] == "critic ×3"
    assert (nested["version"], nested["entrypoint"]) == ("2.1.301", "claude-vscode")
    top = session_metrics(session)
    # The main thread waited on the implementer only: the critics were the implementer's, the helper ran in the background.
    assert (top["foreground_children"], top["foreground_waves"], top["child_types"]) == (1, 1, "implementer ×1")


def pack(tmp_path, practice: str):
    path = tmp_path / "p.yaml"
    path.write_text(f"""
pack: test
rules:
  - id: chain
    applies_to: subagent
    title: Chain
    when: {{all: [{{metric: foreground_waves, op: ">=", value: 3}}]}}
    reviewed: 2026-10-26
{practice}
""", encoding="utf-8")
    return path


def test_a_practice_loads_with_its_quoted_sources(tmp_path):
    rule, = load_rules([pack(tmp_path, """    practice:
      status: changed
      was: one at a time
      now: >
        launch them
        together
      available_from: 2.1.302
      actions:
        - >
          In the subagent's brief, say
          "launch them in one message"
      sources:
        - {url: https://example.com/changelog, date: 2026-10-16, says: "Changed   the default"}""")])
    p = rule.practice
    assert (p.status, p.was, p.now, p.available_from) == ("changed", "one at a time", "launch them together", "2.1.302")
    assert p.sources[0].says == "Changed the default" and str(p.sources[0].date) == "2026-10-16"
    assert p.actions == ['In the subagent\'s brief, say "launch them in one message"']


@pytest.mark.parametrize("practice, error", [
    ("    practice: {status: outdated, sources: [{url: u, says: s}]}", "practice status 'outdated'"),
    ("    practice: {status: changed}", "needs at least one source"),
    ("    practice: {status: changed, sources: [{url: u}]}", "needs a url and the passage"),
    ("    practice: {status: changed, actions: do it, sources: [{url: u, says: s}]}", "a list of things to do"),
])
def test_a_practice_that_moved_needs_a_status_and_a_quoted_source(tmp_path, practice, error):
    with pytest.raises(RuleError, match=error):
        load_rules([pack(tmp_path, practice)])


def test_the_core_practices_rest_on_quoted_sources():
    practices = [r for r in load_rules([builtin_pack_dir()]) if r.practice]
    assert {r.id for r in practices} >= {"foreground-pipeline", "nested-foreground-chain"}
    for rule in practices:
        assert rule.practice.now and rule.practice.sources and rule.practice.actions
        assert all(src.url.startswith("https://") and len(src.says) > 20 for src in rule.practice.sources)
        assert version_tuple(rule.practice.available_from), rule.id


def test_versions_compare_as_numbers():
    assert version_tuple("2.1.302") > version_tuple("2.1.99")
    assert version_tuple("") is None and version_tuple("?") is None and version_tuple("2.1.x") is None


def test_advice_states_the_practice_with_its_sources_and_the_versions_it_ran_on():
    rule = next(r for r in load_rules([builtin_pack_dir()]) if r.id == "nested-foreground-chain")
    hits = [Finding(rule, {"version": v, "entrypoint": "claude-vscode", "delegated_min": m}, f"waited {m} min")
            for v, m in (("2.1.301", 3), ("2.1.301", 40), ("2.1.310", 12), ("2.1.299", 1))]
    lines = advise.practice_lines(hits)
    text = "\n".join(lines)
    assert "### [nested-foreground-chain]" in text and "- status: changed" in text
    assert "2.1.301 (claude-vscode) ×2" in text
    assert '3 of 4 ran before 2.1.302, the first version where "now" is the default' in text
    examples = [line for line in lines if line.startswith("  - waited")]
    assert examples == ["  - waited 40 min", "  - waited 12 min", "  - waited 3 min"]  # longest first
    assert f'"{rule.practice.sources[0].says}"' in text
    assert f"  - {rule.practice.actions[0]}" in lines  # what to change, for advice to fit to their setup


def test_advice_leads_each_item_with_the_change_to_make():
    sessions = [pipeline_session()]
    rule = next(r for r in load_rules([builtin_pack_dir()]) if r.id == "nested-foreground-chain")
    finding = Finding(rule, {"version": "2.1.301", "entrypoint": "claude-vscode", "delegated_min": 9}, "waited")
    prompt = advise.build_prompt(profile_mod.build(sessions, all_entities(sessions), Preferences()), Preferences(),
                                 [finding], [rule], sessions)
    assert "### <the change, in one line, naming the project, file or command it's for>" in prompt
    assert "- Source: a link and its date, never a quote." in prompt
    assert "Then the practices above that apply: one item each, built from its actions" in prompt
    assert "Then 2-4 more items" in prompt and 'under "For the rule pack"' in prompt


def test_advice_without_moved_practices_adds_no_section():
    rule = next(r for r in load_rules([builtin_pack_dir()]) if r.id == "subagent-too-small")
    assert advise.practice_lines([Finding(rule, {}, "small")]) == []


def test_the_newest_version_is_kept_per_surface():
    sessions = [pipeline_session("2.1.301"), pipeline_session("2.1.310"), pipeline_session("garbled")]
    sessions[1].entrypoint = "sdk-py"
    assert advise.newest_versions(sessions) == {"sdk-py": "2.1.310", "claude-vscode": "2.1.301"}
    prompt = advise.build_prompt(profile_mod.build(sessions, all_entities(sessions), Preferences()), Preferences(),
                                 [], [], sessions)
    assert "sdk-py 2.1.310 (last used 2026-10-20), claude-vscode 2.1.301 (last used 2026-10-20)" in prompt
    assert "what's installed isn't known here" in prompt


def test_setup_counts_in_the_folders_sessions_ran_in(tmp_path):
    repo = tmp_path / "QAScripts"
    (repo / ".git").mkdir(parents=True)
    (repo / "server" / ".claude").mkdir(parents=True)
    (repo / "server" / "CLAUDE.md").write_text("# rules", encoding="utf-8")
    (repo / "server" / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    (repo / "server" / "src").mkdir()
    worktree_cwd = str(repo / ".claude" / "worktrees" / "fix" / "server")
    labels = work.subfolder_setup(str(repo), [str(repo / "server" / "src"), worktree_cwd])
    assert labels == ["CLAUDE.md in server", "project Claude Code settings in server"]
    session = pipeline_session()
    session.cwd = str(repo / "server" / "src")
    project, = work.build([session])
    assert "CLAUDE.md in server" in work.describe(project)
    assert work.subfolder_setup(str(repo), [str(tmp_path / "elsewhere")]) == []


def test_enabled_plugins_say_where_they_came_from_and_what_they_provide(tmp_path, no_installed_plugins):
    root = tmp_path / "acme-core"
    (root / "agents").mkdir(parents=True)
    (root / "agents" / "critic.md").write_text(
        "---\nname: critic\ndescription: Reviews a just-written file\n  and returns PASS or REJECT.\n---\n"
        "THE SUBAGENT'S INSTRUCTIONS", encoding="utf-8")
    (root / "skills" / "conventions").mkdir(parents=True)
    (root / "skills" / "conventions" / "SKILL.md").write_text(
        "---\ndescription: " + "word " * 60 + "\n---\nTHE SKILL'S INSTRUCTIONS", encoding="utf-8")
    (root / "commands").mkdir()
    (root / "commands" / "babysit.md").write_text("no frontmatter", encoding="utf-8")
    (root / "hooks").mkdir()
    (root / "hooks" / "hooks.json").write_text(
        '{"hooks": {"PostToolUse": [{"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "secret"}]}],'
        ' "Stop": [{"hooks": []}]}}', encoding="utf-8")
    no_installed_plugins += [{"id": "acme-core@acme-plugins", "enabled": True, "installPath": str(root)},
                             {"id": "off@x", "enabled": False, "installPath": str(root)},
                             {"id": "local", "enabled": True, "installPath": str(tmp_path / "empty")}]
    core, local = work.plugins()
    assert (core.name, core.marketplace, local.marketplace) == ("acme-core", "acme-plugins", "")
    lines = work.describe_plugin(core)
    assert lines[0] == ("- acme-core, from the acme-plugins marketplace: 1 skill, 1 subagent, 1 command; "
                        "hooks on PostToolUse (Write|Edit), Stop")
    assert "  - subagent critic: Reviews a just-written file and returns PASS or REJECT." in lines
    skill = next(line for line in lines if line.startswith("  - skill conventions: "))  # named by its folder
    assert skill.endswith(" ...") and len(skill) < 200  # a long description is cut to a line
    assert "  - command babysit" in lines
    assert work.describe_plugin(local) == ["- local: nothing agentlens can see"]
    sessions = [pipeline_session()]
    prompt = advise.build_prompt(profile_mod.build(sessions, all_entities(sessions), Preferences()), Preferences(),
                                 [], [], sessions)
    assert "## Plugins they have" in prompt and lines[0] in prompt
    assert "secret" not in prompt  # when a hook runs, never what it runs
    assert "INSTRUCTIONS" not in prompt  # what a skill or subagent is for, never what it says


TEAM_PACK = """
pack: acme
context:
  - id: tool-documents-itself
    title: A skill points at the tool's help instead of copying it
    for: anyone writing a skill about a command-line tool
    reviewed: 2026-10-28
    practice:
      status: changed
      was: Usage was copied into docs pages.
      now: The tool's help carries it.
      actions: [Replace copied usage with a pointer to the help.]
      sources:
        - {url: "https://example.com/c/1", date: 2026-09-30, says: A copy would drift as the tool changes.}
"""


def test_team_entries_reach_advice_as_their_pack_states_them(tmp_path):
    (tmp_path / "acme.yaml").write_text(TEAM_PACK, encoding="utf-8")
    entries = load_context([tmp_path / "acme.yaml"])
    sessions = [pipeline_session()]
    prof = profile_mod.build(sessions, all_entities(sessions), Preferences())
    prompt = advise.build_prompt(prof, Preferences(), [], [], sessions, context=entries)
    assert "## What their team has written down" in prompt
    assert "### [tool-documents-itself] A skill points at the tool's help instead of copying it (acme pack)" in prompt
    assert "- for: anyone writing a skill about a command-line tool" in prompt
    assert "- was: Usage was copied into docs pages." in prompt
    assert '  - https://example.com/c/1 (2026-09-30): "A copy would drift as the tool changes."' in prompt
    assert "Then the team entries above whose case their data shows" in prompt and "Then 2-4 more items" in prompt
    assert "the entries their data\n   can't show" in prompt  # an entry that only might apply isn't an item
    plain = advise.build_prompt(prof, Preferences(), [], [], sessions)
    assert "## What their team has written down" not in plain and "team entries" not in plain
    assert "Then 3-5 items" in plain


def test_advice_looks_past_claude_code_and_says_where_each_change_goes():
    sessions = [pipeline_session()]
    prompt = advise.build_prompt(profile_mod.build(sessions, all_entities(sessions), Preferences()), Preferences(),
                                 [], [], sessions)
    assert "Look past Claude Code's own features" in prompt
    assert "- Where: one of their own setup (~/.claude), the repo" in prompt
    assert f"use primary sources from the last {advise.FRESH_MONTHS} months" in prompt
    assert "A\n   vendor's marketing post or a personal blog doesn't carry an item on its own" in prompt
    assert f"At most {advise.MAX_ITEMS} items in all" in prompt  # the word limit alone went unheeded
    # How core grows past Claude Code: proposals a person can check before they go into a pack.
    assert f"at most {advise.MAX_PACK_LINES} lines" in prompt and "quoted word for word" in prompt


def test_subagent_types_are_listed_with_the_model_they_ran_on():
    session = pipeline_session()
    session.main.models = {"claude-opus-5-5"}
    lines = features.describe(features.build([session]))
    line = next(line for line in lines if line.startswith("- subagents by type"))
    assert "critic 3 (claude-haiku-4-5)" in line and "implementer 1 (model not recorded)" in line
    assert "- main conversation's model, by sessions: claude-opus-5-5 1" in lines
    assert features._ran_on(Counter({"claude-haiku-4-5": 2, "claude-opus-5-5": 1})) == "mostly claude-haiku-4-5, 2 of 3"


def test_a_quote_counts_only_when_its_words_are_on_the_page_in_order():
    page = curate.plain("Use the **main conversation** when:\n\n* Multiple phases share `context`, see [docs](https://x)")
    assert curate.quoted_in("Use the main conversation when: [...] Multiple phases share context, see docs", page)
    assert not curate.quoted_in("2.1.302: Use the main conversation when:", page)  # words the page doesn't have
    assert not curate.quoted_in("Multiple phases share context [...] Use the main conversation", page)  # out of order


def test_each_practice_quote_is_checked_against_its_page(tmp_path):
    path = pack(tmp_path, """    practice:
      status: changed
      sources:
        - {url: "https://docs.example/a.md#part", says: "the default changed"}
        - {url: "https://docs.example/a.md", says: "something it never said"}
        - {url: "https://down.example/b.md", says: "anything"}""")
    fetched = []

    def fetch(url):
        fetched.append(url)
        if "down" in url:
            raise OSError("no route")
        return "In 2.1.302 *the default* changed."

    checks = curate.check_quotes(path, fetch)
    assert [c.found for c in checks] == [True, False, None]
    assert fetched == ["https://docs.example/a.md", "https://down.example/b.md"]  # each page once, without its anchor
    prompt = curate.build_prompt(path, quotes=checks)
    assert '- [chain] NOT FOUND: https://docs.example/a.md: "something it never said"' in prompt
    assert "page didn't load: https://down.example/b.md" in prompt
    assert "current, changed, superseded, emerging" in prompt  # the statuses the loader accepts


def test_the_curator_is_given_the_pack_and_the_metrics_it_can_name(tmp_path):
    path = pack(tmp_path, "")
    prompt = curate.build_prompt(path)
    assert path.read_text(encoding="utf-8").strip() in prompt
    assert "The oldest `reviewed` date in the pack is 2026-10-26" in prompt
    subagent_line = next(line for line in prompt.splitlines() if line.startswith("- subagent: "))
    assert "foreground_waves" in subagent_line and "shared_reads" in subagent_line
    assert "Change no file yourself." in prompt


def test_curate_proposes_and_saves_without_touching_the_pack(tmp_path, monkeypatch, capsys):
    path = pack(tmp_path, "")
    before = path.read_text(encoding="utf-8")
    asked = []
    monkeypatch.setattr(curate, "run", lambda prompt, model=None, **kw: asked.append(prompt) or "## Rules\n- chain: holds")
    monkeypatch.setattr(curate, "fetch_page", lambda url: pytest.fail("a pack without practices fetches nothing"))
    assert cli.main(["curate", "--pack", str(path), "--dry-run"]) == 0
    assert not asked and "## The pack (p.yaml)" in capsys.readouterr().out
    assert cli.main(["curate", "--pack", str(path)]) == 0
    saved, = curate.history_dir().glob("*-p.md")
    assert saved.read_text(encoding="utf-8") == "## Rules\n- chain: holds"
    assert path.read_text(encoding="utf-8") == before
    assert cli.main(["curate", "--pack", str(tmp_path / "missing.yaml")]) == 1
