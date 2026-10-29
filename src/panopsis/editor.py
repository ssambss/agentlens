"""Opening what a session touched in your code editor.

panopsis shows what an agent did; your editor is where you read what the code is now.
This hands a file (at the line that changed), a link you clicked or a session's folder to
the editor you picked on the settings screen. A windowed editor takes it and panopsis goes
straight on, so the wall keeps updating while you read on the other monitor; one that runs
in the terminal takes over the screen until you quit it.

The setting is a key of EDITORS, or a command of your own with {file} and {line} where they
go. Left empty, it's $VISUAL or $EDITOR, else the first editor installed.
"""

from __future__ import annotations

import base64
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class Editor:
    """How an editor takes a file: the commands it installs as, and the words after them."""

    label: str
    commands: tuple[str, ...]  # tried in order, on PATH or where it installs
    at_line: tuple[str, ...]  # a file at a line
    folder: tuple[str, ...] | None = None  # None: at_line without its line
    wait: tuple[str, ...] | None = None  # returns when you close the file; None: at_line, waited on
    terminal: bool = False  # runs in this terminal, so panopsis steps aside until you quit it
    locate: Callable[[], str | None] | None = None  # finds it when none of its commands is on PATH
    # Builds the command for a file at a line from (program, file, line), when words can't say it.
    opener: Callable[[str, str, int], list[str]] | None = None
    note: str = ""  # for the settings screen, after the command


def preset(label: str, commands: tuple[str, ...], at_line: str = "{file}:{line}", *, folder: str | None = None,
           wait: str | None = "--wait {file}", terminal: bool = False) -> Editor:
    return Editor(label, commands, tuple(at_line.split()), tuple(folder.split()) if folder else None,
                  tuple(wait.split()) if wait and not terminal else None, terminal)


