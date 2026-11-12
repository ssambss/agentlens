"""Two or more of a goal's sessions debating: each one's reply goes to the others, word for word, round by round,
and the lead gets each side's last reply. No real CLI: the lead's tool calls are made here, as it would make them."""

from test_coordinator import Panes, said, tasks

from agentlens import coordinator
from agentlens.coordinator import BOARD, BOARD_NOTE, COORDINATOR, DEBATE, DEBATE_NOTE, NOTE, Coordinator

TITLES = ("Keep the cache", "Drop the cache", "Shrink the cache")


def reported(tmp_path, sides=2, **panes_kw):
    """A goal with `sides` tasks running, each of which has reported to the lead and waits on it."""
    panes = Panes(**panes_kw)
    c, goal = panes.coordinator(tmp_path)
    c.add_tasks(goal, tasks(*TITLES[:sides]))
    for n in range(1, sides + 1):
        c.start_task(goal, n)
        c.link(f"k{n}", goal, goal.task(n))
        c.turn_ended(f"k{n}", False, f"Task {n} looked at the cache.")
    return panes, c, goal


def to(panes, key):
    return [(text, note, by) for k, text, note, by in panes.sent if k == key]


def test_two_sessions_debate_round_by_round_and_the_lead_gets_both_last_replies(tmp_path):
    panes, c, goal = reported(tmp_path)
    assert said(c.debate(goal, [1, 2], 2, "Keep the cache or drop it?")).startswith("Tasks 1 and 2 are debating it")
    # Each is told what it debates and with whom, as the coordinator's message.
    brief, note, by = to(panes, "k1")[-1]
    assert brief.startswith("Keep the cache or drop it?") and "another of the goal's sessions: task 2 (Drop" in brief
    assert (note, by) == (NOTE, COORDINATOR) and "2 rounds after your openings" in brief
    assert "task 1 (Keep the cache)" in to(panes, "k2")[-1][0]
    assert not goal.task(1).waiting_on_lead and c.under_way(goal) == goal.tasks  # at work for the goal again
    assert "debating task 2: 2 rounds left" in c.board(goal) and c.label("k1").endswith("debating task 2 · coordinator")

    # Their openings: the first in is held until the other's is.
    lead_saw = len(to(panes, "lead"))
    assert c.turn_ended("k1", False, "Opening: keep it, it's 40 ms.") == (True, "debating: waiting on task 2")
    assert len(to(panes, "k2")) == 1  # nothing passed on yet
    assert c.under_way(goal) == goal.tasks  # the one waiting on the other still brings the lead back, through it
    assert c.turn_ended("k2", False, "Opening: drop it, it's stale.") == (True, "debating task 1")
    text, note, by = to(panes, "k1")[-1]
    assert (note, by) == (DEBATE_NOTE, DEBATE)
    assert text.startswith("Task 2, Drop the cache, answered:\n---\nOpening: drop it, it's stale.\n---")
    assert "1 more round after your reply to this" in text
    assert "Opening: keep it, it's 40 ms." in to(panes, "k2")[-1][0]

    # The last round says so.
    c.turn_ended("k2", False, "Stale only for a minute.")
    c.turn_ended("k1", False, "Invalidate it on deploy.")
    assert "Your reply to this is your last in the debate" in to(panes, "k1")[-1][0]
    assert len(to(panes, "lead")) == lead_saw  # none of it went to the lead

    # Both last replies in, the lead gets them, and only them.
    c.turn_ended("k1", False, "Final: keep it, invalidate on deploy.")
    assert c.turn_ended("k2", False, "Final: fine, with invalidation.") == (True, "the coordinator is reading this")
    text, note, by = to(panes, "lead")[-1]
    assert (note, by) == (BOARD_NOTE, BOARD)
    assert text.startswith("The debate between task 1 (Keep the cache) and task 2 (Drop the cache) is over: every "
                           "side has given its last reply. The user's autopilot level: autonomous.")
    assert "Final: keep it, invalidate on deploy." in text and "Final: fine, with invalidation." in text
    assert "Opening" not in text
    assert goal.task(1).waiting_on_lead and goal.task(2).waiting_on_lead and not goal.task(1).debate
    assert c.under_way(goal) == []
    # Over, they're the lead's to answer again.
    assert said(c.message_task(goal, 1, "Deliver it.")) == "Sent to task 1."


