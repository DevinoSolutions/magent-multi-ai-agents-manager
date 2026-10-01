"""No magent daemon is born in logon Session 0 -- the spawn seams refuse.

The hand-off covers the two commands `magent attach` fires on a host over ssh
(`up`, `serve --ensure`). Everything else that can plant a SURVIVOR -- a
detached serve, an Alt+V listener, an attention daemon -- used to do so in
Session 0 without a second look whenever it happened to run there: `--go` or
the menu over ssh, a foreground `magent serve` over ssh (its listener
supervisor), `magent attention -d` over ssh, and that daemon's own watchdog.
A Session-0 serve takes the loopback port the desktop's Alt+V needs; a
Session-0 listener's keyboard hook never sees a key typed at the desktop.

Every seam asks the ONE policy function (``launch.session0_disposition``) and
refuses with the shared wording; only the `attention -d` command shell hands
off, exactly like `serve --ensure`. Session ids come from ``FakePlatform``;
nothing here creates a Session-0 process or a scheduled task.
"""

from __future__ import annotations

import sys
import threading

import pytest

from magent import cli, launch
from magent.cli import attention_cmd
from magent.config import MagentConfig, Settings
from magent.launch import (
    SESSION0_ATTENTION_REFUSAL,
    SESSION0_ATTENTION_TIMEOUT_S,
    SESSION0_HOTKEY_REFUSAL,
    SESSION0_SERVE_REFUSAL,
    RunOpts,
    _LaunchResult,
    _start_psmux_and_upload,
)
from magent.platform import PsmuxWindowOpts
from tests.conftest import FakePlatform


def _policy(monkeypatch, value: str) -> None:
    monkeypatch.setenv("MAGENT_SESSION0_POLICY", value)
    monkeypatch.setattr("magent.env._cached_env", None)


@pytest.fixture
def session0(monkeypatch) -> FakePlatform:
    """This process runs in Session 0 and a desktop exists to hand off to."""
    plat = FakePlatform(
        interactive_session=False, supports_handoff=True, supports_hotkey=True
    )
    monkeypatch.setattr("magent.launch.get_platform", lambda: plat)
    monkeypatch.setattr("magent.platform.get_platform", lambda: plat)
    _policy(monkeypatch, "handoff")
    return plat


