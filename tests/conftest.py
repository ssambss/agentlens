"""Builders for synthetic Claude Code transcripts. Tests never read real session data."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)


def at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def human(minutes: float, text: str = "do the thing") -> dict:
    return {"type": "user", "timestamp": at(minutes), "origin": {"kind": "human"},
            "message": {"role": "user", "content": text}}


def notification(minutes: float, kind: str = "task-notification") -> dict:
    return {"type": "user", "timestamp": at(minutes), "origin": {"kind": kind},
            "message": {"role": "user", "content": "<task-notification>done</task-notification>"}}


def text(minutes: float, body: str = "ok", msg_id: str | None = None, output_tokens: int = 10) -> dict:
    return {"type": "assistant", "timestamp": at(minutes),
            "message": {"id": msg_id or f"msg_{minutes}", "model": "claude-opus-5-5",
                        "usage": {"output_tokens": output_tokens},
                        "content": [{"type": "text", "text": body}]}}


def tool_use(minutes: float, name: str, tool_id: str, msg_id: str | None = None, **tool_input) -> dict:
    return {"type": "assistant", "timestamp": at(minutes),
            "message": {"id": msg_id or f"msg_{minutes}", "model": "claude-opus-5-5",
                        "usage": {"output_tokens": 5},
                        "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]}}


def tool_result(minutes: float, tool_id: str, permission_source: str = "config") -> dict:
    return {"type": "user", "timestamp": at(minutes),
            "permissionDecision": {"decision": "accept", "source": permission_source},
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "x"}]}}


def edit_result(minutes: float, tool_id: str, path: str, new_start: int | None = None, context: int = 0,
                created: bool = False, is_error: bool = False) -> dict:
    """The result of an Edit or Write: Claude Code records the patch it applied beside it."""
    record = tool_result(minutes, tool_id)
    if is_error:
        record["message"]["content"][0]["is_error"] = True
        record["toolUseResult"] = "Error: File has not been read yet."
        return record
    patch = [] if new_start is None else [
        {"oldStart": new_start, "newStart": new_start, "lines": [" same"] * context + ["-old", "+new"]}]
    record["toolUseResult"] = {"filePath": path, "structuredPatch": patch, **({"type": "create"} if created else {})}
    return record


def plain(renderable) -> str:
    """What a Rich renderable reads as, laid out on one wide line where it fits."""
    from rich.console import Console

    console = Console(width=200, record=True, file=io.StringIO())
    console.print(renderable)
    return console.export_text()


def write_jsonl(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


class StreamingClaude:
    """Stands in for subprocess.Popen running `claude -p --output-format stream-json`: each run
    reads its prompt and prints these messages, one JSON object a line."""

    def __init__(self, messages: list[dict], returncode: int = 0, stderr: str = "") -> None:
        self.messages, self.returncode, self.stderr = messages, returncode, stderr
        self.runs: list[dict] = []  # cmd, env and prompt of each run

    def __call__(self, cmd, **kw):
        run = {"cmd": cmd, "env": kw.get("env") or {}, "prompt": ""}
        self.runs.append(run)
        fake = self

        class Prompt(io.StringIO):
            def close(self):
                run["prompt"] = self.getvalue()

        class Process:
            stdin = Prompt()
            stdout = io.StringIO("".join(json.dumps(m) + "\n" for m in fake.messages))
            stderr = io.StringIO(fake.stderr)
            returncode = fake.returncode

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                pass

        return Process()


def claude_result(text: str = "", is_error: bool = False) -> dict:
    return {"type": "result", "subtype": "success", "is_error": is_error, "result": text}


@pytest.fixture
def projects(tmp_path: Path) -> Path:
    return tmp_path / "projects"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    """Settings, preferences and advice go to a temp dir, never the real ~/.agentlens."""
    home = tmp_path / "agentlens-home"
    monkeypatch.setenv("AGENTLENS_HOME", str(home))
    return home


@pytest.fixture
def findings_and_advice(isolated_home: Path) -> None:
    """The experimental report, profile, rules and advice turned on, as the settings do."""
    from agentlens import config as config_mod

    config_mod.save(config_mod.Config(path=isolated_home / "config.yaml", findings_and_advice=True))


@pytest.fixture(autouse=True)
def system_clipboard(monkeypatch) -> list[str]:
    """What the app copies lands here, never on the real clipboard."""
    copied: list[str] = []
    monkeypatch.setattr("agentlens.clipboard.copy", lambda text: copied.append(text) or True)
    return copied


@pytest.fixture(autouse=True)
def clipboard_images(monkeypatch) -> list:
    """What the app pastes as an image comes from here, never the real clipboard: put one in."""
    from agentlens.clipboard import NoImage

    images: list = []

    def paste():
        if not images:
            raise NoImage("There's no image on the clipboard.")
        return images.pop(0)

    monkeypatch.setattr("agentlens.clipboard.paste_image", paste)
    return images


@pytest.fixture
def server_info() -> dict:
    """What ClaudeSDKClient.get_server_info() returns, trimmed from Claude Code 2.1.310."""
    return {
        "commands": [
            {"name": "clear", "description": "Start a new session with empty context", "argumentHint": "[name]",
             "aliases": ["reset", "new"], "builtin": True},
            {"name": "compact", "description": "Free up context by summarizing the conversation so far",
             "argumentHint": "<optional custom summarization instructions>", "builtin": True},
            {"name": "context", "description": "Show current context usage", "argumentHint": "", "builtin": True},
            {"name": "effort", "description": "Set effort level for model usage",
             "argumentHint": "<low|medium|high|xhigh|max|auto|ultracode [on|off]>", "builtin": True},
            {"name": "model", "description": "Set the AI model for Claude Code", "argumentHint": "<model>",
             "builtin": True},
            {"name": "usage", "description": "Show session cost, plan usage", "argumentHint": "",
             "aliases": ["cost", "stats"], "builtin": True},
            {"name": "fast", "description": "Toggle fast mode (Opus 5.5)", "argumentHint": "[on|off]", "builtin": True},
            {"name": "agents", "description": "(removed) Ask Claude to create/manage subagents", "builtin": True},
            {"name": "__remote-workflow", "description": "Run the workflow script", "builtin": True},
            {"name": "output-style", "description": "List output styles or switch to one", "argumentHint": "[style]",
             "builtin": True},
            {"name": "engineering:code-review", "description": "(engineering) Review code changes", "argumentHint": ""},
            {"name": "simplify", "description": "Review the changed code for reuse", "argumentHint": "[<target>]",
             "builtin": True},
        ],
        "models": [
            {"value": "default", "displayName": "Default (recommended)", "description": "Opus 5.5 · Org default"},
            {"value": "opus", "displayName": "Opus 5.5", "description": "For complex work"},
            {"value": "haiku", "displayName": "Haiku 5.5", "description": "Fastest"},
        ],
        "available_output_styles": ["default", "Concise", "Explanatory"],
    }


@pytest.fixture(autouse=True)
def agentlens_keys():
    """The hints name agentlens's own keys, whatever keys a test before this one picked."""
    from agentlens import keys

    keys.use({})
    yield
    keys.use({})