def test_three_debate_and_each_gets_the_others_replies(tmp_path):
    panes, c, goal = reported(tmp_path, sides=3)
    assert said(c.debate(goal, [1, 2, 3], 1, "What do we do with the cache?")).startswith("Tasks 1, 2 and 3 are")
    assert "2 other sessions of the goal: task 2 (Drop the cache) and task 3 (Shrink" in to(panes, "k1")[-1][0]
    assert c.label("k2").endswith("debating tasks 1 and 3 · coordinator")
    c.turn_ended("k1", False, "Keep it.")
    assert c.turn_ended("k2", False, "Drop it.") == (True, "debating: waiting on task 3")
    c.turn_ended("k3", False, "Shrink it.")
    text = to(panes, "k1")[-1][0]
    assert "Task 2, Drop the cache, answered:\n---\nDrop it.\n---" in text and "Shrink it." in text
    assert "Keep it." not in text  # not its own
    assert "Keep it." in to(panes, "k2")[-1][0] and "Keep it." in to(panes, "k3")[-1][0]


def test_one_the_others_convince_drops_out_and_the_rest_debate_on(tmp_path):
    panes, c, goal = reported(tmp_path, sides=3)
    c.debate(goal, [1, 2, 3], 3, "What do we do with the cache?")
    for n, said_it in ((1, "Keep it."), (2, "Drop it."), (3, "Shrink it.")):
        c.turn_ended(f"k{n}", False, said_it)
    c.turn_ended("k1", False, "Keep it, it's 40 ms.")
    c.turn_ended("k2", False, "Still drop it.")
    sent = len(panes.sent)
    assert c.turn_ended("k3", False, "**Agreed:** task 1 is right, shrinking saves too little.") == (
        True, "agreed: tasks 1 and 2 debate on")
    # Its reply goes to the others, marked, and it gets nothing more.
    assert "Task 3, Shrink the cache, agreed, and is out of the debate now:" in to(panes, "k1")[-1][0]
    assert {k for k, *_ in panes.sent[sent:]} == {"k1", "k2"}
    assert "agreed in its debate with tasks 1 and 2: out of it" in c.board(goal)
    k3_got = len(to(panes, "k3"))
    c.turn_ended("k1", False, "Keep it.")
    c.turn_ended("k2", False, "Drop it.")
    assert len(to(panes, "k3")) == k3_got
    assert "agreed, and is out" not in to(panes, "k1")[-1][0]  # this round's, from task 2 only
    # The last of the rest agreeing ends it: the lead gets each side's last reply, the one that agreed's too.
    c.turn_ended("k1", False, "Agreed: task 2 showed the staleness costs more.")
    c.turn_ended("k2", False, "Drop it, then.")
    text = to(panes, "lead")[-1][0]
    assert "is over: task 1 agreed, so only task 2 is left arguing." in text
    assert "Task 3's last reply, agreeing:\n---\n**Agreed:** task 1 is right" in text
    assert "Task 1's last reply, agreeing:" in text and "Task 2's last reply:\n---\nDrop it, then." in text
    assert all(t.waiting_on_lead and not t.debate for t in goal.tasks)


def test_it_stops_at_the_users_usage_limit(tmp_path):
    panes, c, goal = reported(tmp_path)
    c.debate(goal, [1, 2], 3, "Keep the cache or drop it?")
    panes.used = 0.85
    c.turn_ended("k1", False, "Keep it.")
    c.turn_ended("k2", False, "Drop it.")
    assert "the user has used 85% of their five-hour window" in to(panes, "lead")[-1][0]
    assert not goal.task(1).debate and not goal.task(2).debate


def test_you_writing_to_one_still_arguing_ends_it_and_the_lead_gets_the_others_replies(tmp_path):
    panes, c, goal = reported(tmp_path, sides=3)
    c.debate(goal, [1, 2, 3], 2, "What do we do with the cache?")
    c.turn_ended("k1", False, "Keep it.")
    assert c.turn_ended("k2", True, "As you said, I looked at the deploy.") is None  # yours
    text = to(panes, "lead")[-1][0]
    assert "is over: the user wrote to task 2 or stopped it" in text and "Task 1's last reply:\n---\nKeep it." in text
    assert "Task 3 is still on its turn" in text
    assert goal.task(1).waiting_on_lead and not goal.task(2).waiting_on_lead  # 2 is yours, not the lead's
    assert not any(t.debate for t in goal.tasks)


def test_one_that_agreed_can_leave_and_the_rest_go_on(tmp_path):
    panes, c, goal = reported(tmp_path, sides=3)
    c.debate(goal, [1, 2, 3], 3, "What do we do with the cache?")
    for n, said_it in ((1, "Keep it."), (2, "Drop it."), (3, "Agreed: keep it.")):
        c.turn_ended(f"k{n}", False, said_it)
    # The lead finishing the one that agreed gets its reply, and the debate goes on.
    result = said(c.finish_task(goal, 3, "convinced"))
    assert result.startswith("Task 3 had agreed in its debate with tasks 1 and 2, which goes on without it.")
    assert "Agreed: keep it." in result
    assert goal.task(1).debate and goal.task(1).debate == goal.task(2).debate and not goal.task(3).debate
    # You closing one that agreed is the same, without telling the lead.
    panes, c, goal = reported(tmp_path / "again", sides=3)
    c.debate(goal, [1, 2, 3], 3, "What do we do with the cache?")
    for n, said_it in ((1, "Keep it."), (2, "Drop it."), (3, "Agreed: keep it.")):
        c.turn_ended(f"k{n}", False, said_it)
    lead_saw = len(to(panes, "lead"))
    panes.closed.add("k3")
    c.left("k3", "was closed")
    assert len(to(panes, "lead")) == lead_saw and goal.task(1).debate and not goal.task(3).debate


