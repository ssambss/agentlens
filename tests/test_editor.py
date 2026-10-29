import base64
import re

import pytest

from agentlens import editor


@pytest.fixture
def installed(monkeypatch):
    """Editors on this pretend PATH, by the command that finds them."""
    on_path: dict[str, str] = {}
    monkeypatch.setattr(editor.shutil, "which", on_path.get)
    # And no Visual Studio the Installer knows of, whatever this computer has.
    monkeypatch.setitem(editor.EDITORS, "devenv", editor.replace(editor.EDITORS["devenv"], locate=lambda: None))
    return on_path


def test_each_editor_opens_a_file_at_its_line_its_own_way(installed, tmp_path):
    installed.update(code="C:/bin/code.cmd", idea="C:/bin/idea.cmd", nvim="/bin/nvim", emacs="/bin/emacs",
                     subl="/bin/subl")
    # Not on PATH, but where it installs.
    installed[editor.os.path.expandvars(r"%ProgramFiles%\Notepad++\notepad++.exe")] = "C:/npp/notepad++.exe"
    code = editor.resolve("code")
    assert code.command("D:/r/a.py", 12) == ["C:/bin/code.cmd", "--goto", "D:/r/a.py:12"]
    assert code.command("D:/r/a.py") == ["C:/bin/code.cmd", "--goto", "D:/r/a.py:1"]
    assert code.command(str(tmp_path)) == ["C:/bin/code.cmd", str(tmp_path)]  # a folder
    assert code.command("D:/settings.yaml", wait=True) == ["C:/bin/code.cmd", "--wait", "D:/settings.yaml"]
    assert not code.terminal
    assert editor.resolve("jetbrains").command("a.py", 7) == ["C:/bin/idea.cmd", "--line", "7", "a.py"]
    assert editor.resolve("jetbrains").command(str(tmp_path)) == ["C:/bin/idea.cmd", str(tmp_path)]
    assert editor.resolve("subl").command("a.py", 7) == ["/bin/subl", "a.py:7"]
    npp = editor.resolve("notepad++")
    assert npp.command("a.py", 7) == ["C:/npp/notepad++.exe", "-n7", "a.py"]
    assert npp.command(str(tmp_path)) == ["C:/npp/notepad++.exe", "-openFoldersAsWorkspace", str(tmp_path)]
    nvim = editor.resolve("nvim")
    assert nvim.terminal and nvim.command("a.py", 7) == ["/bin/nvim", "+7", "a.py"]
    assert nvim.command("a.py", wait=True) == ["/bin/nvim", "+1", "a.py"]  # it waits anyway
    assert editor.resolve("emacs").command(str(tmp_path)) == ["/bin/emacs", "-nw", str(tmp_path)]


def test_visual_studio_goes_to_the_line_in_the_one_already_open(installed, tmp_path):
    installed["devenv"] = "C:/VS/devenv.exe"
    vs = editor.resolve("devenv")
    command = vs.command("D:/r/it's a file.cs", 42)
    assert command[0].endswith("powershell.exe") and command[-2] == "-EncodedCommand"
    script = base64.b64decode(command[-1]).decode("utf-16-le")
    encoded = [base64.b64decode(b).decode() for b in re.findall(r"FromBase64String\('([^']*)'\)", script)]
    assert encoded == ["D:/r/it's a file.cs", "C:/VS/devenv.exe"]  # no quotes for a name to break out of
    assert "$line = 42" in script and "GetActiveObject('VisualStudio.DTE')" in script
    assert '/Edit $file /Command "Edit.GoTo $line"' in script  # with none open, it starts at the line
    assert vs.command(str(tmp_path)) == ["C:/VS/devenv.exe", str(tmp_path)]
    assert vs.command("D:/config.yaml", wait=True) == ["C:/VS/devenv.exe", "D:/config.yaml"]  # its own window
    assert "already open, it goes to the line" in editor.describe("devenv")
    # Not on PATH: where the Visual Studio Installer says it is.
    found = editor.replace(editor.EDITORS["devenv"], commands=(), locate=lambda: "D:/VS/18/devenv.exe")
    assert editor.find(found) == ("D:/VS/18/devenv.exe", "D:/VS/18/devenv.exe")


def test_your_own_command_puts_the_file_and_line_where_you_say(installed, tmp_path):
    installed.update(kate="/bin/kate", kak="/bin/kak", code="C:/bin/code.cmd")
    installed["C:/Program Files/Kate/kate.exe"] = "C:/Program Files/Kate/kate.exe"
    kate = editor.resolve("kate --line {line} {file}")
    assert kate.command("a.py", 7) == ["/bin/kate", "--line", "7", "a.py"]
    assert kate.command(str(tmp_path)) == ["/bin/kate", str(tmp_path)]  # no line for a folder, nor its option
    assert kate.command("a.py", wait=True) == ["/bin/kate", "--line", "1", "a.py"]  # waited on as it is
    assert not kate.terminal and kate.editor.label == "kate"
    spaced = editor.resolve('"C:/Program Files/Kate/kate.exe" -l {line} {file}')
    assert spaced.command("a.py", 3) == ["C:/Program Files/Kate/kate.exe", "-l", "3", "a.py"]
    assert editor.resolve("kate").command("a.py", 7) == ["/bin/kate", "a.py"]  # no {file}: it goes last
    assert editor.resolve("kak", terminal=True).terminal
    # One that's known keeps its way of waiting, and runs your words.
    mine = editor.resolve("code -r --goto {file}:{line}")
    assert mine.command("a.py", 4) == ["C:/bin/code.cmd", "-r", "--goto", "a.py:4"]
    assert mine.command("a.py", wait=True) == ["C:/bin/code.cmd", "--wait", "a.py"]
    assert editor.resolve("nowhere {file}") is None


