"""Rendering of reports as Rich renderables, shared by the CLI and the app's screens.
Everything here reads model/metrics/rules output; nothing computes.

The report shows evidence: what happened and how often, grouped by cause. Why a rule
matters is on the Rules screen, and what to do about it is advice's job."""

from __future__ import annotations

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from agentlens.model import ACTIVE_KINDS, AWAY, DELEGATED, DORMANT, WAITING_ON_BACKGROUND, WAITING_ON_YOU, Session
from agentlens.profile import AWAY_LABEL, Profile
from agentlens.rules import Finding, FindingGroup, Muted, Rule, group_findings
from agentlens.trend import Trend

SEVERITY_STYLE = {"warn": "bold yellow", "info": "cyan"}
PER_RULE = 3  # causes shown per rule before collapsing into "+N more"
SCRIPTED_PROJECTS = 3  # projects named in the header's line about scripted runs
NOUN = {"session": "session", "subagent": "run", "day": "day"}  # what a rule's findings count


def fmt_min(minutes: float) -> str:
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes:.0f}m"
    return f"{int(minutes // 60)}h {int(minutes % 60):02d}m"


def counted(n: float, noun: str) -> str:
    """'1 session', '3 sessions'."""
    return f"{n:g} {noun}{'' if n == 1 else 's'}"


def report_parts(
    sessions: list[Session],
    profile: Profile,
    findings: list[Finding],
    muted: list[Muted],
    stale: list[tuple[Rule, int | None]],
    goals: list[str],
    hints: bool = True,
    broken: list[tuple[Rule, list[str]]] | None = None,
    trend: Trend | None = None,
    footer: str = "",  # the app's line on where to go next, naming its keys
) -> list[RenderableType]:
    first = min(s.start for s in sessions).astimezone()
    last = max(s.end for s in sessions).astimezone()
    header = Text.from_markup(
        f"[bold]agentlens[/]  {counted(profile.sessions, 'session')} · {counted(profile.subagents, 'subagent')} · "
        f"{counted(profile.projects, 'project')} · {first:%Y-%m-%d} → {last:%Y-%m-%d}"
    )
    if profile.scripted:
        header.append(f"\n{scripted_line(profile)}", style="dim")
    parts: list[RenderableType] = [
        Panel.fit(header, border_style="blue"),
        time_split_panel(profile),
        *([trend_panel(trend)] if trend else []),
        style_panel(profile),
        *findings_parts(findings, goals),
    ]
    if muted:
        parts.append(Text("Muted: " + "; ".join(f"{m.rule.id} ({m.reason})" for m in muted), style="dim"))
    if stale:
        names = ", ".join(f"{r.id} ({'never' if age is None else f'{age}d ago'})" for r, age in stale)
        parts.append(Text.from_markup(f"[yellow]Possibly outdated rules, not reviewed recently:[/] {names}"))
    if broken:
        names = "; ".join(f"{r.id} ({', '.join(metrics)})" for r, metrics in broken)
        parts.append(Text.from_markup(f"[red]Rules that can never fire because they name unknown metrics:[/] {names}"))
    if footer:
        parts.append(Text(footer, style="dim"))
    if hints:
        parts.append(
            Text(
                "Next: `agentlens sessions` to list them, `agentlens session <id>` for one, "
                "`agentlens rules` for why each rule matters, `agentlens advise` for what to do about the "
                "findings, `agentlens mute <rule>` to silence a rule.",
                style="dim",
            )
        )
    return parts


def scripted_line(profile: Profile) -> str:
    """The runs left out: one prompt each, started by a script in bursts."""
    top = profile.scripted.most_common(SCRIPTED_PROJECTS)
    projects = ", ".join(f"{name} {n}" for name, n in top)
    rest = profile.scripted.most_common()[SCRIPTED_PROJECTS:]
    if rest:
        projects += f" and {sum(n for _, n in rest)} in {counted(len(rest), 'other project')}"
    return (f"Left out: {counted(sum(profile.scripted.values()), 'scripted run')} ({projects}), "
            "one prompt each, started in bursts")


