"""The top-level launch path (`magent`, `--go`, `-a`, `-g`, the menu) in a
non-interactive logon session.

`magent up` and `serve --ensure` already hand off to the desktop from Session 0
(`tests/unit/test_attach.py::TestUpHandsOffFromSessionZero`). The path a person
types by hand did not: over ssh, `magent --go` fell through to the psmux choke
point, which could only refuse -- launch.log read "refusing to start psmux
sessions from a non-interactive logon session ... (would have created:
MASTER_CHAT)" and then "tiling: window not found". Same policy function, same
three answers, now asked before anything is created.

Nothing here starts a session: `run_magent` is replaced, and the platform is a
`FakePlatform` that records the hand-off instead of writing a scheduled task.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click.testing
import pytest

import magent.cli as cli_pkg
from magent.cli import checklist
from magent.cli.app import main
from magent.launch import SESSION0_UP_TIMEOUT_S
from magent.platform import HandoffResult
from tests.conftest import FakePlatform

REFUSAL = "refusing to start psmux sessions from a non-interactive logon session"


@pytest.fixture
def launches(monkeypatch):
    """Every `RunOpts` that reached `run_magent` -- i.e. every launch that ran
    in THIS process."""
    import magent.launch as launch_module

    captured: list[launch_module.RunOpts] = []

    def _fake_run(_cfg, opts):
        captured.append(opts)
        return 0

    monkeypatch.setattr(launch_module, "run_magent", _fake_run)
    return captured


@pytest.fixture
def config(tmp_config, tmp_path):
    (tmp_path / "api").mkdir()
    (tmp_path / "web").mkdir()
    return tmp_config(
        {
            "projects": [
                {"path": str(tmp_path / "api"), "group": "backend"},
                {"path": str(tmp_path / "web")},
            ]
        }
    )


def _platform(monkeypatch, *, interactive: bool, **kwargs) -> FakePlatform:
    plat = FakePlatform(interactive_session=interactive, **kwargs)
    monkeypatch.setattr("magent.platform.get_platform", lambda: plat)
    monkeypatch.setattr("magent.launch.get_platform", lambda: plat)
    return plat


def _policy(monkeypatch, value: str) -> None:
    monkeypatch.setenv("MAGENT_SESSION0_POLICY", value)
    monkeypatch.setattr("magent.env._cached_env", None)


def _terminal(monkeypatch, *answers: dict[str, object]) -> None:
    """A real-terminal run: stdin reports a tty and the menu gives `answers`."""
    monkeypatch.setattr(
        click.testing._NamedTextIOWrapper, "isatty", lambda self: True, raising=False
    )
    seq = iter(answers)
    monkeypatch.setattr(cli_pkg, "_show_menu", lambda *_a, **_k: next(seq))


_LAUNCH = {"action": "run", "retile_all": False, "group": None, "reload": False}


class TestAnInteractiveSessionRunsNormally:
    def test_go_runs_here_and_hands_nothing_off(
        self, runner, monkeypatch, config, launches
    ):
        plat = _platform(monkeypatch, interactive=True, supports_handoff=True)
        _policy(monkeypatch, "handoff")

        result = runner.invoke(main, ["--config", config, "--go"])

        assert result.exit_code == 0
        assert len(launches) == 1
        assert plat.handoffs == []

    def test_the_policy_is_not_even_read_for_a_desktop_launch(
        self, runner, monkeypatch, config, launches
    ):
        # `refuse` is for a Session-0 host; an interactive launch must not be
        # able to change behaviour because somebody set it for a headless box.
        plat = _platform(monkeypatch, interactive=True)
        _policy(monkeypatch, "refuse")

        result = runner.invoke(main, ["--config", config, "--go"])

        assert result.exit_code == 0
        assert len(launches) == 1
        assert plat.handoffs == []


class TestSessionZeroHandsOff:
    def test_go_is_rerun_on_the_desktop_with_its_exit_code(
        self, runner, monkeypatch, config, launches
    ):
        plat = _platform(
            monkeypatch,
            interactive=False,
            supports_handoff=True,
            handoff_result=HandoffResult(rc=5, stdout="Done."),
        )
        _policy(monkeypatch, "handoff")

        result = runner.invoke(main, ["--config", config, "--go"])

        assert result.exit_code == 5
        assert "re-running on the desktop" in result.output
        assert "Done." in result.output
        assert launches == []  # nothing was launched, tiled or created HERE
        argv, timeout = plat.handoffs[0]
        assert argv == [sys.executable, "-m", "magent", "--config", config, "--go"]
        assert timeout == SESSION0_UP_TIMEOUT_S

    def test_the_flags_survive_the_handoff(self, runner, monkeypatch, config, launches):
        plat = _platform(monkeypatch, interactive=False, supports_handoff=True)
        _policy(monkeypatch, "handoff")

        runner.invoke(
            main,
            ["--config", config, "-a", "-g", "backend", "--go", "--allow-dirty"],
        )

        argv, _timeout = plat.handoffs[0]
        assert argv[:5] == [sys.executable, "-m", "magent", "--config", config]
        assert argv[5:] == ["--go", "-g", "backend", "--all", "--allow-dirty"]
        assert launches == []

    def test_a_group_launch_without_go_hands_off_the_group(
        self, runner, monkeypatch, config, launches
    ):
        plat = _platform(monkeypatch, interactive=False, supports_handoff=True)
        _policy(monkeypatch, "handoff")

        runner.invoke(main, ["--config", config, "-g", "backend"])

        assert plat.handoffs[0][0][5:] == ["-g", "backend"]

    def test_a_retile_is_tiled_on_the_desktop_never_in_session_zero(
        self, runner, monkeypatch, config, launches
    ):
        plat = _platform(monkeypatch, interactive=False, supports_handoff=True)
        _policy(monkeypatch, "handoff")

        runner.invoke(main, ["--config", config, "--retile-all"])

        assert plat.handoffs[0][0][5:] == ["--retile-all"]
        assert launches == []

    def test_a_dry_run_creates_nothing_so_it_is_left_alone(
        self, runner, monkeypatch, config, launches
    ):
        plat = _platform(monkeypatch, interactive=False, supports_handoff=True)
        _policy(monkeypatch, "handoff")

        result = runner.invoke(main, ["--config", config, "--go", "--dry-run"])

        assert result.exit_code == 0
        assert plat.handoffs == []
        assert [o.dry_run for o in launches] == [True]

    def test_the_menu_runs_here_and_only_the_selection_is_handed_off(
        self, runner, monkeypatch, config, launches
    ):
        # The menu and the checklist are just UI in the ssh terminal; what the
        # desktop copy (which has no terminal) is given is the CONCRETE answer.
        plat = _platform(monkeypatch, interactive=False, supports_handoff=True)
        _policy(monkeypatch, "handoff")
        _terminal(monkeypatch, _LAUNCH)
        monkeypatch.setattr(checklist.picker, "raw_mode_available", lambda: True)
        monkeypatch.setattr(
            checklist,
            "run",
            lambda _state: checklist.ChecklistResult(checklist.LAUNCH, ("web", "api")),
        )

        result = runner.invoke(main, ["--config", config])

        assert result.exit_code == 0
        assert plat.handoffs[0][0][5:] == ["--only", "api", "--only", "web"]
        assert launches == []

    def test_menu_retile_and_group_picks_are_carried(
        self, runner, monkeypatch, config, launches
    ):
        plat = _platform(monkeypatch, interactive=False, supports_handoff=True)
        _policy(monkeypatch, "handoff")
        _terminal(
            monkeypatch,
            {"action": "run", "retile_all": True, "group": "backend", "reload": False},
        )

        runner.invoke(main, ["--config", config])

        assert plat.handoffs[0][0][5:] == ["--retile-all", "-g", "backend"]

    def test_an_aborted_checklist_launches_and_hands_off_nothing(
        self, runner, monkeypatch, config, launches
    ):
        plat = _platform(monkeypatch, interactive=False, supports_handoff=True)
        _policy(monkeypatch, "handoff")
        _terminal(monkeypatch, _LAUNCH)
        monkeypatch.setattr(checklist.picker, "raw_mode_available", lambda: True)
        monkeypatch.setattr(
            checklist, "run", lambda _state: checklist.ChecklistResult(checklist.ABORT)
        )

        result = runner.invoke(main, ["--config", config])

        assert result.exit_code == 0
        assert plat.handoffs == []
        assert launches == []

    def test_a_read_only_menu_action_never_hands_off(
        self, runner, monkeypatch, config, launches
    ):
        # Status reads state and creates nothing: it must keep working in
        # Session 0 exactly as before, with no scheduled task for a read.
        plat = _platform(monkeypatch, interactive=False, supports_handoff=True)
        _policy(monkeypatch, "handoff")
        _terminal(monkeypatch, {"action": "status"}, {"action": "quit"})
        shown: list[int] = []
        monkeypatch.setattr(cli_pkg, "_menu_status", lambda cfg_file: shown.append(1))

        result = runner.invoke(main, ["--config", config])

        assert result.exit_code == 0
        assert shown == [1]
        assert plat.handoffs == []
        assert launches == []


class TestSessionZeroRefuses:
    def test_refuse_says_why_and_exits_one(self, runner, monkeypatch, config, launches):
        plat = _platform(monkeypatch, interactive=False, supports_handoff=True)
        _policy(monkeypatch, "refuse")

        result = runner.invoke(main, ["--config", config, "--go"])

        assert result.exit_code == 1
        assert REFUSAL in result.output
        assert plat.handoffs == []
        assert launches == []

    def test_a_host_with_nobody_logged_on_says_so(
        self, runner, monkeypatch, config, launches
    ):
        # The policy asked for a hand-off; there is simply no desktop to give
        # the work to, and the wording must not tell the user to go use it.
        plat = _platform(monkeypatch, interactive=False, supports_handoff=False)
        _policy(monkeypatch, "handoff")

        result = runner.invoke(main, ["--config", config, "--go"])

        assert result.exit_code == 1
        assert "no user is logged on" in result.output
        assert plat.handoffs == []
        assert launches == []

    def test_refuse_comes_before_the_checklist_asks_anything(
        self, runner, monkeypatch, config, launches
    ):
        _platform(monkeypatch, interactive=False, supports_handoff=True)
        _policy(monkeypatch, "refuse")
        _terminal(monkeypatch, _LAUNCH)
        monkeypatch.setattr(
            checklist, "run", lambda _state: pytest.fail("asked a refused launch")
        )
        monkeypatch.setattr(checklist.picker, "raw_mode_available", lambda: True)

        result = runner.invoke(main, ["--config", config])

        assert result.exit_code == 1
        assert REFUSAL in result.output
        assert launches == []

    def test_allow_runs_in_place_for_a_headless_host(
        self, runner, monkeypatch, config, launches
    ):
        plat = _platform(monkeypatch, interactive=False, supports_handoff=True)
        _policy(monkeypatch, "allow")

        result = runner.invoke(main, ["--config", config, "--go"])

        assert result.exit_code == 0
        assert len(launches) == 1
        assert plat.handoffs == []


class TestTheDesktopCopyHonoursOnly:
    """`--only` is how the checklist's answer reaches a copy with no terminal."""

    def test_only_narrows_the_launch_without_a_checklist(
        self, runner, monkeypatch, config, launches
    ):
        _platform(monkeypatch, interactive=True)
        monkeypatch.setattr(checklist.picker, "raw_mode_available", lambda: True)
        monkeypatch.setattr(
            checklist, "run", lambda _state: pytest.fail("prompted despite --only")
        )

        result = runner.invoke(
            main, ["--config", config, "--go", "--only", "web", "--only", "api"]
        )

        assert result.exit_code == 0
        assert [o.only for o in launches] == [frozenset({"web", "api"})]


