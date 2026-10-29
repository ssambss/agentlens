import json
import os
import subprocess
from datetime import timedelta

import pytest
from conftest import T0, common, edit_result, human, text, tool_result, tool_use, write_jsonl

from panopsis.adapters.claude_transcripts import load_session
from panopsis.live import (
    APPROVAL,
    BACKGROUND,
    QUESTION,
    WAITING,
    WORKING,
    EarlyListing,
    TranscriptTails,
    derive,
    open_sessions,
    pane_place,
    pane_title,
    render,
    sort_sessions,
)


def entry(status, session_id="sess-1"):
    return {"sessionId": session_id, "status": status, "name": "demo-1", "kind": "interactive",
            "cwd": "D:\\work\\demo", "startedAt": int(T0.timestamp() * 1000)}


def session_from(projects, records, session_id="sess-1"):
    return load_session(write_jsonl(projects / "demo" / f"{session_id}.jsonl", common(records, session_id)))


def minutes(m):
    return T0 + timedelta(minutes=m)


def test_idle_after_turn_is_waiting_on_you(projects):
    s = session_from(projects, [human(0), text(1), tool_use(2, "Bash", "t1"), tool_result(3, "t1"), text(4)])
    live = derive(entry("idle"), s, now=minutes(30))
    assert live.state == WAITING and live.since == minutes(4)
    assert live.project == "demo"


def test_panes_say_what_a_session_is_about_and_where_it_works(projects):
    tree = "D:\\work\\demo\\.claude\\worktrees\\parser"
    s = session_from(projects, [human(0, "can we make the parser faster?"),
                                {"type": "ai-title", "aiTitle": "Demo parser speedup"}, {**text(1), "cwd": tree}])
    live = derive(entry("idle"), s, now=minutes(5))
    assert live.worktree == "parser"
    assert pane_title(live, "demo") == "Parser speedup"  # the space is on the border already
    assert pane_title(live, "Other work") == "Demo parser speedup"
    assert pane_place(live, "demo") == "parser · demo"
    events: dict = {}
    path = write_jsonl(projects / "demo" / "sess-2.jsonl", common([human(0, "fix the build"), text(1)], "sess-2"))
    untitled = derive(entry("idle", "sess-2"), load_session(path, events=events), events, now=minutes(5))
    assert pane_title(untitled, "demo") == "fix the build" and pane_place(untitled, "demo") == "demo"
    # Not listed in a transcript yet: the worktree comes from the folder Claude Code reports.
    assert derive({**entry("idle", "new"), "cwd": tree}, None, now=minutes(5)).worktree == "parser"


def test_a_leading_project_name_is_dropped_only_when_it_is_the_whole_name(projects):
    live = derive(entry("idle"), session_from(projects, [human(0), text(1)]), now=minutes(5))
    cases = [
        ("Code lens keyboard space selection", "codelens", "Keyboard space selection"),
        ("Recipe box listing sync", "recipebox", "Listing sync"),
        ("Panopsis: usage view", "panopsis", "Usage view"),
        ("Skyline iOS build", "Skyline", "iOS build"),
        ("Skyline", "Skyline", "Skyline"),  # nothing would be left
        ("Agentic dev loop ideas", "agentic-game", "Agentic dev loop ideas"),
        ("Map editor for agentic-game", "agentic-game", "Map editor for agentic-game"),
    ]
    for title, space, shown in cases:
        live.title = title
        assert pane_title(live, space) == shown, title


def test_busy_shows_running_tool_and_turn_start(projects):
    s = session_from(projects, [human(0), text(1), human(10), text(11), tool_use(12, "Bash", "t1")])
    live = derive(entry("busy"), s, now=minutes(14))
    assert live.state == WORKING
    assert live.detail == "running Bash 2m"
    assert live.since == minutes(10)  # the current turn, not the earlier one


