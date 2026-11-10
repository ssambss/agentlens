"""agentlens command line."""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console

from agentlens import advise, curate, live, preferences, settings_file, timings
from agentlens import version as version_mod
from agentlens import config as config_mod, profile as profile_mod, trend as trend_mod
from agentlens.adapters.claude_transcripts import DEFAULT_ROOT, SessionCache, load_sessions
from agentlens.metrics import all_entities, baselines
from agentlens.model import Session
from agentlens.preferences import Preferences
from agentlens.report import render_report, render_session, render_sessions
from agentlens.rules import (ContextEntry, Rule, RuleError, cause, evaluate, load_context, load_rules, pack_dirs,
                             stale_rules, unknown_metrics)
from agentlens.store import SessionStore


@dataclass
class Context:
    prefs: Preferences
    sessions: list[Session]
    entities: dict[str, list[dict]]
    history: dict
    rules: list[Rule]
    context: list[ContextEntry]


def main(argv: list[str] | None = None) -> int:
    timings.mark("command line loaded")
    if hasattr(sys.stdout, "reconfigure") and not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    if not hasattr(args, "func"):
        # Plain `agentlens` opens the app; the subcommands are for scripting.
        listing = live.EarlyListing()  # before loading the app, so the two happen together
        from agentlens import tui

        tui.run(provider=listing)
        return 0
    console = Console()
    if args.experimental:
        if not config_mod.load().findings_and_advice:
            console.print("Report, profile, rules and advice are early versions, and off. Their switch is hidden: "
                          "Ctrl+D on the app's settings screen (,) shows it, under Experimental. Or put "
                          f"findings_and_advice: true in {config_mod.config_path()}.", highlight=False)
            return 1
    try:
        return args.func(args, console)
    except RuleError as e:
        console.print(f"[red]Rule pack error:[/] {e}")
        return 2


class _Version(argparse.Action):
    """--version: which agentlens this is, with the commit it's from and how old that is. Asked only
    when you ask: it runs git, which every launch needn't wait for."""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS,
                 help="show which agentlens this is, and how old, and exit"):
        super().__init__(option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        print(version_mod.describe())  # to stdout, as argparse's own version action does
        parser.exit()


def _parser() -> argparse.ArgumentParser:
    # Findings and advice are early versions, hidden until you turn them on: so are their commands in
    # the help, though one you run still says how to turn them on.
    findings_and_advice = config_mod.load().findings_and_advice
    parser = argparse.ArgumentParser(prog="agentlens", description=__doc__)
    parser.add_argument("--version", action=_Version)
    sub = parser.add_subparsers(required=False, metavar="command")

    def add(name: str, func, help: str, data: bool = True, experimental: bool = False) -> argparse.ArgumentParser:
        listed = {"help": help} if findings_and_advice or not experimental else {}  # without help: not listed
        p = sub.add_parser(name, description=help, **listed)
        p.set_defaults(func=func, experimental=experimental)
        if data:
            p.add_argument("--since", type=_parse_since, help="only sessions active since e.g. 7d, 48h or 2026-10-01")
            p.add_argument("--project", help="only projects whose name contains this text")
            p.add_argument("--rules", type=Path, action="append", default=[], help="extra rule pack dir or file")
            p.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="Claude Code projects directory")
        return p

    p = add("watch", cmd_watch, "Monitoring wall of every open Claude Code session on this computer, wherever it runs",
            data=False)
    p.add_argument("--interval", type=float, default=2.0, help="seconds between refreshes")
    p.add_argument("--once", action="store_true", help="print a one-off table instead of the full-screen view")
    p.add_argument("--project", help="only projects whose name contains this text")
    p.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="Claude Code projects directory")
    p = add("report", cmd_report, "Where your time went, your working style, and findings", experimental=True)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    add("sessions", cmd_sessions, "List sessions")
    p = add("session", cmd_session, "Show one session and its subagent tree")
    p.add_argument("id", help="session id or unique prefix")
    p = add("profile", cmd_profile, "Your derived working style and stated preferences", experimental=True)
    p.add_argument("--init", action="store_true", help="create the preferences file if missing")
    add("rules", cmd_rules, "List active rules and flag outdated ones", experimental=True)
    p = add("mute", cmd_mute, "Stop showing a rule", data=False, experimental=True)
    p.add_argument("rule_id")
    p.add_argument("--rules", type=Path, action="append", default=[], help="extra rule pack dir or file")
    p = add("advise", cmd_advise, "Personalized written recommendations from Claude", experimental=True)
    p.add_argument("--dry-run", action="store_true", help="print the prompt instead of running it")
    p.add_argument("--model", help="model alias for the advice run, e.g. sonnet")
    p = add("curate", cmd_curate, "Have Claude check a rule pack against its sources and propose edits", data=False,
            experimental=True)
    p.add_argument("--pack", type=Path, help="the pack file to check (default: the built-in core pack)")
    p.add_argument("--dry-run", action="store_true", help="print the prompt instead of running it")
    p.add_argument("--model", help="model alias for the run, e.g. opus")
    p = add("export", cmd_export, "Save your settings in a file, to set up agentlens on another PC", data=False)
    p.add_argument("file", nargs="?", type=Path,
                   help=f"the file to save them in, or a folder to save {settings_file.NAME} in "
                        f"(default: ~/{settings_file.NAME})")
    p = add("import", cmd_import, "Take the settings in a file exported on another PC, in place of yours",
            data=False)
    p.add_argument("file", type=Path, help=f"the file `agentlens export` saved, or the folder its {settings_file.NAME} "
                                           "is in")
    return parser