@lru_cache(maxsize=1)
def visual_studio() -> str | None:
    """The newest Visual Studio, wherever it's installed. devenv is only on PATH in its own
    developer prompt, so ask the Visual Studio Installer's vswhere."""
    vswhere = Path(os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"))
    if os.name != "nt" or not vswhere.is_file():
        return None
    try:
        found = subprocess.run([str(vswhere), "-latest", "-property", "productPath"], capture_output=True,
                               text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    path = found.stdout.strip().splitlines()[0] if found.stdout.strip() else ""
    return path if path.lower().endswith("devenv.exe") and Path(path).is_file() else None


def visual_studio_at_line(program: str, path: str, line: int) -> list[str]:
    """Visual Studio only runs /Command as it starts, so a file handed to one that's already
    open stays at the top. Its automation (DTE) moves that one to the line; with none open,
    devenv starts at it. The file goes into the script as base64: no quoting to get wrong."""
    def text(value: str) -> str:
        encoded = base64.b64encode(value.encode("utf-8")).decode()
        return f"[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'))"
    script = (f"$file = {text(path)}; $devenv = {text(program)}; $line = {int(line)}\n"
              "try {\n"
              "    $dte = [Runtime.InteropServices.Marshal]::GetActiveObject('VisualStudio.DTE')\n"
              "    $dte.ItemOperations.OpenFile($file) | Out-Null\n"
              "    $dte.ActiveDocument.Selection.GotoLine($line, $false)\n"
              "    $dte.MainWindow.Activate()\n"
              "} catch {\n"
              "    & $devenv /Edit $file /Command \"Edit.GoTo $line\"\n"
              "}\n")
    return [shutil.which("powershell") or "powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
            base64.b64encode(script.encode("utf-16-le")).decode()]


VSCODE = {"at_line": "--goto {file}:{line}", "folder": "{file}"}
JETBRAINS = ("idea", "pycharm", "webstorm", "goland", "clion", "rider", "phpstorm", "rubymine", "rustrover",
             "studio")

# The windowed ones first: with nothing picked, the first of those installed is used.
EDITORS: dict[str, Editor] = {
    "code": preset("VS Code", ("code",), **VSCODE),
    "cursor": preset("Cursor", ("cursor",), **VSCODE),
    "windsurf": preset("Windsurf", ("windsurf",), **VSCODE),
    "codium": preset("VSCodium", ("codium", "code-oss"), **VSCODE),
    "code-insiders": preset("VS Code Insiders", ("code-insiders",), **VSCODE),
    "zed": preset("Zed", ("zed",)),
    "subl": preset("Sublime Text", ("subl", r"%ProgramFiles%\Sublime Text\subl.exe")),
    "jetbrains": preset("JetBrains IDE (IntelliJ IDEA, PyCharm, WebStorm…)", JETBRAINS, "--line {line} {file}"),
    "notepad++": preset("Notepad++", ("notepad++", r"%ProgramFiles%\Notepad++\notepad++.exe"), "-n{line} {file}",
                        folder="-openFoldersAsWorkspace {file}", wait="-multiInst -nosession {file}"),
    "xed": preset("Xcode", ("xed",), "--line {line} {file}"),
    # /Edit hands the file to the Visual Studio that's open. Without it, a window of its own
    # opens, which is closed when you're done with the file: a wait.
    "devenv": Editor("Visual Studio", ("devenv",), ("/Edit", "{file}", "/Command", "Edit.GoTo {line}"),
                     folder=("{file}",), wait=("{file}",), locate=visual_studio, opener=visual_studio_at_line,
                     note="In a Visual Studio that's already open, it goes to the line through Visual Studio's "
                          "automation"),
    "nvim": preset("Neovim", ("nvim",), "+{line} {file}", terminal=True),
    "vim": preset("Vim", ("vim", "vi"), "+{line} {file}", terminal=True),
    "emacs": preset("Emacs", ("emacs",), "-nw +{line} {file}", terminal=True),
    "hx": preset("Helix", ("hx", "helix"), terminal=True),
    "micro": preset("micro", ("micro",), "+{line} {file}", terminal=True),
    "nano": preset("nano", ("nano",), "+{line} {file}", terminal=True),
}
NOTEPAD = preset("Notepad", ("notepad",), "{file}", wait=None)


def _name(command: str) -> str:
    """code for C:/…/bin/code.cmd, pycharm for pycharm64.exe."""
    name = re.split(r"[\\/]", command)[-1].lower()
    return re.sub(r"(64)?\.(exe|cmd|bat|sh)$", "", name)


NAMES = {_name(command): key for key, editor in EDITORS.items() for command in editor.commands}


@dataclass(frozen=True)
class Chosen:
    """An editor found on this computer, ready to open things."""

    editor: Editor
    program: str  # where it is
    given: str  # how it was asked for: code, or the full path
    via: str = ""  # how it was found when you didn't pick one: from $EDITOR, the first editor installed

    @property
    def terminal(self) -> bool:
        return self.editor.terminal

    def command(self, path: str, line: int | None = None, wait: bool = False) -> list[str]:
        """Open a file at a line (the first, without one), or a folder. A windowed editor reuses
        the window that has it open. `wait` returns only once you've closed the file."""
        editor = self.editor
        if wait and editor.wait:
            words = editor.wait
        elif not wait and Path(path).is_dir():
            words = editor.folder or without_line(editor.at_line)
        elif editor.opener is not None:
            return editor.opener(self.program, path, line or 1)
        else:
            words = editor.at_line
        return [self.program, *fill(words, path, line or 1)]

    def shown(self) -> str:
        """The command for a file at a line, as the settings screen shows it."""
        words = [self.given, *self.editor.at_line]
        if not any("{file}" in w for w in words):
            words.append("{file}")
        return join(words).replace("{file}", "<file>").replace("{line}", "<line>")


def split(command: str) -> list[str]:
    """A command line as words; Windows paths keep their backslashes."""
    try:
        words = shlex.split(command, posix=os.name != "nt")
    except ValueError:  # an unclosed quote
        words = command.split()
    return [w[1:-1] if len(w) > 1 and w[0] == w[-1] and w[0] in "\"'" else w for w in words]


def join(words: list[str]) -> str:
    return " ".join(f'"{w}"' if " " in w else w for w in words)


def fill(words: tuple[str, ...], path: str, line: int) -> list[str]:
    filled = [w.replace("{line}", str(line)).replace("{file}", path) for w in words]
    return filled if any("{file}" in w for w in words) else [*filled, path]


def without_line(words: tuple[str, ...]) -> tuple[str, ...]:
    """A command for a file at a line, made to open a folder: `--line {line} {file}` gives
    `{file}`, as do `+{line} {file}` and `{file}:{line}`."""
    kept: list[str] = []
    for word in words:
        if word == "{line}":
            if kept and kept[-1].startswith("-") and "{" not in kept[-1]:
                kept.pop()  # the option it's the value of
            continue
        word = word.replace(":{line}", "")
        if "{line}" not in word:
            kept.append(word)
    return tuple(kept)


def find(editor: Editor) -> tuple[str, str] | None:
    """Where an editor is installed, and the command that found it."""
    for command in editor.commands:
        given = os.path.expandvars(command)
        if program := shutil.which(given):
            return program, given
    if editor.locate is not None and (program := editor.locate()):
        return program, program
    return None


def resolve(setting: str | None, terminal: bool = False, environ: Mapping[str, str] = os.environ) -> Chosen | None:
    """The editor to open things with: the one you picked, else $VISUAL or $EDITOR, else the
    first one installed. None when the one you picked isn't on this computer: say so rather
    than open something else. `terminal` says your own command runs in the terminal."""
    if setting:
        return _given(setting, terminal)
    for variable in ("VISUAL", "EDITOR"):
        value = environ.get(variable, "").strip()
        # By convention these run in the terminal, and are waited on.
        if value and (chosen := _given(value, terminal=True)) is not None:
            return replace(chosen, via=f"from ${variable}")
    for editor in EDITORS.values():
        if not editor.terminal and (found := find(editor)):
            return Chosen(editor, *found, via="the first editor installed")
    return None


def _given(setting: str, terminal: bool) -> Chosen | None:
    if setting in EDITORS:
        found = find(EDITORS[setting])
        return Chosen(EDITORS[setting], *found) if found else None
    words = split(setting)
    if not words:
        return None
    given = os.path.expanduser(words[0])
    program = shutil.which(given)
    if program is None:
        return None
    known = EDITORS.get(NAMES.get(_name(given), ""))
    rest = tuple(words[1:])
    if known is None:  # the file goes last, unless {file} says where
        return Chosen(Editor(_name(given), (given,), rest, terminal=terminal), program, given)
    if not any("{file}" in w for w in rest):
        return Chosen(known, program, given)  # code, the full path to it or `code --wait`: VS Code's way
    return Chosen(replace(known, at_line=rest, folder=None, terminal=known.terminal or terminal, opener=None, note=""),
                  program, given)


def own_command(setting: str | None, terminal: bool = False) -> str:
    """The editor you have, as a command of your own to start from: `code --goto {file}:{line}`.
    One that isn't installed gives its usual command, for you to put the full path in."""
    chosen = resolve(setting, terminal)
    if chosen is not None:
        return join([chosen.given, *chosen.editor.at_line])
    if setting in EDITORS:
        return join([EDITORS[setting].commands[0], *EDITORS[setting].at_line])
    return setting or ""


def fallback() -> Chosen | None:
    """For a file, when no editor is set or installed: Notepad, or vi."""
    editor = NOTEPAD if os.name == "nt" else EDITORS["vim"]
    found = find(editor)
    return Chosen(editor, *found) if found else None


def describe(setting: str | None, terminal: bool = False, environ: Mapping[str, str] = os.environ) -> str:
    """What the settings screen says about the editor: what opens files, and how."""
    chosen = resolve(setting, terminal, environ)
    if chosen is None and setting in EDITORS:
        return (f"{EDITORS[setting].label} isn't on your PATH. Install its command-line launcher, or pick "
                "Your own command and give the full path to it.")
    if chosen is None and setting:
        return f"Couldn't find {(split(setting) or [setting])[0]}. Give the full path to it."
    if chosen is None:
        last = fallback()
        return f"No editor found, so files open in {last.editor.label}." if last else "No editor found."
    text = f"Runs {chosen.shown()}"
    if chosen.via:
        text = f"{chosen.editor.label}, {chosen.via}. {text}"
    if chosen.editor.note:
        text += ". " + chosen.editor.note
    if chosen.terminal:
        text += ". It takes over this terminal until you quit it"
    return text + "."


def project_folder(cwd: str) -> str:
    """The folder to open for a session: the repository or worktree it works in, else its folder."""
    path = Path(cwd)
    for candidate in (path, *path.parents):
        try:
            if (candidate / ".git").exists():  # a directory in a repository, a file in a worktree
                return str(candidate)
        except OSError:
            break
    return str(path)


def absolute(path: str, cwd: str) -> str:
    return path if Path(path).is_absolute() or not cwd else str(Path(cwd) / path)


def launch(command: list[str]) -> None:
    """Start the editor's CLI without waiting; it hands the file to the running editor and exits."""
    options: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    subprocess.Popen(command, **options)
