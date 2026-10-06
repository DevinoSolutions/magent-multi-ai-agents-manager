"""Unit tests for the AGENT_TOOLS registry (R8, F-CT-001) and its IDE mirror
IDE_COMMANDS/IDE_TOOLS (P1-03, REC-F4): each registry's shape, the names
derived from it, and the "adding a tool is one dict entry" proofs that are
the whole point of the refactors.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from magent.sessions import (
    AGENT_TOOLS,
    HAPPY_AGENTS,
    IDE_COMMANDS,
    IDE_TOOLS,
    AgentTool,
    agent_image_names,
    agent_image_stem,
    build_resume_command,
    build_start_command,
    fresh_start_command,
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

    def test_every_agent_names_the_process_it_runs_as(self):
        # An agent with no image could never be seen under a pane, so revive
        # would type into it: every registry entry must name one.
        assert all(caps.images for caps in AGENT_TOOLS.values())

    def test_the_agent_images_cover_both_tools_and_their_node_host(self):
        # claude.exe (native install), codex, and node.exe -- the npm shim both
        # can run under, which a snapshot cannot tell from any other node.
        assert {"claude", "codex", "node"} <= agent_image_names()


class TestTheAgentImageStem:
    """The spelling a process's identity image is compared to
    ``agent_image_names`` in. Claude Code's Windows auto-updater renames a
    RUNNING claude.exe aside to ``claude.exe.old.<epoch-ms>``, and the
    process's image name reads that way for the rest of its life."""

    @pytest.mark.parametrize(
        ("raw", "stem"),
        [
            ("claude.exe", "claude"),
            ("claude", "claude"),
            ("claude.exe.old.1790669558315", "claude"),
            ("CLAUDE.EXE.OLD.1", "claude"),
            ("claude.exe.old", "claude"),
            ("C:\\bin\\Claude.exe.old.42", "claude"),
            ("C:/nvm4w/nodejs/claude.exe.old.1790669558315", "claude"),
            (" codex.exe.old.7 ", "codex"),
        ],
    )
    def test_the_rename_aside_suffix_is_dropped(self, raw, stem):
        assert agent_image_stem(raw) == stem

    @pytest.mark.parametrize(
        "raw",
        [
            "notclaude.exe.old.1",
            "claude.exe.older",
            "claude.exe.old.12a",
            "claude.old.exe",
            "claude.exe.bak",
            "claude.exe.old.",
            "claude.exe.old.\u0661",  # a non-ASCII digit is not a timestamp
        ],
    )
    def test_a_look_alike_is_never_an_agent(self, raw):
        assert agent_image_stem(raw) not in agent_image_names()

    def test_the_shell_stem_stays_exact(self):
        # The loose spelling is the reaper's identity check only: a pane's
        # shell reading is still compared exactly.
        from magent import psmux

        assert psmux.image_stem("pwsh.exe.old.1") == "pwsh.exe.old.1"
        assert not psmux.is_idle_command("pwsh.exe.old.1")


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

    def test_its_process_image_is_one_field_on_the_same_entry(self, monkeypatch):
        """The idle proof learns a new agent's process from its registry entry
        -- no second list to keep in step, any case."""
        monkeypatch.setattr(
            "magent.sessions.AGENT_TOOLS",
            dict(AGENT_TOOLS, mytool=AgentTool(images=("MyTool",))),
        )
        assert "mytool" in agent_image_names()

    def test_agent_image_names_reads_a_supplied_registry(self):
        from dataclasses import replace

        tools = {"claude": replace(AGENT_TOOLS["claude"], images=("onlyclaude",))}
        names = agent_image_names(tools)
        assert "onlyclaude" in names and "node" in names
        assert "codex" not in names  # the supplied registry has no codex entry

    def test_new_entry_defaults_are_unset(self):
        """A minimal AgentTool (no session_ids/happy) is a valid, inert entry --
        confirms the dataclass's defaults, not just the fields this repo's two
        tools happen to fill in."""
        minimal = AgentTool()
        assert minimal.session_ids is None
        assert minimal.resume_command is None
        assert minimal.fresh_command is None
        assert minimal.fresh_form is None
        assert minimal.happy is False
        assert minimal.multi_window is False
        assert minimal.images == ()
        assert minimal.idle_probe is None

    def test_only_claude_carries_an_idle_probe(self):
        """The reaper reads live sessions through this field alone, and a tool
        without one is out of scope -- so a dropped probe would silently stop
        every claude park, with nothing logged."""
        from magent.sessions import claude

        probe = AGENT_TOOLS["claude"].idle_probe
        assert probe is claude.claude_idle_probe
        assert probe.sessions_by_pid is claude.read_session_files
        assert probe.last_activity is claude.last_activity
        assert AGENT_TOOLS["codex"].idle_probe is None

    def test_fresh_start_is_one_dict_entry_too(self, monkeypatch):
        """A tool teaches the fresh-start dispatcher about its own
        implicit-resume flag with one more field on its registry entry --
        build_start_command needs no code change to honor it."""
        extended = dict(
            AGENT_TOOLS,
            mytool=AgentTool(
                fresh_command=lambda base, d, config_dir=None: (
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

    def test_the_fresh_form_is_one_dict_entry_too(self, monkeypatch):
        """A tool teaches the store-free fresh form (what a pool node is
        shipped) with one more field on its registry entry --
        fresh_start_command needs no code change to honor it."""
        extended = dict(
            AGENT_TOOLS,
            mytool=AgentTool(
                fresh_form=lambda base: (
                    base.replace(" --pickup", "") if " --pickup" in base else None
                ),
            ),
        )
        monkeypatch.setattr("magent.sessions.AGENT_TOOLS", extended)

        assert fresh_start_command("mytool", "mytool --pickup") == "mytool"
        assert fresh_start_command("mytool", "mytool") is None

    def test_a_tool_decides_for_itself_what_a_config_dir_means(self, monkeypatch):
        """The registry asks each tool WHICH store answers for a project; the
        tool answers. A store that is not account-scoped ignores the argument
        (codex does exactly that) and one that is reads it -- still one dict
        entry, with the callables carrying one more optional argument."""
        seen: list[object] = []

        def _fresh(base, d, config_dir=None):
            seen.append(config_dir)
            return None if config_dir else "mytool"

        monkeypatch.setattr(
            "magent.sessions.AGENT_TOOLS",
            dict(AGENT_TOOLS, mytool=AgentTool(fresh_command=_fresh)),
        )

        assert build_start_command("mytool", "mytool --pickup", "/a") == "mytool"
        assert (
            build_start_command(
                "mytool", "mytool --pickup", "/a", config_dir=Path("/profiles/13")
            )
            == "mytool --pickup"
        )
        assert seen == [None, Path("/profiles/13")]


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
        self._registry(monkeypatch, lambda base, d, config_dir=None: "rewritten")
        assert build_start_command("mytool", "mytool --go", None) == "mytool --go"

    def test_empty_command_stays_empty(self, monkeypatch):
        # eligible_projects uses "" to mean "this tool has no command at all";
        # the probe must not turn that into something runnable.
        self._registry(monkeypatch, lambda base, d, config_dir=None: "rewritten")
        assert build_start_command("mytool", "", "/a/api") == ""

    def test_a_probe_that_fails_runs_the_configured_command(self, monkeypatch):
        """An unreadable session store proves nothing about whether a session
        exists -- guessing "new" here would silently start a fresh chat over a
        conversation that does exist."""

        def _boom(base, project_dir, config_dir=None):
            raise PermissionError(13, "denied")

        self._registry(monkeypatch, _boom)
        assert build_start_command("mytool", "mytool --go", "/a/api") == "mytool --go"

    def test_claude_default_is_stripped_in_a_new_directory(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "magent.sessions.claude.has_claude_session",
            lambda d, config_dir=None: False,
        )
        assert (
            build_start_command("claude", "claude --continue", str(tmp_path))
            == "claude"
        )

    def test_claude_default_survives_where_a_conversation_exists(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            "magent.sessions.claude.has_claude_session", lambda d, config_dir=None: True
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