def test_question_and_plan_override_status(projects):
    s = session_from(projects, [human(0), tool_use(1, "AskUserQuestion", "q1")])
    assert derive(entry("busy"), s, now=minutes(5)).state == QUESTION
    s = session_from(projects, [human(0), tool_use(1, "ExitPlanMode", "p1")], session_id="sess-2")
    assert derive(entry("idle", "sess-2"), s, now=minutes(5)).state == APPROVAL


def test_stale_pending_tool_from_an_earlier_turn_is_ignored(projects):
    # An interrupted AskUserQuestion two turns ago must not keep the session flagged.
    s = session_from(projects, [human(0), tool_use(1, "AskUserQuestion", "q1"), human(5), text(6)])
    assert derive(entry("idle"), s, now=minutes(10)).state == WAITING


def test_running_subagents_and_finished_ones(projects):
    main = write_jsonl(projects / "demo" / "sess-1.jsonl", common([
        human(0), tool_use(1, "Agent", "a"), tool_result(1.1, "a"), text(2)]))
    subs = projects / "demo" / "sess-1" / "subagents"
    write_jsonl(subs / "agent-run.jsonl", common([tool_use(8, "WebFetch", "w"), tool_result(9, "w")]))
    write_jsonl(subs / "agent-done.jsonl", common([tool_use(8, "SubagentHandback", "h", message="report"),
                                                   tool_result(8.1, "h")]))
    for name, desc in (("run", "Research"), ("done", "Finished one")):
        (subs / f"agent-{name}.meta.json").write_text(json.dumps({"description": desc}), encoding="utf-8")
    live = derive(entry("idle"), load_session(main), now=minutes(10))
    assert live.state == BACKGROUND
    assert [s.description for s in live.subagents] == ["Research"]
    assert [(s.description, s.finished) for s in live.past_subagents] == [("Finished one", True)]
    # Ten minutes of silence later, it no longer counts as running.
    later = derive(entry("idle"), load_session(main), now=minutes(25))
    assert later.state == WAITING and not later.subagents
    assert sorted(s.description for s in later.past_subagents) == ["Finished one", "Research"]


def test_changed_files_include_subagents_newest_first(projects):
    a, b = "D:\\work\\demo\\a.py", "D:\\work\\demo\\b.py"
    main = write_jsonl(projects / "demo" / "sess-1.jsonl", common([
        human(0),
        tool_use(1, "Edit", "e1", file_path=a), edit_result(2, "e1", a, new_start=10),
        tool_use(3, "Agent", "s"), tool_result(9, "s"),
        tool_use(10, "Edit", "e2", file_path=b), edit_result(11, "e2", b, new_start=3),
    ]))
    subs = projects / "demo" / "sess-1" / "subagents"
    write_jsonl(subs / "agent-x.jsonl", common([
        tool_use(4, "Edit", "x1", file_path=a), edit_result(5, "x1", a, new_start=80)]))
    (subs / "agent-x.meta.json").write_text(json.dumps({"description": "Refactor"}), encoding="utf-8")
    live = derive(entry("idle"), load_session(main), now=minutes(12))
    assert [(c.path, c.line, c.edits) for c in live.changes] == [(b, 3, 1), (a, 80, 2)]  # a's latest: the subagent's


