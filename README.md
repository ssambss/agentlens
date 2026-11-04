# panopsis

Every Claude Code session you have open, on one screen in your terminal: which ones need
you, what each is doing and has changed, and how much of your plan's limits you've used.
The name is Greek for seeing everything at once.

- **A live wall** of every open session, wherever it runs: a terminal, an editor or the
  background. Grouped into spaces by repository. When one starts needing you it rings and
  lights up, and `j` takes you there.
- **Sessions you start here** (experimental): type into them, answer their questions and
  permission prompts, and hand them to Claude Code's background service and back. With
  [autopilot](#autopilot) on, Claude gives the go-aheads for you, and only what needs you
  rings.
- **History** of every past session, to read again or continue.

It runs locally, on Claude Code's own session files
([what leaves your machine](#what-leaves-your-machine)). Findings about how you work and
advice from Claude are in it too, but they're early versions, so they're off until you turn
them on ([findings and advice](#findings-and-advice-experimental)).

```
uv tool install .      # from this folder; or `uv run panopsis` without installing
panopsis               # the app
```

It used to be called agentlens. The first run copies what agentlens kept in `~/.agentlens`
(settings, spaces, keys, history) to `~/.panopsis`, and leaves the old folder as it was.

In the app, `?` lists every key and Ctrl+P searches every command by name. The keys below
are panopsis's own; you can change them ([keys](#keys)).

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

In Windows, the window panopsis is in has panopsis's icon, four panes on violet, and a
taskbar button of its own, so you can find it among your other terminals. That's
Windows' own console window or Windows Terminal's; a Windows Terminal window with other
tabs keeps the icon while panopsis is one of them. When panopsis quits, or you close its
tab, the window gets its own icon back.

Among your terminal's tabs, panopsis's is named `🟪 panopsis`, the violet square for its
icon. In Konsole the tab has the icon itself, and in iTerm2 the tab is violet, until
panopsis quits. Other terminals, and macOS and Linux, don't let a program change its tab
or window beyond its name, so there it has no icon or taskbar button of its own.

Beside the state, at the bottom of the border, is how full the session's context window
is (`working 4m · 42% context`). It takes the theme's warning colour at 75% and its error
colour at 90%, as the plan meter does: Claude Code compacts the conversation when it's
nearly full. It's the last model call's whole input, as Claude Code counts it, against the
model's window: 1M tokens, or 200K for models older than the 4.6 generation unless you
picked their 1M variant. Zoomed in, it's a meter with the tokens
(`Context ███▍░░░░ 42% 420K of 1M`). Subagents have their own context windows, not
counted here.

When a session starts needing you, you get a notification and a terminal bell. Its Open
button, or `j` from anywhere, takes you there; `j` again goes to the next one.

Once you've opened one, it's read: it stops counting as needing you and its border dims,
until it next does (its next turn ends, or it asks something). So the count, `j`, `f` and
the spaces list cover only what you haven't seen yet, even if you leave a session for
later. Being on its pane on the wall doesn't read it; opening it does. Which ones you've
read is kept in `~/.panopsis/read.json`, so a restart doesn't bring them back.

Enter zooms in: the whole conversation, the files the session changed (Enter opens one
in your editor at the line), and a map of its subagents: which ran, which launched which,
and which ran at the same time. Top right, on the wall and zoomed in, is your plan's
usage: the 5-hour and weekly limits and when they reset. At launch it shows the last
reading, dimmed, until Claude Code answers. Left of it, while panopsis is busy in the
background for more than a moment, a line says with what: reading your sessions or your
history (with how many of how many), checking your plan usage, getting advice (with what
Claude is reading). Its bar fills by count, or against how long the same job took last
time, kept in `~/.panopsis/durations.json`. It never shows a time.

With [findings and advice](#findings-and-advice-experimental) on, your history is read in the
background too, once the wall is filled in, so the report, profile, advice and rules screens
open at once. Open one on history read more than a minute
before and it shows that at once, reads it again quietly, and shows it again if anything
changed.

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
| `R` `A` `P` `L` | report, advice, profile, rules, once [turned on](#findings-and-advice-experimental) |
| `U`, `,` | usage and what's using it; settings |

Drag to select text and Ctrl+C copies it, over SSH too. Links open on click: web pages in
your browser, file links like `[app.py:42](src/app.py#L42)` in your editor at that line.

The changed-files list comes from the session's Edit and Write calls, so changes made
through a shell command aren't in it. The subagent map shows that a subagent ran, not
what it did. For a long session, both cover only its recent part: the wall reads the last
4 MB of its transcript, and leaves out the subagents that finished before that.

### Spaces and settings

Each repository is a space, its worktrees folded in. Press `g` to put a session in a
space of your own, or define them in `~/.panopsis/config.yaml`:

```yaml
spaces:
  - name: TV integration
    paths: [D:/work/tv-app]                 # sessions working under these folders
    titles: [tv, chromecast]                # or with these words in their title
    sessions: [e04cd49e-...]                # or picked by hand with g
```

Everything else is on the settings screen (`,`), saved as you pick:

- **Look**: theme, colours, columns, and whether the sidebar, tool calls and timestamps
  show. Seven looks are panopsis's own (Violet, Oxblood, Plum night, Ember, Deep sea,
  Aurora, Forest); there are calm, colour-blind safe and vivid presets too.
- **Sessions**: the permission mode for new sessions, your editor, which Claude Code to
  run, how often the usage meter checks, and how far back History (`H`) goes: the past
  week unless you pick otherwise. History lists sessions as it reads them, and `m` there
  reaches further back. It also lists sessions whose transcripts Claude Code has deleted
  (it does 30 days after a session was last active): panopsis keeps what it found in
  each session, for a year unless you pick otherwise.
- **Editor**: where changed files, file links, `E` and the settings files open. Pick
  VS Code, Cursor, Windsurf, VSCodium, Zed, Sublime Text, a JetBrains IDE, Notepad++,
  Xcode or Visual Studio, which open beside panopsis, or Neovim, Vim, Emacs, Helix,
  micro or nano, which take over the terminal until you quit them. Each opens a file at
  the changed line in its own way. For anything else, choose *Your own command* and put
  `{file}` and `{line}` where they go, like `kate --line {line} {file}`. Left on
  *Automatic*, it's `$VISUAL` or `$EDITOR`, otherwise the first windowed editor that's
  installed. Under the picker you can see the exact command it will run.
- **Keys**: a preset, panopsis's own or Vim's (`h` `j` `k` `l` move between panes too,
  and Space goes to a session that needs you), and a way into the keys list.
- **Experimental**: [autopilot](#autopilot) for the sessions you start here, and
  [findings and advice](#findings-and-advice-experimental), both off until you turn them
  on. With findings and advice on, **What you're aiming for** shows too: your goals and a
  deliberate working style.
- **Another PC**: Export saves your settings in one file, and Import on the other PC puts
  them in place of its own, straight away. That's the look, keys, how far back History goes
  and how long it keeps sessions, the usage meter, the mode and autopilot for new sessions
  and your editor, with your goals, style, muted rules and thresholds. If the other PC doesn't have that
  editor, the settings screen says so. Nothing else about the PC goes in it: not your spaces
  (they're its folders), its Claude Code, or the sessions panopsis restores or hides, so the
  other PC keeps its own.

  The file is `~/panopsis-settings.yaml` unless you type another path, or Browse… to the
  folder it goes in, like a USB drive's or a synced one's. A file that's there already and
  isn't an export is replaced only if you press Export again. `panopsis export [FILE]` and
  `panopsis import FILE` do the same from the command line, where FILE can be a folder too,
  with panopsis closed: one that's open saves its own settings over them.

### Keys

Any key in the keys list (`?`) changes: Enter on it, then press the new key. It's saved
straight away, and the footer, the keys list and the hints in the app name the key you
picked. If another action already has that key, panopsis says which; pressing it again
takes it, and that action keeps its other key or gets the one you replaced, so nothing is
left without one. Backspace puts panopsis's own back.
Esc, Tab, the numbers 1-9 (they answer Claude), Ctrl+C, Ctrl+Q and Ctrl+P stay as they are.
Keys of one screen, like `m` in History, can be the same as a wall key; there the screen's
wins, as `r` re-reads History rather than refreshing the wall.

They're kept under `keys` in `~/.panopsis/config.yaml`, only the ones you changed, by
what they do. A comma gives an action two keys:

```yaml
keys:
  space-prev: less_than_sign     # < and > switch spaces, on keyboards where [ ] need AltGr
  space-next: greater_than_sign
  move-down: down,j
```

The font and line spacing are your terminal's. A coding font made for screens (Cascadia
Code, JetBrains Mono) at 11-12 pt, with a line height around 1.2, reads well.

## Sessions you start here (experimental)

`n` starts a session in a folder you pick; `o` in History continues a past one. They run
your own installed, unmodified `claude` headless through the Agent SDK, with Claude Code's
own system prompt, tools and settings, so they behave like your other sessions.
panopsis never reads or passes credentials.

| Key | Does |
|---|---|
| `i`, Esc | type in the session's box; leave it |
| Enter, Shift+Enter | in the box: send the message; start a new line (Ctrl+J too) |
| `/` | in the box: the session's own commands, skills and plugins, and their values (`/model`, `/effort`) |
| `@` | in the box: your other sessions, for Claude to ask or tell something |
| `1` `2` `3` | allow, allow and don't ask again, deny. For a question, pick an option; for a plan, `1` approves and `3` says what to change |
| `x` | stop the current turn |
| `p` | [autopilot](#autopilot): off, balanced, autonomous, in turn |
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
  Ctrl+C, Ctrl+X and Ctrl+Z copy, cut and undo. It grows to ten lines with your message.
  Ctrl+Backspace deletes a word in panopsis's other boxes too. Not every terminal tells
  Shift+Enter from Enter: Windows' do, through panopsis, and so do those with the kitty
  keyboard protocol (kitty, WezTerm, Ghostty, foot). In the others, Ctrl+J starts a line.
- In the box, `/usage` opens the usage view, `/exit` closes the session, and `/clear`
  starts a new conversation in the same pane.
- Claude Code lets Claude message your other sessions on this machine, by the name each
  has there. Unless you name a session, that's its folder's with two characters after it
  (`tubeless-d4`), alike for every session in a repo and its worktrees, so neither you nor
  Claude could tell which is which. Sessions you start here take their title as their name
  once Claude Code gives them one, and Claude Code keeps it for them, so "ask the shared
  links session what it decided" reaches the right one. One from before panopsis named its
  sessions takes its title the next time it runs: here, in Claude Code from panopsis, or in
  the background. A name you gave a session with `/rename` stays. `@` in the box lists
  your other open sessions by what the wall calls them and puts in the name Claude Code
  knows them by, such as `@"Shared links"`, or `@tubeless-d4` for one in a terminal or an
  editor, which keeps its own name. A session paused here, as they all are when you reopen
  panopsis, isn't running, so Claude Code doesn't list it and nothing can message it. `@`
  lists those too, and a message that names one starts it first, under its name. Claude
  Code's own box tells Claude a mention is a session; a message sent through the SDK
  doesn't, so panopsis adds a line saying so to what Claude reads. The pane shows your
  message as you typed it.
- Alt+V in the box pastes a screenshot or image from the clipboard (Ctrl+V too, where the
  terminal passes it through). It shows above the box as `[Image #1]`, Backspace in an
  empty box removes it, and it goes with your next message, shrunk to 2000 px on the long
  edge. The first message in the `n` dialog takes images the same way. Not over SSH; on
  Linux it needs `wl-paste` or `xclip`.
- They come back, paused, when you reopen panopsis, until you close (`w`) or detach them;
  a closed one's conversation stays in History. One that's open in a terminal or an editor
  by then comes back once it's closed there. Quitting while some are working asks whether
  to detach them or stop them for now.
- If Claude Code stops under a session (it exits, or sends something panopsis can't
  read), the pane says why and pauses; your next message continues the conversation.
- Detaching needs a folder terminal Claude Code trusts, which your home folder never is.
  For a project folder, Ctrl+T in the detach dialog opens Claude Code there so you can
  accept its trust prompt once.
- A session open in a terminal or an editor can't also be driven from here. Close it
  there first.

### Autopilot

If you'd rather let sessions run than steer each turn, autopilot answers what you'd only
nod along to. When a turn ends, Haiku reads Claude's reply with your last few messages and
answers it the way you would, or leaves it for you:

- **Balanced** gives the go-aheads: the "shall I go on with the rest?" in the middle of
  what you asked for.
- **Autonomous** also answers routine questions, such as a name, a small detail, or whether
  to add tests, when your messages make the answer clear or a default is easy to change
  later.

Finished work, design choices, things to try by hand, a session that's stuck, and anything
that pushes, merges, releases or deletes, it leaves for you. Then the session rings as it
would without autopilot, and the line under its title says what it needs, like "Claude
wants you to pick: Redis or in-memory". Questions, permission prompts and plans come in
the middle of a turn, so it never sees them: those are always yours. After five answers in
a row to one session, the next one is yours anyway.

Its messages show as Autopilot's, in the pane and in History, and Claude is told you didn't
type them. The report doesn't count them as yours, nor the seconds before them as Claude
waiting on you. While it reads a reply, typing your own message goes instead of its answer,
and `x` stops it. `p` changes it for the session you're on (the box says when it's on), and
the settings pick it for new sessions. It's off unless you turn it on. Each check is a
short `claude -p` on Haiku, with no tools, so it counts against your plan.

**Your Claude login:** these sessions use your own `claude` sign-in, made through
Anthropic's own flow. panopsis never signs you in, and never reads, stores or passes on a
login: it starts unmodified Claude Code and shows you what it does. Colleagues each run
their own copy, signed in as themselves. Anthropic's
[legal and compliance page](https://code.claude.com/docs/en/legal-and-compliance.md) allows
that, and [Use the Claude Agent SDK with your Claude plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)
covers it on Pro, Max, Team and Enterprise plans. That article also says how this use
counts against your plan, including the `claude -p` that advice and the usage meter run.

## Findings and advice (experimental)

These are early versions, so they're off until you tick *Findings and advice* in the
settings (`,`), under Experimental. Then `R`, `P`, `L` and `A` open them, and the
`report`, `profile`, `rules`, `mute`, `advise` and `curate` commands run.

The report (`R`) splits each session's time by what ended each gap: your prompt (waiting
on you), a tool result, a foreground subagent's result, a background-task notification,
or a message to an idle subagent. Gaps over 2 hours waiting on you or on background work
count as away, and so does the time a session was closed before you resumed it.

The report shows evidence, not advice:

- **Findings grouped by cause.** A rule's findings are grouped by what they have in
  common: a subagent's type, a session's project, the plugin a pipeline's subagents come
  from. Twenty runs of one plugin's subagent waiting on a check after every file make one
  line, with how often, how long in all, and the biggest case. Why a rule matters is on
  the Rules screen (`L`); what to do about it is advice's job (`A`).
- **The past 7 days against the 7 before.** Where the time went and how often each rule
  fired in each, so you can see whether a change helped.
- **Scripted runs left out.** Sessions with one prompt that start in a burst, ten or more
  each within 10 minutes of the one before in one folder (or in sibling folders a script
  made for each run), are a script running `claude -p` over a list. They'd make your
  sessions look short and your baselines low, so the findings, baselines and profile
  leave them out, and the report's header says how many there were.

From that:

1. **Your own baseline.** Rule thresholds can be percentiles of your history
   (`value: p90`), so "unusually long" means unusual for you. A subagent is compared with
   your other runs of its type: a reviewer and a planner take different times.
2. **Derived style.** Steering (close/balanced/autonomous), parallelism and delegation,
   computed from your sessions. If it contradicts what you said you want, the report says so.
3. **Stated preferences.** Goals rank findings; a deliberate style mutes rules that would
   nag you about it; per-rule thresholds and mutes.
4. **Advice that follows up.** `A` has Claude read your profile, findings, plugins and team
   packs, search the web for how teams work with agents now, and write advice for the kind
   of work you do. It looks past Claude Code's own features, and each item says where the
   change goes: your own setup, the repo, your team's plugin, or CI and tools outside
   Claude Code. Each one is kept in `~/.panopsis/advice/`; the next starts by saying
   whether those tips were taken up and helped, and suggests what you haven't tried rather
   than what you already do.

None of this reaches Claude. Rules aren't enforced; findings and advice are for you to act
on, in your CLAUDE.md, your settings or how you work. Apart from the permission mode for
sessions you start in the app, and [autopilot](#autopilot) answering them once you turn it
on, panopsis never changes how Claude works.

**Rule packs.** Practices change faster than tools ship, so rules are YAML. Each records
its `source` and `reviewed` date, and rules older than the pack's `max_age_days` are
flagged. Add a team pack with `--rules DIR`, `PANOPSIS_RULES`, or `rule_packs:` in your
profile. A rule with the same id overrides the built-in one; `disabled: true` removes it.

**What a team has written down.** Much of what a team learns can't be measured from
sessions, such as where a skill's knowledge belongs or which writes need a person. A pack's
`context:` entries hold those: a title, who it's `for`, and a practice with its sources.
Advice gets them all and turns the ones that fit your work into items, saying which pack
they come from. `panopsis rules` lists them under the rules.

**Practices that moved.** `/insights` tells you how you use Claude Code's features. A
rule's `practice` says how a way of working has moved: what you do, what it was, what to
do now, from which Claude Code version, and when the old way still fits. Each claim is
backed by a source quoted word for word, such as a changelog entry or a docs passage.
Advice states these and checks them against the versions your sessions ran. It doesn't
recall them, so it can be no more current than the pack.

**Keeping a pack current.** `panopsis curate` has Claude read the pack's `watch` list and
every source its rules cite. It reports, rule by rule, whether each still holds, needs an
update (with the replacement YAML and the passage that says so) or should go. It also
lists new practices the sources support. Quotes are checked first in code against each
page as downloaded, because a model's summary of a page can't confirm a quote. Claude then
explains the ones that no longer match. The report is saved in `~/.panopsis/curator/` for
a person to review. The pack itself is never changed, so `reviewed` dates mean someone
checked. `--pack FILE` curates a team pack.

## What leaves your machine

panopsis reads your sessions on your machine. The sessions you start in it are your own
`claude`, sending what any Claude Code session sends. Besides those, through your own
`claude` CLI:

- **The usage meter** runs Claude Code's `/usage` every few minutes, which asks Anthropic
  how much of your plan's limits you've used. Nothing from your sessions is sent. Turn it
  off in the settings.
- **Autopilot**, in a session you've turned it on for, sends Claude's reply when a turn
  ends, your last four messages to that session and its title, to Haiku. That's
  conversation text, the session's own, which it sent when it ran. Off unless you turn it on.

With findings and advice on, two more. For these `claude` gets two tools, web search and web
fetch, and no others, so it can't read or change your files:

- **Advice** sends:
  - aggregate metrics
  - the titles of the sessions, subagents and projects its findings mention (Claude
    Code's summaries of what you asked)
  - per project, the languages of files read, recent session titles, and which build and
    Claude Code setup files exist in the repo and in the folders your sessions ran in (not
    what's in them)
  - which Claude Code features you've used, the models your subagents ran on, and the
    Claude Code versions you run
  - the plugins you've enabled, the marketplace each came from, on which events and tools
    its hooks run (not what they run), and the names and descriptions of its skills,
    subagents and commands (not their instructions)
  - the practices behind its findings, and your team packs' entries, with their sources
  - your last advice

  Never code or conversation text. `panopsis advise --dry-run` prints exactly what would
  be sent.
- **The curator** sends the rule pack it checks, the names of the metrics rules can use, and
  which of the pack's quotes it found on their pages. Nothing from your sessions. It checks
  the quotes in code by downloading each page the pack cites: plain HTTPS requests that send
  nothing. `panopsis curate --dry-run` prints what it would send.

## Command line

For scripting: `panopsis sessions`, `session <id>`, and `watch --once` for a plain table.
`export [FILE]` and `import FILE` take your settings to another PC ([another PC](#spaces-and-settings)).
With findings and advice on, also `report [--json]`, `profile`, `rules`, `mute <rule>`,
`advise [--dry-run]` and `curate [--pack FILE] [--dry-run]`. Most take `--since 7d` and
`--project NAME`.

## How it's built

```
adapters/   read one tool's data into a common model    (swap or add sources here)
model.py    sessions, agent runs, time spans
metrics.py  flat metrics per session, subagent and day   (add a metric -> every rule can use it); flags scripted runs
rules/      best practices as YAML, with source + review date, and how practices moved, quoted
profile.py  your working style, derived from your data
trend.py    the past 7 days against the 7 before, for the report
preferences.py  your goals and deliberate style (~/.panopsis/profile.yaml)
advise.py   Claude reads the profile + findings + practices + team entries + current practice and writes advice, following up the last
curate.py   Claude checks a pack against its sources and watch list and proposes edits for a person to review
work.py     what you work on: languages, build and Claude Code setup per repo
features.py which Claude Code features your sessions use, so advice can suggest new ones
live.py     open sessions + transcript tails, as live state
read.py     the sessions needing you that you've already read (~/.panopsis/read.json)
feed.py     how activity reads: conversation blocks, markdown, folded tool calls, questions and answers
subagent_map.py  the zoom view's map: subagents in waves, nested under who launched them
tui.py      the app
driver.py   sessions panopsis runs itself (experimental)
autopilot.py  Haiku answering a turn that has ended for you, or leaving it to you (experimental)
tui_driven.py  their panes, request cards, new-session dialog, the / command menu and @ for your other sessions
textbox.py  the boxes you type in: a message over lines, and a Windows text box's editing keys
terminal.py the tab's name, panopsis's icon on its window or tab, and in Windows the keys its consoles don't pass on
icon.py     the icon, drawn for each size it's shown at
tui_settings.py  the settings screen (,)
keys.py     the keys you can change, their presets, and which key each has now
tui_keys.py the keys list (?), where Enter on a key changes it
commands.py Claude Code's slash commands for that menu, from the session's own list
usage.py    your plan's limits, from Claude Code's /usage and sessions' rate-limit events
config.py   app settings and spaces (~/.panopsis/config.yaml)
settings_file.py  your settings in one file, to set up panopsis on another PC, and nothing of this one
editor.py   the editors panopsis knows, and opening a file at its line or a folder in yours
analysis.py full-history analysis shared by the app's report screens
store.py    panopsis's own record of past sessions (~/.panopsis/history)
```

## Caveat

The adapter reads Claude Code's transcript files, whose format is internal and can change
in any release. The parser skips what it doesn't understand rather than failing. Each
session it has read is kept in panopsis's own format in `~/.panopsis/history`: times,
counts, titles and file paths, never the conversation, about 6 KB a session compressed.
Sessions read before a format change keep reading the same after it, and History and the
report reach back past Claude Code's 30 days. A hook plugin writing events in that format
could add to the adapter later; it can't replace it, since hooks don't carry Claude's
replies or token counts.

## License

[MIT](LICENSE): free to use, change and share, for anything, commercial or not. Keep the
copyright notice with copies. It comes with no warranty.
