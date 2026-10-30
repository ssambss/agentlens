"""The keys, and which of them you can change.

The keys list (?) changes one (Enter on it, then the new key) and the settings (,) have
presets. Your changes are saved in config.yaml under `keys`, by the ids below, and only
the ones you changed: a key that comes back to panopsis's own isn't saved. panopsis's
bindings take their keys from here, so the footer, the keys list and the hints in the app
all name the key you picked.

Keys are named as Textual names them: `r`, `R`, `ctrl+w`, `question_mark`, `left`, `f5`.
A comma between two gives an action both.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.binding import Binding
from textual.keys import _character_to_key, format_key  # _character_to_key: how Textual's own keymap reads "?"

APP = "app"  # the wall's keys, which work on every screen but a dialog


@dataclass(frozen=True)
class Action:
    """A key you can change."""

    id: str  # what config.yaml's `keys` calls it
    keys: str  # panopsis's own, comma-separated; hints name the first
    help: str  # what it does, as the keys list says it
    # Where its key works: the wall's keys, and the screens with keys of their own. Two actions
    # that work in one place can't share a key. A screen's keys win over the wall's there.
    where: frozenset[str] = frozenset({APP})


@dataclass(frozen=True)
class Fixed:
    """A key the keys list explains that can't be changed: the arrows in a list, typing in a box."""

    keys: str  # as the list shows it
    help: str


HISTORY_SCREENS = frozenset({"history", "report", "advice", "profile", "rules", "usage"})

HELP: list[tuple[str, list[Action | Fixed]]] = [
    ("The selected session", [
        Action("compose", "i", "type a message to it"),
        Fixed("/", "in its box: Claude Code's commands (/model, /compact, /clear, your skills)"),
        Fixed("alt+v", "in its box: paste an image from the clipboard, to go with your message"),
        Action("interrupt", "x", "stop its current turn"),
        Fixed("1-9", "answer its question or permission prompt"),
        Action("zoom", "enter", "zoom in: the whole conversation, a map of its subagents and the files it changed"),
        Action("open-folder", "E", "open its repository or worktree in your editor"),
        Action("copy-reply", "y", "copy Claude's last reply", frozenset({APP, "zoom"})),
        Action("bring-here", "o", "bring a background session here, so you can type into it; in History, "
               "continue a past one here", frozenset({APP, "history"})),
        Action("attach", "a", "attach to a background session in this terminal", frozenset({APP, "zoom"})),
        Action("detach", "d", "detach: hand it to Claude Code's background service"),
        Action("claude-code", "c", "open it in full Claude Code; quit that to come back. In History, a past one",
               frozenset({APP, "history"})),
        Action("move-to-space", "g", "move it to a space"),
        Action("close", "w,delete", "close it, once you've said so again (stops a background session, hides "
               "one in a terminal or an editor)"),
    ]),
    ("Zoomed in", [
        Fixed("→  ←", "over to the files it changed and its subagents, and back"),
        Fixed("↑  ↓", "through the files, then on down into the subagents"),
        Fixed("enter", "open the highlighted file in your editor, at the change"),
    ]),
    ("The wall", [
        Action("new-session", "n", "start a new session"),
        Action("move-left", "left", "the session to the left; from the first column, the spaces list, where "
               "↑ ↓ switch space"),
        Action("move-right", "right", "the session to the right"),
        Action("move-up", "up", "the session above"),
        Action("move-down", "down", "the session below"),
        Action("toggle-filter", "f", "show only sessions that need you"),
        Action("toggle-working", "F", "show only sessions that are working, or waiting on their own subagents"),
        Action("space-prev", "left_square_bracket", "previous space"),
        Action("space-next", "right_square_bracket", "next space"),
        Action("refresh", "r", "refresh now; in History, the report screens and usage, read them again",
               frozenset({APP}) | HISTORY_SCREENS),
    ]),
    ("Look and settings", [
        Action("settings", "comma", "settings: theme, colours, columns, keys, your editor, defaults, experimental "
               "screens"),
        Action("columns-more", "plus,equals_sign", "more columns"),
        Action("columns-fewer", "minus", "fewer columns"),
        Action("toggle-sidebar", "s", "show or hide the spaces sidebar"),
        Action("toggle-view", "v", "conversation, or one line per event", frozenset({APP, "zoom"})),
        Action("toggle-tools", "t", "show or hide tool calls", frozenset({APP, "zoom"})),
    ]),
    ("Your history", [
        Action("report", "R", "report: where your time went"),
        Action("history", "H", "history: every past session"),
        Action("history-older", "m", "in History: older sessions", frozenset({"history"})),
        Action("advise", "A", "advice on how you work with agents"),
        Action("advice-new", "g", "in Advice: fresh advice", frozenset({"advice"})),
        Action("profile", "P", "your working-style profile"),
        Action("profile-edit", "e", "in your profile: edit your preferences", frozenset({"profile"})),
        Action("rules", "L", "the best-practice rules"),
        Action("rules-mute", "m", "in Rules: mute or unmute the rule you're on", frozenset({"rules"})),
    ]),
    ("Anywhere", [
        Fixed("drag", "select text with the mouse; ctrl+c copies it"),
        Fixed("click", "open a link: web pages in your browser, files in your editor"),
        Action("usage", "U", "usage: how much of your plan's session and weekly limits is used"),
        Action("jump", "j", "go to a session that needs you: the newest alert first, then each in turn"),
        Fixed("ctrl+p", "search all commands by name"),
        Action("show-keys", "question_mark", "this list"),
        Fixed("esc", "back, or leave a text box"),
        Action("quit", "q", "quit"),
    ]),
]

