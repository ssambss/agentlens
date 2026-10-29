"""When a screen paints what changed on it.

Textual paints a screen's changes when its update timer ticks, at most 60 times a second. That
timer keeps to a fixed beat, and once it has missed beats (any time the screen was still for a
moment) it waits for the beat after next: 17 to 33 ms. While sessions stream, something on the
wall changes all the time, so a key you typed often waited that long for its frame, and typing
lagged. A paced screen paints at once when its last frame is a beat old, else a beat after it,
and a change to what has the focus at once whatever the beat.
"""

from __future__ import annotations

from asyncio import Handle, get_running_loop
from time import perf_counter

from textual.screen import UPDATE_PERIOD, Screen
from textual.timer import Timer


class FramePacer:
    """Stands in for a screen's update timer: Textual resumes it when something on the screen
    changed, and pauses it while it paints. It leans on Screen's insides as Textual 8.2 has
    them (_update_timer, _on_timer_update, _dirty_widgets): check them when Textual changes."""

    def __init__(self, screen: Screen) -> None:
        self.screen = screen
        self._last = 0.0  # when the last frame was painted
        self._timer: Timer | None = None  # a frame a beat after the last one
        self._soon: Handle | None = None  # a frame at once

    def resume(self) -> None:
        # What has the focus (the box you type in, the pane you move to) shows at once; the
        # rest, a streaming reply say, waits for its beat.
        yours = self.screen.focused in self.screen._dirty_widgets
        if self._soon is not None or (self._timer is not None and not yours):
            return  # a frame is on its way
        wait = 0.0 if yours else self._last + UPDATE_PERIOD - perf_counter()
        if wait > 0.001:
            self._timer = self.screen.set_timer(wait, self._paint, name="frame")
        else:
            self.pause()  # a frame waiting for its beat comes now instead
            self._soon = get_running_loop().call_soon(self._paint)

    def pause(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if self._soon is not None:
            self._soon.cancel()
            self._soon = None

    def _paint(self) -> None:
        self._timer = self._soon = None
        if not self.screen.is_running:
            return  # closed meanwhile
        self._last = perf_counter()
        try:
            self.screen._on_timer_update()
        except Exception as error:  # noqa: BLE001 - as Textual's timer reports it
            self.screen.app._handle_exception(error)


class PacedScreen(Screen):
    """A screen whose changes reach the terminal a frame after they happen, not two."""

    _pacer: FramePacer | None = None

    @property
    def _update_timer(self) -> FramePacer:  # type: ignore[override]
        if self._pacer is None:
            self._pacer = FramePacer(self)
        return self._pacer
