import json

import pytest
from conftest import at, common, edit_result, human, notification, text, tool_result, tool_use, write_jsonl

from panopsis.adapters.claude_transcripts import load_session, load_sessions
from panopsis.model import AWAY, DELEGATED, DORMANT, MODEL, TOOL, WAITING_ON_BACKGROUND, WAITING_ON_YOU


def kinds(run):
    return [(s.kind, round(s.minutes, 2)) for s in run.spans]


def test_spans_are_classified_by_what_ends_them(projects):
    path = write_jsonl(projects / "demo" / "sess-1.jsonl", common([
        human(0),
        text(1),                                     # model 1
        tool_use(1.5, "Bash", "t1", command="ls"),  # model 0.5
        tool_result(3.5, "t1"),                      # tool 2
        text(4),                                     # model 0.5, turn ends
        {"type": "queue-operation", "timestamp": at(13.9), "operation": "enqueue"},
        human(14),                                   # waiting on you 10, not split by queue-operation
        tool_use(15, "AskUserQuestion", "t2"),
        tool_result(19, "t2"),                       # agent asked you: waiting on you 4
        tool_use(20, "Bash", "t3"),
        tool_result(22, "t3", permission_source="user_temporary"),  # permission prompt: waiting on you 2
        tool_use(23, "Agent", "t4", description="look around"),
        tool_result(33, "t4"),                       # blocked on foreground subagent 10
        text(34),
        notification(50),                            # waiting on background 16
        text(51),
        human(51 + 5 * 60),                          # 5h later: away
        text(52 + 5 * 60),
        text(52 + 5 * 60 + 45),                      # 45 min "model" gap: suspended session, away
    ]))
    session = load_session(path)
    assert kinds(session.main) == [
        (MODEL, 1), (MODEL, 0.5), (TOOL, 2), (MODEL, 0.5), (WAITING_ON_YOU, 10),
        (MODEL, 1), (WAITING_ON_YOU, 4), (MODEL, 1), (WAITING_ON_YOU, 2), (MODEL, 1),
        (DELEGATED, 10), (MODEL, 1), (WAITING_ON_BACKGROUND, 16), (MODEL, 1),
        (AWAY, 300), (MODEL, 1), (AWAY, 45),
    ]
    assert len(session.human_prompts) == 3


def test_long_background_waits_and_closed_sessions_count_as_away(projects):
    orphan = notification(400)
    orphan["message"]["content"] = ("<task-notification><task-id>b1</task-id><task-id>__orphan_summary__:shell"
                                    "</task-id><status>stopped</status></task-notification>")
    path = write_jsonl(projects / "demo" / "sess-1.jsonl", common([
        human(0),
        text(1),
        notification(1 + 16 * 60),       # a watcher left running overnight: away
        text(962),
        notification(962 + 3 * 60, kind="peer"),  # another session's message wakes an idle one: away
        text(1143),
        {**orphan, "timestamp": at(1153)},  # resumed after closing it: away, however short
        text(1154),
    ]))
    assert kinds(load_session(path).main) == [
        (MODEL, 1), (AWAY, 960), (MODEL, 1), (AWAY, 180), (MODEL, 1), (AWAY, 10), (MODEL, 1),
    ]


def test_a_record_with_a_unicode_line_separator_in_it_is_kept(projects):
    # Records end at newlines only. A reply can hold U+2028 raw, which JSON allows.
    path = projects / "demo" / "sess-1.jsonl"
    path.parent.mkdir(parents=True)
    records = common([human(0, "first second"), text(1, "one two")])
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    session = load_session(path)
    assert len(session.human_prompts) == 1 and session.main.output_tokens == 10


def test_title_record_without_timestamp_is_kept(projects):
    path = write_jsonl(projects / "demo" / "sess-1.jsonl", common([
        human(0), text(1), {"type": "ai-title", "aiTitle": "Fix the build"},
    ]))
    assert load_session(path).title == "Fix the build"


def test_worktree_is_the_one_the_session_works_in_now(projects):
    tree = "D:\\work\\demo\\.claude\\worktrees\\fix-build"
    path = write_jsonl(projects / "demo" / "sess-1.jsonl", common([
        human(0), {**text(1), "cwd": tree}, {**text(2), "cwd": tree + "\\src"},  # a cd into a subfolder
    ]))
    session = load_session(path)
    assert session.worktree == "fix-build" and session.cwd == "D:\\work\\demo"
    left = write_jsonl(projects / "demo" / "sess-2.jsonl", common([human(0), {**text(1), "cwd": tree}, text(2)], "sess-2"))
    assert load_session(left).worktree == ""  # back in the repository


def test_split_assistant_records_count_usage_once(projects):
    path = write_jsonl(projects / "demo" / "sess-1.jsonl", common([
        human(0),
        text(1, "part one", msg_id="m1", output_tokens=700),
        tool_use(1, "Read", "t1", msg_id="m1", file_path="D:\\work\\demo\\a.py"),
        tool_result(2, "t1"),
    ]))
    session = load_session(path)
    assert session.main.output_tokens == 700
    assert session.main.files_read == {"d:/work/demo/a.py"}