def test_subagent_parents_and_foreground_returns(projects):
    # Main launches a foreground agent, which launches one of its own and is still waiting on it.
    main = write_jsonl(projects / "demo" / "sess-1.jsonl", common([
        human(0), tool_use(1, "Agent", "fg"), tool_result(6, "fg"), tool_use(7, "Agent", "fg2")]))
    subs = projects / "demo" / "sess-1" / "subagents"
    write_jsonl(subs / "agent-outer.jsonl", common([tool_use(1.5, "Agent", "inner"), tool_result(5, "inner"),
                                                    text(5.5, "done")]))
    write_jsonl(subs / "agent-inner.jsonl", common([tool_use(2, "Grep", "g"), tool_result(3, "g"), text(4.5)]))
    write_jsonl(subs / "agent-again.jsonl", common([tool_use(7.5, "Read", "r"), tool_result(8, "r")]))
    for name, call in (("outer", "fg"), ("inner", "inner"), ("again", "fg2")):
        (subs / f"agent-{name}.meta.json").write_text(json.dumps({"description": name, "toolUseId": call}),
                                                      encoding="utf-8")
    live = derive(entry("busy"), load_session(main), now=minutes(9))
    by_name = {s.description: s for s in [*live.subagents, *live.past_subagents]}
    assert by_name["outer"].parent == "main" and by_name["inner"].parent == "outer"
    # Foreground calls that returned are finished at once, not "running" for ten more minutes.
    assert by_name["outer"].finished and by_name["inner"].finished
    assert [s.description for s in live.subagents] == ["again"]
    assert by_name["inner"].started == minutes(2) and by_name["inner"].spans


def test_tail_read_subagent_keeps_its_real_start(projects):
    main = write_jsonl(projects / "demo" / "sess-1.jsonl", common([human(0), tool_use(1, "Agent", "a")]))
    subs = projects / "demo" / "sess-1" / "subagents"
    write_jsonl(subs / "agent-long.jsonl", common([text(2 + i, "x" * 200) for i in range(100)]))
    (subs / "agent-long.meta.json").write_text(json.dumps({"description": "Long one"}), encoding="utf-8")
    [run] = load_session(main, tail_bytes=5000).subagents
    assert run.start == minutes(2) and run.spans[0].start > minutes(50)


def test_tail_read_leaves_out_subagents_done_before_it(projects):
    main = write_jsonl(projects / "demo" / "sess-1.jsonl", common(
        [tool_use(0, "Agent", "a"), tool_result(1, "a")] + [text(2 + i, "x" * 200) for i in range(100)]))
    subs = projects / "demo" / "sess-1" / "subagents"
    for name in ("early", "recent"):
        write_jsonl(subs / f"agent-{name}.jsonl", common([tool_use(0.5, "Read", "r"), tool_result(0.6, "r")]))
        (subs / f"agent-{name}.meta.json").write_text(json.dumps({"description": name}), encoding="utf-8")
    finished = minutes(1).timestamp()
    os.utime(subs / "agent-early.jsonl", (finished, finished))  # last written long before the tail begins
    assert [r.description for r in load_session(main, tail_bytes=5000).subagents] == ["recent"]
    # Read whole, the session keeps every subagent.
    assert sorted(r.description for r in load_session(main, tail_bytes=10**6).subagents) == ["early", "recent"]
    assert sorted(r.description for r in load_session(main).subagents) == ["early", "recent"]


def test_a_refresh_rereads_only_the_subagents_that_changed(projects, monkeypatch):
    # A working session's transcript changes on every refresh; its subagents mostly don't.
    from panopsis.adapters import claude_transcripts

    main = write_jsonl(projects / "demo" / "sess-1.jsonl", common([human(0), tool_use(1, "Agent", "a")]))
    subs = projects / "demo" / "sess-1" / "subagents"
    for name in ("one", "two"):
        write_jsonl(subs / f"agent-{name}.jsonl", common([tool_use(1.5, "Read", "r"), tool_result(1.6, "r")]))
        (subs / f"agent-{name}.meta.json").write_text(json.dumps({"description": name}), encoding="utf-8")
    read, real = [], claude_transcripts._read_subagent
    monkeypatch.setattr(claude_transcripts, "_read_subagent", lambda meta, path, *a: read.append(path.stem) or
                        real(meta, path, *a))
    tails = TranscriptTails(projects)

    def subagents():
        session, events = tails.get("sess-1")
        return {r.description: r.last_tool for r in session.subagents}, set(events) - {"main"}

    assert subagents() == ({"one": "Read", "two": "Read"}, {"one", "two"}) and sorted(read) == ["agent-one", "agent-two"]
    with open(main, "a", encoding="utf-8") as f:
        f.write(json.dumps(common([text(2)])[0]) + "\n")
    read.clear()
    assert subagents() == ({"one": "Read", "two": "Read"}, {"one", "two"}) and read == []  # kept, feeds too
    write_jsonl(subs / "agent-two.jsonl", common([tool_use(1.5, "Read", "r"), tool_result(1.6, "r"),
                                                  tool_use(2.5, "Grep", "g")]))
    assert subagents()[0] == {"one": "Read", "two": "Grep"} and read == ["agent-two"]


