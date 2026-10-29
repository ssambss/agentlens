"""Keep a rule pack current: Claude reads what the pack watches and proposes edits to it.

Practices with agents change faster than anyone re-reads a pack, and advice can only be
as current as the pack it states. This asks the user's own `claude` to check every rule
and every quoted source against the pack's `watch` list and the rules' own sources, and
to write down what it would change, each change resting on a quoted passage. A person
reviews that and edits the pack; nothing here writes to it.

Before asking, it fetches each page a practice quotes and checks the quote is on it, word
for word: a summary of a page can't confirm a quote, so that check is plain code.

It sends the pack file, the names of the metrics rules can use and the result of the
quote check. Nothing from sessions. The pages it fetches are the ones the pack cites.
"""

from __future__ import annotations

import re
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from agentlens import advise, preferences
from agentlens.metrics import all_entities
from agentlens.model import MODEL, WAITING_ON_YOU, AgentRun, Session, Span
from agentlens.rules import PRACTICE_STATUSES, builtin_pack_dir

SESSION_NAME = "agentlens curate"
STAMP = advise.STAMP
TIMEOUT = 1200  # seconds; it reads several pages
FETCH_TIMEOUT = 30
ELLIPSIS = "[...]"  # where a quote leaves words out
LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")  # [text](url) -> text
ESCAPE = re.compile(r"\\(.)")  # markdown's \& -> &
MARKUP = re.compile(r"[*`]")


@dataclass
class QuoteCheck:
    rule_id: str
    url: str
    says: str
    found: bool | None  # None: the page didn't load


def history_dir() -> Path:
    return preferences.home() / "curator"


def default_pack() -> Path:
    return builtin_pack_dir() / "core.yaml"


def metric_names() -> dict[str, list[str]]:
    """The metrics a rule's `when` can name, per entity kind, read off a made-up session."""
    t = datetime(2026, 1, 1).astimezone()
    main = AgentRun("main", "main", "", False, t, t + timedelta(minutes=2),
                    spans=[Span(MODEL, t, t + timedelta(minutes=1)),
                           Span(WAITING_ON_YOU, t + timedelta(minutes=1), t + timedelta(minutes=2))])
    sub = AgentRun("a1", "general-purpose", "", False, t, t + timedelta(minutes=1))
    session = Session("s", "p", "t", "cli", "1", main, subagents=[sub])
    return {kind: sorted(rows[0]) for kind, rows in all_entities([session]).items() if rows}


