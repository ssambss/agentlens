import asyncio

from textual.screen import UPDATE_PERIOD

from panopsis.frames import FramePacer
from panopsis.tui import PanopsisApp
from panopsis.tui_keys import KeysScreen


class FakeTimer:
    def __init__(self, handle: asyncio.TimerHandle) -> None:
        self.handle = handle

    def stop(self) -> None:
        self.handle.cancel()


class FakeScreen:
    """What the pacer asks of a screen: its focus, what changed on it, timers, and painting."""

    def __init__(self) -> None:
        self.focused = None
        self._dirty_widgets: set = set()
        self.is_running = True
        self.frames = 0
        self.waits: list[float] = []

    def set_timer(self, delay, callback, name=None):
        self.waits.append(delay)
        return FakeTimer(asyncio.get_running_loop().call_later(delay, callback))

    def _on_timer_update(self) -> None:
        self.frames += 1
        self._dirty_widgets.clear()


def test_a_change_paints_at_once_unless_the_last_frame_was_just_now():
    async def go():
        screen = FakeScreen()
        pacer = FramePacer(screen)
        pacer.resume()
        await asyncio.sleep(0)
        assert screen.frames == 1 and screen.waits == []  # nothing painted for a while: at once
        pacer.resume()
        pacer.resume()  # however many changes come meanwhile, one frame a beat after the last
        await asyncio.sleep(0)
        assert screen.frames == 1 and len(screen.waits) == 1 and 0 < screen.waits[0] <= UPDATE_PERIOD
        await asyncio.sleep(UPDATE_PERIOD + 0.05)
        assert screen.frames == 2
    asyncio.run(go())


def test_a_change_to_what_has_the_focus_paints_at_once():
    async def go():
        screen = FakeScreen()
        pacer = FramePacer(screen)
        pacer.resume()
        await asyncio.sleep(0)
        pacer.resume()  # a streamed word, just after that frame: it waits for its beat
        screen.focused = box = object()
        screen._dirty_widgets.add(box)  # a key typed into the box
        pacer.resume()
        await asyncio.sleep(0)
        assert screen.frames == 2  # the key's frame didn't wait, and took the streamed word along
        await asyncio.sleep(UPDATE_PERIOD + 0.05)
        assert screen.frames == 2  # the beat's frame was called off: there was nothing left for it
    asyncio.run(go())


def test_a_paused_pacer_paints_nothing_until_resumed():
    async def go():
        screen = FakeScreen()
        pacer = FramePacer(screen)
        pacer.resume()
        pacer.pause()  # Textual pauses it as it lays the screen out itself
        await asyncio.sleep(UPDATE_PERIOD + 0.05)
        assert screen.frames == 0
        pacer.resume()
        await asyncio.sleep(0)
        assert screen.frames == 1
    asyncio.run(go())


def test_the_apps_screens_paint_through_the_pacer(tmp_path, monkeypatch):
    painted = []
    real_paint = FramePacer._paint
    monkeypatch.setattr(FramePacer, "_paint", lambda self: painted.append(type(self.screen).__name__) or real_paint(self))
    app = PanopsisApp(provider=lambda: [], root=tmp_path / "projects", interval=60)

    async def go():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.1)
            assert "WallScreen" in painted
            await pilot.press("question_mark")  # a dialog: the keys
            await pilot.pause(0.1)
            assert isinstance(app.screen, KeysScreen) and "KeysScreen" in painted
    asyncio.run(go())


def test_a_closed_screen_is_not_painted():
    async def go():
        screen = FakeScreen()
        pacer = FramePacer(screen)
        pacer.resume()
        screen.is_running = False
        await asyncio.sleep(0)
        assert screen.frames == 0
    asyncio.run(go())