def _load(args) -> Context:
    prefs = preferences.load()
    if prefs.ignored:
        err = Console(stderr=True)  # stderr, so `report --json` output stays parseable
        err.print(f"[yellow]Ignoring values in {prefs.path} that aren't options:[/]", highlight=False)
        for value in prefs.ignored:
            err.print(f"[yellow]  {value}[/]", highlight=False)
    # With what agentlens kept: faster, and the sessions Claude Code has deleted are in it too.
    sessions = list(load_sessions(root=args.root, since=args.since, cache=SessionCache(SessionStore())))
    if args.project:
        needle = args.project.lower()
        sessions = [s for s in sessions if needle in s.project.lower()]
    entities = all_entities(sessions)
    dirs = pack_dirs(args.rules, prefs)
    return Context(prefs, sessions, entities, baselines(entities), load_rules(dirs), load_context(dirs))


def _require_sessions(ctx: Context, console: Console) -> bool:
    if ctx.sessions:
        return True
    console.print("No Claude Code sessions found for that filter.")
    return False


def cmd_watch(args, console: Console) -> int:
    try:
        if args.once:
            live.print_snapshot(console, project=args.project, root=args.root)
        else:
            listing = live.EarlyListing()
            from agentlens import tui

            tui.run(interval=args.interval, project=args.project, root=args.root, provider=listing)
    except KeyboardInterrupt:
        pass
    except RuntimeError as e:
        console.print(f"[red]watch failed:[/] {e}")
        return 1
    return 0


def cmd_report(args, console: Console) -> int:
    ctx = _load(args)
    if not _require_sessions(ctx, console):
        return 1
    prof = profile_mod.build(ctx.sessions, ctx.entities, ctx.prefs)
    findings, muted = evaluate(ctx.rules, ctx.entities, ctx.history, ctx.prefs)
    stale = stale_rules(ctx.rules)
    broken = unknown_metrics(ctx.rules, ctx.entities)
    trend = trend_mod.build(ctx.sessions, ctx.entities, findings)
    if args.json:
        payload = {
            "profile": prof,
            "trend": trend,
            "findings": [
                {"rule": f.rule.id, "severity": f.rule.severity, "message": f.message, "notes": f.notes,
                 "cause": cause(f), "entity": {"kind": f.entity["kind"], "id": f.entity["id"]}}
                for f in findings
            ],
            "muted": [{"rule": m.rule.id, "reason": m.reason} for m in muted],
            "stale_rules": [{"rule": r.id, "age_days": age} for r, age in stale],
            "unknown_metrics": [{"rule": r.id, "metrics": metrics} for r, metrics in broken],
            "entities": ctx.entities,
        }
        print(json.dumps(payload, default=_json_default, indent=2))
        return 0
    render_report(console, ctx.sessions, prof, findings, muted, stale, ctx.prefs.goals, broken=broken, trend=trend)
    return 0


def cmd_sessions(args, console: Console) -> int:
    ctx = _load(args)
    if not _require_sessions(ctx, console):
        return 1
    render_sessions(console, ctx.entities["session"])
    return 0


