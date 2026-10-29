"""The usage meter: reading Claude Code's /usage and its rate-limit events, and drawing them."""

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from claude_agent_sdk import RateLimitInfo

from panopsis import usage
from panopsis.usage import Usage, Window, run_probe  # the real one; tests get a stand-in by default

# What Claude Code 2.1.310's /usage prints on a subscription (trimmed).
PRINTED = """You are currently using your subscription to power your Claude Code usage

Current session: 49% used · resets Oct 25, 5:10pm (Europe/Helsinki)
Current week (Fable): 5% used · resets Oct 26, 8am (Europe/Helsinki)

What's contributing to your limits usage?
Approximate · sessions on this machine only · shares overlap

Last 24h · 1811 requests · 26 sessions
  84%  Start fresh when you switch tasks · run /clear
"""

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


def local(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute).astimezone().astimezone(timezone.utc)


def test_reads_the_windows_and_keeps_the_rest_as_text():
    windows, notes = usage.parse_usage_text(PRINTED, NOW)
    session, week = windows
    assert (session.key, session.title, session.used) == ("five_hour", "Session", 0.49)
    assert session.resets_at == local(2026, 10, 25, 17, 10)
    assert (week.key, week.title, week.used) == ("seven_day_fable", "Week (Fable)", 0.05)
    assert week.resets_at == local(2026, 10, 26, 8)
    assert notes[0].startswith("You are currently using your subscription")
    assert "What's contributing to your limits usage?" in notes
    assert notes[-1].strip().startswith("84%")


def test_reset_times_claude_code_prints():
    assert usage.parse_reset("Oct 26, 7:59am (Europe/Helsinki)", NOW) == local(2026, 10, 26, 7, 59)
    assert usage.parse_reset("3pm", NOW) == local(2026, 10, 25, 15)
    assert usage.parse_reset("Jan 2, 9am", datetime(2026, 12, 30, tzinfo=timezone.utc)) == local(2027, 1, 2, 9)
    assert usage.parse_reset("whenever", NOW) is None


def test_other_wording_is_kept_not_misread():
    windows, notes = usage.parse_usage_text("You are using an API key.\nSession cost: $0.12", NOW)
    assert windows == [] and notes == ["You are using an API key.", "Session cost: $0.12"]
    windows, _ = usage.parse_usage_text("Current week (all models): 100% used", NOW)
    assert windows[0].key == "seven_day" and windows[0].status == "rejected" and windows[0].resets_at is None


def test_rate_limit_events_update_a_window_and_keep_its_name():
    info = RateLimitInfo("allowed", resets_at=1792941000, rate_limit_type="five_hour", raw={
        "unifiedWindows": {"five_hour": {"utilization": 0.57, "resetsAt": 1792941000, "status": "allowed"}}})
    [window] = usage.from_rate_limit(info)
    assert (window.key, window.used) == ("five_hour", 0.57)
    assert window.resets_at == datetime.fromtimestamp(1792941000, tz=timezone.utc)
    # Older CLIs: the top-level fields only.
    [old] = usage.from_rate_limit(RateLimitInfo("allowed_warning", rate_limit_type="seven_day", utilization=0.8))
    assert (old.key, old.status) == ("seven_day", "allowed_warning")
    assert usage.from_rate_limit(RateLimitInfo("allowed")) == []

    known = Usage()
    known.merge(usage.parse_usage_text(PRINTED, NOW)[0])
    assert known.merge([Window("seven_day_fable", 0.06)])
    assert known.windows["seven_day_fable"].title == "Week (Fable)"
    assert not known.merge([Window("seven_day_fable", 0.06)])  # nothing new
    assert [w.key for w in known.ordered()] == ["five_hour", "seven_day_fable"]