def trend_panel(trend: Trend) -> Panel:
    recent, before = trend.recent, trend.before
    table = Table.grid(padding=(0, 2))
    table.add_column()
    table.add_column(justify="right")
    table.add_column(justify="right")
    table.add_row("", Text("past 7 days", style="bold"), Text("the 7 before", style="bold"))
    table.add_row("sessions", str(recent.sessions), str(before.sessions))
    shares = []
    for week in (recent, before):
        present = {k: v for k, v in week.time_split.items() if k != AWAY_LABEL}
        total = sum(present.values()) or 1
        shares.append({k: (v, v / total) for k, v in present.items()})
    for label in shares[0]:
        table.add_row(label, *(f"{fmt_min(m)}  {share:>3.0%}" for m, share in (week[label] for week in shares)))
    fired = sorted(set(recent.findings) | set(before.findings))
    if fired:
        table.add_row("", "", "")
        table.add_row(Text("findings", style="bold"), "", "")
        for rule_id in fired:
            table.add_row(rule_id, str(recent.findings.get(rule_id, 0)), str(before.findings.get(rule_id, 0)))
    return Panel(table, title="The past 7 days against the 7 before", title_align="left", border_style="dim")


def render_report(console: Console, *args, **kwargs) -> None:
    for part in report_parts(*args, **kwargs):
        console.print(part)


def time_split_panel(profile: Profile) -> Panel:
    present = {k: v for k, v in profile.time_split.items() if k != AWAY_LABEL}
    total = sum(present.values()) or 1
    table = Table.grid(padding=(0, 2))
    table.add_column(justify="left")
    table.add_column(justify="right")
    table.add_column(justify="right")
    table.add_column()
    for label, minutes in present.items():
        share = minutes / total
        table.add_row(label, fmt_min(minutes), f"{share:.0%}", Text("█" * round(share * 30), style="blue"))
    away = Text(
        f"\nNot counted: {fmt_min(profile.time_split.get(AWAY_LABEL, 0))} of long gaps "
        "(over 2h waiting on you or on background work, or a session closed or suspended mid-request).",
        style="dim",
    )
    return Panel(Group(table, away), title="Where main-thread time went", title_align="left", border_style="dim")


def style_panel(profile: Profile) -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_column(style="dim")
    for style in profile.styles:
        table.add_row(style.dimension, style.value, style.evidence)
    parts: list = [table]
    tools = ", ".join(f"{name} {count}" for name, count in profile.top_tools[:6])
    skills = ", ".join(f"{name} {count}" for name, count in profile.skills.most_common(4)) or "none"
    surfaces = ", ".join(f"{name} {count}" for name, count in profile.surfaces.most_common())
    parts.append(
        Text(
            f"\nTop tools: {tools}\nSkills used: {skills}\nWhere you run it: {surfaces}\n"
            f"Median session {fmt_min(profile.median_session_min)}, "
            f"{profile.compaction_rate:.1f} compactions per session",
            style="dim",
        )
    )
    for mismatch in profile.mismatches:
        parts.append(Text("\n" + mismatch, style="yellow"))
    return Panel(Group(*parts), title="Your working style", title_align="left", border_style="dim")


def findings_parts(findings: list[Finding], goals: list[str]) -> list[RenderableType]:
    if not findings:
        return [Text.from_markup("[green]No findings.[/] Nothing in the active rule packs matched your sessions.")]
    heading = "Findings" + (f" (ranked for your goals: {', '.join(goals)})" if goals else "")
    parts: list[RenderableType] = [Text(f"\n{heading}", style="bold")]
    for groups in group_findings(findings).values():
        rule = groups[0].rule
        lines = Text()
        lines.append(f" {rule.severity.upper()} ", style=SEVERITY_STYLE.get(rule.severity, ""))
        lines.append(" ")
        lines.append(rule.title, style="bold")
        lines.append(f"  [{rule.id}]", style="dim")
        for group in groups[:PER_RULE]:
            _group_lines(lines, group)
        rest = groups[PER_RULE:]
        if rest:
            names = ", ".join(g.cause for g in rest[:PER_RULE] if g.cause)
            more = "…" if len(rest) > PER_RULE else ""
            lines.append(f"\n   +{len(rest)} more{f': {names}{more}' if names else ''}", style="dim")
        if rule.source:
            lines.append(f"\n   source: {rule.source} · reviewed {rule.reviewed}", style="dim")
        lines.append("\n")
        parts.append(lines)
    return parts


