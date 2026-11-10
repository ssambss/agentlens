import asyncio
import faulthandler
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agentlens import crash, tui


def log() -> str:
    return crash.path().read_text(encoding="utf-8")


@pytest.fixture
def no_faulthandler():
    """As agentlens runs for you: pytest has faulthandler on, which the log leaves as it is."""
    was = faulthandler.is_enabled()
    faulthandler.disable()
    yield
    if was:
        faulthandler.enable(sys.__stderr__)


def test_an_error_goes_in_the_log_with_its_traceback():
    try:
        raise ValueError("bad goal")
    except ValueError as e:
        crash.write(e, "stopped agentlens")
    text = log()
    assert text.startswith("--- ") and " · stopped agentlens · Python " in text.splitlines()[0]
    assert "Traceback (most recent call last)" in text and "ValueError: bad goal" in text
    assert crash.written == crash.path()


def test_an_error_ending_the_run_is_logged_and_still_raised(no_faulthandler):
    with pytest.raises(RuntimeError):
        with crash.logged():
            raise RuntimeError("boom")
    assert "stopped agentlens" in log() and "RuntimeError: boom" in log()


def test_an_error_in_a_thread_is_logged_and_the_run_goes_on(monkeypatch):
    monkeypatch.setattr(threading, "excepthook", quiet := lambda args: None)
    with crash.logged():
        thread = threading.Thread(target=lambda: 1 / 0, name="agentlens-listing")
        thread.start()
        thread.join()
    assert "an error in agentlens-listing; agentlens kept running" in log() and "ZeroDivisionError" in log()
    assert threading.excepthook is quiet


def test_a_run_without_errors_leaves_no_log(no_faulthandler):
    with crash.logged():
        assert faulthandler.is_enabled() and crash.path().exists()  # open all along, for a crash
    assert not faulthandler.is_enabled() and not crash.path().exists()
    assert crash.written is None


def test_a_crash_of_pythons_own_is_logged(tmp_path):
    src = Path(crash.__file__).parents[1]
    env = {**os.environ, "AGENTLENS_HOME": str(tmp_path), "PYTHONPATH": str(src)}
    run = subprocess.run([sys.executable, "-c", "import faulthandler\nfrom agentlens import crash\n"
                          "with crash.logged():\n    faulthandler._sigsegv()"], env=env, capture_output=True)
    assert run.returncode != 0
    text = (tmp_path / "crash.log").read_text(encoding="utf-8")
    assert "Windows fatal exception: access violation" in text or "Fatal Python error" in text
    assert 'File "<string>", line 4' in text


def test_an_error_that_stops_the_app_is_logged(projects):
    app = tui.AgentlensApp(provider=lambda: [], root=projects, interval=60)

    async def go():
        async with app.run_test() as pilot:
            app.call_later(lambda: {}["goal"])
            await pilot.pause(0.3)

    with pytest.raises(KeyError):
        asyncio.run(go())
    assert "stopped agentlens" in log() and "KeyError: 'goal'" in log()


def test_a_workers_error_is_logged_with_its_own_traceback(projects):
    app = tui.AgentlensApp(provider=lambda: [], root=projects, interval=60)

    async def listing_failed():
        raise LookupError("no sessions")

    async def go():
        async with app.run_test() as pilot:
            app.run_worker(listing_failed())
            await pilot.pause(0.3)

    with pytest.raises(Exception):
        asyncio.run(go())
    assert "LookupError: no sessions" in log() and "in listing_failed" in log()
    assert "WorkerFailed" not in log()
