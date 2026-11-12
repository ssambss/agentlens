# agentlens

Every Claude Code session you have open on this computer, on one screen in your terminal:
which ones need you, what each is doing and has changed, and how much of your plan's limits
you've used.

- **A live wall** of every open session on this computer, wherever it runs: a terminal, an
  editor or the background. Grouped into spaces by repository. When one starts needing you
  it rings and lights up, and `j` takes you there.
- **Sessions you start here**: type into them, answer their questions and
  permission prompts, and hand them to Claude Code's background service and back. With
  [autopilot](#autopilot) on, Claude answers them for you, and only what needs you rings.
- **History** of every past session, to read again or continue.

It runs locally, on the session files Claude Code keeps on this computer
([what leaves your machine](#what-leaves-your-machine)), so sessions on another computer or
in Claude Code on the web aren't on the wall. For another computer's, run agentlens there,
over SSH for example.

```
uv tool install .      # from this folder; or `uv run agentlens` without installing
agentlens              # the app
```

The title bar says which agentlens you run (`agentlens — 0.1.76`), as `agentlens --version`
and `uv tool list` do. It counts the commits, so a copy that's fallen behind has a lower one
than the latest. The last number goes up to 99 and the middle one to 9: the 100th commit is
0.2.0, the 900th 1.0.0. To update an installed copy, pull, then run
`uv tool install --reinstall .`, so uv builds it again from what you pulled.

For a week it was called panopsis. The first run copies what panopsis kept in `~/.panopsis`
(settings, spaces, keys, history) to `~/.agentlens`, and leaves `~/.panopsis` as it was. An
`~/.agentlens` from before then is older, so it's kept beside, in `~/.agentlens.before-panopsis`.

In the app, `?` lists every key and Ctrl+P searches every command by name. The keys below
are agentlens's own; you can change them ([keys](#keys)).

## The wall

One pane per open session, read as a conversation: your messages on a tinted card,
Claude's replies as markdown, runs of tool calls folded into one line
(`▸ Read a.py · Edit a.py ×2 · Bash ×3`). A question Claude asked you reads as what it
asked, with its options, and then what you answered, on a card like your messages
(or that you didn't answer, and what you said instead). A plan Claude asks you to
approve reads as its replies do, all of it when you zoom in, and then that you approved
it, or said to keep planning and what to change. Panes keep their place, and the
border colour is the state: yellow needs you (heavier for a question or a plan to
approve), green working, blue waiting on its own subagents.

A session a program runs through the Agent SDK, like one another Claude's test script
started, isn't waiting on you. Claude Code records that program's messages as if you had
typed them, but agentlens can tell them apart. While the newest message came through the SDK
and agentlens doesn't run the session itself, the pane's border says `run by a program`. The
program's messages show as *A program*, not *You*, and when the session waits, it waits on
that program: blue, with no ring. Once you type into it yourself, it's yours again.

In Windows, the window agentlens is in has agentlens's icon, four panes on violet, and a
taskbar button of its own, so you can find it among your other terminals. That's
Windows' own console window or Windows Terminal's; a Windows Terminal window with other
tabs keeps the icon while agentlens is one of them. When agentlens quits, or you close its
tab, the window gets its own icon back.

Among your terminal's tabs, agentlens's is named `🟪 agentlens`, the violet square for its
icon. In Konsole the tab has the icon itself, and in iTerm2 the tab is violet, until
agentlens quits. Other terminals, and macOS and Linux, don't let a program change its tab
or window beyond its name, so there it has no icon or taskbar button of its own.

The top of the border ends with the model the session runs on, after `here` or `bg`
(`parser · repo · here · Opus 5.5`).
Beside the state, at the bottom of the border, is how full the session's context window
is (`working 4m · 42% context`). It takes the theme's warning colour at 75% and its error
colour at 90%, as the plan meter does: Claude Code compacts the conversation when it's
nearly full. It's the last model call's whole input, as Claude Code counts it, against the
model's window: 1M tokens, or 200K for models older than the 4.6 generation unless you
picked their 1M variant. Zoomed in, it's a meter with the tokens, after the model the
session runs on (`Model Opus 5.5   Context ███▍░░░░ 42% 420K of 1M`). That's the model
its newest reply came from, which isn't always the one asked for. After `/model`, it's the
one Claude Code says it set, straight away. A session agentlens runs that hasn't replied
yet shows the one Claude Code started it on. Subagents have their own context windows, not
counted here.

When a session starts needing you, you get a notification and a terminal bell. Its Open
button, or `j` from anywhere, takes you there; `j` again goes to the next one.

Once you've opened one, it's read: it stops counting as needing you and its border dims,
until it next does (its next turn ends, or it asks something). So the count, `j`, `f` and
the spaces list cover only what you haven't seen yet, even if you leave a session for
later. Being on its pane on the wall doesn't read it; opening it does. Which ones you've
read is kept in `~/.agentlens/read.json`, so a restart doesn't bring them back.

Enter zooms in: the whole conversation, the files the session changed (Enter opens one
in your editor at the line), and a map of its subagents: which ran, which launched which,
and which ran at the same time. Top right, on the wall and zoomed in, is your plan's
usage: the 5-hour and weekly limits and when they reset. At launch it shows the last
reading, dimmed, until Claude Code answers. Left of it, while agentlens is busy in the
background for more than a moment, a line says with what: reading your sessions or your
history (with how many of how many), or checking your plan usage. Its bar fills by count,
or against how long the same job took last time, kept in `~/.agentlens/durations.json`. It
never shows a time.

| Key | Does |
|---|---|
| arrows, Enter, Esc | move between panes, zoom in, go back. Zoomed in, `→` reaches the files and subagents |
| `j`, `f` | go to a session that needs you; show only those |
| `F` | show only sessions that are working, or waiting on their own subagents: not the idle, not those waiting on you |
| `[` `]`, `g` | previous / next space; move a session to a space |
| `E`, `y` | open the session's repo or worktree in your editor; copy Claude's last reply |
| `w` | close a session you're done with (`w` again confirms): a background one stops, one in a terminal or an editor is hidden |
| `v`, `t` | conversation or one line per event; tool calls on or off |
| `H` | history |
| `U`, `,` | usage and what's using it; settings |
| `D` | what [autopilot](#autopilot) answered for you, in every session |
| `G` | start a goal that a lead Claude runs across sessions: the [coordinator](#coordinator), experimental. `n` can start one too |

Drag to select text and Ctrl+C copies it, over SSH too. Links open on click: web pages in
your browser, file links like `[app.py:42](src/app.py#L42)` in your editor at that line.

agentlens copies through the terminal, which works over SSH, and straight to the clipboard
where it runs. Some terminals don't take a program's copy, GNOME Terminal among them, so the
second way matters. In Linux that's `wl-copy`, `xclip` or `xsel` if one is installed, and
otherwise agentlens's own, through X, which Wayland desktops run too. Ctrl+V in a box pastes
what's on the clipboard. Linux's terminals pass it on to agentlens (their own paste is
Ctrl+Shift+V, which works too), and Windows' paste on it themselves. Over SSH, Ctrl+V pastes
what you last copied in agentlens; the terminal's own paste pastes the rest.

The changed-files list comes from the session's Edit and Write calls, so changes made
through a shell command aren't in it. The subagent map shows that a subagent ran, not
what it did. For a long session, both cover only its recent part: the wall reads the last
4 MB of its transcript, and leaves out the subagents that finished before that.

### Spaces and settings

Each repository is a space, its worktrees folded in. Press `g` to put a session in a
space of your own, or define them in `~/.agentlens/config.yaml`:

```yaml
spaces:
  - name: TV integration
    paths: [D:/work/tv-app]                 # sessions working under these folders
    titles: [tv, chromecast]                # or with these words in their title
    sessions: [e04cd49e-...]                # or picked by hand with g
```

A session you start (`n`) in a space of your own goes in it, whatever folder it works in. In
a repository's space, even one you've moved sessions into, it goes in its own folder's space.
Sessions that come back at launch stay in their own space.

A repository's space shows while one of its sessions is open. Start a session in it (`n`, or a
goal with `G`) and it's kept in your settings: once you've closed all its sessions it stays in the
list, and `n` there starts one in its folder again. Take one out of `spaces` in the settings file
to drop it.

Under the spaces, **Coordinators** lists each [coordinator](#coordinator) with a pane on the
wall, by its goal's name (`◆ Split the parser`). Its entry shows its lead and its sessions
together, whichever spaces they're in, and they stay in those spaces too. `[` `]` and ↑ ↓
in the list reach them after the spaces. An entry goes once its lead and sessions are closed.

Everything else is on the settings screen (`,`), saved as you pick:

- **Look**: theme, colours, columns, and whether the sidebar, tool calls and timestamps
  show. Seven looks are agentlens's own (Violet, Oxblood, Plum night, Ember, Deep sea,
  Aurora, Forest); there are calm, colour-blind safe and vivid presets too. Change a
  colour… lists every colour agentlens uses, like the border of the session you're on, and
  Enter on one picks another for it: one of the theme's (it follows the theme), a fixed
  one, or one you type (`#ff8800`, `orange`, `$accent`). The rest stay as the preset had
  them, and picking a preset again puts all of its colours back.
- **Sessions**: the permission mode for new sessions, your editor, which Claude Code to
  run, how often the usage meter checks, and how far back History (`H`) goes: the past
  week unless you pick otherwise. History lists sessions as it reads them, and `m` there
  reaches further back. It also lists sessions whose transcripts Claude Code has deleted
  (it does 30 days after a session was last active): agentlens keeps what it found in
  each session, for a year unless you pick otherwise.
- **Editor**: where changed files, file links, `E` and the settings files open. Pick
  VS Code, Cursor, Windsurf, VSCodium, Zed, Sublime Text, a JetBrains IDE, Notepad++,
  Xcode or Visual Studio, which open beside agentlens, or Neovim, Vim, Emacs, Helix,
  micro or nano, which take over the terminal until you quit them. Each opens a file at
  the changed line in its own way. For anything else, choose *Your own command* and put
  `{file}` and `{line}` where they go, like `kate --line {line} {file}`. Left on
  *Automatic*, it's `$VISUAL` or `$EDITOR`, otherwise the first windowed editor that's
  installed. Under the picker you can see the exact command it will run.
- **Keys**: a preset, agentlens's own or Vim's (`h` `j` `k` `l` move between panes too,
  and Space goes to a session that needs you), and a way into the keys list.
- **Experimental**: [autopilot](#autopilot) for the sessions agentlens runs, and the
  [coordinator](#coordinator) for a goal that takes several, off until you turn them on.
- **Another PC**: Export saves your settings in one file, and Import on the other PC puts
  them in place of its own, straight away. That's the look, keys, how far back History goes
  and how long it keeps sessions, the usage meter, the mode for new sessions, autopilot
  and your editor. If the other PC doesn't have that editor, the settings screen says so. Nothing else about the PC goes in it: not your spaces
  (they're its folders), its Claude Code, or the sessions agentlens restores or hides, so the
  other PC keeps its own.

  The file is `~/agentlens-settings.yaml` unless you type another path, or Browse… to the
  folder it goes in, like a USB drive's or a synced one's. A file that's there already and
  isn't an export is replaced only if you press Export again. `agentlens export [FILE]` and
  `agentlens import FILE` do the same from the command line, where FILE can be a folder too,
  with agentlens closed: one that's open saves its own settings over them.

### Keys

Any key in the keys list (`?`) changes: Enter on it, then press the new key. It's saved
straight away, and the footer, the keys list and the hints in the app name the key you
picked. If another action already has that key, agentlens says which; pressing it again
takes it, and that action keeps its other key or gets the one you replaced, so nothing is
left without one. Backspace puts agentlens's own back.
Esc, Tab, the numbers 1-9 (they answer Claude), Ctrl+C, Ctrl+Q and Ctrl+P stay as they are.
Keys of one screen, like `m` in History, can be the same as a wall key; there the screen's
wins, as `r` re-reads History rather than refreshing the wall.

They're kept under `keys` in `~/.agentlens/config.yaml`, only the ones you changed, by
what they do. A comma gives an action two keys:

```yaml
keys:
  space-prev: less_than_sign     # < and > switch spaces, on keyboards where [ ] need AltGr
  space-next: greater_than_sign
  move-down: down,j
```

The font and line spacing are your terminal's. A coding font made for screens (Cascadia
Code, JetBrains Mono) at 11-12 pt, with a line height around 1.2, reads well.

## Sessions you start here

`n` starts a session in a folder you pick, on Claude Code's default model unless you pick
another there; `o` in History continues a past one. They run
your own installed, unmodified `claude` headless through the Agent SDK, with Claude Code's
own system prompt, tools and settings, so they behave like your other sessions.
agentlens never reads or passes credentials. It's agentlens's own front end on your `claude`,
not Claude Code's interface, so a few things work differently; they're in the list below.

| Key | Does |
|---|---|
| `i`, Esc | type in the session's box; leave it |
| Enter, Shift+Enter | in the box: send the message; start a new line (Alt+Enter and Ctrl+J too) |
| `/` | in the box: the session's own commands, skills and plugins, and their values (`/model`, `/effort`) |
| `@` | in the box: your other sessions, for Claude to ask or tell something |
| `1` `2` `3` | allow, allow and don't ask again, deny. For a question, pick an option; for a plan, `1` approves and `3` says what to change |
| `x` | stop the current turn |
| `p` | [autopilot](#autopilot) for this pane: off, balanced, autonomous, in turn. Back on your setting's, it follows the setting again |
| `d`, `o` | detach to Claude Code's background service, which keeps working after you quit; bring a background session back, which stops it there, and one that was working carries on here where it left off |
| Alt+V | in the box: paste an image from the clipboard, to go with your next message |
| `c` | open it in full Claude Code in this terminal; quitting that comes back here |

- From the moment you send a message, a line above the box shows Claude working on it:
  what it's doing (thinking, writing, running `Bash: npm test`), for how long, and the tokens
  it has written so far, so a long file being written still counts up. It shows with tool
  calls hidden too.
- You can type while Claude works; the message goes in at its next step.
- The box is edited as a Windows text box is: Ctrl+Backspace and Ctrl+Delete delete a
  word, Shift with the arrows, Home and End selects (with Ctrl, a word at a time), and
  Ctrl+C, Ctrl+X, Ctrl+V and Ctrl+Z copy, cut, paste and undo. It grows to ten lines with
  your message. Ctrl+Backspace deletes a word in agentlens's other boxes too. Most
  terminals send Shift+Enter as a plain Enter, so agentlens asks whether Shift is down
  as it comes: Windows can say, and on a Linux desktop that runs X, the X server can, in
  any terminal. Terminals with the kitty keyboard protocol (kitty, WezTerm, Ghostty, foot)
  say so themselves. Where nothing can, on Wayland or over SSH, Alt+Enter or Ctrl+J
  starts a line. Alt+Enter is also what Claude Code's `/terminal-setup` has Shift+Enter
  send, so in a terminal it set up Shift+Enter works too.
- In the box, `/usage` opens the usage view, `/exit` closes the session, and `/clear`
  starts a new conversation in the same pane.
- Claude Code lets Claude message your other sessions on this machine, by the name each
  has there. Unless you name a session, that's its folder's with two characters after it
  (`tubeless-d4`), alike for every session in a repo and its worktrees, so neither you nor
  Claude could tell which is which. Sessions you start here take their title as their name
  once Claude Code gives them one, and Claude Code keeps it for them, so "ask the shared
  links session what it decided" reaches the right one. One from before agentlens named its
  sessions takes its title the next time it runs: here, in Claude Code from agentlens, or in
  the background. A name you gave a session with `/rename` stays. Detached, a session
  keeps its name too: Claude Code's background service would otherwise give it one of its
  own, like "resume plan review", which nothing knows it by. `@` in the box lists
  your other open sessions by what the wall calls them and puts in the name Claude Code
  knows them by, such as `@"Shared links"`, or `@tubeless-d4` for one in a terminal or an
  editor, which keeps its own name. A session paused here, as they all are when you reopen
  agentlens, isn't running, so Claude Code doesn't list it and nothing can message it. `@`
  lists those too, and a message that names one starts it first, under its name. Claude
  Code's own box tells Claude a mention is a session; a message sent through the SDK
  doesn't, so agentlens adds a line saying so to what Claude reads. The pane shows your
  message as you typed it.
- Alt+V in the box pastes a screenshot or image from the clipboard (Ctrl+V too, where the
  terminal passes it through). It shows above the box as `[Image #1]`, Backspace in an
  empty box removes it, and it goes with your next message, shrunk to 2000 px on the long
  edge. The first message in the `n` dialog takes images the same way. Not over SSH; on
  Linux it takes `wl-paste` or `xclip`, or, with neither installed, X.
- They come back, paused, when you reopen agentlens, until you close (`w`) or detach them;
  a closed one's conversation stays in History. One that's open in a terminal or an editor
  by then comes back once it's closed there. Quitting while some are working asks whether
  to detach them or stop them for now. One waiting on your answer isn't working: it can't
  go on without you, so it closes, and comes back with what it asked in its conversation.
- If Claude Code stops under a session (it exits, or sends something agentlens can't
  read), the pane says why and pauses; your next message continues the conversation.
- Detaching needs a folder terminal Claude Code trusts, which your home folder never is.
  For a project folder, Ctrl+T in the detach dialog opens Claude Code there so you can
  accept its trust prompt once.
- A session open in a terminal or an editor can't also be driven from here. Close it
  there first.

### Autopilot

If you'd rather let sessions run than steer each turn, autopilot answers what you'd only
nod along to. When a turn ends, Sonnet reads Claude's reply with the session before it, so it
knows what you came to the session for and what Claude has done there. It answers the way you
would, or leaves it for you:

- **Balanced** gives the go-aheads: the "shall I go on with the rest?" in the middle of
  what you asked for.
  Finished work, design choices, things to try by hand, a session that's stuck, and anything
  that pushes, merges, releases or deletes, it leaves for you.
- **Autonomous** runs the session for you and leaves you the big decisions. It answers
  Claude's questions with the option Claude recommends, or a sensible default, when that fits
  what you asked for. It takes small follow-ups Claude offers, and cleans up what the work
  left behind. When Claude asks you to try something, it first has Claude check what it can
  itself. When the work is done and checked, it gives the go-ahead to deliver it. How is up
  to the repo and your own skills, so it doesn't say. Where changes need someone's review
  (a pull request, a Gerrit change), sending them for review is as far as it goes:
  approving, submitting and merging are the reviewers'. What it leaves for you:
  - design decisions with lasting consequences, and scope beyond what you asked for
  - releases, deploys, publishing, force-pushes, approving or submitting a change that needs
    review, and deletes you can't easily get back
  - what only you can do or judge, like a purchase, restarting an app, or how something
    looks once Claude has checked it
  - finished work that's checked and delivered: pushed, or sent for review
  - a session that's stuck or isn't sure what you meant

When Claude only says it's waiting on work it started in the session, like a test run in
the background, a subagent, a monitor or a wakeup it scheduled, autopilot answers nothing and
the session doesn't ring. It shows as waiting on agents until that work finishes and Claude
carries on, and autopilot reads that turn too. agentlens checks that some of that work is
still going: with none, the turn is yours. A CI run, a deploy, or another session's or
person's work doesn't bring Claude back, so a turn waiting on those is yours too. If the work
ends and Claude doesn't carry on within five minutes, the session rings after all. A turn
another session's message starts is always yours.

What it leaves for you rings as it would without autopilot, and the line under its title says
what it needs, like "Claude wants you to pick: Redis or in-memory". Questions, permission
prompts and plans come in the middle of a turn, so it never sees them: those are always
yours. After five answers in a row to one session (twelve on autonomous), the next one is
yours anyway.

It writes what you'd type, often a word or two like "yes" or "go on", without repeating
Claude's question back. Its messages show as Autopilot's, in the pane and in History, and
Claude is told you didn't type them. While it reads a reply, typing your own message goes instead of its answer,
and `x` stops it. A turn you wrote to while Claude worked is yours, so it doesn't read that
one at all. It's off unless you turn it on. Each check is a short `claude -p` on
Sonnet at medium effort, with no tools, in an empty folder of its own, so no project's
CLAUDE.md or memory comes into it. It counts against your plan: a few seconds,
once for each turn that ends in a session it's on for.

The setting is for every session agentlens runs. Changed, it reaches the open ones from their
next turn, not the turn already waiting on you, so they aren't all read at once. `p` gives
the session you're on a level of its own, until it comes round to your setting's again.
Each pane's bottom border says when autopilot is on: `autopilot autonomous` as your setting
is, or `autopilot balanced, this pane` for a level of its own (`autopilot off, this pane`,
too, when your setting is on).

**What it answered**, so you needn't watch the sessions to know: `D` lists its answers in
every session, newest first, each with what Claude asked and what it said for you:

```
● 14:32  Config loader split  agentlens · config-split
    Claude asked whether to update the remaining three callers
    → Yes, carry on with tui_driven.py, feed.py and report.py.
```

Enter opens that session. The turns it left you are only counted, at the top ("today it
answered 7 and left 3 for you"), since their replies say what they need. `●` marks the
answers since you last looked, and until you do, the line at the top of the wall says how
many (`autopilot answered 4`; clicking it opens the list). They're kept in
`~/.agentlens/autopilot.jsonl`, the newest 500, so a restart or a closed pane doesn't lose them.

**Your Claude login:** these sessions use your own `claude` sign-in, made through
Anthropic's own flow. agentlens never signs you in, and never reads, stores or passes on a
login: it starts unmodified Claude Code and shows you what it does. Colleagues each run
their own copy, signed in as themselves. Anthropic's
[legal and compliance page](https://code.claude.com/docs/en/legal-and-compliance.md) allows
that, and [Use the Claude Agent SDK with your Claude plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)
covers it on Pro, Max, Team and Enterprise plans. That article also says how this use
counts against your plan, including the `claude -p` that autopilot and the usage meter run.

### Coordinator

Autopilot answers one session. The coordinator runs a goal across several. Turn it on in the
settings, under Experimental. Then `G` asks for a folder and the goal, like `n` does, and
starts a lead Claude in a pane of its own, in the space you're in:

1. The lead reads the repository and breaks the goal into tasks. Each task has a brief that a
   fresh session can act on, and says which tasks it waits for. The brief says what's wanted,
   not how: working that out is the session's.
2. It starts a session for each task beside itself, in the mode you picked. Each is told to
   keep its changes apart from the others' the way the repo does it. How, in a worktree or
   on a branch, is up to the repo and your own skills.
3. When one of those sessions ends a turn, agentlens sends its reply to the lead, and the
   session waits without ringing ("the coordinator is reading this"). The lead answers it,
   marks it done, or brings it to you. What you tell the lead about a task it passes on in
   your words, with only what the session can't know, and no steps.
4. When the tasks are done and checked, it has them delivered one at a time, the way the
   repo does it. Where changes need review, sent for review counts as done: approving and
   submitting are the reviewers', never the lead's or a session's.
5. A task's session closes once the lead marks it done, as `w` would close it, and its
   conversation stays in History. One still working closes when its turn ends.
6. It tells you when the goal is done, and closes any of its sessions still open.

`n` starts a goal too. While the coordinator is on, its dialog has a session or a goal to
pick at the top, and Ctrl+G switches between them; `G` opens the same dialog on the goal. To
keep `n` for sessions and `G` for goals, untick "n starts them too", beside the coordinator in
the settings.

You can also plan the work first, in an ordinary session you started here. When the plan is
ready, tell that session to delegate it to a coordinator. While the coordinator is on, each
session agentlens runs has one tool for this, which its Claude uses only when you ask. It
hands over the goal and the plan, and a lead starts in a pane of its own, as with `G`, in
the planning session's mode. The lead's first message is the plan, marked as handed over
from that session, and the lead keeps to what you decided there. The planning session stays
as it was, and a note on the wall opens the lead's pane. Sessions already open when you turn
the coordinator on get the tool the next time agentlens starts.

The lead doesn't work on the code itself. It has Read, Grep and Glob, and agentlens's own
tools for the board: add tasks, start one, message one, have some debate, finish one, ask you,
finish the goal.
Its pane rings when it asks you something, answers you, or the goal is done. When it passes
what you said on to its sessions, starting one or messaging one, it waits on them instead, as
after any other turn of its own. What it asks
you reads in its conversation, options and all, as Claude's own questions do, and you answer
by writing to it. Asking ends its turn, so the question is the last thing it says. It also
rings when it ends a turn while none of its sessions is at work: those waiting on its answer
don't count, since nothing would bring it back, so what it said is for you. The line under
its title is then the start of its reply. Otherwise it shows what's under way
("coordinating: 2 running, 1 of 3 done").

A goal's panes stay in the space you started it in, among your other sessions, but stand
out, and the goal has an entry of its own under Coordinators in the spaces list
([spaces](#spaces-and-settings)). Their borders are double lines. Its sessions' are in the
state's colour; the lead's is in the coordinator's colour, so you can tell it from them,
except when it needs you or failed, when it's in that state's colour like any session's.
Each top border starts with the goal's name (`◆ Split the parser · agentlens`), in the
coordinator's colour too. Each look has one of its own; pick another in Change a colour… on
the settings screen. That name is your goal's first words, or the goal a session handed
over, and the lead's pane goes by it too. Claude Code's own title for the lead doesn't
replace it, since Claude Code titles the lead from what agentlens tells it. The lead's
bottom border says `coordinator · 2 running · 1/3 done`, and each of its sessions' says
`task 2 of 3 · coordinator`.

Two or more of a goal's sessions can argue a question out between themselves, when the goal
turns on views that differ: designs to choose from, say, or a cause one session found and
another doubts. The lead gives the question, the tasks (each must have reported and be
waiting on it) and up to 3 rounds. As the coordinator runs 3 sessions at once, that's 2 or 3
sides. agentlens then passes each one's reply to the others word for word. Replies are held
until every side has given its own, so none sees what the others said in a round before
answering. Their panes wait quietly meanwhile, and their bottom borders say
`task 1 of 3 · debating tasks 2 and 3 · coordinator`. A side that starts its reply with
"Agreed:" is out of it from then on, and the others debate on. The lead gets each side's last
reply when the rounds are done, when fewer than two sides are left arguing, or when your usage
passes the limit, and weighs them or brings them to you. Writing to one still arguing yourself,
closing it, or the lead messaging or finishing it ends the debate, and the lead gets what the
others said last. The sides are told to change nothing while they debate, but keep the tools
they had.

How much it decides follows your autopilot level:

- **Off**: it brings you every reply that needs an answer, with its suggestion.
- **Balanced**: it gives the go-aheads.
- **Autonomous**: it answers as autopilot's autonomous does.

On off and balanced it shows you the plan and starts nothing until you've answered. It does
that again whenever the plan changes.

The limits are agentlens's, not the lead's to choose:

- 3 sessions at once and 10 per goal
- none started once 80% of your five-hour window is used
- the same number of answers in a row to one session as autopilot gives

You can still type into any of its sessions. That reply comes to you, not the lead, and a
session you've written to is yours: it isn't closed when its task is done. Releases,
deploys, publishing, force-pushes, approving or submitting a change that needs review, and
deletes you can't get back are always yours. Goals are
kept in `~/.agentlens/goals.json`, so a restart brings the lead and its sessions back as they
were.

## What leaves your machine

agentlens reads your sessions on your machine. The sessions you start in it are your own
`claude`, sending what any Claude Code session sends. Besides those, through your own
`claude` CLI:

- **The usage meter** runs Claude Code's `/usage` every few minutes, which asks Anthropic
  how much of your plan's limits you've used. Nothing from your sessions is sent. Turn it
  off in the settings.
- **Autopilot**, in a session you've turned it on for, sends Claude's reply when a turn
  ends, the session before it and its title, to Sonnet: your messages, Claude's replies
  (shortened), and the tools it ran (one short line each). It sends the session's start and
  as much of its end as fits in about 24,000 characters. That's conversation text, the
  session's own, which it sent when it ran. Off unless you turn it on.
- **The coordinator**'s lead and its sessions are sessions like the others. What agentlens
  passes between them is the sessions' own replies. Off unless you turn it on.

## Command line

For scripting: `agentlens sessions`, `session <id>`, and `watch --once` for a plain table.
`export [FILE]` and `import FILE` take your settings to another PC ([another PC](#spaces-and-settings)).
`sessions` takes `--since 7d` and `--project NAME`.

## How it's built

```
adapters/   read one tool's data into a common model    (swap or add sources here)
model.py    sessions, agent runs, time spans
metrics.py  flat metrics per session, subagent and day; flags scripted runs
preferences.py  where agentlens keeps its files (~/.agentlens, or AGENTLENS_HOME), each written whole
live.py     open sessions + transcript tails, as live state
read.py     the sessions needing you that you've already read (~/.agentlens/read.json)
feed.py     how activity reads: conversation blocks, markdown, folded tool calls, questions and answers
subagent_map.py  the zoom view's map: subagents in waves, nested under who launched them
tui.py      the app
driver.py   sessions agentlens runs itself
autopilot.py  Sonnet answering a turn that has ended for you, or leaving it to you (experimental)
coordinator.py  a goal's board, its lead's tools and limits, and its sessions' turns going to the lead (experimental)
tui_driven.py  their panes, request cards, new-session dialog, the / command menu and @ for your other sessions
textbox.py  the boxes you type in: a message over lines, and a Windows text box's editing keys
terminal.py the tab's name, agentlens's icon on its window or tab, and in Windows the keys its consoles don't pass on
icon.py     the icon, drawn for each size it's shown at
tui_settings.py  the settings screen (,)
tui_colors.py  the colours list there, where Enter on a colour picks another for it
version.py  which agentlens this is, from how many commits it has (hatch_build.py writes it into a build)
keys.py     the keys you can change, their presets, and which key each has now
tui_keys.py the keys list (?), where Enter on a key changes it
commands.py Claude Code's slash commands for that menu, from the session's own list
usage.py    your plan's limits, from Claude Code's /usage and sessions' rate-limit events
config.py   app settings and spaces (~/.agentlens/config.yaml)
settings_file.py  your settings in one file, to set up agentlens on another PC, and nothing of this one
editor.py   the editors agentlens knows, and opening a file at its line or a folder in yours
store.py    agentlens's own record of past sessions (~/.agentlens/history)
crash.py    what agentlens leaves when it stops on an error (~/.agentlens/crash.log)
```

Findings and advice, early versions that are hidden and off, are in
[docs/findings-and-advice.md](docs/findings-and-advice.md): what they do, what they send, and their code.

## Caveat

The adapter reads Claude Code's transcript files, whose format is internal and can change
in any release. The parser skips what it doesn't understand rather than failing. Each
session it has read is kept in agentlens's own format in `~/.agentlens/history`: times,
counts, titles and file paths, never the conversation, about 6 KB a session compressed.
Sessions read before a format change keep reading the same after it, and History reaches
back past Claude Code's 30 days. A hook plugin writing events in that format
could add to the adapter later; it can't replace it, since hooks don't carry Claude's
replies or token counts.

When agentlens stops on an error, its traceback is kept in `~/.agentlens/crash.log`, as are
errors in its threads and crashes of Python's own. One ended by another program
(`Stop-Process -Force`, `taskkill /f`) leaves nothing there, but its files are written whole,
so your goals and settings stay as they were last saved.

## License

[MIT](LICENSE): free to use, change and share, for anything, commercial or not. Keep the
copyright notice with copies. It comes with no warranty.
