from datetime import datetime, timedelta, timezone

import pytest
import yaml

from agentlens import preferences
from agentlens.metrics import _minutes_at_or_above, _peak_overlap, percentile

T = datetime(2026, 10, 20, tzinfo=timezone.utc)


def iv(a, b):
    return (T + timedelta(minutes=a), T + timedelta(minutes=b))


def test_overlap():
    intervals = [iv(0, 10), iv(5, 15), iv(8, 9), iv(15, 20)]
    assert _peak_overlap(intervals) == 3
    assert _minutes_at_or_above(intervals, 2) == 5  # minutes 5..10
    assert _peak_overlap([iv(0, 5), iv(5, 10)]) == 1  # touching is not overlapping


def test_percentile():
    assert percentile([1, 2, 3, 4, 5], 50) == 3
    assert percentile([], 90) is None


def test_mute_preserves_file_and_handles_block_lists(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_text("goals: [speed]\nmute:\n  - a\n  - b\nstyle: {steering: close}\n", encoding="utf-8")
    preferences.add_mute("c", path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data == {"goals": ["speed"], "mute": ["a", "b", "c"], "style": {"steering": "close"}}
    prefs = preferences.load(path)
    assert prefs.mute == ["a", "b", "c"] and prefs.style == {"steering": "close"}


@pytest.mark.parametrize("mute_text", [
    "mute:\n- a\n- b\n",  # unindented block list, as yaml.dump writes it
    "mute: [a,\n  b]\n",  # flow list over several lines
])
def test_mute_rewrites_the_whole_old_list(tmp_path, mute_text):
    path = tmp_path / "profile.yaml"
    path.write_text("goals:\n- speed\n" + mute_text + "thresholds: {}\n", encoding="utf-8")
    preferences.add_mute("c", path)
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["mute"] == ["a", "b", "c"]
    preferences.remove_mute("a", path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data == {"goals": ["speed"], "mute": ["b", "c"], "thresholds": {}}


def test_goals_and_style_from_the_settings_screen_keep_the_file_as_written(tmp_path):
    path = tmp_path / "profile.yaml"
    preferences.set_goals(["quality", "speed", "fame"], path)  # creates the file from the template
    preferences.set_style("steering", "close", path)
    preferences.set_style("parallelism", "few", path)
    preferences.set_style("steering", None, path)  # back to judging it from your sessions
    text = path.read_text(encoding="utf-8")
    assert "# What you're optimizing for." in text and "#  delegation: fan-out" in text
    prefs = preferences.load(path)
    assert prefs.goals == ["speed", "quality"] and prefs.style == {"parallelism": "few"} and not prefs.ignored


def test_unknown_preference_values_are_ignored(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_text("goals: [speed, fame]\nstyle: {steering: wild, parallelism: few}\n", encoding="utf-8")
    prefs = preferences.load(path)
    assert prefs.goals == ["speed"] and prefs.style == {"parallelism": "few"}
    assert prefs.ignored == ["goal 'fame' (options: speed, cost, quality, focus, learning)",
                             "steering 'wild' (options: close, balanced, autonomous)"]
