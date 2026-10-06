"""The command a pane runs: pinned on the ``--go`` path (characterization,
green before the derivation moved) and, below, proven identical across the
three consumers that used to derive it separately."""

from __future__ import annotations

import pytest

from magent.config import MagentConfig, ProjectConfig, Settings, WindowConfig
from magent.launch import RunOpts, run_magent

TOOLS = {
    "claude": "claude --continue",
    "codex": "codex",
    "agy": "agy",
}


@pytest.fixture(autouse=True)
def _no_stored_sessions(monkeypatch):
    # No conversation anywhere: the fresh-start probe answers "none", and the
    # multi-window scan finds no ids, so every command is deterministic.
    monkeypatch.setattr(
        "magent.sessions.claude.has_claude_session",
        lambda project_dir, config_dir=None: False,
    )
    monkeypatch.setattr("magent.psmux.find_psmux", lambda: None)


def _cfg(tmp_path, project: ProjectConfig, **settings) -> MagentConfig:
    project.path = str(tmp_path)
    return MagentConfig(
        projects=[project],
        settings=Settings(tools=dict(TOOLS), default_tool="claude", **settings),
    )


def _go_commands(fake_platform, cfg) -> list[str]:
    assert run_magent(cfg, RunOpts()) == 0
    return [t.command for t in fake_platform.launched_terminals]


class TestGoPathCommandCharacterization:
    def test_plain_single_window_drops_the_implicit_resume(
        self, fake_platform, tmp_path
    ):
        cfg = _cfg(tmp_path, ProjectConfig(path="", tool="claude", title="p"))
        assert _go_commands(fake_platform, cfg) == ["claude"]

    def test_global_happy_wraps_the_agent(self, fake_platform, tmp_path):
        cfg = _cfg(
            tmp_path, ProjectConfig(path="", tool="claude", title="p"), happy=True
        )
        assert _go_commands(fake_platform, cfg) == ["happy claude"]

    def test_a_project_can_turn_global_happy_off(self, fake_platform, tmp_path):
        cfg = _cfg(
            tmp_path,
            ProjectConfig(path="", tool="claude", title="p", happy=False),
            happy=True,
        )
        assert _go_commands(fake_platform, cfg) == ["claude"]

    def test_a_non_happy_tool_is_never_wrapped(self, fake_platform, tmp_path):
        cfg = _cfg(tmp_path, ProjectConfig(path="", tool="agy", title="p", happy=True))
        assert _go_commands(fake_platform, cfg) == ["agy"]

    def test_a_window_command_is_literal_but_still_happy_wrapped(
        self, fake_platform, tmp_path
    ):
        cfg = _cfg(
            tmp_path,
            ProjectConfig(
                path="",
                tool="claude",
                title="p",
                happy=True,
                windows=[WindowConfig(command="claude --model x")],
            ),
        )
        assert _go_commands(fake_platform, cfg) == ["happy claude --model x"]

    def test_a_window_tool_override_runs_that_tools_command(
        self, fake_platform, tmp_path
    ):
        cfg = _cfg(
            tmp_path,
            ProjectConfig(
                path="",
                tool="claude",
                title="p",
                happy=True,
                windows=[WindowConfig(tool="codex")],
            ),
        )
        assert _go_commands(fake_platform, cfg) == ["happy codex"]

    def test_an_unknown_window_tool_falls_back_to_the_base_tool(
        self, fake_platform, tmp_path
    ):
        cfg = _cfg(
            tmp_path,
            ProjectConfig(
                path="",
                tool="claude",
                title="p",
                windows=[WindowConfig(tool="ghost")],
            ),
        )
        assert _go_commands(fake_platform, cfg) == ["claude"]

    def test_multi_window_with_no_stored_session_starts_each_fresh(
        self, fake_platform, tmp_path
    ):
        cfg = _cfg(
            tmp_path,
            ProjectConfig(
                path="",
                tool="claude",
                title="p",
                windows=[WindowConfig(), WindowConfig(tool="codex")],
            ),
        )
        assert _go_commands(fake_platform, cfg) == ["claude", "codex"]