def _group_lines(lines: Text, group: FindingGroup) -> None:
    """One cause: how often and how much in all, then its biggest case. A single case is just that."""
    biggest = group.findings[0]
    if len(group.findings) == 1:
        lines.append(f"\n   • {biggest.message}")
    else:
        noun = NOUN.get(group.rule.applies_to, "finding")
        summary = counted(len(group.findings), noun) + (f", {group.total}" if group.total else "")
        lines.append("\n   • ")
        if group.cause:
            lines.append(group.cause, style="bold")
            lines.append(": ")
        lines.append(summary)
        lines.append(f"\n     {'biggest' if group.total else 'e.g.'}: {biggest.message}", style="dim")
    for note in biggest.notes:
        lines.append(f"\n     {note}", style="dim")


def sessions_table(rows: list[dict]) -> Table:
    table = Table(title="Sessions", title_justify="left", header_style="bold")
    for col, justify in [
        ("id", "left"),
        ("started", "left"),
        ("project", "left"),
        ("title", "left"),
        ("wall", "right"),
        ("working", "right"),
        ("waiting on you", "right"),
        ("prompts", "right"),
        ("subagents", "right"),
    ]:
        table.add_column(col, justify=justify, no_wrap=col != "title")
    for r in sorted(rows, key=lambda r: r["start"], reverse=True):
        table.add_row(
            r["id"][:8],
            f"{r['start'].astimezone():%m-%d %H:%M}",
            r["project"],
            r["title"][:48],
            fmt_min(r["wall_min"]),
            fmt_min(r["active_min"]),
            fmt_min(r["waiting_on_you_min"]),
            str(r["human_prompts"]),
            str(r["subagents"]) if r["subagents"] else "",
        )
    return table


def render_sessions(console: Console, rows: list[dict]) -> None:
    console.print(sessions_table(rows))


def session_parts(session: Session, sub_rows: dict[str, dict], findings: list[Finding]) -> list[RenderableType]:
    main = session.main
    parts: list[RenderableType] = [
        Panel.fit(
            f"[bold]{session.title}[/]\n{session.project} · {session.id} · {session.entrypoint} · v{session.version}\n"
            f"{session.start.astimezone():%Y-%m-%d %H:%M} → {session.end.astimezone():%Y-%m-%d %H:%M}",
            border_style="blue",
        )
    ]
    split = [
        ("working", main.minutes(*ACTIVE_KINDS) - main.minutes(DELEGATED)),
        ("blocked on subagents", main.minutes(DELEGATED)),
        ("waiting on you", main.minutes(WAITING_ON_YOU)),
        ("waiting on background", main.minutes(WAITING_ON_BACKGROUND)),
        ("away", main.minutes(AWAY)),
    ]
    parts.append(Text.from_markup("  ·  ".join(f"{label} [bold]{fmt_min(m)}[/]" for label, m in split if m >= 0.5)))
    parts.append(
        Text(
            f"{counted(len(session.human_prompts), 'prompt')} · {counted(sum(main.tool_calls.values()), 'tool call')} · "
            f"{counted(session.compactions, 'compaction')} · skills: {', '.join(session.skills) or 'none'}"
        )
    )
    tree = Tree(Text.assemble(("main thread", "bold"), f"  {', '.join(sorted(main.models))}"))
    for run in sorted(session.subagents, key=lambda r: r.start):
        row = sub_rows[run.id]
        mode = "background" if run.background else "foreground"
        dormant = f", dormant {fmt_min(run.minutes(DORMANT))}" if run.minutes(DORMANT) >= 1 else ""
        label = Text.assemble(
            (row["description"], "bold"),
            (f"  {run.agent_type}, {mode}\n", "dim"),
            f"{run.start.astimezone():%H:%M} · worked {fmt_min(row['active_min'])}{dormant} · "
            f"{counted(row['tool_calls'], 'tool call')} · shared reads {row['shared_reads']} · hand-back {row['final_chars']} chars",
        )
        tree.add(label)
    parts.append(tree)
    if findings:
        lines = Text("\nFindings for this session", style="bold")
        for f in findings:
            lines.append(f"\n  {f.rule.title}", style=SEVERITY_STYLE.get(f.rule.severity, ""))
            lines.append(f": {f.message}")
        parts.append(lines)
    return parts


def render_session(console: Console, session: Session, sub_rows: dict[str, dict], findings: list[Finding]) -> None:
    for part in session_parts(session, sub_rows, findings):
        console.print(part)
