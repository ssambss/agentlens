"""Personalized written recommendations, produced by Claude Code itself.

The deterministic rules catch known patterns; this step covers what they can't:
reading the user's profile as a whole, fitting advice to the kind of work they do and
the team they do it in, and looking past Claude Code's own features to how teams work
with agents now, searching the web for current practice newer than the rule packs. It
runs through the user's own `claude` CLI, so no API key is needed. It sends aggregate
metrics; the session, subagent and project names that findings mention; per project
the languages of files read, which build and Claude Code setup files the repo has and
recent session titles; the enabled plugins, the marketplace each came from and what
their skills, subagents and commands are for, from their descriptions (see work.py);
which Claude Code features the sessions used, the models they ran on and the Claude
Code versions (see features.py); the practices behind the findings, and the entries
team packs have written down, as the packs state them with their sources; and the last
advice Claude wrote. Never conversation text.

Each advice is kept in ~/.agentlens/advice/, so the next one can follow up on the last:
whether its tips were taken up, and whether the numbers moved since.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from agentlens import features as features_mod, preferences, profile as profile_mod, usage as usage_mod
from agentlens import work as work_mod
from agentlens.metrics import all_entities, day_metrics
from agentlens.model import Session
from agentlens.preferences import Preferences
from agentlens.profile import Profile
from agentlens.report import counted, fmt_min
from agentlens.rules import ContextEntry, Finding, Practice, Rule, version_tuple
from agentlens.terminal import claude_env

MAX_FINDINGS = 15
PRACTICE_EXAMPLES = 3  # findings quoted per practice
# How every item of advice reads: the change first, ready to make, then one line of why. The
# docs back it up with a link; how Claude Code works isn't what someone acts on.
ITEM_FORMAT = [
    "Every item is a change they can make to how they work this week, written as:",
    "",
    "### <the change, in one line, naming the project, file or command it's for>",
    "<the change itself, ready to use: the lines to add to a CLAUDE.md or a subagent's",
    "definition, the settings or hook snippet, or the prompt to type>",
    "- Where: one of their own setup (~/.claude), the repo (its CLAUDE.md or .claude/), their",
    "  team's plugin (a proposal for whoever maintains it), or CI or a tool outside Claude Code.",
    "- Why: one line, with their numbers.",
    "- Check first: one line on what to look at, only when it might not apply to them.",
    "- Source: a link and its date, never a quote.",
    "",
    "Fit each change to their setup above: don't suggest what they already have, change what",
    "they have instead. Where a plugin they have already does the job, point them at it.",
    "Explain how Claude Code works only as far as the one line of why needs.",
]
MAX_PROJECTS = 8
MIN_PROJECT_MIN = 5  # minutes of agent work; smaller projects (quick tests) are only counted
THIN_HISTORY = 20  # sessions; below this the usage numbers say little on their own
FOLLOWUP_MIN = 3  # sessions since the last advice before it can say whether that advice helped
FRESH_MONTHS = 12  # how old a source on current practice may be; agent practice moves faster than that
MAX_CONTEXT = 20  # team pack entries sent; advice picks the ones that fit
MAX_ITEMS = 6  # changes in one advice, whatever the steps add up to: a longer list goes unread
MAX_PACK_LINES = 3  # under "For the rule pack"
STAMP = "%Y-%m-%d-%H%M%S"  # advice file names, in local time
# The run's display name in `claude agents`, so the app can leave it off the grid.
SESSION_NAME = "agentlens advise"
WEB_TOOLS = "WebFetch,WebSearch"  # all it may use: --tools hides the rest, --allowedTools lets these run unasked


@dataclass
class Advice:
    at: datetime  # when Claude wrote it
    text: str
    path: Path


def history_dir() -> Path:
    return preferences.home() / "advice"


def save(text: str, at: datetime | None = None) -> Path:
    path = history_dir() / f"{(at or datetime.now()).astimezone():{STAMP}}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def latest() -> Advice | None:
    """The last advice Claude wrote. Before the history folder, that was a single advice.md."""
    dated = []
    for path in history_dir().glob("*.md"):
        try:
            dated.append((datetime.strptime(path.stem, STAMP).astimezone(), path))
        except ValueError:
            continue
    legacy = preferences.home() / "advice.md"
    if not dated and legacy.exists():
        dated.append((datetime.fromtimestamp(legacy.stat().st_mtime).astimezone(), legacy))
    if not dated:
        return None
    at, path = max(dated)
    return Advice(at, path.read_text(encoding="utf-8").strip(), path)


def build_prompt(profile: Profile, prefs: Preferences, findings: list[Finding], rules: list[Rule],
                 sessions: list[Session] | None = None, previous: Advice | None = None,
                 context: list[ContextEntry] | None = None) -> str:
    sessions = sessions or []
    lines = [
        "You are reviewing how one developer works with AI agents through Claude Code, based on",
        "metrics from their own session history. Give them personalized, practical advice that fits",
        "the kind of work they do and the team they do it in. Look past Claude Code's own features",
        "too: how teams building software with agents work now, and where that is moving.",
    ]
    if sessions:
        days = (max(s.main.end for s in sessions) - min(s.main.start for s in sessions)).days + 1
        lines += ["", f"History on this machine: {profile.sessions} sessions over {days} days."]
        if newest := newest_versions(sessions):
            last_used = {s.entrypoint: s.end for s in sorted(sessions, key=lambda s: s.end)}
            lines.append("Claude Code versions in their sessions, the newest per surface and when that surface was "
                         "last used (what's installed isn't known here, and may be newer): "
                         + ", ".join(f"{surface} {version} (last used {last_used[surface]:%Y-%m-%d})"
                                     for surface, version in newest.items()))
        projects = work_mod.build(sessions)
        shown = [w for w in projects if w.active_min >= MIN_PROJECT_MIN][:MAX_PROJECTS]
        lines += ["", "## What they work on (by agent time; languages from files read, setup from repo files)"]
        lines += [work_mod.describe(w) for w in shown]
        if len(projects) > len(shown):
            lines.append(f"- ...and {len(projects) - len(shown)} smaller projects")
        lines.append("- set up in their user folder, for every project: "
                     + (", ".join(work_mod.user_setup()) or "nothing"))
        if installed := work_mod.plugins():
            lines += ["", "## Plugins they have (what their team, company or others already provide, "
                          "from each plugin's descriptions)"]
            for plugin in installed:
                lines += work_mod.describe_plugin(plugin)
    lines += ["", "## Their working style (derived from data)"]
    lines += [f"- {s.dimension}: {s.value} ({s.evidence})" for s in profile.styles]
    lines += [f"- {label}: {fmt_min(m)}" for label, m in profile.time_split.items()]
    lines.append(f"- top tools: {', '.join(f'{n} {c}' for n, c in profile.top_tools)}")
    lines.append(f"- skills used: {', '.join(f'{n} {c}' for n, c in profile.skills.most_common()) or 'none'}")
    lines.append(f"- {counted(profile.sessions, 'session')} across {counted(profile.projects, 'project')}, "
                 f"median {fmt_min(profile.median_session_min)}")
    if sessions:
        lines += ["", "## Claude Code features in their sessions"]
        lines += features_mod.describe(features_mod.build(sessions))
    lines += ["", "## What they told us"]
    lines.append(f"- goals: {', '.join(prefs.goals) or 'not stated'}")
    lines.append(f"- deliberate style: {prefs.style or 'not stated'}")
    lines += [f"- mismatch: {m}" for m in profile.mismatches]
    lines += ["", "## Findings from the rule packs (most relevant first)"]
    for f in findings[:MAX_FINDINGS]:
        lines.append(f"- [{f.rule.id}] {f.rule.title}: {f.message}")
    if len(findings) > MAX_FINDINGS:
        lines.append(f"- ...and {len(findings) - MAX_FINDINGS} more")
    practices = practice_lines(findings)
    lines += practices
    team = context_lines(context or [])
    lines += team
    lines += ["", "## Rules in use (id, last reviewed)"]
    lines += [f"- {r.id}: {r.reviewed}" for r in rules]
    if previous:
        lines += followup(previous, sessions, findings)

    lines += ["", "## What to write", *ITEM_FORMAT, ""]
    steps = []
    if previous:
        steps.append([
            'Start with "Since last time": one line per change in their last advice, saying whether',
            "the numbers since show it made and whether it helped, citing them. With fewer than",
            f"{FOLLOWUP_MIN} sessions since, say in one line that it's too early to tell instead. A",
            "change not made can come back once, more concretely, if it still matters most.",
        ])
    if practices:
        steps.append([
            "Then the practices above that apply: one item each, built from its actions and fitted",
            "to their setup. Whether the versions they ran have the change goes in the why line.",
            "Where their history shows the case in which their way still fits, say so in one line",
            "instead of an item. Rest what changed on the sources given or on docs you fetched;",
            "mark anything else you believe about it as your opinion.",
        ])
    if team:
        steps.append([
            "Then the team entries above whose case their data shows: a plugin, skill, setup file,",
            "count or session title above that the entry is about, named in the why line. One item",
            "each, built from its actions and fitted to their setup, saying which team pack it's",
            "from. State the evidence no stronger than it is: two descriptions that overlap are an",
            "overlap, not a duplicate. Leave out, without mentioning them, the entries their data",
            "can't show, the ones that only might apply, and the ones their setup already follows.",
        ])
    learning = ": learning is one of their goals, so lean on these" if "learning" in prefs.goals else ""
    steps.append([
        f"Then {'2-4 more' if practices or team else '3-5'} items, most impactful first, for the kind of work above.",
        "Look past Claude Code's own features to how teams building software with agents work now",
        "and where that is moving: where checks run, how work is split between people and agents,",
        "how review keeps up with what agents produce, how a team shares what it learns. A large",
        "C++ system and a full-stack web app need different things (what goes in CLAUDE.md, which",
        "build and test commands to hand Claude or run from hooks, when to plan first, what to give",
        "subagents). Search the web for current practice, and check the current Claude Code docs",
        "(https://code.claude.com/docs) for features newer than the rules' review dates. Each item",
        "rests on a source, linked with its date where it has one. For where practice is moving,",
        f"use primary sources from the last {FRESH_MONTHS} months: the docs and changelogs of the tools",
        "involved, and engineering posts or research from teams describing their own work. A",
        "vendor's marketing post or a personal blog doesn't carry an item on its own. Mark",
        "anything else you believe as your opinion.",
        "Each item starts from something in their data above: a practice with nothing there to tie",
        "it to is not an item. Fit them to the stated goals and deliberate style; don't argue with a",
        "style they chose on purpose. Where a feature or practice they've never used fits their work,",
        f"an item can be trying it{learning}.",
        f"With fewer than {THIN_HISTORY} sessions, the usage numbers say little: say so in one line,",
        "and base the items mainly on the kind of work and current practice.",
    ])
    steps.append([
        'Last, under "For the rule pack" (for whoever keeps agentlens\'s rules, not for this',
        f"person), at most {MAX_PACK_LINES} lines: a rule above that looks outdated or wrong given current",
        "docs, or a practice a pack should carry. Each rests on a primary source: its link, its",
        "date and the passage that says so, quoted word for word, so the pack's quote check can",
        "confirm it.",
    ])
    for i, step in enumerate(steps, 1):
        lines.append(f"{i}. {step[0]}")
        lines += [f"   {line}" for line in step[1:]]
    aside = '"Since last time" and "For the rule pack"' if previous else '"For the rule pack"'
    lines.append(f"At most {MAX_ITEMS} items in all, {aside} aside. When the steps above give more,")
    lines.append("keep the ones with the strongest evidence in their data and drop the rest without")
    lines.append("mentioning them.")
    lines.append("State facts about their setup only where the data above says so. Where it doesn't, say")
    lines.append("you can't tell from here rather than guess.")
    words = 350 + (100 if previous else 0) + (150 if practices else 0) + (100 if team else 0)
    lines.append(f"Be brief: under {words} words. Plain markdown.")
    return "\n".join(lines)


def newest_versions(sessions: list[Session]) -> dict[str, str]:
    """The newest Claude Code version seen per surface (claude-vscode, sdk-cli...), newest first."""
    newest: dict[str, tuple[tuple[int, ...], str]] = {}
    for s in sessions:
        parsed = version_tuple(s.version)
        if parsed and (s.entrypoint not in newest or parsed > newest[s.entrypoint][0]):
            newest[s.entrypoint] = (parsed, s.version)
    ranked = sorted(newest.items(), key=lambda kv: kv[1][0], reverse=True)
    return {surface: version for surface, (_, version) in ranked}


def practice_lines(findings: list[Finding]) -> list[str]:
    """The practices behind the findings, stated from the pack: what moved, since when, and who says so."""
    grouped: dict[str, list[Finding]] = {}
    for f in findings:
        if f.rule.practice and f.rule.practice.status != "current":
            grouped.setdefault(f.rule.id, []).append(f)
    if not grouped:
        return []
    lines = ["", "## Practices that have moved (from the rule packs: state these, don't recall them)"]
    for rule_id, hits in grouped.items():
        rule, practice = hits[0].rule, hits[0].rule.practice
        lines += ["", f"### [{rule_id}] {rule.title}"]
        lines.append(f"- status: {practice.status}")
        lines.append(f"- seen {counted(len(hits), 'time')}; the ones that waited longest:")
        longest = sorted(hits, key=lambda f: f.entity.get("delegated_min") or 0, reverse=True)
        lines += [f"  - {f.message}" for f in longest[:PRACTICE_EXAMPLES]]
        lines += _version_lines(hits, practice.available_from)
        lines += _practice_body(practice)
    return lines


def context_lines(entries: list[ContextEntry]) -> list[str]:
    """What the user's team packs have written down, for advice to use where it fits their work."""
    if not entries:
        return []
    lines = ["", "## What their team has written down (from team packs: state these, don't recall them)"]
    for entry in entries[:MAX_CONTEXT]:
        lines += ["", f"### [{entry.id}] {entry.title} ({entry.pack} pack)", f"- for: {entry.audience}",
                  f"- status: {entry.practice.status}"]
        lines += _practice_body(entry.practice)
    if len(entries) > MAX_CONTEXT:
        lines += ["", f"...and {len(entries) - MAX_CONTEXT} more entries, not shown"]
    return lines