def test_meter_line():
    known = Usage(checked=NOW)
    known.merge([Window("five_hour", 0.49, NOW + timedelta(hours=2, minutes=10)), Window("seven_day", 0.8)])
    line = usage.meter(known, NOW, ("blue", "yellow", "red")).plain
    assert line == "Session ███▉░░░░ 49% in 2h 10m   Week ██████▍░ 80%"
    assert usage.bar(0, 4) == ("", "░░░░") and usage.bar(1, 4) == ("████", "")
    assert usage.level_color(Window("x", 0.5), ("n", "w", "e")) == "n"
    assert usage.level_color(Window("x", 0.8), ("n", "w", "e")) == "w"
    assert usage.level_color(Window("x", 0.1, status="rejected"), ("n", "w", "e")) == "e"
    assert usage.resets_text(Window("x", 0.1, NOW + timedelta(days=3)), NOW).startswith(
        f"{(NOW + timedelta(days=3)).astimezone():%a} ")


def test_a_saved_reading_comes_back_without_the_windows_that_reset_since(tmp_path):
    path = tmp_path / "usage.json"
    saved = Usage(checked=NOW, notes=["What's contributing to your limits usage?"])
    saved.merge([Window("five_hour", 0.49, NOW + timedelta(hours=2), label="Session"),
                 Window("seven_day", 0.8, NOW - timedelta(minutes=1)),  # reset since
                 Window("seven_day_fable", 0.05, resets="whenever")])
    usage.save_cached(saved, path)
    back = usage.load_cached(NOW, path)
    assert back.last_run and back.checked == NOW and back.notes == saved.notes
    assert list(back.windows) == ["five_hour", "seven_day_fable"]
    assert back.windows["five_hour"] == saved.windows["five_hour"]
    for broken in ("{not json", json.dumps({"windows": [{"key": "five_hour"}]}), "[]"):
        path.write_text(broken, encoding="utf-8")
        assert usage.load_cached(NOW, path) == Usage()
    assert usage.load_cached(NOW, tmp_path / "missing.json") == Usage()


def test_the_meter_dims_a_reading_from_the_last_launch():
    fresh = Usage(checked=NOW)
    fresh.merge([Window("five_hour", 0.49)])
    last_run = Usage(checked=NOW, last_run=True)
    last_run.merge([Window("five_hour", 0.49)])
    colors = ("blue", "yellow", "red")
    assert usage.meter(fresh, NOW, colors).plain == usage.meter(last_run, NOW, colors).plain
    assert [s.style for s in usage.meter(last_run, NOW, colors).spans if s.start == 0] == ["dim"]
    assert [s.style for s in usage.meter(fresh, NOW, colors).spans if s.start == 0] == []


def test_the_wall_starts_with_the_last_reading_and_saves_the_new_one(projects, no_usage_probe):
    import asyncio

    from panopsis.tui import PanopsisApp

    now = datetime.now(timezone.utc)
    saved = Usage(checked=now - timedelta(minutes=5))
    saved.merge([Window("five_hour", 0.3, now + timedelta(hours=1)), Window("seven_day", 0.1, now - timedelta(hours=1))])
    usage.save_cached(saved)
    no_usage_probe.append(PRINTED)
    app = PanopsisApp(provider=lambda: [], root=projects, interval=60)
    assert app.usage.last_run and list(app.usage.windows) == ["five_hour"]  # the week's has reset since

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            for _ in range(100):  # the first check runs once the first listing is in
                await pilot.pause(0.05)
                if not app.usage.last_run:
                    break
            assert "49%" in str(app.query_one("#usage-meter").content)

    asyncio.run(go())
    data = json.loads(usage.cache_path().read_text(encoding="utf-8"))
    assert datetime.fromisoformat(data["checked"]) > saved.checked
    assert [w["key"] for w in data["windows"]] == ["five_hour", "seven_day_fable"]


def test_the_wall_shows_the_meter_and_U_the_details(projects, no_usage_probe):
    import asyncio

    from panopsis.tui import PanopsisApp, UsageScreen

    no_usage_probe.append(PRINTED)
    app = PanopsisApp(provider=lambda: [], root=projects, interval=60)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            for _ in range(100):  # the first check runs once the first listing is in
                await pilot.pause(0.05)
                if app.usage.windows:
                    break
            meter = str(app.query_one("#usage-meter").content)
            assert "Session" in meter and "49%" in meter and "Week (Fable)" in meter and "5%" in meter
            await pilot.press("U")
            await pilot.pause(0.2)
            assert isinstance(app.screen, UsageScreen)
            body = str(app.screen.query_one("#body").content)
            assert "49% used" in body and "What's contributing to your limits usage?" in body
            await pilot.press("escape")

    asyncio.run(go())