def test_one_closed_mid_debate_ends_it_and_the_other_reports_to_the_lead_as_usual(tmp_path):
    panes, c, goal = reported(tmp_path)
    c.debate(goal, [1, 2], 2, "Keep the cache or drop it?")
    panes.closed.add("k2")
    c.left("k2", "was closed")
    text = to(panes, "lead")[-1][0]
    assert "is over: task 2 was closed." in text and "Task 1 is still on its turn" in text
    # The other's turn, when it ends, goes to the lead like any other.
    c.turn_ended("k1", False, "Keep it.")
    assert "Keep the cache (task 1) ended a turn" in to(panes, "lead")[-1][0]


def test_the_lead_messaging_or_finishing_one_still_arguing_ends_it(tmp_path):
    panes, c, goal = reported(tmp_path)
    c.debate(goal, [1, 2], 2, "Keep the cache or drop it?")
    c.turn_ended("k2", False, "Drop it.")
    result = said(c.message_task(goal, 1, "Stop there: the user decided."))
    assert result.startswith("The debate between task 1 (Keep the cache) and task 2 (Drop the cache) is over: you "
                             "messaged task 1.")
    assert "Task 2's last reply:\n---\nDrop it." in result and result.endswith("Sent to task 1.")
    assert goal.task(2).waiting_on_lead

    panes, c, goal = reported(tmp_path / "again")
    c.debate(goal, [1, 2], 2, "Keep the cache or drop it?")
    result = said(c.finish_task(goal, 2, "dropped"))
    assert "is over: you finished task 2." in result and "Task 1 is still on its turn" in result


def test_what_a_debate_needs(tmp_path):
    panes, c, goal = reported(tmp_path)
    assert "two or more of the goal's tasks" in said(c.debate(goal, [1, 1], 2, "Q"))
    assert "two or more of the goal's tasks" in said(c.debate(goal, [1, 9], 2, "Q"))
    assert "two or more of the goal's tasks" in said(c.debate(goal, None, 2, "Q"))
    assert "Give 1 to 3 rounds" in said(c.debate(goal, [1, 2], 4, "Q"))
    assert "Say what they're to debate" in said(c.debate(goal, [1, 2], 2, " "))
    c.message_task(goal, 2, "Look again.")
    assert "Task 2 is still on its turn" in said(c.debate(goal, [1, 2], 2, "Q"))
    c.turn_ended("k2", True, "Looked, as you asked.")  # yours now
    assert "Task 2 is the user's" in said(c.debate(goal, [1, 2], 2, "Q"))
    assert not goal.task(1).debate  # refused, nothing changed


def test_a_debate_survives_a_restart(tmp_path):
    panes, c, goal = reported(tmp_path)
    c.debate(goal, [1, 2], 2, "Keep the cache or drop it?")
    c.turn_ended("k1", False, "Keep it.")
    again = coordinator.load(tmp_path / "goals.json")[goal.id]
    assert (again.task(1).debate, again.task(1).rounds_left, again.task(1).held) == (1, 2, "Keep it.")
    assert again.task(2).debate == 1


def test_the_goal_done_ends_a_debate_under_way(tmp_path):
    panes, c, goal = reported(tmp_path)
    c.debate(goal, [1, 2], 2, "Keep the cache or drop it?")
    c.turn_ended("k1", False, "Keep it.")
    c.goal_done(goal, "Kept the cache")
    assert all(not t.debate and not t.held for t in goal.tasks)


def test_its_messages_read_back_from_the_transcript_as_the_debates():
    sent = f"Task 2, Drop the cache, answered:\n---\nDrop it.\n---\n\n{DEBATE_NOTE}"
    assert coordinator.said_by_it(sent) == (DEBATE, "Task 2, Drop the cache, answered:\n---\nDrop it.\n---")
    assert "debate" in coordinator.TOOLS and "- debate:" in coordinator.INSTRUCTIONS
    assert "two or more of your sessions" in coordinator.INSTRUCTIONS


def test_the_lead_has_the_tool(tmp_path):
    c, goal = Panes().coordinator(tmp_path)
    assert f"mcp__{coordinator.SERVER}__debate" in c.lead_options(goal)["allowed_tools"]
    assert isinstance(c, Coordinator)