def _practice_body(practice: Practice) -> list[str]:
    """What moved and what to do now, as the pack states it, and the sources that say so."""
    lines = []
    for label, text in (("was", practice.was), ("now", practice.now), ("still fits", practice.still_fits)):
        if text:
            lines.append(f"- {label}: {text}")
    if practice.actions:
        lines.append("- what someone can change (fit these to their setup):")
        lines += [f"  - {action}" for action in practice.actions]
    lines.append("- sources:")
    lines += [f'  - {src.url}{f" ({src.date})" if src.date else ""}: "{src.says}"' for src in practice.sources]
    return lines


def _version_lines(hits: list[Finding], available_from: str) -> list[str]:
    seen = Counter(f"{f.entity.get('version') or '?'} ({f.entity.get('entrypoint') or '?'})" for f in hits)
    lines = ["- Claude Code they ran these on: " + ", ".join(f"{v} ×{n}" for v, n in seen.most_common())]
    since = version_tuple(available_from)
    if since:
        before = sum(1 for f in hits if (v := version_tuple(f.entity.get("version"))) and v < since)
        lines.append(f'- {before} of {len(hits)} ran before {available_from}, the first version where "now" is the default')
    return lines


def followup(previous: Advice, sessions: list[Session], findings: list[Finding],
             now: datetime | None = None) -> list[str]:
    """The last advice, and how the numbers moved since: what lets the next one say whether it helped."""
    days = ((now or datetime.now(timezone.utc)) - previous.at).days
    before = [s for s in sessions if s.start < previous.at]
    since = [s for s in sessions if s.start >= previous.at]
    lines = ["", f"## Their last advice, from {previous.at:%Y-%m-%d} "
                 f"({'today' if days < 1 else counted(days, 'day') + ' ago'})", ""]
    lines += [f"> {line}".rstrip() for line in previous.text.splitlines()]
    lines += ["", f"## Since then: {counted(len(since), 'session')}, against {len(before)} before"]
    if not before or not since:
        return lines
    earlier, lately = _summary(before), _summary(since)
    lines += [f"- {key}: {earlier[key]} before, {lately[key]} since" for key in earlier]
    lines += _rule_rates(findings, sessions, previous.at)
    used_before, used_since = features_mod.build(before).features, features_mod.build(since).features
    tried = [f"{name} ({used} of {len(since)} sessions)" for name, used in used_since.items()
             if used and not used_before[name]]
    if tried:
        lines.append(f"- first tried since: {', '.join(tried)}")
    return lines


