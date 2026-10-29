from collections import Counter

from conftest import common, human, text, tool_result, tool_use, write_jsonl

from panopsis import advise, work
from panopsis.adapters.claude_transcripts import load_session
from panopsis.preferences import Preferences
from panopsis.profile import Profile


def session_in(projects, repo, session_id, title, files, minutes=5):
    records = [human(0), {"type": "ai-title", "aiTitle": title}]  # written after the first message
    for i, path in enumerate(files):
        records += [tool_use(1 + i, "Read", f"r{i}", file_path=str(repo / path)), tool_result(1.5 + i, f"r{i}")]
    records.append(text(minutes))
    return load_session(write_jsonl(projects / repo.name / f"{session_id}.jsonl",
                                    common(records, session_id, cwd=str(repo))))


def test_projects_say_what_the_work_is(projects, tmp_path):
    engine = tmp_path / "engine"
    (engine / ".git").mkdir(parents=True)
    (engine / "CMakeLists.txt").write_text("project(engine)")
    (engine / "CLAUDE.md").write_text("# engine")
    shop = tmp_path / "shop"
    (shop / ".git").mkdir(parents=True)
    (shop / "package.json").write_text("{}")
    sessions = [
        session_in(projects, engine, "e1", "Fix the renderer", ["src/render.cpp", "src/render.h", "CMakeLists.txt"], 9),
        session_in(projects, engine, "e2", "Profile the job system", ["src/jobs.cpp"], 12),
        session_in(projects, shop, "s1", "Checkout page", ["web/Checkout.tsx"], 4),
    ]
    engine_work, shop_work = work.build(sessions)  # most agent time first
    assert engine_work.name == "engine" and engine_work.sessions == 2
    assert engine_work.languages == {"C++": 2, "C/C++ headers": 1, "CMake": 1}
    assert engine_work.setup == ["CMake", "CLAUDE.md"]
    assert engine_work.titles == ["Profile the job system", "Fix the renderer"]  # most recently active first
    assert shop_work.setup == ["npm"] and shop_work.languages == {"TypeScript/React": 1}
    line = work.describe(engine_work)
    assert "files read: C++ 2" in line and "repo: CMake, CLAUDE.md" in line and '"Fix the renderer"' in line

    profile = Profile(sessions=3, projects=2, styles=[], time_split={}, top_tools=[], skills=Counter(), surfaces=Counter(),
                      median_session_min=30, compaction_rate=0)
    prompt = advise.build_prompt(profile, Preferences(), [], [], sessions)
    assert "History on this machine: 3 sessions over 1 days." in prompt
    assert "## What they work on" in prompt and "- engine:" in prompt
    assert "- shop:" not in prompt and "...and 1 smaller projects" in prompt  # 4 minutes: only counted
    assert "fewer than 20 sessions" in prompt  # thin history is called out, not papered over


def test_claude_code_setup_is_seen_in_the_repo_and_your_user_folder(projects, tmp_path, monkeypatch):
    game = tmp_path / "game"
    (game / ".git").mkdir(parents=True)
    (game / "CLAUDE.md").write_text("# game")
    (game / ".claude" / "agents").mkdir(parents=True)
    (game / ".claude" / "agents" / "level-tester.md").write_text("---\nname: level-tester\n---")
    (game / ".claude" / "CLAUDE.md").write_text("# more")  # a second CLAUDE.md is still one entry
    (game / ".claude" / "settings.local.json").write_text('{"hooks": {"PostToolUse": []}}')
    (game / ".claude" / "settings.json").write_text('{"permissions": {}}')
    me = tmp_path / "me"
    (me / ".claude" / "skills" / "synced" / "deploy").mkdir(parents=True)
    (me / ".claude" / "skills" / "synced" / "deploy" / "SKILL.md").write_text("---\nname: deploy\n---")
    (me / ".claude" / "settings.json").write_text('{"hooks": {}}')  # empty: no hooks
    monkeypatch.setattr(work.Path, "home", classmethod(lambda cls: me))

    session = session_in(projects, game, "g1", "Tune the jump", ["src/Player.cs"], 9)
    [w] = work.build([session])
    assert w.setup == ["CLAUDE.md", "project Claude Code settings", "custom subagents", "hooks"]
    assert work.user_setup() == ["skills"]
    profile = Profile(sessions=1, projects=1, styles=[], time_split={}, top_tools=[], skills=Counter(), surfaces=Counter(),
                      median_session_min=30, compaction_rate=0)
    prompt = advise.build_prompt(profile, Preferences(), [], [], [session])
    assert "- set up in their user folder, for every project: skills" in prompt
    assert "don't suggest what they already have, change what" in prompt


def test_the_home_folder_is_not_a_project_setup(projects, tmp_path, monkeypatch):
    home = tmp_path / "me"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{}")  # your user settings, not a project's
    monkeypatch.setattr(work.Path, "home", classmethod(lambda cls: home))
    [w] = work.build([session_in(projects, home, "h1", "Quick question", [])])
    assert w.home and w.setup == []
    assert "home folder, not a repository" in work.describe(w)