def test_the_meter_shows_the_last_reading_before_the_first_check_answers(projects, monkeypatch):
    import asyncio
    import threading

    from panopsis.tui import PanopsisApp

    now = datetime.now(timezone.utc)
    saved = Usage(checked=now - timedelta(minutes=5))
    saved.merge([Window("five_hour", 0.3, now + timedelta(hours=1))])
    usage.save_cached(saved)
    answer = threading.Event()
    monkeypatch.setattr("panopsis.usage.run_probe", lambda claude, timeout=60: answer.wait(5) and "")
    app = PanopsisApp(provider=lambda: [], root=projects, interval=60)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.pause(0.2)
            assert app.usage_checking and "30%" in str(app.query_one("#usage-meter").content)
            answer.set()

    asyncio.run(go())


def test_a_zoomed_in_session_shows_the_meter_too(projects, no_usage_probe):
    import asyncio

    from conftest import T0, common, human, text, write_jsonl

    from panopsis.tui import PanopsisApp, UsageScreen, ZoomScreen

    write_jsonl(projects / "demo" / "s1.jsonl", common([human(0), text(1)], "s1"))
    entries = [{"sessionId": "s1", "status": "idle", "name": "s1", "kind": "interactive", "cwd": "D:\\work\\demo",
                "startedAt": int(T0.timestamp() * 1000)}]
    no_usage_probe.append(PRINTED)
    app = PanopsisApp(provider=lambda: entries, root=projects, interval=60)

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            for _ in range(100):  # the first check runs once the first listing is in
                await pilot.pause(0.05)
                if app.usage.windows:
                    break
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert isinstance(app.screen, ZoomScreen)
            meter = app.screen.query_one("#zoom-usage")
            assert "Session" in str(meter.content) and "49%" in str(meter.content)
            assert meter.region.y == app.screen.query_one("#zoom-where").region.y  # top right, beside where it runs
            assert meter.region.right <= 160 and app.screen.query_one("#zoom-title").region.width > 150
            app._rate_limit_seen([Window("five_hour", 0.8)])  # a session panopsis runs heard from Anthropic
            await pilot.pause()
            assert "80%" in str(meter.content)
            assert "80%" in str(app.query_one("#usage-meter").content)  # and the wall's, under the zoom
            await pilot.click("#zoom-usage")
            await pilot.pause(0.2)
            assert isinstance(app.screen, UsageScreen)

    asyncio.run(go())


def test_meter_can_be_turned_off(projects, monkeypatch):
    import asyncio

    from panopsis.config import Config
    from panopsis.tui import PanopsisApp

    calls = []
    monkeypatch.setattr("panopsis.usage.run_probe", lambda claude, timeout=60: calls.append(1) or "")
    app = PanopsisApp(provider=lambda: [], root=projects, interval=60, config=Config(usage_minutes=0))

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(1.5)
            assert calls == [] and str(app.query_one("#usage-meter").content) == ""

    asyncio.run(go())


def test_probe_runs_claude_code_without_a_session_hooks_or_mcp(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"is_error": False, "result": PRINTED}), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert run_probe("claude") == PRINTED
    cmd = calls[0]
    assert cmd[:3] == ["claude", "-p", "/usage"]
    assert {"--no-session-persistence", "--strict-mcp-config"} <= set(cmd)
    assert cmd[cmd.index("-n") + 1] == "panopsis usage"  # so the grid can leave it out
    assert json.loads(cmd[cmd.index("--settings") + 1]) == {"disableAllHooks": True}

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "not logged in"))
    with pytest.raises(RuntimeError, match="not logged in"):
        run_probe("claude")
    with pytest.raises(RuntimeError, match="not found"):
        run_probe(None)