def _summary(sessions: list[Session]) -> dict[str, str]:
    """The numbers advice usually aims to move, over one stretch of sessions."""
    prof = profile_mod.build(sessions, all_entities(sessions), Preferences())
    out = {s.dimension: s.value for s in prof.styles}
    present = sum(m for label, m in prof.time_split.items() if label != profile_mod.AWAY_LABEL) or 1.0
    for label in ("waiting on you", "blocked on subagents"):
        out[f"share of time {label}"] = f"{prof.time_split[label] / present:.0%}"
    out["subagents per session"] = f"{sum(len(s.subagents) for s in sessions) / len(sessions):.1f}"
    out["compactions per session"] = f"{prof.compaction_rate:.1f}"
    out["median session"] = fmt_min(prof.median_session_min)
    return out


def _rule_rates(findings: list[Finding], sessions: list[Session], at: datetime) -> list[str]:
    """How often each rule that fired did so, before the last advice and since."""
    totals = {"session": [0, 0], "subagent": [0, 0], "day": [0, 0]}
    for s in sessions:
        totals["session"][s.start >= at] += 1
        for run in s.subagents:
            totals["subagent"][run.start >= at] += 1
    for day in day_metrics(sessions):
        if (side := _side(day, at)) is not None:
            totals["day"][side] += 1
    hits: dict[str, list[int]] = {}
    rules: dict[str, Rule] = {}
    for f in findings:
        side = _side(f.entity, at)
        if side is not None and f.rule.applies_to in totals:
            hits.setdefault(f.rule.id, [0, 0])[side] += 1
            rules[f.rule.id] = f.rule
    lines = []
    for rule_id, (before, since) in hits.items():
        rule = rules[rule_id]
        n_before, n_since = totals[rule.applies_to]
        lines.append(f"- [{rule_id}] {rule.title}: {before} of {counted(n_before, rule.applies_to)} before"
                     f"{_share(before, n_before)}, "
                     + (f"{since} of {n_since} since{_share(since, n_since)}" if n_since else "none since yet"))
    return lines