def test_a_bare_command_or_path_that_agentlens_knows_opens_its_way(installed):
    installed.update(cursor="C:/bin/cursor.cmd")
    installed["C:/Users/me/AppData/Local/Programs/Microsoft VS Code/bin/code.cmd"] = "C:/vscode/code.cmd"
    installed["D:/JetBrains/PyCharm/bin/pycharm64.exe"] = "D:/JetBrains/PyCharm/bin/pycharm64.exe"
    assert editor.resolve("cursor").command("a.py", 2) == ["C:/bin/cursor.cmd", "--goto", "a.py:2"]
    vscode = editor.resolve('"C:/Users/me/AppData/Local/Programs/Microsoft VS Code/bin/code.cmd"')
    assert vscode.editor.label == "VS Code" and vscode.command("a.py", 2) == ["C:/vscode/code.cmd", "--goto", "a.py:2"]
    pycharm = editor.resolve("D:/JetBrains/PyCharm/bin/pycharm64.exe")
    assert pycharm.command("a.py", 2) == ["D:/JetBrains/PyCharm/bin/pycharm64.exe", "--line", "2", "a.py"]


def test_unset_it_is_visual_or_editor_then_the_first_installed(installed):
    installed.update(cursor="C:/bin/cursor.cmd", zed="C:/bin/zed.exe", nvim="/bin/nvim", code="C:/bin/code.cmd")
    installed["emacsclient"] = "/bin/emacsclient"
    assert editor.resolve(None, environ={}).editor.label == "VS Code"
    assert editor.resolve(None, environ={}).via == "the first editor installed"
    installed.pop("code")
    assert editor.resolve(None, environ={}).editor.label == "Cursor"  # the windowed ones, in their order
    from_env = editor.resolve(None, environ={"EDITOR": "nvim", "VISUAL": "code --wait"})
    assert from_env.editor.label == "Neovim" and from_env.via == "from $EDITOR"  # VISUAL's code isn't installed
    visual = editor.resolve(None, environ={"VISUAL": "zed --wait"})
    assert visual.command("a.py", 3) == ["C:/bin/zed.exe", "a.py:3"]  # zed's way, not --wait every time
    unknown = editor.resolve(None, environ={"EDITOR": "emacsclient -t"})
    assert unknown.terminal and unknown.command("a.py", 3) == ["/bin/emacsclient", "-t", "a.py"]
    # Picked but not installed: say so rather than open another.
    assert editor.resolve("windsurf", environ={"EDITOR": "nvim"}) is None


def test_the_settings_screen_says_what_will_run(installed):
    installed.update(code="C:/bin/code.cmd", nvim="/bin/nvim")
    assert editor.describe("code") == "Runs code --goto <file>:<line>."
    assert editor.describe(None, environ={}) == "VS Code, the first editor installed. Runs code --goto <file>:<line>."
    assert editor.describe(None, environ={"EDITOR": "nvim"}) == (
        "Neovim, from $EDITOR. Runs nvim +<line> <file>. It takes over this terminal until you quit it.")
    assert "Sublime Text isn't on your PATH" in editor.describe("subl")
    assert editor.describe("kate {file}") == "Couldn't find kate. Give the full path to it."
    # Your own command starts from the one you have, or its usual command when it isn't installed.
    assert editor.own_command("code") == "code --goto {file}:{line}"
    assert editor.own_command("jetbrains") == "idea --line {line} {file}"
    installed.clear()
    assert editor.describe(None, environ={}) == "No editor found."
    installed.update(notepad="C:/Windows/notepad.exe", vi="/bin/vi")
    assert editor.describe(None, environ={}) in ("No editor found, so files open in Notepad.",
                                                  "No editor found, so files open in Vim.")


def test_the_folder_is_the_repository_or_worktree(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "src" / "deep").mkdir(parents=True)
    worktree = repo / ".claude" / "worktrees" / "feature"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: ../../../.git/worktrees/feature", encoding="utf-8")
    plain = tmp_path / "notes"
    plain.mkdir()
    assert editor.project_folder(str(repo / "src" / "deep")) == str(repo)
    assert editor.project_folder(str(worktree)) == str(worktree)  # its own checkout, not the main one
    assert editor.project_folder(str(plain)) == str(plain)
    assert editor.absolute("src/a.py", "D:/r") == str(editor.Path("D:/r") / "src/a.py")
    assert editor.absolute("D:/elsewhere/a.py", "D:/r") == "D:/elsewhere/a.py"
