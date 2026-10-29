import asyncio

from conftest import common, human, plain, text, write_jsonl
from textual.widgets import OptionList, Select, Static

from panopsis import config as config_mod
from panopsis import keys
from panopsis.driver import ENDED, DrivenSession
from panopsis.feed import Palette
from panopsis.tui import PanopsisApp, HistoryScreen, KeyFooter, SessionPane, ZoomScreen
from panopsis.tui_driven import ChoiceScreen, request_card
from panopsis.tui_keys import KeyCaptureScreen, KeysScreen
from panopsis.tui_settings import SettingsScreen


def entry(session_id, started_min=0, cwd="D:\\work\\demo"):
    return {"sessionId": session_id, "status": "idle", "name": session_id, "kind": "interactive", "cwd": cwd,
            "startedAt": 1_700_000_000_000 + started_min * 60_000}


def make_app(projects, tmp_path, ids=("one", "two"), **config):
    for i, session_id in enumerate(ids):
        write_jsonl(projects / "demo" / f"{session_id}.jsonl",
                    common([human(i, "fix the build"), text(i + 1, "Done.")], session_id))
    cfg = config_mod.Config(path=tmp_path / "config.yaml", **config)
    return PanopsisApp(provider=lambda: [entry(s, i) for i, s in enumerate(ids)], root=projects, interval=60,
                        config=cfg)


def run_app(app, steps, size=(160, 44)):
    async def go():
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause(0.2)
            await steps(app, pilot)
    asyncio.run(go())


def footer_keys(app):
    return {binding.description: binding.key for binding in
            (b for _, b, enabled, _ in app.screen.active_bindings.values() if b.show and enabled)}


def test_every_binding_with_an_id_is_one_you_can_change():
    from panopsis import tui, tui_settings

    ids = set()
    for owner in (tui.PanopsisApp, tui.ZoomScreen, tui.AnalysisScreen, tui.ProfileScreen, tui.AdviseScreen,
                  tui.RulesScreen, tui.HistoryScreen, tui.UsageScreen, KeysScreen, tui_settings.SettingsScreen):
        for binding in owner.BINDINGS:
            if binding.id:
                assert binding.id in keys.ACTIONS, binding.id
                assert binding.key == keys.ACTIONS[binding.id].keys  # its keys come from the list
                ids.add(binding.id)
    assert ids == set(keys.ACTIONS)  # and every key the list offers to change is bound somewhere


def test_panopsis_keys_and_the_presets_never_share_a_key_in_one_place():
    assert keys.clashes({}) == []
    for _, preset in keys.KEY_PRESETS.values():
        assert keys.clashes(preset) == []
    # Screens have keys of their own, which may be the wall's: History's m isn't the wall's.
    assert "m" in keys.keys_of("history-older", {}) and "m" in keys.keys_of("rules-mute", {})
    assert keys.preset_of({}) == "panopsis" and keys.preset_of({"jump": "J"}) is None


def test_taking_a_key_another_action_has():
    # Refresh's r goes to typing; typing's i goes to refresh, so neither is left without a key.
    changes, others = keys.assign("compose", "r", {})
    assert changes == {"compose": "r", "refresh": "i"} and [a.id for a in others] == ["refresh"]
    # Close keeps its other key.
    changes, _ = keys.assign("columns-more", "w", {})
    assert changes == {"columns-more": "w", "close": "delete"}
    # Refresh works in History and Rules too, so both of their m keys give it up.
    changes, others = keys.assign("refresh", "m", {})
    assert changes == {"refresh": "m", "history-older": "r", "rules-mute": "r"}
    # History's own keys win there over the wall's: R there is older sessions, and that's fine.
    assert keys.assign("history-older", "R", {}) == ({"history-older": "R"}, [])
    # Not if the one giving it up would then share a key: refresh would get Rules' m, which is
    # History's older sessions too.
    changes, others = keys.assign("rules-mute", "r", {})
    assert changes == {} and [a.id for a in others] == ["refresh"]