def _side(entity: dict, at: datetime) -> int | None:
    """0 before the last advice, 1 since. None for the day it was written: that's some of each."""
    if isinstance(entity.get("start"), datetime):
        return int(entity["start"] >= at)
    if entity.get("kind") == "day":
        day, advised = date.fromisoformat(entity["date"]), at.astimezone().date()
        return None if day == advised else int(day > advised)
    return None


def _share(part: int, whole: int) -> str:
    return f" ({part / whole:.0%})" if whole else ""


def run(prompt: str, model: str | None = None, name: str = SESSION_NAME, timeout: int = 600,
        on_step: Callable[[str], None] = lambda step: None,
        on_rate_limit: Callable[[list[usage_mod.Window]], None] = lambda windows: None) -> str:
    """Ask the user's own `claude`, which may read the web and nothing else. `name` is how the
    run shows in `claude agents`, so the app can leave it off the wall. Its output streams in,
    so `on_step` hears what Claude is on as it starts it: "searching the web for …", "reading
    code.claude.com/docs/…", "thinking", "writing"."""
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("`claude` CLI not found on PATH")
    cmd = [claude, "-p", "--no-session-persistence", "-n", name, "--tools", WEB_TOOLS, "--allowedTools", WEB_TOOLS,
           "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    if model:
        cmd += ["--model", model]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", env=claude_env())
    killed = threading.Event()
    timer = threading.Timer(timeout, lambda: (killed.set(), proc.kill()))
    timer.start()
    errors: list[str] = []
    drain = threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True)  # or a full pipe stalls it
    drain.start()
    result: dict = {}
    last = ""
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
        for line in proc.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("type") == "result":
                result = message
            elif message.get("type") == "rate_limit_event":
                windows = usage_mod.from_rate_limit(_rate_limit_info(message.get("rate_limit_info")))
                if windows:
                    on_rate_limit(windows)
            elif (step := _step(message)) and step != last:
                last = step
                on_step(step)
        proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:  # something went wrong reading it: don't leave it running unread
            proc.kill()
    drain.join(timeout=1)
    if killed.is_set():
        raise subprocess.TimeoutExpired(cmd, timeout)
    if not result or result.get("is_error") or result.get("subtype") != "success":
        detail = str(result.get("result") or "").strip() or "".join(errors).strip()
        raise RuntimeError(detail or f"claude exited with {proc.returncode}")
    return str(result.get("result") or "").strip()