def cmd_session(args, console: Console) -> int:
    ctx = _load(args)
    matches = [s for s in ctx.sessions if s.id.startswith(args.id)]
    if not matches:
        console.print(f"No session id starts with {args.id!r}. `agentlens sessions` lists them.")
        return 1
    if len(matches) > 1:
        console.print(f"{len(matches)} session ids start with {args.id!r}; give a longer prefix:")
        for s in sorted(matches, key=lambda s: s.start, reverse=True):
            console.print(f"  {s.id}  {s.start.astimezone():%Y-%m-%d %H:%M}  {s.project}  {s.title}", highlight=False)
        return 1
    session = matches[0]
    sub_rows = {r["id"]: r for r in ctx.entities["subagent"] if r["session_id"] == session.id}
    findings, _ = evaluate(ctx.rules, ctx.entities, ctx.history, ctx.prefs)
    mine = [
        f for f in findings
        if f.entity["id"] == session.id or f.entity.get("session_id") == session.id
    ]
    render_session(console, session, sub_rows, mine)
    return 0


def cmd_profile(args, console: Console) -> int:
    if args.init:
        console.print(f"Preferences file: {preferences.ensure_file()}")
    ctx = _load(args)
    if not _require_sessions(ctx, console):
        return 1
    prof = profile_mod.build(ctx.sessions, ctx.entities, ctx.prefs)
    console.print("[bold]Derived from your sessions[/]")
    for s in prof.styles:
        console.print(f"  {s.dimension:<12} [bold]{s.value}[/]  [dim]{s.evidence}[/]")
    console.print("\n[bold]What you told agentlens[/]  [dim]" + str(ctx.prefs.path) + "[/]")
    if not ctx.prefs.path or not ctx.prefs.path.exists():
        console.print("  nothing yet: run `agentlens profile --init` and edit the file")
    else:
        console.print(f"  goals: {', '.join(ctx.prefs.goals) or '-'}")
        console.print(f"  style: {ctx.prefs.style or '-'}")
        console.print(f"  muted: {', '.join(ctx.prefs.mute) or '-'}")
    for m in prof.mismatches:
        console.print(f"[yellow]{m}[/]")
    return 0


def cmd_rules(args, console: Console) -> int:
    ctx = _load(args)
    stale = {r.id: age for r, age in stale_rules(ctx.rules)}
    broken = {r.id: metrics for r, metrics in unknown_metrics(ctx.rules, ctx.entities)}
    for rule in ctx.rules:
        flag = ""
        if rule.id in stale:
            flag = " [yellow](not reviewed in {})[/]".format(f"{stale[rule.id]}d" if stale[rule.id] else "ever")
        if rule.id in ctx.prefs.mute:
            flag += " [dim](muted)[/]"
        console.print(f"[bold]{rule.id}[/] [dim]{rule.pack} · {rule.applies_to} · reviewed {rule.reviewed}[/]{flag}")
        console.print(f"  {rule.title}", highlight=False)
        if rule.advice:
            console.print(f"  [dim]{rule.advice}[/]", highlight=False)
        if rule.id in broken:
            console.print(f"  [red]unknown metrics for {rule.applies_to}: {', '.join(broken[rule.id])}[/]")
    if ctx.context:
        console.print("\n[bold]Written down by teams[/] [dim](no metric detects these: advice gets them all and "
                      "uses the ones that fit your work)[/]")
    stale_context = {e.id: age for e, age in stale_rules(ctx.context)}
    for entry in ctx.context:
        flag = ""
        if entry.id in stale_context:
            age = stale_context[entry.id]
            flag = " [yellow](not reviewed in {})[/]".format(f"{age}d" if age else "ever")
        console.print(f"[bold]{entry.id}[/] [dim]{entry.pack} · reviewed {entry.reviewed}[/]{flag}")
        console.print(f"  {entry.title}", highlight=False)
        console.print(f"  [dim]for {entry.audience}[/]", highlight=False)
    console.print(f"\n[dim]Packs searched: {', '.join(str(d) for d in pack_dirs(args.rules, ctx.prefs))}[/]")
    return 0


def cmd_mute(args, console: Console) -> int:
    prefs = preferences.load()
    ids = [r.id for r in load_rules(pack_dirs(args.rules, prefs))]
    if args.rule_id not in ids:
        close = difflib.get_close_matches(args.rule_id, ids, n=3, cutoff=0.8)
        hint = f" Did you mean {' or '.join(close)}?" if close else " `agentlens rules` lists them."
        console.print(f"[red]No rule called {args.rule_id!r}.[/]{hint}", highlight=False)
        return 1
    if args.rule_id in prefs.mute:
        console.print(f"{args.rule_id} is already muted in {prefs.path}")
        return 0
    path = preferences.add_mute(args.rule_id)
    console.print(f"Muted {args.rule_id} in {path}")
    return 0


