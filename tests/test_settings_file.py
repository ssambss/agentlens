from pathlib import Path

import pytest
import yaml

from panopsis import cli, config as config_mod, preferences, settings_file
from panopsis.config import Config, Space


def this_pc(home: Path) -> Config:
    """Settings of one's own, and things only this PC has: its folders, programs and sessions."""
    cfg = Config(path=home / "config.yaml", theme="plum-night", colors=dict(config_mod.LOOKS["ember"].colors),
                 columns=3, show_times=False, usage_minutes=10.0, history_days=30, history_keep_days=0,
                 default_mode="auto", keys={"jump": "space"}, findings_and_advice=True,
                 last_space="work", last_folder="D:/work/tv-app", cli_path="C:/tools/claude.exe",
                 editor="kate --line {line} {file}", editor_terminal=True,
                 spaces=[Space("TV", paths=["D:/work/tv-app"], titles=["tv"], sessions=["e04cd49e"])],
                 driven_sessions=[{"session_id": "abc-1", "cwd": "D:/work/tv-app", "title": "Fix the remote",
                                   "mode": "auto"}],
                 hidden_sessions=["def-2"])
    config_mod.save(cfg)
    preferences.ensure_file()
    preferences.set_goals(["cost", "quality"])
    preferences.set_style("steering", "close")
    preferences.add_mute("subagent-too-small")
    path = preferences.profile_path()
    path.write_text(path.read_text(encoding="utf-8").replace(
        "thresholds: {}", "thresholds: {long-wait: {minutes: p90}}").replace(
        "rule_packs: []", "rule_packs: [D:/team/rules]"), encoding="utf-8")
    return cfg


def test_settings_go_to_another_pc_and_nothing_of_this_one_does(isolated_home, tmp_path, monkeypatch):
    exported = settings_file.export(this_pc(isolated_home), tmp_path / "panopsis-settings.yaml")
    text = exported.read_text(encoding="utf-8").split(settings_file.HEADER)[1]  # past the comment, which says so
    for local in ("tv-app", "TV", "e04cd49e", "abc-1", "def-2", "Fix the remote", "claude.exe", "team/rules",
                  "last_", "spaces"):
        assert local not in text

    # The other PC has its own sessions, spaces and folders, and keeps them; the editor is yours.
    other = tmp_path / "other-pc"
    monkeypatch.setenv("PANOPSIS_HOME", str(other))
    config_mod.save(Config(path=other / "config.yaml", editor="zed", last_folder="C:/src",
                           spaces=[Space("Mine", paths=["C:/src/mine"])],
                           driven_sessions=[{"session_id": "xyz-9", "cwd": "C:/src", "title": "", "mode": "default"}]))
    assert cli.main(["import", str(exported)]) == 0
    cfg = config_mod.load()
    assert (cfg.theme, cfg.colors, cfg.columns, cfg.show_times, cfg.usage_minutes, cfg.history_days,
            cfg.history_keep_days, cfg.default_mode, cfg.editor, cfg.editor_terminal, cfg.keys,
            cfg.findings_and_advice) == \
        ("plum-night", config_mod.LOOKS["ember"].colors, 3, False, 10.0, 30, 0, "auto", "kate --line {line} {file}",
         True, {"jump": "space"}, True)
    assert (cfg.cli_path, cfg.last_folder, cfg.last_space) == (None, "C:/src", None)
    assert [s.name for s in cfg.spaces] == ["Mine"] and [d["session_id"] for d in cfg.driven_sessions] == ["xyz-9"]
    prefs = preferences.load()
    assert (prefs.goals, prefs.style, prefs.mute, prefs.thresholds, prefs.rule_packs) == \
        (["cost", "quality"], {"steering": "close"}, ["subagent-too-small"], {"long-wait": {"minutes": "p90"}}, [])
    assert "# Rule ids you never want to see" in preferences.profile_path().read_text(encoding="utf-8")  # as written


