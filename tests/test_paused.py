"""The tools a session agentlens runs has for your sessions paused here: Claude Code lists only the ones
running, so Claude couldn't find one paused unless you named it with @. No real CLI: the tools' calls are
made here, as Claude would make them."""

import asyncio
from datetime import datetime, timedelta, timezone

from conftest import common, human, text as said_by_claude, write_jsonl
from test_driver import app_with_session, started, until

from agentlens import coordinator, paused as paused_mod
from agentlens.driver import WAITING
from agentlens.model import APPROVED, AUTOPILOT, Event
from agentlens.paused import Paused

AT = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
ICON = Paused("d-1", "Taskbar icon", "Taskbar icon", "C:/work/repo", "icon-size", AT)
LINKS = Paused("d-2", "tubeless-d4", "Shared links", "C:/work/tubeless", last=AT + timedelta(days=1))


def said(result):
    return result["content"][0]["text"]


def tools(sessions, events=None, start=None):
    """The three tools over these sessions, and the keys they started."""
    asked = []

    async def started_one(key):
        asked.append(key)
        return start(key) if start else (True, next(s.name for s in sessions if s.key == key))

    found = {t.name: t for t in paused_mod.tools(lambda: list(sessions), lambda key: (events or {}).get(key, []),
                                                 started_one)}
    return found, asked


def call(tool, **args):
    return asyncio.run(tool.handler(args))


def test_the_list_gives_each_paused_sessions_title_folder_and_when_it_was_last_at_work():
    found, _ = tools([ICON, LINKS])
    listed = said(call(found[paused_mod.LIST]))
    assert listed.index('"Shared links"') < listed.index('"Taskbar icon"')  # the last at work first
    assert "C:/work/repo, worktree icon-size" in listed
    assert f"last at work {paused_mod.when(AT)}" in listed  # in your time, with its weekday
    # By its name too, where Claude Code lists it by another than its title once it runs.
    assert "ListAgents lists it as tubeless-d4 once it runs" in listed
    assert "lists it as Taskbar icon" not in listed
    assert "None of the user's sessions is paused" in said(call(tools([])[0][paused_mod.LIST]))


def test_reading_a_paused_session_gives_its_conversation_without_starting_it():
    events = {"d-1": [
        Event(AT, "you", "make the icon bigger", "make the icon bigger"),
        Event(AT, "tool", "Bash: magick icon.png"),  # its tools aren't its conversation
        Event(AT, "plan", "Resize it", "Resize it to 32 px."),
        Event(AT, "answer", "", tag=APPROVED),
        Event(AT, "claude", "Done.", "Done: it's 32 px now, as decided."),
        Event(AT, "you", "check the taskbar", "check the taskbar", tag=AUTOPILOT),
    ]}
    found, started_keys = tools([ICON, LINKS], events)
    read = said(call(found[paused_mod.READ], session='@"taskbar icon"'))  # as a mention, in any case
    assert read.startswith('"Taskbar icon", paused in agentlens, in C:/work/repo: its last 5 messages, oldest first.')
    assert read.index("The user:\nmake the icon bigger") < read.index("Claude's plan:\nResize it to 32 px.")
    assert "The user approved the plan" in read and "Claude:\nDone: it's 32 px now, as decided." in read
    assert "agentlens's autopilot, for the user:\ncheck the taskbar" in read
    assert "magick" not in read and started_keys == []
    # Fewer, when Claude asks for fewer: the newest.
    two = said(call(found[paused_mod.READ], session="Taskbar icon", messages=2))
    assert "its last 2 messages, oldest first (3 earlier not shown)" in two and "make the icon bigger" not in two
    assert "has none of its conversation" in said(call(found[paused_mod.READ], session="Shared links"))


def test_a_long_conversation_is_read_from_its_end():
    long = "start " + "x" * 9000 + " the end"
    events = {"d-1": [Event(AT, "you", f"message {i}", f"message {i}") for i in range(40)]
              + [Event(AT, "claude", "long", long)]}
    found, _ = tools([ICON], events)
    read = said(call(found[paused_mod.READ], session="Taskbar icon", messages=40))
    assert "Claude:\n…" in read and read.endswith(" the end") and "start x" not in read  # a long one, its end
    assert len(read) < paused_mod.READ_CHARS + 500 and "message 39" in read and "message 0\n" not in read


def test_starting_one_says_the_name_to_message_it_by_or_why_it_couldnt():
    found, started_keys = tools([ICON, LINKS], start=lambda key: (True, "Taskbar icon") if key == "d-1"
                                else (False, "its folder is gone"))
    ok = call(found[paused_mod.START], session="icon")  # in its title, and no other's
    assert not ok.get("is_error") and started_keys == ["d-1"]
    assert said(ok) == ('"Taskbar icon" is running now, with nothing to do until it hears from you: message it '
                        'with SendMessage, to "Taskbar icon".')
    failed = call(found[paused_mod.START], session="tubeless-d4")  # by its name
    assert failed["is_error"] and said(failed) == 'agentlens couldn\'t start "Shared links": its folder is gone'