def cmd_advise(args, console: Console) -> int:
    ctx = _load(args)
    if not _require_sessions(ctx, console):
        return 1
    prof = profile_mod.build(ctx.sessions, ctx.entities, ctx.prefs)
    findings, _ = evaluate(ctx.rules, ctx.entities, ctx.history, ctx.prefs)
    prompt = advise.build_prompt(prof, ctx.prefs, findings, ctx.rules, ctx.sessions, previous=advise.latest(),
                                 context=ctx.context)
    if args.dry_run:
        print(prompt)
        return 0
    asking = "Asking Claude for personalized advice (it searches the web for current practice)"
    with console.status(asking + "...") as status:
        try:
            text = advise.run(prompt, args.model, on_step=lambda step: status.update(f"{asking}: {step}..."))
        except (RuntimeError, OSError) as e:
            console.print(f"[red]advise failed:[/] {e}")
            return 1
    from rich.markdown import Markdown

    console.print(Markdown(text))
    console.print(f"[dim]Saved in {advise.save(text)}; the next advice follows up on it.[/]")
    return 0


def cmd_curate(args, console: Console) -> int:
    pack = args.pack or curate.default_pack()
    if not pack.is_file():
        console.print(f"[red]No rule pack at {pack}[/]")
        return 1
    with console.status("Checking each practice's quotes against their pages..."):
        quotes = curate.check_quotes(pack)
    prompt = curate.build_prompt(pack, quotes=quotes)
    if args.dry_run:
        print(prompt)
        return 0
    missing = [q for q in quotes if q.found is not True]
    console.print(f"[dim]{len(quotes) - len(missing)} of {len(quotes)} quoted sources found on their pages.[/]")
    for line in curate.describe_quotes(missing):
        console.print(line, style="yellow", highlight=False, markup=False)
    asking = f"Asking Claude to check {pack.name} against its sources"
    with console.status(asking + " (reads several pages)...") as status:
        try:
            text = curate.run(prompt, args.model, on_step=lambda step: status.update(f"{asking}: {step}..."))
        except (RuntimeError, OSError) as e:
            console.print(f"[red]curate failed:[/] {e}")
            return 1
    from rich.markdown import Markdown

    console.print(Markdown(text))
    console.print(f"[dim]Saved in {curate.save(text, pack)}. Nothing in {pack.name} was changed.[/]")
    return 0


def cmd_export(args, console: Console) -> int:
    path = settings_file.in_folder((args.file or settings_file.default_path()).expanduser())
    if not settings_file.replaceable(path):
        console.print(f"[red]{path} is already there, and isn't agentlens settings.[/] Give another name, or "
                      "remove it first.", highlight=False)
        return 1
    try:
        settings_file.export(config_mod.load(), path)
    except OSError as e:
        console.print(f"[red]Couldn't save {path}: {e.strerror or e}[/]", highlight=False)
        return 1
    console.print(f"Saved your settings in {path}. On the other PC: agentlens import {path.name}, or Import on "
                  "the settings screen (,).", highlight=False)
    return 0


def cmd_import(args, console: Console) -> int:
    path = settings_file.in_folder(args.file.expanduser())
    try:
        exported = settings_file.read(path)
    except settings_file.SettingsFileError as e:
        console.print(f"[red]{e}[/]", highlight=False)
        return 1
    cfg = config_mod.load()
    changes = exported.changes(cfg)
    for name, value in changes.items():
        setattr(cfg, name, value)
    if changes:
        config_mod.save(cfg)
    changed = [*changes, *preferences.take(exported.preferences)]
    if not changed:
        console.print(f"Your settings are already those in {path}.", highlight=False)
        return 0
    console.print(f"Took the {settings_file.named(changed)} in {path}.", highlight=False)
    # An open agentlens saves the settings it has over these with the next one you change.
    console.print("[dim]If agentlens is open, quit it and start it again to have them.[/]", highlight=False)
    return 0


def _parse_since(value: str) -> datetime:
    """argparse type for --since, so bad input is a usage error rather than a traceback."""
    match = re.fullmatch(r"(\d+)([dh])", value.strip())
    if match:
        amount, unit = int(match[1]), match[2]
        return datetime.now(timezone.utc) - timedelta(**{"days" if unit == "d" else "hours": amount})
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected e.g. 7d, 48h or 2026-10-01, got {value!r}") from None


def _json_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if hasattr(obj, "__dataclass_fields__"):
        return {k: getattr(obj, k) for k in obj.__dataclass_fields__}
    return str(obj)


if __name__ == "__main__":
    sys.exit(main())
