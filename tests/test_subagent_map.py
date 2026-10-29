import io
from datetime import timedelta

from conftest import T0
from rich.console import Console

from agentlens.feed import Palette
from agentlens.live import LiveSubagent
from agentlens.model import DORMANT, MODEL, TOOL, Span
from agentlens.subagent_map import SubagentMap, _bar, tree_order, waves


def minutes(m):
    return T0 + timedelta(minutes=m)


def agent(name, start, end, parent="main", running=False, finished=True, spans=None):
    return LiveSubagent(name, name, None, minutes(end), started=minutes(start), parent=parent, running=running,
                        finished=finished, background=True,
                        spans=spans if spans is not None else [Span(TOOL, minutes(start), minutes(end))])


def test_overlapping_agents_form_one_wave_and_a_gap_starts_the_next():
    a, b, c, d = agent("a", 0, 5), agent("b", 4, 10), agent("c", 9, 12), agent("d", 30, 31)
    assert [[x.id for x in w] for w in waves([d, c, b, a], minutes(40))] == [["a", "b", "c"], ["d"]]
    # A running agent lasts until now, so one launched since overlaps it.
    e = agent("e", 50, 51, running=True, finished=False)
    assert len(waves([e, agent("f", 55, 56)], minutes(60))) == 1


def test_nested_agents_follow_their_parent():
    rows = tree_order([agent("a", 0, 9), agent("b", 1, 8), agent("a1", 2, 3, parent="a"),
                       agent("a1x", 2.5, 2.8, parent="a1"), agent("orphan", 4, 5, parent="gone")])
    assert [(depth, x.id) for depth, x in rows] == [(0, "a"), (1, "a1"), (2, "a1x"), (0, "b"), (0, "orphan")]


def test_bar_marks_working_idle_and_absent_time():
    lane = _bar(10, minutes(0), minutes(10), (minutes(2), minutes(8)), [(minutes(2), minutes(4))])
    assert lane == "  ━━────  "
    # Only the end of a long transcript was read: the rest is shown as unknown, not idle.
    lane = _bar(10, minutes(0), minutes(10), (minutes(0), minutes(10)), [(minutes(6), minutes(10))], minutes(6))
    assert lane == "┄┄┄┄┄┄━━━━"
    # Too short for a cell of its own: still one mark where it ran.
    assert _bar(10, minutes(0), minutes(10), (minutes(5), minutes(5)), []).strip() == "━"


def test_map_shows_waves_main_lane_and_quiet_agents():
    fan_out = [agent("Tyres", 0, 20), agent("Chassis", 0, 15), agent("Search", 1, 2, parent="Tyres")]
    stalled = agent("Model the car", 60, 70, finished=False,
                    spans=[Span(MODEL, minutes(60), minutes(62)), Span(DORMANT, minutes(62), minutes(70))])
    main = [Span(MODEL, minutes(0), minutes(1)), Span(TOOL, minutes(1), minutes(100))]
    console = Console(record=True, width=70, file=io.StringIO())
    console.print(SubagentMap([*fan_out, stalled], main, minutes(100), Palette()))
    out = console.export_text()
    assert "3 finished · 1 quiet · 2 waves" in out.splitlines()[0]
    assert "2 in parallel, 1 nested · 20m" in out
    assert "  └ ✓ Search" in out and "○ Model the car" in out
    assert out.count("main thread") == 2
    assert out.index("Model the car") < out.index("Tyres")  # newest wave first
    assert max(len(line) for line in out.splitlines()) <= 70


def test_no_subagents():
    console = Console(record=True, width=40, file=io.StringIO())
    console.print(SubagentMap([], [], minutes(0), Palette()))
    assert "No subagents" in console.export_text()
