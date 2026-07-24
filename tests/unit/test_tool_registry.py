"""Unit tests for the AGENT_TOOLS registry (R8, F-CT-001) and its IDE mirror
IDE_COMMANDS/IDE_TOOLS (P1-03, REC-F4): each registry's shape, the names
derived from it, and the "adding a tool is one dict entry" proofs that are
the whole point of the refactors.
"""

from __future__ import annotations

from magent.launch import HAPPY_AGENTS
from magent.sessions import (
    AGENT_TOOLS,
    IDE_COMMANDS,
    IDE_TOOLS,
    AgentTool,
    build_resume_command,
    ide_command,
    is_ide_tool,
)


class TestRegistryShape:
    def test_registered_tools(self):
        assert set(AGENT_TOOLS) == {"claude", "codex"}

    def test_all_entries_are_happy(self):
        assert all(caps.happy is True for caps in AGENT_TOOLS.values())

    def test_all_entries_are_multi_window(self):
        assert all(caps.multi_window is True for caps in AGENT_TOOLS.values())

    def test_happy_agents_derived_from_registry(self):
        assert {t for t, c in AGENT_TOOLS.items() if c.happy} == HAPPY_AGENTS


class TestOneEditExtensionProof:
    def test_adding_a_tool_is_one_dict_entry(self, monkeypatch):
        """Adding tool support is one new AGENT_TOOLS entry -- the dispatcher
        (build_resume_command) needs no code change to pick it up."""
        extended = dict(
            AGENT_TOOLS,
            mytool=AgentTool(
                resume_command=lambda base, session: f"{base} R {session}",
            ),
        )
        monkeypatch.setattr("magent.sessions.AGENT_TOOLS", extended)

        assert (
            build_resume_command("mytool", "mytool run", "id-1") == "mytool run R id-1"
        )

    def test_new_entry_defaults_are_unset(self):
        """A minimal AgentTool (no session_ids/happy) is a valid, inert entry --
        confirms the dataclass's defaults, not just the fields this repo's two
        tools happen to fill in."""
        minimal = AgentTool()
        assert minimal.session_ids is None
        assert minimal.resume_command is None
        assert minimal.happy is False
        assert minimal.multi_window is False


class TestIdeRegistryShape:
    def test_registered_ide_tools(self):
        assert frozenset({"code", "vscode", "cursor"}) == IDE_TOOLS

    def test_ide_tools_derives_from_the_command_dict(self):
        assert frozenset(IDE_COMMANDS) == IDE_TOOLS

    def test_vscode_is_an_alias_for_code(self):
        assert ide_command("code") == "code"
        assert ide_command("vscode") == "code"
        assert ide_command("cursor") == "cursor"

    def test_ide_and_agent_registries_are_disjoint(self):
        assert not IDE_TOOLS & set(AGENT_TOOLS)

    def test_non_ide_tools_do_not_match(self):
        assert not is_ide_tool("claude")
        assert not is_ide_tool("")


class TestIdeOneEditExtensionProof:
    def test_adding_an_ide_is_one_dict_entry(self, monkeypatch):
        """Adding IDE support is one new IDE_COMMANDS entry -- membership
        (is_ide_tool) and command mapping (ide_command) need no code change
        to pick it up."""
        extended = dict(IDE_COMMANDS, zed="zed")
        monkeypatch.setattr("magent.sessions.IDE_COMMANDS", extended)

        assert is_ide_tool("zed")
        assert ide_command("zed") == "zed"

    def test_unknown_tool_falls_back_to_code_command(self):
        """Pins the historical launch-path fallback: any tool that reaches
        ide_command without a registry entry opens with plain `code`."""
        assert ide_command("ghost-ide") == "code"
