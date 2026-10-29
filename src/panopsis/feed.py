"""How a session's activity reads on screen: a conversation, not a log.

Your messages and Claude's are separate, spaced blocks; Claude's replies render as
markdown (headings, lists, tables, highlighted code); runs of tool calls fold into a
single muted summary line. The dense one-line-per-event log is still available as
"activity" mode for when you want to see every command.

Web addresses are links, and the margins around your messages and code blocks are marked
as frame, so copying text from the screen leaves them out (see selectable.py).
"""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar

from markdown_it import MarkdownIt
from markdown_it.token import Token
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.markdown import BlockQuote, CodeBlock, Heading, Markdown
from rich.measure import Measurement
from rich.padding import Padding
from rich.segment import Segment
from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text

from panopsis.model import Event

PANE_BLOCKS = 14  # conversation blocks kept in a pane; it scrolls to the newest
PANE_MESSAGE_LINES = 12  # a long reply is clipped in a pane; zoom shows all of it
PANE_PROMPT_LINES = 4
BLOCKS_KEPT = 4000  # rendered conversation blocks cached, across all panes and the zoom view
WIDTHS_KEPT = 4  # widths one block keeps a rendering for (pane, zoom, a resize or two)
FRAME = "panopsis-frame"  # style meta on cells that are layout, not text you'd copy
# Activity lines are cut at 240 characters; a web address there may be cut short too.
LINE_LIMIT = 240
WEB_URL = re.compile(r"https?://[^\s<>\"'`…]+", re.IGNORECASE)


class Rendered:
    """A renderable that remembers what it rendered to, per width.

    Panes repaint far more often than their conversation changes: moving between panes
    restyles two of them, a streamed word repaints one, the refresh timer touches them all.
    Laying out the same markdown again each time is what made the wall lag. What's wrapped
    here renders the same at a given width whatever the height, which is what makes this safe.
    """

    __slots__ = ("renderable", "_renders")

    def __init__(self, renderable: RenderableType) -> None:
        self.renderable = renderable
        self._renders: dict[tuple, list[Segment]] = {}

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        key = (options.max_width, options.justify, options.overflow, options.no_wrap, options.ascii_only,
               options.legacy_windows, options.is_terminal)
        segments = self._renders.get(key)
        if segments is None:
            if len(self._renders) >= WIDTHS_KEPT:
                self._renders.clear()
            segments = self._renders[key] = list(console.render(self.renderable, options))
        return segments

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        return Measurement.get(console, options, self.renderable)


_blocks: OrderedDict[tuple, Rendered] = OrderedDict()


def _cached(key: tuple, build: Callable[[], RenderableType]) -> Rendered:
    """Build and render a piece of a conversation once, for as long as it stays the same."""
    rendered = _blocks.get(key)
    if rendered is None:
        rendered = _blocks[key] = Rendered(build())
        if len(_blocks) > BLOCKS_KEPT:
            _blocks.popitem(last=False)
    else:
        _blocks.move_to_end(key)
    return rendered


def markdown(text: str, code_theme: str) -> Rendered:
    return _cached(("markdown", text, code_theme), lambda: ReadableMarkdown(text, code_theme=code_theme))


@dataclass(frozen=True)  # hashable: it's part of the key rendered blocks are cached under
class Palette:
    you: str = "#FEA62B"
    claude: str = "#0178D4"
    tool: str = "#7f8c98"
    error: str = "#B93C5B"
    notice: str = "#a371f7"
    card: str = "#242F38"  # background behind your messages
    code_theme: str = "monokai"


def palette_from(variables: dict[str, str], dark: bool) -> Palette:
    """Colours from the app's current theme, so light themes read as well as dark ones."""

    def pick(*names: str, default: str) -> str:
        for name in names:
            value = variables.get(name, "")
            if value.startswith("#"):
                return value[:7]
        return default

    base = Palette()
    # al-*: the colours from the settings file, when the app has them.
    return Palette(
        you=pick("al-you", "warning", "accent", default=base.you),
        claude=pick("al-claude", "primary", default=base.claude),
        tool=pick("al-tool", "foreground-darken-2", "secondary", default=base.tool),
        error=pick("al-error", "error", default=base.error),
        notice=pick("al-notice", "accent", default=base.notice),
        card=pick("al-card", "panel", "surface", default=base.card),
        code_theme="monokai" if dark else "friendly",
    )


@dataclass
class Block:
    kind: str  # you | claude | tools | notice | compact
    at: datetime
    text: str = ""
    events: list[Event] = field(default_factory=list)
    tag: str = ""


def to_blocks(events: list[Event]) -> list[Block]:
    blocks: list[Block] = []
    for e in events:
        if e.kind in ("tool", "error"):
            if blocks and blocks[-1].kind == "tools":
                blocks[-1].events.append(e)
            else:
                blocks.append(Block("tools", e.at, events=[e]))
        elif e.kind in ("you", "claude"):
            blocks.append(Block(e.kind, e.at, e.detail or e.text, tag=e.tag))
        else:
            blocks.append(Block(e.kind, e.at, e.text))
    return blocks