ACTIONS = {entry.id: entry for _, entries in HELP for entry in entries if isinstance(entry, Action)}

# The keys of the experimental screens, which the keys list leaves out until the settings turn them on.
FINDINGS_AND_ADVICE = frozenset({"report", "advise", "advice-new", "profile", "profile-edit", "rules", "rules-mute"})


def help_groups(findings_and_advice: bool) -> list[tuple[str, list[Action | Fixed]]]:
    """The keys list's groups, without the experimental screens' keys while they're off."""
    if findings_and_advice:
        return HELP
    return [(group, [e for e in entries if not (isinstance(e, Action) and e.id in FINDINGS_AND_ADVICE)])
            for group, entries in HELP]

# Keys no action can have, and why.
RESERVED = {
    "escape": "it goes back, and out of a text box",
    "backspace": "here it puts panopsis's key back",
    "tab": "it moves between the parts of a screen",
    "shift+tab": "it moves between the parts of a screen",
    "ctrl+c": "it copies",
    "ctrl+q": "it quits",
    "ctrl+p": "it searches the commands",
    **{str(n): "the numbers answer Claude's questions" for n in range(1, 10)},
}

# The presets the settings offer: an id, what it's called, and the keys it changes.
KEY_PRESETS = {
    "panopsis": ("panopsis's own", {}),
    "vim": ("Vim: h j k l move too, and space goes to a session that needs you", {
        "move-left": "left,h", "move-down": "down,j", "move-up": "up,k", "move-right": "right,l", "jump": "space",
    }),
}


def binding(id: str, action: str, description: str = "", **kwargs) -> Binding:
    """A binding for one of the actions above, on panopsis's keys until you change them."""
    return Binding(ACTIONS[id].keys, action, description, id=id, **kwargs)


def normalize(keys: str) -> str:
    """Keys as Textual names them: `?` is `question_mark`, as in a binding."""
    names = [key.strip() for key in keys.split(",") if key.strip()]
    return ",".join(_character_to_key(key) if len(key) == 1 else key for key in names)


def show(key: str) -> str:
    """One key as the app writes it: `ctrl+w`, `?`, `enter`, `←`."""
    *modifiers, name = key.split("+")  # `+` itself is `plus`
    shown = {"enter": "enter", "backspace": "backspace", "escape": "esc"}.get(name) or format_key(name)
    return "+".join([*modifiers, shown])


def keys_of(id: str, keymap: dict[str, str]) -> list[str]:
    """An action's keys, with your changes."""
    return (keymap.get(id) or ACTIONS[id].keys).split(",")


def changed(keymap: dict[str, str], changes: dict[str, str]) -> dict[str, str]:
    """Your keys with these changes made, keeping only the ones that aren't panopsis's own."""
    merged = {**keymap, **changes}
    return {id: keys for id, keys in merged.items() if id in ACTIONS and keys and keys != ACTIONS[id].keys}


def preset_of(keymap: dict[str, str]) -> str | None:
    """The preset these keys are, or None for keys of your own."""
    return next((id for id, (_, preset) in KEY_PRESETS.items() if changed({}, preset) == changed({}, keymap)), None)


def holders(key: str, where: frozenset[str], keymap: dict[str, str]) -> list[Action]:
    """The actions with this key in any of these places."""
    return [a for a in ACTIONS.values() if a.where & where and key in keys_of(a.id, keymap)]


def clashes(keymap: dict[str, str]) -> list[tuple[str, Action, Action]]:
    """Keys two actions have in one place: Textual keeps only one of them."""
    found = []
    actions = list(ACTIONS.values())
    for i, a in enumerate(actions):
        for b in actions[i + 1:]:
            if a.where & b.where:
                found += [(key, a, b) for key in keys_of(a.id, keymap) if key in keys_of(b.id, keymap)]
    return found


def assign(id: str, key: str, keymap: dict[str, str]) -> tuple[dict[str, str], list[Action]]:
    """Give an action this key instead of its own. The actions that have the key where this one
    works give it up: each keeps its other keys, or gets this one's old key. Returns the changes
    and the actions that give the key up, or no changes if one of those would then share a key."""
    old = keys_of(id, keymap)[0]
    others = [a for a in holders(key, ACTIONS[id].where, keymap) if a.id != id]
    changes = {id: key}
    for other in others:
        changes[other.id] = ",".join([k for k in keys_of(other.id, keymap) if k != key] or [old])
    new = changed(keymap, changes)
    if any(a.id in changes or b.id in changes for _, a, b in clashes(new)):
        return {}, others
    return changes, others


# The keys in use: the app sets them as it starts and whenever you change one, and the hints in
# the panes and dialogs name them. One app runs in a process.
_in_use: dict[str, str] = {}


def use(keymap: dict[str, str]) -> None:
    global _in_use
    _in_use = dict(keymap)


def current(id: str) -> str:
    """An action's keys as Textual names them, comma-separated: to answer a dialog with them."""
    return ",".join(keys_of(id, _in_use))


def name(id: str) -> str:
    """An action's key, as a hint names it: `press {name('compose')} to type`."""
    return show(keys_of(id, _in_use)[0])


def names(id: str, keymap: dict[str, str] | None = None) -> str:
    """All of an action's keys, as the keys list shows them: `w  del`."""
    return "  ".join(show(key) for key in keys_of(id, _in_use if keymap is None else keymap))