def test_changed_files_record_where_each_change_starts(projects):
    a, b, c = "D:\\work\\demo\\a.py", "D:\\work\\demo\\new.md", "D:\\work\\demo\\c.py"
    batch = tool_result(9, "e5")  # a result record carrying several results has no usable details
    batch["message"]["content"].append({"type": "tool_result", "tool_use_id": "e6", "content": "ok"})
    path = write_jsonl(projects / "demo" / "sess-1.jsonl", common([
        human(0),
        tool_use(1, "Edit", "e1", file_path=a, old_string="old", new_string="new"),
        edit_result(2, "e1", a, new_start=40, context=3),       # the hunk opens with 3 unchanged lines
        tool_use(3, "Write", "e2", file_path=b, content="# hi"),
        edit_result(4, "e2", b, created=True),
        tool_use(5, "Edit", "e3", file_path=c, old_string="x", new_string="y"),
        edit_result(6, "e3", c, is_error=True),                 # failed: nothing changed
        tool_use(7, "Edit", "e4", file_path="d:/work/demo/A.py", old_string="p", new_string="q"),
        edit_result(8, "e4", "d:/work/demo/A.py", new_start=7),  # same file, spelled differently
        tool_use(8.5, "Edit", "e5", file_path=b, old_string="hi", new_string="hello"),
        tool_use(8.6, "MultiEdit", "e6", file_path=c, edits=[]),
        batch,
    ]))
    changes = load_session(path).main.files_changed
    assert set(changes) == {"d:/work/demo/a.py", "d:/work/demo/new.md", "d:/work/demo/c.py"}
    assert (changes["d:/work/demo/a.py"].line, changes["d:/work/demo/a.py"].edits) == (7, 2)
    assert (changes["d:/work/demo/new.md"].line, changes["d:/work/demo/new.md"].edits) == (1, 2)  # kept from the Write
    assert changes["d:/work/demo/c.py"].line is None and changes["d:/work/demo/c.py"].edits == 1


def test_subagent_dormancy_handback_and_shared_reads(projects):
    main = write_jsonl(projects / "demo" / "sess-1.jsonl", common([
        human(0),
        tool_use(1, "Read", "r1", file_path="D:/work/demo/a.py"),
        tool_result(1.5, "r1"),
        tool_use(2, "Agent", "spawn", description="Research"),
        tool_result(2.1, "spawn"),
        text(3),
    ]))
    sub_dir = projects / "demo" / "sess-1" / "subagents"
    write_jsonl(sub_dir / "agent-abc.jsonl", common([
        {"type": "user", "timestamp": at(2.1), "message": {"role": "user", "content": "Research the thing"}},
        tool_use(3, "Read", "s1", file_path="d:\\work\\demo\\a.py"),
        tool_result(4, "s1"),
        tool_use(5, "SubagentHandback", "s2", message="x" * 1200),
        tool_result(5.1, "s2"),
        notification(5.1 + 600, kind="coordinator"),  # parent resumes it 10h later
        text(5.1 + 601),
    ]))
    (sub_dir / "agent-abc.meta.json").write_text(json.dumps({
        "agentType": "general-purpose", "description": "Research", "toolUseId": "spawn",
        "requestShape": "background",
    }), encoding="utf-8")

    session = load_session(main)
    [run] = session.subagents
    assert run.background and run.agent_type == "general-purpose" and run.launched_by == "spawn"
    assert run.minutes(DORMANT) == pytest.approx(600)
    assert run.final_chars == 1200
    assert run.files_read & session.main.files_read == {"d:/work/demo/a.py"}
    # The subagent's opening prompt is not one of your prompts.
    assert len(session.human_prompts) == 1


def test_compactions_come_from_boundary_records(projects):
    boundary = {"type": "system", "subtype": "compact_boundary", "timestamp": at(5),
                "compactMetadata": {"trigger": "auto", "preTokens": 967000}}
    summary = {"type": "user", "timestamp": at(5), "isCompactSummary": True,
               "message": {"role": "user", "content": "summary"}}
    path = write_jsonl(projects / "demo" / "sess-1.jsonl", common([human(0), text(1), boundary, summary, text(6)]))
    session = load_session(path)
    assert session.compactions == 1  # the summary message is not counted twice
    assert session.compaction_pre_tokens == [967000]
    assert len(session.human_prompts) == 1
    assert session.manual_compactions == 0


def test_permission_modes_slash_commands_and_your_own_compactions(projects):
    command = {"type": "user", "timestamp": at(2), "message": {"role": "user", "content":
               "<command-name>/compact</command-name>\n<command-message>compact</command-message>\n"
               "<command-args>keep the plan</command-args>"}}
    boundary = {"type": "system", "subtype": "compact_boundary", "timestamp": at(3),
                "compactMetadata": {"trigger": "manual", "preTokens": 400000}}
    planned, auto = human(0), human(4)
    planned["permissionMode"], auto["permissionMode"] = "plan", "auto"
    path = write_jsonl(projects / "demo" / "sess-1.jsonl", common([planned, text(1), command, boundary, auto, text(5)]))
    session = load_session(path)
    assert session.permission_modes == {"plan": 1, "auto": 1}
    assert session.commands == {"/compact": 1}
    assert session.manual_compactions == 1
    assert len(session.human_prompts) == 2  # the command is not one of your prompts


def test_bad_lines_and_unknown_records_are_skipped(projects):
    path = projects / "demo" / "sess-1.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        "not json\n[1, 2]\n"
        + json.dumps({"type": "brand-new-record-type", "timestamp": at(0)}) + "\n"
        + "\n".join(json.dumps(r) for r in common([human(1), text(2)])) + "\n",
        encoding="utf-8",
    )
    session = load_session(path)
    assert len(session.human_prompts) == 1
    assert list(load_sessions(projects))[0].id == "sess-1"


def test_synthetic_messages_are_not_a_model(projects):
    synthetic = text(2, "No response requested.")
    synthetic["message"]["model"] = "<synthetic>"
    path = write_jsonl(projects / "demo" / "sess-1.jsonl", common([human(0), text(1), synthetic]))
    assert load_session(path).main.models == {"claude-opus-5-5"}