def fetch_page(url: str) -> str:
    if not url.startswith("https://"):
        raise ValueError(f"not an https url: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "agentlens"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        return response.read().decode("utf-8", "replace")


def plain(text: str) -> str:
    """Text as it reads, for comparing a quote with its page: no markup, one space between words."""
    return " ".join(MARKUP.sub("", ESCAPE.sub(r"\1", LINK.sub(r"\1", text))).split())


def quoted_in(says: str, page: str) -> bool:
    """Whether every part of `says` is on the page (already plain), in order. `[...]` marks words left out."""
    at = 0
    for part in (plain(p) for p in says.split(ELLIPSIS)):
        if not part:
            continue
        found = page.find(part, at)
        if found < 0:
            return False
        at = found + len(part)
    return True


def check_quotes(pack: Path, fetch: Callable[[str], str] | None = None) -> list[QuoteCheck]:
    """Each practice source's quote, looked up on its page as fetched (the url without its #anchor)."""
    fetch = fetch or fetch_page
    data = yaml.safe_load(pack.read_text(encoding="utf-8")) or {}
    pages: dict[str, str | None] = {}
    checks = []
    for rule in data.get("rules") or []:
        for source in (rule.get("practice") or {}).get("sources") or []:
            url, says = str(source.get("url", "")), str(source.get("says", ""))
            page_url = url.split("#")[0]
            if page_url not in pages:
                try:
                    pages[page_url] = plain(fetch(page_url))
                except (OSError, ValueError):
                    pages[page_url] = None
            page = pages[page_url]
            checks.append(QuoteCheck(rule.get("id", "?"), url, plain(says),
                                     None if page is None else quoted_in(says, page)))
    return checks


def describe_quotes(checks: list[QuoteCheck]) -> list[str]:
    label = {True: "found", False: "NOT FOUND", None: "page didn't load"}
    return [f'- [{c.rule_id}] {label[c.found]}: {c.url}: "{c.says}"' for c in checks]


def build_prompt(pack: Path, today: date | None = None, quotes: list[QuoteCheck] | None = None) -> str:
    text = pack.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    reviewed = [r.get("reviewed") for r in data.get("rules") or [] if r.get("reviewed")]
    oldest = min(str(d) for d in reviewed) if reviewed else "the start"
    lines = [
        "You keep a pack of rules about working with Claude Code current. Each rule detects a way",
        "of working in someone's session metrics and says what to do about it; some carry a",
        "`practice` saying how that way of working has moved, with quoted sources. Practices",
        "change fast, so check every rule against its sources and the pack's `watch` list, and",
        "propose edits for a person to review. Change no file yourself.",
        "",
        f"Today is {(today or date.today()).isoformat()}. The oldest `reviewed` date in the pack is {oldest}.",
        "",
        f"## The pack ({pack.name})",
        "",
        "```yaml",
        text.rstrip(),
        "```",
        "",
        "## Metrics a rule's `when` can name, per `applies_to`",
    ]
    lines += [f"- {kind}: {', '.join(names)}" for kind, names in metric_names().items()]
    if quotes:
        lines += ["", "## Quotes agentlens matched against their pages, word for word (markup and spacing aside)"]
        lines += describe_quotes(quotes)
    lines += [
        "",
        "## What to do",
        "1. Fetch every `watch` url, every rule's `source` and every practice source with WebFetch.",
        "   For a changelog, read the entries since the oldest `reviewed` date. For a blog index,",
        "   open the posts since then that are about working with agents. Prefer each page's raw",
        "   form where there is one (for code.claude.com, the .md url).",
        "2. For each rule, one verdict:",
        "   - **holds**: nothing since its `reviewed` date changes it. One line saying what you checked.",
        "   - **update**: something changed it, such as a default, a feature, or the guidance. Quote the",
        "     passage with its url and date, then give the replacement YAML for the fields that change,",
        "     `reviewed` set to today. A new `practice` or a changed one needs `status` (one of",
        f"     {', '.join(PRACTICE_STATUSES)}), `was`, `now`, `still_fits`, `available_from` where a",
        "     version applies, and `sources` with each `says` copied exactly from the page, `[...]`",
        "     where you leave words out. The next run checks each one against its page in code.",
        "   - **retire**: it no longer holds or can no longer happen. Quote why.",
        "3. For each quote above that agentlens didn't find, or whose page didn't load, find what the",
        "   page says now and give the replacement `says`, or say the rule has lost its source.",
        "4. New practices: only where a primary source (Claude Code's docs or changelog, or Anthropic)",
        "   says a way of working has moved, and only ones someone's session metrics could show. Give",
        "   each as a full YAML rule whose `when` names only the metrics listed above. When no listed",
        "   metric could detect it, describe the metric it would need instead of inventing a name.",
        "5. List any source you couldn't load, and the rules that rest on it.",
        "",
        "Write plain markdown in four sections: Rules, Quotes that changed, New practices, Couldn't",
        "check. Rest every claim that something changed on a passage you quote; mark anything else as",
        "your opinion. No prose beyond what each item needs.",
    ]
    return "\n".join(lines)


def save(text: str, pack: Path, at: datetime | None = None) -> Path:
    path = history_dir() / f"{(at or datetime.now()).astimezone():{STAMP}}-{pack.stem}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def run(prompt: str, model: str | None = None, on_step: Callable[[str], None] = lambda step: None) -> str:
    return advise.run(prompt, model, name=SESSION_NAME, timeout=TIMEOUT, on_step=on_step)