def test_keys_are_saved_as_you_changed_them_and_read_as_textual_names_them(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("keys:\n  show-keys: '?'\n  jump: J\n  refresh: r\n  nonsense: x\n  quit: ''\n", encoding="utf-8")
    cfg = config_mod.load(path)
    # ? is panopsis's own, as is r; an id panopsis doesn't have, or no key, is left out.
    assert cfg.keys == {"jump": "J"}
    cfg.keys = keys.changed(cfg.keys, {"space-prev": "<", "jump": "j"})
    config_mod.save(cfg)
    assert config_mod.load(path).keys == {"space-prev": "less_than_sign"}
    assert "keys:" in path.read_text(encoding="utf-8").split("\n\n")[0]  # the header explains them


def test_hints_name_the_keys_you_picked():
    d = DrivenSession("D:/work/demo", cli_path="claude")
    d.state = ENDED
    assert "Press c to open it" in request_card(d, Palette()).plain
    keys.use({"claude-code": "ctrl+o"})
    assert "Press ctrl+o to open it" in request_card(d, Palette()).plain
    assert keys.name("show-keys") == "?" and keys.names("close") == "w  del" and keys.name("zoom") == "enter"


def test_changing_a_key_in_the_keys_list(projects, tmp_path):
    app = make_app(projects, tmp_path)

    async def steps(app, pilot):
        await pilot.press("question_mark")
        await pilot.pause(0.1)
        screen = app.screen
        assert isinstance(screen, KeysScreen)
        listing = screen.query_one("#keys-list", OptionList)
        assert listing.get_option_at_index(listing.highlighted).id == "compose"  # the first you can change
        listing.highlighted = listing.get_option_index("toggle-filter")
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert isinstance(app.screen, KeyCaptureScreen)
        await pilot.press("escape")  # leaves it as it was
        await pilot.pause(0.1)
        assert app.screen is screen and app.config.keys == {}

        await pilot.press("enter", "tab")  # tab moves between parts of a screen: not a key you can have
        await pilot.pause(0.1)
        assert "can't be changed" in str(app.screen.query_one("#capture-note", Static).render())
        await pilot.press("k")
        await pilot.pause(0.1)
        assert app.screen is screen and app.config.keys == {"toggle-filter": "k"}
        assert config_mod.load(app.config.path).keys == {"toggle-filter": "k"}  # saved as you change it
        line = plain(listing.get_option("toggle-filter").prompt)
        assert line.startswith("k ") and "(panopsis's: f)" in line

        # r is refresh's: the first press says so, the second takes it, and refresh gets k.
        listing.highlighted = listing.get_option_index("toggle-filter")
        await pilot.press("enter", "r")
        await pilot.pause(0.1)
        note = str(app.screen.query_one("#capture-note", Static).render())
        assert "r is already the key to refresh now" in note and "it then has k" in note
        await pilot.press("r")
        await pilot.pause(0.1)
        assert app.config.keys == {"toggle-filter": "r", "refresh": "k"}

        await pilot.press("question_mark")  # the key that opened it closes it
        await pilot.pause(0.1)
        assert not isinstance(app.screen, KeysScreen)
        await pilot.press("r")  # now the filter
        await pilot.pause(0.1)
        assert app.only_needs_you
        await pilot.press("f")  # no longer anything
        await pilot.pause(0.1)
        assert app.only_needs_you
        assert "need you (r)" in str(app.query_one("#summary", Static).render())  # the hint names it

        # Backspace puts panopsis's back, unless another action has it now.
        await pilot.press("question_mark")
        await pilot.pause(0.1)
        listing = app.screen.query_one("#keys-list", OptionList)
        listing.highlighted = listing.get_option_index("refresh")
        await pilot.press("enter", "backspace")
        await pilot.pause(0.1)
        assert "panopsis's r is the key to show only sessions" in str(
            app.screen.query_one("#capture-note", Static).render())
        await pilot.press("escape")
        listing.highlighted = listing.get_option_index("toggle-filter")
        await pilot.press("enter", "backspace")
        await pilot.pause(0.1)
        listing.highlighted = listing.get_option_index("refresh")
        await pilot.press("enter", "backspace")
        await pilot.pause(0.1)
        assert app.config.keys == {}

    run_app(app, steps)


def test_the_footer_and_the_close_question_use_your_keys(projects, tmp_path):
    app = make_app(projects, tmp_path, keys={"close": "x", "interrupt": "ctrl+x", "zoom": "z"})

    async def steps(app, pilot):
        assert footer_keys(app)["Close"] == "x" and footer_keys(app)["Zoom"] == "z"
        footer = app.screen.query_one(KeyFooter)
        assert any("Close" in str(key.render()) for key in footer.children)
        await pilot.press("w")  # panopsis's own close key does nothing now
        await pilot.pause(0.1)
        assert not isinstance(app.screen, ChoiceScreen)
        await pilot.press("x")
        await pilot.pause(0.1)
        assert isinstance(app.screen, ChoiceScreen)
        assert " x  close it" in str(app.screen.query("Label").last().render())
        await pilot.press("w")  # not the answer
        await pilot.pause(0.1)
        assert isinstance(app.screen, ChoiceScreen)
        await pilot.press("x")
        await pilot.pause(0.2)
        assert app.config.hidden_sessions == ["one"]  # a VS Code session: closing hides it
        await pilot.press("z")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ZoomScreen)

    run_app(app, steps)


def test_a_preset_in_the_settings(projects, tmp_path):
    app = make_app(projects, tmp_path, ids=("one", "two", "three"))

    async def steps(app, pilot):
        panes = list(app.query(SessionPane))
        await pilot.press("comma")
        await pilot.pause(0.2)
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        select = screen.query_one("#set-keys", Select)
        assert select.value == "panopsis" and "own" not in [value for _, value in select._options]
        select.value = "vim"
        await pilot.pause(0.1)
        assert app.config.keys == keys.KEY_PRESETS["vim"][1]
        assert config_mod.load(app.config.path).keys == keys.KEY_PRESETS["vim"][1]

        # Change one by hand from there: the preset is then yours.
        await pilot.click("#set-keys-list")
        await pilot.pause(0.1)
        listing = app.screen.query_one("#keys-list", OptionList)
        listing.highlighted = listing.get_option_index("history")
        await pilot.press("enter", "Y")
        await pilot.pause(0.1)
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert app.screen is screen and select.value == "own"
        assert app.config.keys == {**keys.KEY_PRESETS["vim"][1], "history": "Y"}

        await pilot.press("comma")  # the key that opened the settings closes them
        await pilot.pause(0.1)
        assert not isinstance(app.screen, SettingsScreen)
        assert app.focused is panes[0]
        await pilot.press("l")
        await pilot.pause(0.1)
        assert app.focused is panes[1]
        await pilot.press("j")  # Vim's down: two columns for three panes
        await pilot.pause(0.1)
        assert app.focused is panes[2]
        await pilot.press("Y")
        await pilot.pause(0.2)
        assert isinstance(app.screen, HistoryScreen)

    run_app(app, steps)


def test_keys_that_clash_in_the_file_are_pointed_out(projects, tmp_path):
    app = make_app(projects, tmp_path, keys={"jump": "r"})
    warned = []
    app.notify = lambda message, **kwargs: warned.append(message)

    async def steps(app, pilot):
        assert any("r for refresh and jump" in message for message in warned)

    run_app(app, steps)