class TestTheMenuBringUpHandsOff:
    """Menu `u` is a headless bring-up: in Session 0 it is `magent up`."""

    def _drive(self, monkeypatch, tmp_config, tmp_path, *, choice):
        from magent.cli import status as status_mod

        (tmp_path / "api").mkdir(exist_ok=True)
        monkeypatch.setattr(
            "magent.launch.psmux_status",
            lambda cfg, group=None: (
                [],
                [{"name": "api", "session": "api", "group": "backend"}],
                [{}],
            ),
        )
        monkeypatch.setattr(
            "magent.launch.bring_up_psmux",
            lambda *a, **k: pytest.fail("Session 0 must never create sessions"),
        )
        monkeypatch.setattr(status_mod.click, "prompt", lambda *a, **k: choice)
        monkeypatch.setattr(status_mod.click, "pause", lambda *a, **k: None)
        cfgpath = tmp_config(
            {
                "version": 2,
                "projects": [{"path": str(tmp_path / "api"), "group": "backend"}],
            }
        )
        status_mod._menu_up(Path(cfgpath))
        return cfgpath

    def test_everything_that_is_down_is_handed_off_as_a_plain_up(
        self, monkeypatch, tmp_config, tmp_path
    ):
        plat = _platform(monkeypatch, interactive=False, supports_handoff=True)
        _policy(monkeypatch, "handoff")

        cfgpath = self._drive(monkeypatch, tmp_config, tmp_path, choice="a")

        argv, timeout = plat.handoffs[0]
        assert argv == [sys.executable, "-m", "magent", "--config", cfgpath, "up"]
        assert timeout == SESSION0_UP_TIMEOUT_S

    def test_a_group_pick_is_up_dash_g(self, monkeypatch, tmp_config, tmp_path):
        plat = _platform(monkeypatch, interactive=False, supports_handoff=True)
        _policy(monkeypatch, "handoff")

        self._drive(monkeypatch, tmp_config, tmp_path, choice="backend")

        assert plat.handoffs[0][0][-3:] == ["up", "-g", "backend"]

    def test_refuse_creates_nothing_and_returns_to_the_menu(
        self, monkeypatch, tmp_config, tmp_path, capsys
    ):
        plat = _platform(monkeypatch, interactive=False, supports_handoff=True)
        _policy(monkeypatch, "refuse")

        self._drive(monkeypatch, tmp_config, tmp_path, choice="a")

        assert plat.handoffs == []
        assert REFUSAL in capsys.readouterr().err
