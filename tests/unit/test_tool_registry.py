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
    build_start_command,
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
        assert minimal.fresh_command is None
        assert minimal.happy is False
        assert minimal.multi_window is False

    def test_fresh_start_is_one_dict_entry_too(self, monkeypatch):
        """A tool teaches the fresh-start dispatcher about its own
        implicit-resume flag with one more field on its registry entry --
        build_start_command needs no code change to honor it."""
        extended = dict(
            AGENT_TOOLS,
            mytool=AgentTool(
                fresh_command=lambda base, d: (
                    base.replace(" --pickup", "") if d == "/new" else None
                ),
            ),
        )
        monkeypatch.setattr("magent.sessions.AGENT_TOOLS", extended)

        assert build_start_command("mytool", "mytool --pickup", "/new") == "mytool"
        assert (
            build_start_command("mytool", "mytool --pickup", "/old")
            == "mytool --pickup"
        )


class TestBuildStartCommand:
    """The one function every command-build site routes through. Its whole
    contract is that ONLY a positively-determined "this directory has no
    stored session" rewrites anything -- everything else, including a failing
    probe, runs the configured command so a real failure stays visible."""

    def _registry(self, monkeypatch, fresh_command):
        monkeypatch.setattr(
            "magent.sessions.AGENT_TOOLS",
            dict(AGENT_TOOLS, mytool=AgentTool(fresh_command=fresh_command)),
        )

    def test_unknown_tool_runs_the_configured_command(self):
        assert (
            build_start_command("ghost", "ghost --continue", "/a/api")
            == "ghost --continue"
        )

    def test_a_tool_with_no_probe_runs_the_configured_command(self, monkeypatch):
        monkeypatch.setattr(
            "magent.sessions.AGENT_TOOLS", dict(AGENT_TOOLS, mytool=AgentTool())
        )
        assert build_start_command("mytool", "mytool --go", "/a/api") == "mytool --go"

    def test_no_project_dir_runs_the_configured_command(self, monkeypatch):
        # A remote project's command runs on the far host: callers pass None
        # rather than deciding it from this machine's session store.
        self._registry(monkeypatch, lambda base, d: "rewritten")
        assert build_start_command("mytool", "mytool --go", None) == "mytool --go"

    def test_empty_command_stays_empty(self, monkeypatch):
        # eligible_projects uses "" to mean "this tool has no command at all";
        # the probe must not turn that into something runnable.
        self._registry(monkeypatch, lambda base, d: "rewritten")
        assert build_start_command("mytool", "", "/a/api") == ""

    def test_a_probe_that_fails_runs_the_configured_command(self, monkeypatch):
        """An unreadable session store proves nothing about whether a session
        exists -- guessing "new" here would silently start a fresh chat over a
        conversation that does exist."""

        def _boom(base, project_dir):
            raise PermissionError(13, "denied")

        self._registry(monkeypatch, _boom)
        assert build_start_command("mytool", "mytool --go", "/a/api") == "mytool --go"

    def test_claude_default_is_stripped_in_a_new_directory(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "magent.sessions.claude.has_claude_session", lambda d, home=None: False
        )
        assert (
            build_start_command("claude", "claude --continue", str(tmp_path))
            == "claude"
        )

    def test_claude_default_survives_where_a_conversation_exists(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            "magent.sessions.claude.has_claude_session", lambda d, home=None: True
        )
        assert (
            build_start_command("claude", "claude --continue", str(tmp_path))
            == "claude --continue"
        )


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