@pytest.fixture
def spawned(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(launch, "spawn_detached", calls.append)
    return calls


class TestTheServeSeamRefuses:
    """``ensure_upload_server`` is the one probe-then-spawn every serve spawner
    goes through (--go, menu "u", `up`, `serve --ensure`); refusing HERE is what
    covers the paths that never hand off."""

    def test_a_dead_port_in_session_zero_spawns_nothing(
        self, monkeypatch, session0, spawned, caplog
    ):
        monkeypatch.setattr(launch, "_probe_upload_port", lambda _p: False)

        with caplog.at_level("WARNING", logger="magent.upload"):
            assert launch.ensure_upload_server(8099, "cfg.json") is False

        assert spawned == []
        assert SESSION0_SERVE_REFUSAL in caplog.text

    @pytest.mark.parametrize("policy", ["refuse", "handoff"])
    def test_every_non_run_disposition_refuses(
        self, monkeypatch, session0, spawned, policy
    ):
        # A seam cannot hand off -- it is not the command the user typed -- so
        # "handoff" refuses here exactly like "refuse" (as psmux's does).
        _policy(monkeypatch, policy)
        monkeypatch.setattr(launch, "_probe_upload_port", lambda _p: False)

        assert launch.ensure_upload_server(8099) is False
        assert spawned == []

    def test_allow_spawns_where_it_is(self, monkeypatch, session0, spawned):
        _policy(monkeypatch, "allow")
        monkeypatch.setattr(launch, "_probe_upload_port", lambda _p: False)

        assert launch.ensure_upload_server(8099) is True
        assert spawned == [launch.upload_server_argv(8099, None)]

    def test_a_live_server_is_not_a_refusal(
        self, monkeypatch, session0, spawned, caplog
    ):
        # Nothing would have been spawned, so there is nothing to refuse and
        # nothing to log -- an `up` over ssh must not grow a warning per call.
        monkeypatch.setattr(launch, "_probe_upload_port", lambda _p: True)

        with caplog.at_level("WARNING", logger="magent.upload"):
            assert launch.ensure_upload_server(8099) is False

        assert SESSION0_SERVE_REFUSAL not in caplog.text

    def test_an_interactive_session_is_untouched(self, monkeypatch, spawned):
        plat = FakePlatform(interactive_session=True)
        monkeypatch.setattr("magent.launch.get_platform", lambda: plat)
        _policy(monkeypatch, "refuse")
        monkeypatch.setattr(launch, "_probe_upload_port", lambda _p: False)

        assert launch.ensure_upload_server(8099) is True
        assert len(spawned) == 1


class TestTheWatchdogRefuses:
    """An attention daemon that finds itself in Session 0 (an older magent, a
    foreground `attention` over ssh) must not revive serve THERE."""

    class _Clock:
        def __init__(self) -> None:
            self.t = 1000.0

        def __call__(self) -> float:
            return self.t

    def test_a_dead_port_is_not_revived_in_session_zero(
        self, monkeypatch, session0, spawned, caplog
    ):
        monkeypatch.setattr(launch, "_probe_upload_port", lambda _p: False)
        sup = launch.UploadServerSupervisor(8099, cooldown_s=60.0, now=self._Clock())

        with caplog.at_level("WARNING", logger="magent.attention"):
            assert sup.tick() is False

        assert spawned == []
        assert SESSION0_SERVE_REFUSAL in caplog.text

    def test_the_refusal_is_said_once_per_cooldown_not_once_per_poll(
        self, monkeypatch, session0, spawned, caplog
    ):
        monkeypatch.setattr(launch, "_probe_upload_port", lambda _p: False)
        clock = self._Clock()
        sup = launch.UploadServerSupervisor(8099, cooldown_s=60.0, now=clock)

        with caplog.at_level("WARNING", logger="magent.attention"):
            sup.tick()
            clock.t += 5
            sup.tick()
            clock.t += 5
            sup.tick()

        assert caplog.text.count(SESSION0_SERVE_REFUSAL) == 1
        assert spawned == []


class TestTheListenerSeamRefuses:
    """``start_hotkey_listener`` is the one listener spawn (launch path, attach,
    and serve's supervisor via ``ensure_hotkey_listener``). A keyboard hook in
    Session 0 never sees a key typed at the desktop."""

    def test_nothing_is_spawned_and_the_hotkey_module_is_never_reached(
        self, session0, spawned, caplog
    ):
        # Off Windows `magent.hotkey` raises ImportError at import time, so this
        # passing on every OS is the proof the refusal comes first.
        with caplog.at_level("WARNING", logger="magent.hotkey"):
            assert launch.start_hotkey_listener("http://127.0.0.1:8099") is None

        assert spawned == []
        assert SESSION0_HOTKEY_REFUSAL in caplog.text

    def test_serves_supervisor_stands_down_instead_of_retrying(
        self, monkeypatch, session0
    ):
        from magent import upload_server

        monkeypatch.setattr(upload_server, "supervision_enabled", lambda: True)
        calls: list[str] = []
        monkeypatch.setattr("magent.launch.ensure_hotkey_listener", calls.append)
        stop = threading.Event()

        # Returns on its own: a supervisor that looped here would log a refusal
        # every 30s for the life of the server.
        upload_server._supervise_hotkey("http://127.0.0.1:8099", stop, interval=0.01)

        assert calls == []


class TestTheGoPathRefuses:
    """`--go` and menu "u" never hand off (the psmux choke point refuses their
    sessions) -- and they must not leave a serve and a listener behind either."""

    def test_no_server_and_no_listener_and_it_says_why(
        self, monkeypatch, session0, spawned, capsys
    ):
        listeners: list[str] = []
        monkeypatch.setattr(launch, "_probe_upload_port", lambda _p: False)
        monkeypatch.setattr("magent.launch.tailnet.ip4", lambda: None)
        monkeypatch.setattr(
            "magent.launch.start_hotkey_listener",
            lambda url, ssh_host=None: listeners.append(url),
        )
        monkeypatch.setattr("magent.psmux.launch_verified", lambda _p, _w: {})
        cfg = MagentConfig(
            projects=[],
            settings=Settings(psmux=True, upload_server=True, upload_port=9911),
        )
        result = _LaunchResult(
            targets=[],
            psmux_windows=[
                PsmuxWindowOpts(window_name="a", cwd="/tmp/a", command="claude")
            ],
            psmux_colors={"a": None},
        )

        _start_psmux_and_upload(session0, cfg, RunOpts(), result)

        assert spawned == []
        assert listeners == []
        out = capsys.readouterr().out
        assert "refusing to start the upload server" in out
        assert "open on phone" not in out


class TestAttentionDaemonHandsOff:
    """`attention -d` plants a survivor, like `serve --ensure`, so it gets the
    same treatment: hand the command to the desktop, or refuse."""

    @pytest.fixture
    def cfg_path(self, tmp_config) -> str:
        return tmp_config({"version": 2, "projects": [{"path": "api"}]})

    @pytest.fixture(autouse=True)
    def _pid(self, monkeypatch, tmp_path):
        monkeypatch.setattr(attention_cmd, "_PID_PATH", tmp_path / "attention.pid")

    def test_it_reruns_the_daemon_on_the_desktop(
        self, runner, session0, spawned, cfg_path
    ):
        result = runner.invoke(
            cli.main,
            ["--config", cfg_path, "attention", "-d", "--interval", "2.5"],
        )

        assert result.exit_code == 0, result.output
        assert spawned == []  # nothing started HERE
        argv, timeout = session0.handoffs[0]
        assert argv == [
            sys.executable,
            "-m",
            "magent",
            "--config",
            cfg_path,
            "attention",
            "-d",
            "--interval",
            "2.5",
        ]
        assert timeout == SESSION0_ATTENTION_TIMEOUT_S

    def test_the_desktop_exit_code_is_ours(self, runner, session0, cfg_path):
        from magent.platform import HandoffResult

        session0._handoff_result = HandoffResult(rc=1)  # the desktop copy failed

        result = runner.invoke(cli.main, ["--config", cfg_path, "attention", "-d"])

        assert result.exit_code == 1

    def test_refuse_says_why_and_spawns_nothing(
        self, runner, monkeypatch, session0, spawned, cfg_path
    ):
        _policy(monkeypatch, "refuse")

        result = runner.invoke(cli.main, ["--config", cfg_path, "attention", "-d"])

        assert result.exit_code == 1
        assert spawned == []
        assert session0.handoffs == []
        assert SESSION0_ATTENTION_REFUSAL in result.output

    def test_stop_is_never_handed_off(self, runner, session0, cfg_path):
        # Stopping is not planting; it must act where it was asked to.
        result = runner.invoke(cli.main, ["--config", cfg_path, "attention", "--stop"])

        assert result.exit_code == 0
        assert session0.handoffs == []
