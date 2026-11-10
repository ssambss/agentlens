# Findings and advice

What your history says about how you work with Claude Code, and advice from Claude on it.
These are early versions that nobody is working on now, so they're off, and their switch is
hidden: on the settings screen (`,`), Ctrl+D shows *Findings and advice*, under
Experimental. Or put `findings_and_advice: true` in `~/.agentlens/config.yaml`. Then `R`,
`A`, `P` and `L` open the report, advice, profile and rules screens, and the `report`,
`profile`, `rules`, `mute`, `advise` and `curate` commands run and show in
`agentlens --help`. Ctrl+D shows the switch again while they're on, to turn them off.

With them on, your history is read in the background too, once the wall is filled in, so
the report, profile, advice and rules screens open at once. Open one on history read more
than a minute before and it shows that at once, reads it again quietly, and shows it again
if anything changed. The settings screen also shows **What you're aiming for**: your goals
and a deliberate working style. Export takes those to another PC, with your muted rules and
thresholds.

## The report

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
   Claude Code. Each one is kept in `~/.agentlens/advice/`; the next starts by saying
   whether those tips were taken up and helped, and suggests what you haven't tried rather
   than what you already do.

None of this reaches Claude. Rules aren't enforced; findings and advice are for you to act
on, in your CLAUDE.md, your settings or how you work. Apart from the permission mode for
sessions you start in the app, and [autopilot](#autopilot) answering them once you turn it
on, agentlens never changes how Claude works.

**Rule packs.** Practices change faster than tools ship, so rules are YAML. Each records
its `source` and `reviewed` date, and rules older than the pack's `max_age_days` are
flagged. Add a team pack with `--rules DIR`, `AGENTLENS_RULES`, or `rule_packs:` in your
profile. A rule with the same id overrides the built-in one; `disabled: true` removes it.

**What a team has written down.** Much of what a team learns can't be measured from
sessions, such as where a skill's knowledge belongs or which writes need a person. A pack's
`context:` entries hold those: a title, who it's `for`, and a practice with its sources.
Advice gets them all and turns the ones that fit your work into items, saying which pack
they come from. `agentlens rules` lists them under the rules.

**Practices that moved.** `/insights` tells you how you use Claude Code's features. A
rule's `practice` says how a way of working has moved: what you do, what it was, what to
do now, from which Claude Code version, and when the old way still fits. Each claim is
backed by a source quoted word for word, such as a changelog entry or a docs passage.
Advice states these and checks them against the versions your sessions ran. It doesn't
recall them, so it can be no more current than the pack.

**Keeping a pack current.** `agentlens curate` has Claude read the pack's `watch` list and
every source its rules cite. It reports, rule by rule, whether each still holds, needs an
update (with the replacement YAML and the passage that says so) or should go. It also
lists new practices the sources support. Quotes are checked first in code against each
page as downloaded, because a model's summary of a page can't confirm a quote. Claude then
explains the ones that no longer match. The report is saved in `~/.agentlens/curator/` for
a person to review. The pack itself is never changed, so `reviewed` dates mean someone
checked. `--pack FILE` curates a team pack.

Autopilot's messages don't count as yours in the report, nor the seconds before them as
Claude waiting on you.

## What they send

When they're on, two more things go to Claude, through your own `claude`. For these `claude`
gets two tools, web search and web
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

  Never code or conversation text. `agentlens advise --dry-run` prints exactly what would
  be sent.
- **The curator** sends the rule pack it checks, the names of the metrics rules can use, and
  which of the pack's quotes it found on their pages. Nothing from your sessions. It checks
  the quotes in code by downloading each page the pack cites: plain HTTPS requests that send
  nothing. `agentlens curate --dry-run` prints what it would send.

## Commands

`report [--json]`, `profile`, `rules`, `mute <rule>`, `advise [--dry-run]` and
`curate [--pack FILE] [--dry-run]`. Most take `--since 7d` and `--project NAME`. While
findings and advice are off, they say how to turn them on and do nothing else.

## The code

```
rules.py, rules/  best practices as YAML, with source + review date, and how practices moved, quoted;
                  any metric in metrics.py is one a rule can use
report.py   the report, the session view and the rules, rendered for the CLI and the app's screens
analysis.py full-history analysis shared by the app's report screens
profile.py  your working style, derived from your data
trend.py    the past 7 days against the 7 before, for the report
preferences.py  your goals and deliberate style (~/.agentlens/profile.yaml), beside where agentlens keeps its files
advise.py   Claude reads the profile + findings + practices + team entries + current practice and writes advice, following up the last
curate.py   Claude checks a pack against its sources and watch list and proposes edits for a person to review
work.py     what you work on: languages, build and Claude Code setup per repo
features.py which Claude Code features your sessions use, so advice can suggest new ones
```

The settings screen's `findings-switch` widgets are the hidden switch, and
`keys.FINDINGS_AND_ADVICE` the keys the keys list leaves out while they're off.
