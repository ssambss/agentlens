"""What agentlens leaves behind when it stops on an error: ~/.agentlens/crash.log.

An error none of its code handles stops the app, and Textual prints the traceback once the terminal is
back to normal, on a console that's gone when its tab closes. The log keeps it, with errors in
agentlens's own threads (which go unseen, as it keeps running) and Python's own crashes, which
faulthandler writes. Another program ending it (Stop-Process -Force, taskkill /f) leaves it no chance
to write anything.
"""

from __future__ import annotations

import faulthandler
import platform
import threading
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from agentlens.preferences import home

written: Path | None = None  # the log, once this run has added to it


def path() -> Path:
    return home() / "crash.log"


def write(error: BaseException, what: str) -> None:
    """Add `error` and its traceback to the log, after a line saying when and what it did (`what`).
    Never raises: it runs on the way out of something that's failing already."""
    global written
    try:
        file = path()
        file.parent.mkdir(parents=True, exist_ok=True)
        when = datetime.now().astimezone().isoformat(" ", "seconds")
        head = f"--- {when} · {what} · Python {platform.python_version()} on {platform.platform()}\n"
        lines = traceback.format_exception(type(error), error, error.__traceback__)
        with file.open("a", encoding="utf-8") as f:
            f.write(head + "".join(lines) + "\n")
        written = file
    except Exception:  # noqa: BLE001 - the error it was writing down is the one that matters
        pass


@contextmanager
def logged() -> Iterator[None]:
    """While agentlens runs: an error that ends the run here, one in a thread of its own, and a crash
    of Python's own go to the log. The app's own errors come through AgentlensApp._handle_exception.
    A log this run left empty (faulthandler's file is open all along) is gone after it. Faulthandler
    already on (python -X faulthandler) is left as it is."""
    global written
    written = None
    file = path()
    before = threading.excepthook

    def thread_failed(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None and args.exc_type is not SystemExit:
            name = args.thread.name if args.thread else "a thread"
            write(args.exc_value, f"an error in {name}; agentlens kept running")
        before(args)

    threading.excepthook = thread_failed
    crashes = None
    if not faulthandler.is_enabled():
        try:
            file.parent.mkdir(parents=True, exist_ok=True)
            crashes = file.open("a", encoding="utf-8")
            faulthandler.enable(crashes, all_threads=True)
        except OSError:
            pass  # the rest still goes to the log, as far as it can be written
    try:
        yield
    except Exception as error:
        write(error, "stopped agentlens")
        raise
    finally:
        threading.excepthook = before
        if crashes is not None:
            faulthandler.disable()
            crashes.close()
            try:
                if file.stat().st_size == 0:
                    file.unlink()
            except OSError:
                pass  # another agentlens has it open
