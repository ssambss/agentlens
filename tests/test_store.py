import json
import os
from datetime import datetime, timedelta, timezone

from conftest import T0, common, edit_result, human, text, tool_result, tool_use, write_jsonl

from agentlens.adapters import claude_transcripts
from agentlens.adapters.claude_transcripts import SessionCache, load_session, load_sessions
from agentlens.model import Session
from agentlens.store import SessionStore, from_plain, to_plain


def busy_session(projects, sid="sess-1"):
    """A session with a bit of everything a summary keeps: spans, tools, an edit, a subagent."""
    path = write_jsonl(projects / "demo" / f"{sid}.jsonl", common([
        human(0, "fix the build"), tool_use(1, "Read", "r1", file_path="D:\\work\\demo\\a.py"), tool_result(2, "r1"),
        tool_use(3, "Edit", "e1", file_path="D:\\work\\demo\\a.py"), edit_result(4, "e1", "D:\\work\\demo\\a.py", 12),
        tool_use(5, "Agent", "a1", description="look around"), tool_result(9, "a1"),
        {"type": "user", "timestamp": "2026-10-20T09:10:00Z",
         "message": {"role": "user", "content": "<command-name>/model</command-name>"}},
        tool_use(10, "Skill", "s1", skill="review"), tool_result(10.5, "s1"),
        text(11, "**Done.**"), {"type": "ai-title", "aiTitle": "Fix the build"},
    ], sid))
    subs = projects / "demo" / sid / "subagents"
    write_jsonl(subs / "agent-x1.jsonl", common([tool_use(6, "Grep", "g1"), tool_result(7, "g1"), text(8)], sid))
    (subs / "agent-x1.meta.json").write_text(json.dumps({"description": "look around", "toolUseId": "a1"}),
                                             encoding="utf-8")
    return path


def test_a_summary_reads_back_as_it_was_made(projects):
    session = load_session(busy_session(projects))
    # Something in each kind of part: subagents, a dict of dataclasses, Counters, sets, times.
    assert session.subagents and session.main.files_changed and session.commands and session.skills
    assert session.main.files_read and session.human_prompts
    back = from_plain(Session, json.loads(json.dumps(to_plain(session))))
    assert back == session and back.end == session.end
    # A field this agentlens doesn't know is left out; one the file hasn't got gets its default.
    plain = to_plain(session)
    plain["from_a_later_agentlens"] = 1
    del plain["commands"]
    assert from_plain(Session, plain).commands == {}


def test_a_session_read_once_comes_from_the_store_next_launch(projects, isolated_home, monkeypatch):
    path = busy_session(projects)
    read, load = [], claude_transcripts.load_session
    monkeypatch.setattr(claude_transcripts, "load_session", lambda p, *a, **k: read.append(p.stem) or load(p, *a, **k))
    first = SessionCache(SessionStore()).get(path)
    assert read == ["sess-1"] and list((isolated_home / "history" / "demo").iterdir())[0].name == "sess-1.json.gz"
    assert SessionCache(SessionStore()).get(path) == first and read == ["sess-1"]  # not read again
    # The transcript changed: read again.
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(common([text(12)])[0]) + "\n")
    store = SessionStore()
    SessionCache(store).get(path)
    assert read == ["sess-1", "sess-1"] and store.outdated == 0
    # A different reader (agentlens updated): read again too, while the transcript's there. The
    # app says that's why it takes longer.
    store = SessionStore()
    store.reader = "another"
    SessionCache(store).get(path)
    assert read == ["sess-1", "sess-1", "sess-1"] and store.outdated == 1


def test_reading_history_tells_how_many_transcripts_it_has_been_through(projects, isolated_home):
    for sid in ("a", "b", "c"):
        busy_session(projects, sid)
    heard = []
    sessions = list(load_sessions(projects, cache=SessionCache(SessionStore()),
                                  progress=lambda done, total: heard.append((done, total))))
    assert len(sessions) == 3 and heard == [(1, 3), (2, 3), (3, 3)]
    # Also through the ones outside the window it skips.
    heard.clear()
    assert not list(load_sessions(projects, since=T0 + timedelta(days=1), progress=lambda *n: heard.append(n)))
    assert heard[-1] == (3, 3)


def test_a_session_whose_transcript_is_gone_stays_in_history(projects, isolated_home):
    path = busy_session(projects, "old")
    busy_session(projects, "kept")
    store = SessionStore()
    before = sorted(s.id for s in load_sessions(projects, cache=SessionCache(store)))
    assert before == ["kept", "old"]
    path.unlink()  # Claude Code's cleanup, 30 days on
    after = list(load_sessions(projects, cache=SessionCache(SessionStore())))
    assert sorted(s.id for s in after) == ["kept", "old"]
    assert next(s for s in after if s.id == "old").title == "Fix the build"
    # Its file's time is when it was last active, so a window can skip it unopened.
    assert not list(load_sessions(projects, since=T0 + timedelta(days=1), cache=SessionCache(SessionStore())))
    # Another agentlens reader makes no difference to a session only the store has.
    store = SessionStore()
    store.reader = "another"
    assert [s.id for s in store.gone(projects)] == ["old"]


def test_sessions_whose_transcripts_are_gone_are_forgotten_after_keep_days(projects, isolated_home):
    for sid in ("old", "kept"):
        busy_session(projects, sid)
    store = SessionStore()
    list(load_sessions(projects, cache=SessionCache(store)))
    (projects / "demo" / "old.jsonl").unlink()
    now = T0 + timedelta(days=40)
    assert store.forget(projects, keep_days=0, now=now) == 0  # always keep
    assert store.forget(projects, keep_days=90, now=now) == 0
    assert store.forget(projects, keep_days=30, now=now) == 1  # old: gone from Claude Code, and from here
    assert [s.id for s in store.gone(projects)] == []
    assert (isolated_home / "history" / "demo" / "kept.json.gz").exists()  # its transcript is still there


def test_reading_another_projects_folder_leaves_the_usual_ones_sessions_alone(projects, tmp_path, isolated_home):
    busy_session(projects)
    list(load_sessions(projects, cache=SessionCache(SessionStore())))
    other = tmp_path / "other-projects"  # agentlens report --root <other>
    other.mkdir()
    assert list(load_sessions(other, cache=SessionCache(SessionStore()))) == []
    assert SessionStore().forget(other, keep_days=1, now=T0 + timedelta(days=400)) == 0
    assert (isolated_home / "history" / "demo" / "sess-1.json.gz").exists()


def test_a_damaged_summary_is_read_from_the_transcript_again(projects, isolated_home):
    path = busy_session(projects)
    SessionCache(SessionStore()).get(path)
    (isolated_home / "history" / "demo" / "sess-1.json.gz").write_bytes(b"not gzip")
    assert SessionCache(SessionStore()).get(path).title == "Fix the build"
    path.unlink()
    (isolated_home / "history" / "demo" / "sess-1.json.gz").write_bytes(b"not gzip")
    assert list(SessionStore().gone(projects)) == []


def test_saving_keeps_what_the_session_was_last_active(projects, isolated_home):
    session = SessionCache(SessionStore()).get(busy_session(projects))
    file = isolated_home / "history" / "demo" / "sess-1.json.gz"
    assert datetime.fromtimestamp(os.stat(file).st_mtime, timezone.utc) == session.end