def tools_summary(events: list[Event], palette: Palette) -> Text:
    """`▸ Bash ×3 · Edit PLAN.md ×2 · Read tui.py`, plus an error count."""
    groups: OrderedDict[str, list[str]] = OrderedDict()
    errors = 0
    for e in events:
        if e.kind == "error":
            errors += 1
            continue
        name, _, target = e.text.partition(": ")
        groups.setdefault(name, []).append(target)
    parts = []
    for name, targets in groups.items():
        same = len(set(targets)) == 1 and targets[0]
        label = f"{name} {_tail(targets[0])}" if same else name
        parts.append(label + (f" ×{len(targets)}" if len(targets) > 1 else ""))
    text = Text("▸ ", style=palette.tool)
    text.append(" · ".join(parts) or "tool calls", style=palette.tool)
    if errors:
        text.append(f"   ✗ {errors} error{'s' if errors > 1 else ''}", style=f"bold {palette.error}")
    return text


def conversation(
    events: list[Event],
    palette: Palette,
    *,
    full: bool = False,
    clip: bool | None = None,  # clip long messages; by default only when not full
    show_times: bool = True,
    max_blocks: int = PANE_BLOCKS,
) -> RenderableType:
    clip = not full if clip is None else clip
    blocks = to_blocks(events)[-max_blocks:]
    if not blocks:
        return Text("No activity yet.", style="dim")
    out: list[RenderableType] = []
    for block in blocks:
        stamp = f"  {block.at.astimezone():%H:%M}" if show_times else ""
        events_key = tuple((e.kind, e.text) for e in block.events)
        key = (block.kind, block.text, block.tag, events_key, stamp, full, clip, palette)
        out.append(_cached(key, lambda: _render_block(block, palette, stamp, full, clip)))
        out.append(_GAP)
    return Group(*out[:-1])


_GAP = Text("")


def _render_block(block: Block, palette: Palette, stamp: str, full: bool, clip: bool) -> RenderableType:
    if block.kind == "you":
        body = _clip_lines(block.text, PANE_PROMPT_LINES) if clip else block.text
        label = Text.assemble(("You", f"bold {palette.you}"), (stamp, "dim"))
        if block.tag == "queued":
            label.append("  · sent while Claude was working", style="italic dim")
        card = Group(label, with_links(Text(body)))
        return Framed(card, (0, 1), f"on {palette.card}")
    if block.kind == "claude":
        body = _clip_lines(block.text, PANE_MESSAGE_LINES) if clip else block.text
        who = ("Claude Code", "bold dim") if block.tag == "command" else ("Claude", f"bold {palette.claude}")
        return Group(Text.assemble(who, (stamp, "dim")), markdown(body, palette.code_theme))
    if block.kind == "tools":
        if not full:
            return tools_summary(block.events, palette)
        lines = Text()
        for i, e in enumerate(block.events):
            style = palette.error if e.kind == "error" else palette.tool
            mark = "✗" if e.kind == "error" else "▸"
            lines.append(("\n" if i else "") + f"{mark} {e.text}", style=style)
        return Padding(lines, (0, 0, 0, 2))
    if block.kind == "notice":
        return Text.assemble(("◆ ", palette.notice), (block.text, palette.notice), (stamp, "dim"))
    return Text.assemble(("⟳ ", "dim"), (block.text, "dim"), (stamp, "dim"))


def activity_log(events: list[Event], palette: Palette, *, show_times: bool = True, limit: int = 80) -> Text:
    """The dense view: one line per event, repeats merged."""
    labels = {
        "you": ("you ", f"bold {palette.you}"),
        "claude": ("ai  ", f"bold {palette.claude}"),
        "tool": ("  ▸ ", palette.tool),
        "error": ("  ✗ ", palette.error),
        "notice": ("  ◆ ", palette.notice),
        "compact": ("  ⟳ ", "dim"),
    }
    text = Text()
    for i, (event, count) in enumerate(collapse_repeats(events)[-limit:]):
        if i:
            text.append("\n")
        if show_times:
            text.append(f"{event.at.astimezone():%H:%M} ", style="dim")
        label, style = labels.get(event.kind, ("    ", ""))
        text.append(label, style=style)
        line = Text(event.text, style=palette.tool if event.kind == "tool" else "")
        if event.kind in ("you", "claude") and len(event.text) < LINE_LIMIT:
            with_links(line)
        text.append_text(line)
        if count > 1:
            text.append(f"  ×{count}", style=f"bold {palette.tool}")
    return text


def collapse_repeats(events: list[Event]) -> list[tuple[Event, int]]:
    """Merge back-to-back identical tool calls (four Edits of one file) into one line."""
    out: list[tuple[Event, int]] = []
    for event in events:
        if out and event.kind == "tool" and out[-1][0].kind == "tool" and out[-1][0].text == event.text:
            out[-1] = (out[-1][0], out[-1][1] + 1)
        else:
            out.append((event, 1))
    return out


