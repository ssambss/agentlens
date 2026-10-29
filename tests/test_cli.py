from datetime import datetime, timedelta, timezone

import pytest
from conftest import common, human, text, write_jsonl

from panopsis import cli, preferences
from panopsis.report import counted


def test_since_accepts_relative_and_iso():
    assert abs(datetime.now(timezone.utc) - timedelta(days=7) - cli._parse_since("7d")) < timedelta(minutes=1)
    assert cli._parse_since("2026-10-01T00:00:00+00:00") == datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_bad_since_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["report", "--since", "yesterday"])
    assert exit_info.value.code == 2
    assert "argument --since: expected e.g. 7d, 48h or 2026-10-01, got 'yesterday'" in capsys.readouterr().err


def test_findings_and_advice_commands_say_how_to_turn_them_on(isolated_home, capsys):
    for command in (["report"], ["profile"], ["rules"], ["mute", "subagent-too-small"], ["advise"], ["curate"]):
        assert cli.main(command) == 1
        assert "experimental, and off. Turn them on in the app's settings (,), under Experimental" in \
            " ".join(capsys.readouterr().out.split())  # wrapped
    assert not (isolated_home / "profile.yaml").exists()  # nothing ran


@pytest.mark.usefixtures("findings_and_advice")
def test_mute_rejects_unknown_rule_and_suggests_one(isolated_home, capsys):
    assert cli.main(["mute", "subagent-to-small"]) == 1
    assert "Did you mean subagent-too-small?" in capsys.readouterr().out
    assert not (isolated_home / "profile.yaml").exists()
    assert cli.main(["mute", "subagent-too-small"]) == 0
    assert cli.main(["mute", "subagent-too-small"]) == 0
    assert "already muted" in capsys.readouterr().out
    assert preferences.load().mute == ["subagent-too-small"]


def test_ambiguous_session_prefix_lists_the_matches(projects, capsys):
    for session_id in ("abc-1", "abc-2"):
        write_jsonl(projects / "demo" / f"{session_id}.jsonl", common([human(0), text(1)], session_id))
    assert cli.main(["session", "abc", "--root", str(projects)]) == 1
    out = capsys.readouterr().out
    assert "2 session ids start with 'abc'" in out and "abc-1" in out and "abc-2" in out
    assert cli.main(["session", "xyz", "--root", str(projects)]) == 1


def test_counted():
    assert [counted(1, "project"), counted(3, "project"), counted(0, "tool call")] == [
        "1 project", "3 projects", "0 tool calls"]