def test_no_transcript_yet():
    live = derive(entry("idle"), None, now=minutes(3))
    assert live.state == WAITING and live.detail == "new session" and live.project == "demo"
    live = derive(entry("busy"), None, now=minutes(3))
    assert live.state == WORKING and live.detail == "starting"


def test_background_sessions_keep_their_short_job_id():
    # `claude stop` and `claude attach` take this id, not the full session id.
    assert derive({**entry("idle"), "kind": "background", "id": "1715db93"}, None, now=minutes(3)).job_id == "1715db93"


def test_panopsis_own_runs_are_not_sessions(monkeypatch):
    # The advice run, and the usage check that runs in your home folder every few minutes.
    listed = [entry("idle"), {**entry("busy", "advise-run"), "name": "panopsis advise"},
              {**entry("busy", "usage-probe"), "name": "panopsis usage"}]
    monkeypatch.setattr("panopsis.live.shutil.which", lambda name: "claude")
    monkeypatch.setattr("panopsis.live.subprocess.run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=json.dumps(listed), stderr=""))
    assert [e["sessionId"] for e in open_sessions()] == ["sess-1"]


def test_the_first_listing_starts_before_the_app_asks_for_it(monkeypatch):
    # Starting `claude` can take seconds on a work PC, so it runs while the app loads.
    import threading

    runs, release = [], threading.Event()

    def listing():
        runs.append(threading.current_thread())
        release.wait(5)
        return [entry("idle")]

    monkeypatch.setattr("panopsis.live.open_sessions", listing)
    early = EarlyListing()
    release.set()
    assert early() == [entry("idle")]
    assert len(runs) == 1 and runs[0] is not threading.current_thread()
    early()  # later refreshes list again, as open_sessions does
    assert len(runs) == 2 and runs[1] is threading.current_thread()


def test_an_early_listing_that_failed_fails_when_used(monkeypatch):
    def listing():
        raise RuntimeError("`claude` CLI not found on PATH")

    monkeypatch.setattr("panopsis.live.open_sessions", listing)
    with pytest.raises(RuntimeError, match="not found on PATH"):
        EarlyListing()()


def test_sort_and_render(projects):
    s = session_from(projects, [human(0), text(1)])
    waiting_long = derive(entry("idle"), s, now=minutes(60))
    working = derive(entry("busy"), s, now=minutes(60))
    ordered = sort_sessions([working, waiting_long], minutes(60))
    assert [x.state for x in ordered] == [WAITING, WORKING]
    render(ordered, minutes(60))  # renders without error


def test_tail_cache_rereads_only_on_change(projects):
    path = write_jsonl(projects / "demo" / "sess-1.jsonl", common([human(0), text(1)]))
    tails = TranscriptTails(projects)
    first = tails.get("sess-1")
    assert tails.get("sess-1") is first
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(common([human(5)])[0]) + "\n")
    assert len(tails.get("sess-1")[0].human_prompts) == 2
    assert tails.get("missing")[0] is None


def test_tail_read_skips_partial_first_line(projects):
    records = common([human(i) for i in range(200)])
    path = write_jsonl(projects / "demo" / "sess-1.jsonl", records)
    session = load_session(path, tail_bytes=2000)
    assert 0 < len(session.human_prompts) < 200
    assert session.human_prompts[-1] == T0 + timedelta(minutes=199)
