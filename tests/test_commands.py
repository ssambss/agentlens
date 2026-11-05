"""The slash-command menu's catalog: what it lists, in what order, and what it offers next."""

from agentlens import commands
from agentlens.commands import Catalog, Command, local_command


def names(suggestions):
    return [s.command.name for s in suggestions]


def test_lists_what_works_in_a_session_claude_code_runs_for_agentlens(server_info):
    catalog = Catalog.from_server_info(server_info)
    listed = {c.name for c in catalog.commands}
    assert {"clear", "compact", "model", "engineering:code-review"} <= listed
    # Unavailable headless, removed, or internal.
    assert not listed & {"fast", "agents", "__remote-workflow"}
    assert [m.value for m in catalog.models] == ["default", "opus", "haiku"]


def test_typing_narrows_the_list_name_first_then_aliases_words_and_descriptions(server_info):
    catalog = Catalog.from_server_info(server_info).with_local()
    assert names(catalog.suggest("/mo")) == ["model"]
    assert names(catalog.suggest("/c"))[:3] == ["clear", "compact", "context"]
    assert names(catalog.suggest("/cost")) == ["usage"]  # an alias
    assert catalog.suggest("/cost")[0].command.local  # agentlens's own /usage, not Claude Code's
    assert names(catalog.suggest("/review")) == ["engineering:code-review", "simplify"]  # a word, then description
    everything = names(catalog.suggest("/"))
    assert everything.index("engineering:code-review") > everything.index("simplify")  # built-ins first
    assert catalog.suggest("fix the build") == [] and catalog.suggest("/nothing-like-this") == []


def test_after_the_name_the_menu_offers_the_arguments_values(server_info):
    catalog = Catalog.from_server_info(server_info)
    assert [s.text for s in catalog.suggest("/model ")] == ["/model default", "/model opus", "/model haiku"]
    assert [s.text for s in catalog.suggest("/model ha")] == ["/model haiku"]
    assert [s.choice.value for s in catalog.suggest("/effort ")][:3] == ["low", "medium", "high"]
    assert [s.choice.value for s in catalog.suggest("/output-style ")] == ["default", "Concise", "Explanatory"]
    assert catalog.suggest("/compact keep the API notes") == []  # free text: no list


def test_required_and_optional_arguments():
    assert Command("model", hint="<model>").needs_argument
    assert not Command("compact", hint="<optional custom summarization instructions>").needs_argument
    assert not Command("clear", hint="[name]").needs_argument
    assert not Command("context").needs_argument


def test_commands_agentlens_handles_itself():
    assert local_command("/usage").name == "usage"
    assert local_command("  /stats ").name == "usage"
    assert local_command("/exit").name == "exit" and local_command("/quit").name == "exit"
    assert local_command("/model opus") is None and local_command("usage") is None


def test_the_list_is_kept_for_next_time(server_info, isolated_home):
    assert commands.load_cached().commands == []  # nothing seen yet
    commands.save_cached(Catalog.from_server_info(server_info))
    again = commands.load_cached()
    assert again == Catalog.from_server_info(server_info)
    assert (isolated_home / "claude-commands.json").exists()