def test_a_session_it_names_wrong_or_that_more_than_one_has_is_told_apart():
    twin = Paused("d-3", "Taskbar icon", "Taskbar icon", "C:/work/other")
    found, started_keys = tools([ICON, LINKS, twin])
    missing = call(found[paused_mod.READ], session="Release  notes")
    assert missing["is_error"] and said(missing).startswith('No session paused in agentlens goes by "Release notes". '
                                                            'Paused now: "Taskbar icon" (id d-1), "Shared links", ')
    assert said(missing).endswith("One that's running is in ListAgents.")
    both = call(found[paused_mod.START], session="Taskbar icon")
    assert both["is_error"] and "(id d-1), in C:/work/repo" in said(both) and "(id d-3), in C:/work/other" in said(both)
    assert said(both).endswith("Give one's whole title, or its id.") and started_keys == []
    assert "(id d-1)" in said(call(found[paused_mod.LIST]))  # the list says which is which
    assert "in C:/work/other" in said(call(found[paused_mod.READ], session="d-3"))
    vague = call(tools([ICON, LINKS])[0][paused_mod.READ], session="s")  # in both titles, which differ
    assert vague["is_error"] and said(vague).endswith("Give one's whole title.")
    assert call(found[paused_mod.READ], session="")["is_error"]


def test_the_server_tells_claude_when_to_use_them_and_the_delegate_tool_goes_beside_them():
    async def start(key):
        return True, key

    plain = paused_mod.options(list, lambda key: [], start)
    assert plain["allowed_tools"] == paused_mod.TOOL_NAMES  # allowed without asking: they change nothing of yours
    server = plain["mcp_servers"][paused_mod.SERVER]["instance"]
    assert server.instructions == paused_mod.INSTRUCTIONS
    assert server.create_initialization_options().instructions == paused_mod.INSTRUCTIONS  # what Claude Code gets
    assert "ListAgents doesn't list them" in paused_mod.INSTRUCTIONS
    with_coordinator = paused_mod.options(list, lambda key: [], start, hand_over=lambda goal, plan: (True, ""))
    assert with_coordinator["allowed_tools"] == [*paused_mod.TOOL_NAMES, coordinator.DELEGATE_TOOL]


def test_a_session_finds_one_paused_here_reads_it_and_starts_it_to_message_it(projects, tmp_path):
    app, folder, fakes = app_with_session(projects, tmp_path)
    write_jsonl(projects / "repo" / "s-old.jsonl", common([
        human(0, "make the taskbar icon bigger"), said_by_claude(1, "Done: it's 32 px now."),
        {"type": "ai-title", "aiTitle": "Taskbar icon"}], "s-old", cwd=str(folder)))

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            paused = app.open_driven(str(folder), resume="s-old", start=False)  # as after a restart
            driven = await started(app, pilot, folder)
            driven.title = "Fix the build"
            options = fakes[0].options
            assert set(paused_mod.TOOL_NAMES) <= set(options.allowed_tools)
            assert options.mcp_servers[paused_mod.SERVER]["instance"].instructions == paused_mod.INSTRUCTIONS
            assert coordinator.DELEGATE_TOOL not in options.allowed_tools  # the coordinator is off
            [found] = app._paused_for_claude(driven.key)
            assert (found.key, found.title, found.name, found.cwd) == (paused.key, "Taskbar icon", "Taskbar icon",
                                                                      str(folder))
            assert app._paused_for_claude(paused.key) == []  # not itself, and the other one runs
            read = paused_mod.conversation(found, app._conversation_of(found.key))
            assert "The user:\nmake the taskbar icon bigger" in read and "Claude:\nDone: it's 32 px now." in read
            assert paused.parked and len(fakes) == 1  # reading it started nothing
            ok, name = await app._start_for(driven, found.key)
            assert (ok, name) == (True, "Taskbar icon")  # under its title, which SendMessage finds it by
            await until(pilot, lambda: paused.state == WAITING)
            assert fakes[1].options.resume == "s-old" and not paused.parked
            assert paused.detail == "ready · started so Fix the build can message it"
            assert app._paused_for_claude(driven.key) == []  # running now: ListAgents lists it

    asyncio.run(go())


def test_a_goals_sessions_have_them_and_its_lead_has_only_the_boards(projects, tmp_path):
    from test_coordinator import Prefixes
    from test_driver import done, text

    scripts = Prefixes({"Split the parser": [text("I'll plan it."), done()], "Do Parser.": [text("Split it."), done()]})
    app, folder, fakes = app_with_session(projects, tmp_path, scripts)
    app.config.coordinator, app.config.autopilot = True, "autonomous"

    async def go():
        async with app.run_test(size=(160, 44)) as pilot:
            lead = app.start_goal(str(folder), "Split the parser", "acceptEdits", "", title="Split the parser")
            await until(pilot, lambda: fakes)
            goal = app.coordinator.role(lead.key)[0]
            app.coordinator.add_tasks(goal, [{"title": "Parser", "brief": "Do Parser."}])
            app.coordinator.start_task(goal, 1)
            await until(pilot, lambda: len(fakes) == 2)
            lead_tools, task_tools = fakes[0].options.allowed_tools, fakes[1].options.allowed_tools
            assert not set(paused_mod.TOOL_NAMES) & set(lead_tools)  # it reads the repository and runs the board
            assert set(paused_mod.TOOL_NAMES) <= set(task_tools) and coordinator.DELEGATE_TOOL not in task_tools

    asyncio.run(go())