def frame_style(style: Style) -> Style:
    return style + Style(meta={FRAME: True})


class Framed:
    """Margins around a block (your message card, a code block) that copying leaves out."""

    def __init__(self, renderable: RenderableType, pad: tuple[int, int], style: str | Style = "") -> None:
        self.renderable = renderable
        self.vertical, self.horizontal = pad
        self.style = style

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        style = console.get_style(self.style) if isinstance(self.style, str) else self.style
        frame = frame_style(style)
        width = options.max_width
        inner = max(1, width - 2 * self.horizontal)
        lines = console.render_lines(self.renderable, options.update_width(inner), style=style, pad=True)
        side = Segment(" " * self.horizontal, frame)
        blank = Segment(" " * width, frame)
        new_line = Segment.line()
        for _ in range(self.vertical):
            yield from (blank, new_line)
        for line in lines:
            yield side
            yield from line
            yield from (side, new_line)
        for _ in range(self.vertical):
            yield from (blank, new_line)


def with_links(text: Text) -> Text:
    """Make the web addresses in plain text clickable."""
    for match in WEB_URL.finditer(text.plain):
        url = _trim_url(match.group())
        text.stylize(Style(link=url, underline=True), match.start(), match.start() + len(url))
    return text


def _trim_url(url: str) -> str:
    """Leave off the punctuation a sentence puts after an address: `(see https://x.org).`"""
    while url:
        if url[-1] in ".,;:!?*_~":
            url = url[:-1]
        elif url[-1] in ")]}" and url.count(url[-1]) > url.count({")": "(", "]": "[", "}": "{"}[url[-1]]):
            url = url[:-1]
        else:
            return url
    return url


_PARSER = MarkdownIt("commonmark", {"linkify": True}).enable(["strikethrough", "table", "linkify"])
_PARSER.linkify.set({"fuzzy_link": False, "fuzzy_email": False})  # README.md and tui.py aren't web addresses


def _link_code_urls(tokens: list[Token]) -> list[Token]:
    """An address in backticks (`http://localhost:3000`) is still one you'd want to click."""
    for token in tokens:
        if token.type != "inline" or not token.children:
            continue
        children, in_link = [], 0
        for child in token.children:
            in_link += {"link_open": 1, "link_close": -1}.get(child.type, 0)
            if child.type == "code_inline" and not in_link and re.fullmatch(r"https?://\S+", child.content, re.I):
                children += [Token("link_open", "a", 1, attrs={"href": child.content}), child,
                             Token("link_close", "a", -1)]
            else:
                children.append(child)
        token.children = children
    return tokens


class LeftHeading(Heading):
    """Rich centres top-level headings; in a pane that reads as a banner, not a heading."""

    LEVEL_ALIGN: ClassVar[dict] = {f"h{i}": "left" for i in range(1, 7)}


class FramedCodeBlock(CodeBlock):
    """Rich's code block, with its padding marked as frame: copied code has no stray indent."""

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        code = str(self.text).rstrip()
        syntax = Syntax(code, self.lexer_name, theme=self.theme, word_wrap=True, padding=0)
        yield Framed(syntax, (1, 1), Syntax.get_theme(self.theme).get_background_style())


class FramedQuote(BlockQuote):
    """Rich's block quote, with the bar down its side marked as frame."""

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        lines = console.render_lines(self.elements, options.update(width=options.max_width - 4), style=self.style)
        bar = Segment("▌ ", frame_style(console.get_style(self.style)))
        for line in lines:
            yield bar
            yield from line
            yield Segment.line()


class ReadableMarkdown(Markdown):
    elements: ClassVar[dict] = {
        **Markdown.elements,
        "heading_open": LeftHeading,
        "fence": FramedCodeBlock,
        "code_block": FramedCodeBlock,
        "blockquote_open": FramedQuote,
    }

    def __init__(self, markup: str, **kwargs) -> None:
        super().__init__("", **kwargs)
        # Parsed here rather than by Rich, so web addresses in the text become links.
        self.markup = markup
        self.parsed = _link_code_urls(_PARSER.parse(markup))


def _clip_lines(text: str, max_lines: int) -> str:
    lines = text.strip().splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    clipped = lines[:max_lines]
    # Don't leave a code fence open, or the rest of the pane renders as code.
    if sum(1 for ln in clipped if ln.lstrip().startswith("```")) % 2:
        clipped.append("```")
    return "\n".join(clipped) + "\n\n*… Enter to read the rest*"


def _tail(target: str) -> str:
    """Last path part of a file target, kept short for summaries."""
    if "/" in target and " " not in target:
        return target.rsplit("/", 1)[-1]
    return target if len(target) <= 40 else target[:39] + "…"
