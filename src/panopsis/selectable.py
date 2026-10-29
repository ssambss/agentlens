"""Text on screen you can select with the mouse, copy with Ctrl+C, and click links in.

Textual can only select plain text, and the feeds are Rich renderables (markdown, cards,
highlighted code). So a feed is rendered to lines at the width it's shown at, and rendered
again when that width changes. Cells styled as frame (see feed.Framed) are left out of what
you copy, as is the space that pads each line out to the edge.

Each repaint draws only the rows on screen, from those lines. Textual's own text widgets lay
out and style every line again when they repaint, and a drag repaints on every move of the
mouse: on a long conversation, selecting lagged behind the pointer. Dragging the window edge
re-wraps a long conversation once the edge rests, rather than at every step of the drag.
"""

from __future__ import annotations

from functools import lru_cache
from time import monotonic

from rich.console import Console, ConsoleOptions, RenderableType
from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.color import Color
from textual.geometry import Size
from textual.selection import Selection
from textual.strip import Strip
from textual.style import Style as VisualStyle
from textual.timer import Timer
from textual.widget import Widget

from panopsis.feed import FRAME

GAP = ""  # stands in for a frame cell in the copy text, so offsets still line up
QUICK = 0.01  # seconds: text that re-wraps faster than this does so as you drag the window edge
SETTLE = 0.2  # seconds the edge rests before slower text re-wraps


def flatten(renderable: RenderableType, width: int, console: Console,
            options: ConsoleOptions) -> tuple[list[list[Segment]], str]:
    """The renderable as lines `width` cells wide, and its text with frame cells as GAP.

    The lines are plain segments, made into strips only as they're drawn. A Textual Strip
    carries seven caches of its own, and a streaming conversation is laid out again many times
    a second: kept as strips, its lines were most of what the garbage collector went over,
    and its full sweeps held up the keys you typed."""
    rendered = console.render_lines(renderable, options.update_width(width).update(height=None, highlight=False),
                                    pad=False)
    lines: list[list[Segment]] = []
    copy: list[str] = []
    for line in rendered:
        # An empty line still needs a segment: it carries where the line is, so a drag that
        # ends on a blank line selects up to it, not the whole feed.
        segments = [segment for segment in line if not segment.control] or [Segment("")]
        lines.append(segments)
        copy.append("".join(GAP * len(s.text) if s.style is not None and s.style.meta.get(FRAME) else s.text
                            for s in segments))
    return lines, "\n".join(copy)


def copyable(selected: str) -> str:
    """Selected text without frame: rows that are all frame go, trailing space is trimmed."""
    lines = [line for line in selected.split("\n") if not line or line.strip(GAP)]
    return "\n".join(line.replace(GAP, "").rstrip() for line in lines)


@lru_cache(maxsize=1024)
def selected_style(style: Style, selection: VisualStyle) -> Style:
    """A cell's style with the selection drawn over it, its colours blended as Textual does."""
    under = Color.from_rich_color(style.bgcolor) if style.bgcolor is not None else None
    background = selection.background
    if background is not None and under is not None:
        background = under + background
    foreground = selection.foreground
    return style + Style(bgcolor=background.rich_color if background is not None else None,
                         color=foreground.rich_color if foreground is not None and foreground.a else None)


def highlight(line: Strip, start: int, end: int, selection: VisualStyle) -> Strip:
    """The line with the characters from start to end (-1: the end of the line) selected."""
    segments: list[Segment] = []
    x = 0
    for segment in line:
        text, style, control = segment
        first, x = x, x + len(text)
        a = max(start - first, 0)
        b = len(text) if end == -1 else min(end - first, len(text))
        if control or a >= b:
            segments.append(segment)
            continue
        if a:
            segments.append(Segment(text[:a], style))
        segments.append(Segment(text[a:b], selected_style(style or Style(), selection)))
        if b < len(text):
            segments.append(Segment(text[b:], style))
    return Strip(segments, line.cell_length)


class Selectable(Widget):
    """Shows a Rich renderable as selectable text. With follow, its scrolling parent stays at
    the end as it grows, unless you've scrolled up."""

    DEFAULT_CSS = "Selectable { width: 1fr; height: auto; }"

    def __init__(self, *, follow: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.follow = follow
        self._source: RenderableType | None = None
        self._lines: list[list[Segment]] = []
        self._copy = ""
        self._width = 0
        self._held = False
        self._rewrap_seconds = 0.0  # the longest laying the text out at a new width has taken
        self._settle: Timer | None = None

    @property
    def text(self) -> str:
        """What's shown, a line per row, frame and all."""
        return "\n".join("".join(segment.text for segment in line) for line in self._lines)

    def show(self, renderable: RenderableType) -> None:
        self._source = renderable
        if self.text_selection is not None:
            self._held = True  # keep still while you have text selected in it; it catches up after
            return
        self._fit()

    def on_resize(self, event: events.Resize) -> None:
        if self.size.width == self._width:
            return
        if self._settle is not None:
            self._settle.stop()
            self._settle = None
        if self._width and self._rewrap_seconds > QUICK:
            # A long conversation takes longer to lay out again than a step of the drag does:
            # it keeps its old width until the edge rests, so the window keeps up meanwhile.
            self._settle = self.set_timer(SETTLE, self._settled)
        else:
            self._fit()

    def _settled(self) -> None:
        self._settle = None
        if self.size.width != self._width:
            self._fit()

    def selection_updated(self, selection: Selection | None) -> None:
        super().selection_updated(selection)
        if selection is None and self._held:
            self._held = False
            self._fit()

    def _fit(self) -> None:
        width = self.size.width
        if self._source is None or width <= 0:
            return  # not laid out yet; on_resize fits it
        parent = self.parent
        following = self.follow and parent is not None and parent.scroll_y >= parent.max_scroll_y - 1
        start = monotonic()
        lines, self._copy = flatten(self._source, width, self.app.console, self.app.console_options)
        if width != self._width:  # at the same width most of it was laid out before (feed.Rendered)
            self._rewrap_seconds = max(self._rewrap_seconds, monotonic() - start)
        taller = len(lines) != len(self._lines)
        self._lines, self._width = lines, width
        self.refresh(layout=taller)  # the same number of lines: no need to lay out the screen again
        if following:
            parent.call_after_refresh(parent.scroll_end, animate=False)

    def get_content_width(self, container: Size, viewport: Size) -> int:
        return max((Segment.get_line_length(line) for line in self._lines), default=0)

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return len(self._lines)

    def render_line(self, y: int) -> Strip:
        base = self.visual_style.rich_style
        if y >= len(self._lines):
            return Strip.blank(self.size.width, base)
        line = Strip(self._lines[y]).apply_style(base)
        selection = self.text_selection
        if selection is not None and (span := selection.get_span(y)) is not None:
            selected = VisualStyle.from_styles(self.screen.get_component_styles("screen--selection"))
            line = highlight(line, *span, selected)
        return line.apply_offsets(0, y)  # where each character is, for selecting

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        if not self._copy:
            return None
        return copyable(selection.extract(self._copy)), "\n"

    def on_click(self, event: events.Click) -> None:
        link = event.style.link
        if link and self.text_selection is None:  # a drag that ends on a link is a selection, not a click
            event.stop()
            self.app.open_link(link, self)  # type: ignore[attr-defined]