def test_import_says_what_it_changed(isolated_home, tmp_path, capsys):
    exported = tmp_path / "settings.yaml"
    assert cli.main(["export", str(exported)]) == 0
    assert f"Savedyoursettingsin{exported}." in "".join(capsys.readouterr().out.split())  # wrapped
    assert cli.main(["import", str(exported)]) == 0
    assert "already those in" in capsys.readouterr().out
    assert not preferences.profile_path().exists()  # nothing to change, so no file made

    data = yaml.safe_load(exported.read_text(encoding="utf-8"))
    data["settings"].update(theme="nord", keys={"jump": "space"})
    data["preferences"]["goals"] = ["speed"]
    exported.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert cli.main(["import", str(exported)]) == 0
    assert "Took the theme, keys and goals in" in " ".join(capsys.readouterr().out.split())


def test_export_goes_where_you_say_and_replaces_only_an_export(isolated_home, tmp_path, monkeypatch, capsys):
    usb = tmp_path / "usb"
    usb.mkdir()
    assert cli.main(["export", str(usb)]) == 0  # a folder: the file of the usual name in it
    assert settings_file.read(usb / settings_file.NAME).settings["theme"] == "textual-dark"
    config_mod.save(Config(path=isolated_home / "config.yaml", theme="nord"))
    assert cli.main(["export", str(usb)]) == 0  # the one exported before is replaced
    assert settings_file.read(usb / settings_file.NAME).settings["theme"] == "nord"

    notes = usb / "notes.txt"
    notes.write_text("my notes", encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["export", str(notes)]) == 1
    assert "is already there, and isn't panopsis settings" in " ".join(capsys.readouterr().out.split())
    assert notes.read_text(encoding="utf-8") == "my notes"

    monkeypatch.setenv("PANOPSIS_HOME", str(tmp_path / "other-pc"))
    assert cli.main(["import", str(usb)]) == 0  # on the other PC, the folder is enough
    assert config_mod.load().theme == "nord"


def test_import_takes_only_what_may_come_from_another_pc(isolated_home, tmp_path):
    exported = tmp_path / "settings.yaml"
    # Written by hand, by Notepad: a byte order mark, settings of this PC's, and values that aren't options.
    exported.write_text("\ufeff" + yaml.safe_dump({
        "settings": {"theme": "nord", "cli_path": "C:/claude.exe", "spaces": [{"name": "x", "paths": ["C:/x"]}],
                     "colors": {"you": "orange", "nonsense": "red"}, "columns": -2, "history_days": "lots"},
        "preferences": {"goals": ["speed", "fame"], "style": {"steering": "sideways"}, "rule_packs": ["C:/x"]},
    }), encoding="utf-8")
    taken = settings_file.read(exported)
    assert taken.settings == {"theme": "nord", "colors": {**config_mod.DEFAULT_COLORS, "you": "orange"},
                              "columns": None, "history_days": 7}
    assert cli.main(["import", str(exported)]) == 0
    cfg = config_mod.load()
    assert cfg.theme == "nord" and cfg.cli_path is None and cfg.spaces == []
    prefs = preferences.load()
    assert prefs.goals == ["speed"] and prefs.style == {} and prefs.rule_packs == []


@pytest.mark.parametrize("content, error", [
    (None, "No file at"),
    ("theme: nord\n", "isn't a settings file panopsis exported"),
    ("settings: [unclosed\n", "Couldn't read"),
    ("settings: {refresh_seconds: often}\n", "has a setting panopsis can't read"),
])
def test_import_refuses_a_file_it_cant_take(isolated_home, tmp_path, capsys, content, error):
    path = tmp_path / "settings.yaml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(settings_file.SettingsFileError, match=error):
        settings_file.read(path)
    assert cli.main(["import", str(path)]) == 1
    assert error in " ".join(capsys.readouterr().out.split())
    assert not (isolated_home / "config.yaml").exists()


def test_named():
    assert settings_file.named(["theme"]) == "theme"
    assert settings_file.named(["theme", "history_keep_days", "mute"]) == \
        "theme, how long sessions are kept and muted rules"
