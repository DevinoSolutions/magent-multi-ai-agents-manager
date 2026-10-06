"""The command a pane runs: pinned on the ``--go`` path (characterization,
green before the derivation moved) and, below, proven identical across the
three consumers that used to derive it separately."""

from __future__ import annotations

import time

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
    # --go's per-window launch delay and the tiling retry loop both sleep.
    monkeypatch.setattr(time, "sleep", lambda seconds: None)


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


PARITY_PROJECTS = {
    "plain": ProjectConfig(path="", tool="claude", title="p"),
    "happy": ProjectConfig(path="", tool="claude", title="p", happy=True),
    "happy-codex": ProjectConfig(path="", tool="codex", title="p", happy=True),
    "window-command": ProjectConfig(
        path="",
        tool="claude",
        title="p",
        happy=True,
        windows=[WindowConfig(command="claude --model x")],
    ),
    "window-tool": ProjectConfig(
        path="",
        tool="claude",
        title="p",
        happy=True,
        windows=[WindowConfig(tool="codex")],
    ),
    "window-unknown-tool": ProjectConfig(
        path="", tool="claude", title="p", windows=[WindowConfig(tool="ghost")]
    ),
}


class TestEveryConsumerRunsTheSamePaneCommand:
    """`--go`, `up`, revive and status used to derive the pane command in two
    places; `happy` and per-window overrides reached `--go` only."""

    @pytest.fixture
    def psmux_platform(self, monkeypatch):
        from tests.conftest import FakePlatform

        fp = FakePlatform(supports_psmux=True)
        monkeypatch.setattr("magent.launch.get_platform", lambda: fp)
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)

        # The creation verify is not under test here (and would shell out to a
        # psmux binary): record what was handed to the platform, report no
        # casualties.
        def _create(plat, windows):
            plat.launch_psmux_session(windows)
            return {}

        monkeypatch.setattr("magent.psmux.launch_verified", _create)
        return fp

    @pytest.mark.parametrize("case", sorted(PARITY_PROJECTS))
    def test_go_up_revive_and_status_agree(
        self, case, psmux_platform, tmp_path, monkeypatch
    ):
        from magent import psmux

        def _config() -> MagentConfig:
            proj = PARITY_PROJECTS[case]
            return _cfg(tmp_path, ProjectConfig(**vars(proj)), psmux=True)

        assert run_magent(_config(), RunOpts()) == 0
        [go] = [w.command for w in psmux_platform.launched_psmux]

        [status] = psmux.eligible_projects(_config())
        assert status["cmd"] == go

        psmux_platform.launched_psmux.clear()
        assert psmux.bring_up(_config())[0] == ["p"]
        [up] = [w.command for w in psmux_platform.launched_psmux]
        assert up == go

        sent: list[str] = []
        monkeypatch.setattr(psmux, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(
            psmux, "_probe_live", lambda names, binary, timeout: set(names)
        )
        monkeypatch.setattr(psmux, "idle_sessions", lambda names, **kw: list(names))
        monkeypatch.setattr(
            psmux,
            "send_keys",
            lambda name, *keys, target=None, psmux=None: sent.append(keys[0]) or True,
        )
        assert psmux.revive_sessions(_config()) == ["p"]
        assert [k.removeprefix("cmd /c ") for k in sent] == [go]

    def test_happy_is_in_the_expected_cases(self, psmux_platform, tmp_path):
        # Guard against the parity test passing vacuously on all-plain commands.
        from magent import psmux

        cfg = _cfg(tmp_path, ProjectConfig(**vars(PARITY_PROJECTS["happy"])))
        assert psmux.eligible_projects(cfg)[0]["cmd"] == "happy claude"
        cfg = _cfg(tmp_path, ProjectConfig(**vars(PARITY_PROJECTS["window-tool"])))
        assert psmux.eligible_projects(cfg)[0]["cmd"] == "happy codex"