def _step(message: dict) -> str | None:
    """What a line of Claude's stream says it has started on, or None for one that doesn't."""
    if message.get("type") == "stream_event":
        block = (message.get("event") or {}).get("content_block") or {}
        return {"thinking": "thinking", "text": "writing"}.get(block.get("type"))
    if message.get("type") != "assistant":
        return None
    step = None
    for block in (message.get("message") or {}).get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        given = block.get("input") if isinstance(block.get("input"), dict) else {}
        if block.get("name") == "WebSearch" and given.get("query"):
            step = f"searching the web for {given['query']}"
        elif block.get("name") == "WebFetch" and given.get("url"):
            step = f"reading {_page(str(given['url']))}"
        else:
            step = f"using {block.get('name')}"
    return step


def _page(url: str) -> str:
    """code.claude.com/docs/en/hooks for https://code.claude.com/docs/en/hooks.md"""
    page = url.split("://", 1)[-1].split("#", 1)[0].split("?", 1)[0].rstrip("/")
    return page.removeprefix("www.").removesuffix(".md")


def _rate_limit_info(info) -> SimpleNamespace | None:
    """The stream's rate-limit info as the Agent SDK hands it to sessions agentlens runs."""
    if not isinstance(info, dict):
        return None
    return SimpleNamespace(raw=info, utilization=info.get("utilization"), rate_limit_type=info.get("rateLimitType"),
                           resets_at=info.get("resetsAt"), status=info.get("status"))