@pytest.fixture(autouse=True)
def not_in_windows_console(monkeypatch) -> None:
    """Tests behave the same whichever terminal runs them: the app does a few things only in
    Windows' own console window. Tests about those say they're in one."""
    monkeypatch.setattr("agentlens.terminal.classic_console", lambda: False)


@pytest.fixture(autouse=True)
def no_installed_plugins(monkeypatch) -> list[dict]:
    """Advice lists the user's plugins through the real `claude plugin list`; tests never ask it.
    Tests can append what the fake list returns."""
    listed: list[dict] = []
    monkeypatch.setattr("agentlens.work.list_plugins", lambda: listed)
    return listed


@pytest.fixture(autouse=True)
def no_usage_probe(monkeypatch) -> list[str]:
    """The usage meter asks the real Claude Code's /usage; tests never do. Tests can append
    what the fake /usage prints."""
    printed: list[str] = []
    monkeypatch.setattr("agentlens.usage.run_probe", lambda claude, timeout=60: printed[-1] if printed else "")
    return printed


@pytest.fixture(autouse=True)
def no_history_preload(monkeypatch) -> None:
    """The app reads history once the wall is filled in; tests read it only when they open a
    screen that needs it, unless they're about the preload."""
    monkeypatch.setattr("agentlens.tui.PRELOAD_SECONDS", None)


def common(records: list[dict], session_id: str = "sess-1", cwd: str = "D:\\work\\demo") -> list[dict]:
    for r in records:
        r.setdefault("sessionId", session_id)
        r.setdefault("cwd", cwd)
        r.setdefault("entrypoint", "claude-vscode")
        r.setdefault("version", "2.1.300")
    return records
