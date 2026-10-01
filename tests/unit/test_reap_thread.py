"""serve's reaper thread, ``upload_server._supervise_idle_reap``, and the gate
it starts behind.

The thread exits at startup only on what no config edit can change (the env
kill switch, the two platform probes). It waits an interval before the first
sweep, reloads the config for every sweep, and outlives every failure.
``reap.sweep_once`` is a spy and the timer a scripted stop event: nothing is
walked, killed or typed."""

from __future__ import annotations

import contextlib
import json
import threading
from typing import TYPE_CHECKING

import pytest

from magent import config, log, reap, upload_server
from magent.lockfile import LockHeld
from tests.conftest import FakePlatform

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

_MB = 1024 * 1024


def _said(caplog: pytest.LogCaptureFixture, level: str | None = None) -> list[str]:
    """What the reaper's own logger said, optionally at one level only."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "magent.reap" and (level is None or r.levelname == level)
    ]


@pytest.fixture
def reaping_on(monkeypatch):
    # conftest pins MAGENT_IDLE_REAP=0; only tests about the reaper undo it.
    monkeypatch.setenv("MAGENT_IDLE_REAP", "1")
    monkeypatch.setattr("magent.env._cached_env", None)


def _config_text(*, enabled: bool = True) -> str:
    return json.dumps(
        {
            "version": config.SCHEMA_VERSION,
            "projects": [],
            "settings": {"idleReap": {"enabled": enabled}},
        }
    )


class _Serve:
    """One reaper thread's world. ``ticks`` is how many waits return False (one
    sweep each) before the stop. ``before_wait[k]`` runs at the start of the
    k-th wait (1-based), so a test can edit the config between sweeps.
    ``results`` scripts sweep_once, one entry per sweep: a list of ParkResults,
    or an exception to raise. ``events`` records waits, locks and sweeps in
    order."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        plat: FakePlatform | None = None,
        ticks: int = 1,
        enabled: bool = True,
        results: list[list[reap.ParkResult] | Exception] | None = None,
        before_wait: dict[int, Callable[[], None]] | None = None,
    ) -> None:
        self.path = tmp_path / "magent.config.json"
        self.path.write_text(_config_text(enabled=enabled), encoding="utf-8")
        self.plat = plat or FakePlatform(supports_psmux=True)
        self.ticks = ticks
        self.results = list(results or [])
        self.before_wait = before_wait or {}
        self.held = False
        self.waits = 0
        self.events: list[tuple[object, ...]] = []
        # Configure the logger now: its first get_logger sets its level, which
        # would undo a caplog level a test sets before run().
        log.get_logger("reap")
        monkeypatch.setattr("magent.platform.get_platform", lambda: self.plat)
        monkeypatch.setattr(upload_server, "exclusive_lock", self._lock)
        monkeypatch.setattr(reap, "sweep_once", self._sweep)

    def wait(self, timeout: float) -> bool:
        self.waits += 1
        if self.waits in self.before_wait:
            self.before_wait[self.waits]()
        self.events.append(("wait", timeout))
        return self.waits > self.ticks

    @contextlib.contextmanager
    def _lock(self, name: str) -> Iterator[None]:
        self.events.append(("lock", name))
        if self.held:
            raise LockHeld(name)
        yield

    def _sweep(self, cfg: config.MagentConfig, **kw: object) -> list[reap.ParkResult]:
        self.events.append(("sweep", cfg.settings.idle_reap.enabled, kw))
        out = self.results.pop(0) if self.results else []
        if isinstance(out, Exception):
            raise out
        return out

    def run(self, interval: float = 300.0) -> None:
        upload_server._supervise_idle_reap(str(self.path), self, interval=interval)

    def kinds(self) -> list[object]:
        return [event[0] for event in self.events]


class TestTheGate:
    def test_off_reason_is_the_setting_then_the_process_half(self, reaping_on):
        plat = FakePlatform(supports_psmux=True)
        on = config.MagentConfig(projects=[])
        off = config.MagentConfig(projects=[])
        off.settings.idle_reap.enabled = False
        assert reap.off_reason(on, plat) is None
        assert reap.off_reason(off, plat) == "off in settings.idleReap"
        assert reap.process_off_reason(plat) is None

    @pytest.mark.parametrize(
        ("env_on", "plat", "reason"),
        [
            (False, FakePlatform(supports_psmux=True), "off (MAGENT_IDLE_REAP=0)"),
            (
                True,
                FakePlatform(supports_psmux=False),
                "unsupported platform (no psmux)",
            ),
            (
                True,
                FakePlatform(supports_psmux=True, interactive_session=False),
                "non-interactive logon session",
            ),
            (
                False,
                FakePlatform(supports_psmux=False, interactive_session=False),
                "off (MAGENT_IDLE_REAP=0)",
            ),
            (
                True,
                FakePlatform(supports_psmux=False, interactive_session=False),
                "unsupported platform (no psmux)",
            ),
        ],
        ids=["env-off", "no-psmux", "non-interactive", "env-first", "psmux-second"],
    )
    def test_the_process_half_in_order(self, monkeypatch, env_on, plat, reason):
        if env_on:
            monkeypatch.setenv("MAGENT_IDLE_REAP", "1")
            monkeypatch.setattr("magent.env._cached_env", None)
        assert reap.process_off_reason(plat) == reason
        assert reap.off_reason(config.MagentConfig(projects=[]), plat) == reason

    def test_an_environment_that_does_not_validate_is_named_as_that(self, monkeypatch):
        # Failing closed is right, but it is not the user's deliberate 0: the
        # reason names the real cause, which doctor's env check then details.
        monkeypatch.setenv("MAGENT_IDLE_REAP", "not-a-bool")
        monkeypatch.setattr("magent.env._cached_env", None)
        plat = FakePlatform(supports_psmux=False, interactive_session=False)
        reason = "off (the MAGENT_* environment did not validate)"
        assert reap.env_enabled() is False
        assert reap.process_off_reason(plat) == reason
        assert reap.off_reason(config.MagentConfig(projects=[]), plat) == reason


class TestStartup:
    # Every line the reaper's logger says, exactly: "off" once, and an
    # environment that does not validate is ONE warning, then the startup line.
    @pytest.mark.parametrize(
        ("switch", "plat", "said"),
        [
            (
                "0",
                FakePlatform(supports_psmux=True),
                ["idle reaper off (MAGENT_IDLE_REAP=0)"],
            ),
            (
                "not-a-bool",
                FakePlatform(supports_psmux=True),
                [
                    "idle reap: environment did not validate; reaping disabled",
                    "idle reaper off (the MAGENT_* environment did not validate)",
                ],
            ),
            (
                "1",
                FakePlatform(supports_psmux=False),
                ["idle reaper off: unsupported platform (no psmux)"],
            ),
            (
                "1",
                FakePlatform(supports_psmux=True, interactive_session=False),
                ["idle reaper off: non-interactive logon session"],
            ),
        ],
        ids=["env-off", "env-invalid", "no-psmux", "non-interactive"],
    )
    def test_a_process_level_off_logs_once_and_never_waits(
        self, monkeypatch, tmp_path, caplog, switch, plat, said
    ):
        monkeypatch.setenv("MAGENT_IDLE_REAP", switch)
        monkeypatch.setattr("magent.env._cached_env", None)
        serve = _Serve(monkeypatch, tmp_path, plat=plat)
        with caplog.at_level("INFO", logger="magent.reap"):
            serve.run()
        assert serve.events == []  # returned before the first wait
        assert _said(caplog) == said

    def test_on_says_so_once(self, monkeypatch, tmp_path, caplog, reaping_on):
        serve = _Serve(monkeypatch, tmp_path, ticks=0)
        with caplog.at_level("INFO", logger="magent.reap"):
            serve.run(interval=300.0)
        assert _said(caplog) == ["idle reaper on: sweeping every 300s"]

    def test_no_config_path_reads_the_config_find_config_resolves(
        self, monkeypatch, tmp_path, reaping_on
    ):
        serve = _Serve(monkeypatch, tmp_path, enabled=False)
        asked: list[str | None] = []

        def _find(arg: str | None) -> Path:
            asked.append(arg)
            return serve.path

        monkeypatch.setattr("magent.paths.find_config", _find)
        upload_server._supervise_idle_reap(None, serve, interval=300.0)
        assert asked == [None]
        assert [e[1] for e in serve.events if e[0] == "sweep"] == [False]

    def test_settings_off_at_startup_keeps_the_thread_and_an_edit_turns_it_on(
        self, monkeypatch, tmp_path, reaping_on
    ):
        # The setting is read per sweep (sweep_once's own gate), never at
        # startup: a config edited to ON is honored without a serve restart.
        serve = _Serve(
            monkeypatch,
            tmp_path,
            enabled=False,
            ticks=2,
            before_wait={2: lambda: serve.path.write_text(_config_text(enabled=True))},
        )
        serve.run()
        sweeps = [e[1] for e in serve.events if e[0] == "sweep"]
        assert sweeps == [False, True]


class TestTheLoop:
    def test_it_waits_first_then_sweeps_each_interval_until_stop(
        self, monkeypatch, tmp_path, reaping_on
    ):
        serve = _Serve(monkeypatch, tmp_path, ticks=3)
        serve.run(interval=12.5)
        assert serve.kinds() == [*["wait", "lock", "sweep"] * 3, "wait"]
        assert {e[1] for e in serve.events if e[0] == "wait"} == {12.5}

    def test_each_sweep_holds_the_idle_reaper_lock_and_gets_the_platform(
        self, monkeypatch, tmp_path, reaping_on
    ):
        serve = _Serve(monkeypatch, tmp_path)
        serve.run()
        assert serve.events[1] == ("lock", "idle-reaper")
        assert serve.events[2] == ("sweep", True, {"plat": serve.plat})

    def test_a_held_lock_skips_the_sweep_and_the_thread_lives(
        self, monkeypatch, tmp_path, caplog, reaping_on
    ):
        serve = _Serve(monkeypatch, tmp_path, ticks=2)
        serve.held = True
        with caplog.at_level("DEBUG", logger="magent.reap"):
            serve.run()
        assert serve.kinds() == ["wait", "lock", "wait", "lock", "wait"]
        assert (
            _said(caplog, "DEBUG")
            == ["idle reaper: another serve holds the sweep lock"] * 2
        )

    @pytest.mark.parametrize(
        ("break_it", "named"),
        [
            (
                lambda p: p.write_text(
                    json.dumps(
                        {
                            "version": config.SCHEMA_VERSION,
                            "projects": [],
                            "settings": {"idleReap": {"afterMinutes": "x"}},
                        }
                    )
                ),
                "settings.idleReap.afterMinutes must be an integer",
            ),
            (lambda p: p.unlink(), "Config file not found"),
        ],
        ids=["invalid", "missing"],
    )
    def test_a_config_that_will_not_load_skips_that_sweep_and_names_why(
        self, monkeypatch, tmp_path, caplog, reaping_on, break_it, named
    ):
        serve = _Serve(
            monkeypatch,
            tmp_path,
            ticks=2,
            before_wait={
                1: lambda: break_it(serve.path),
                2: lambda: serve.path.write_text(_config_text()),
            },
        )
        with caplog.at_level("WARNING", logger="magent.reap"):
            serve.run()
        assert serve.kinds() == ["wait", "lock", "wait", "lock", "sweep", "wait"]
        warnings = _said(caplog, "WARNING")
        assert len(warnings) == 1
        assert named in warnings[0]
        assert _said(caplog, "ERROR") == []

    def test_a_config_path_that_will_not_resolve_skips_that_sweep_and_names_why(
        self, monkeypatch, tmp_path, caplog, reaping_on
    ):
        # find_config reads Path.cwd(), which raises once serve's working
        # directory is deleted. Resolved once at startup, outside every guard,
        # that raise ended the thread before its first sweep: the error went to
        # the threading excepthook, and serve ran on with the reaper gone.
        serve = _Serve(monkeypatch, tmp_path, ticks=2)
        asked: list[str | None] = []

        def _find(arg: str | None) -> Path:
            asked.append(arg)
            if len(asked) == 1:
                raise FileNotFoundError("the working directory is gone")
            return serve.path

        monkeypatch.setattr("magent.paths.find_config", _find)
        with caplog.at_level("WARNING", logger="magent.reap"):
            upload_server._supervise_idle_reap(None, serve, interval=300.0)
        assert serve.kinds() == ["wait", "lock", "wait", "lock", "sweep", "wait"]
        warnings = _said(caplog, "WARNING")
        assert len(warnings) == 1
        assert "the working directory is gone" in warnings[0]
        assert _said(caplog, "ERROR") == []

    def test_a_config_that_breaks_later_is_not_swept_from_memory(
        self, monkeypatch, tmp_path, reaping_on
    ):
        serve = _Serve(
            monkeypatch,
            tmp_path,
            ticks=2,
            before_wait={2: lambda: serve.path.write_text("{not json")},
        )
        serve.run()
        assert serve.kinds() == ["wait", "lock", "sweep", "wait", "lock", "wait"]

    def test_a_sweep_that_raises_is_logged_and_the_loop_goes_on(
        self, monkeypatch, tmp_path, caplog, reaping_on
    ):
        serve = _Serve(
            monkeypatch, tmp_path, ticks=2, results=[RuntimeError("boom"), []]
        )
        with caplog.at_level("ERROR", logger="magent.reap"):
            serve.run()
        assert serve.kinds() == [*["wait", "lock", "sweep"] * 2, "wait"]
        errors = [
            r
            for r in caplog.records
            if r.name == "magent.reap" and r.levelname == "ERROR"
        ]
        assert len(errors) == 1
        assert errors[0].getMessage() == "idle reaper: sweep failed"
        assert errors[0].exc_info is not None
        assert "boom" in caplog.text

    def test_a_sweep_that_parks_says_how_many_and_what_it_freed(
        self, monkeypatch, tmp_path, caplog, reaping_on
    ):
        results: list[list[reap.ParkResult] | Exception] = [
            [
                reap.ParkResult("a", True, 700 * _MB, None),
                reap.ParkResult("b", False, 90 * _MB, "root-survived"),
                reap.ParkResult("c", True, 324 * _MB, None),
            ],
            [reap.ParkResult("d", False, 0, "abort:root-identity")],
        ]
        serve = _Serve(monkeypatch, tmp_path, ticks=2, results=results)
        with caplog.at_level("INFO", logger="magent.reap"):
            serve.run()
        assert _said(caplog) == [
            "idle reaper on: sweeping every 300s",
            "idle reaper: parked 2 session(s), freed~1024MB",
        ]


class TestServeOwnsIt:
    def test_run_server_starts_it_with_the_config_path_and_stops_it(
        self, monkeypatch, tmp_path
    ):
        started: list[tuple[object, tuple[object, ...]]] = []
        real_thread = threading.Thread

        class _Recording(real_thread):
            """Records what run_server wanted to run, and runs none of it."""

            def __init__(self, *args, target=None, **kwargs) -> None:
                super().__init__(*args, target=target, **kwargs)
                self.recorded = (target, kwargs.get("args", ()))

            def start(self) -> None:
                started.append(self.recorded)

        class _FakeServer:
            def __init__(self, addr, _handler) -> None:
                self.server_address = addr

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def shutdown(self) -> None:
                return None

            def server_close(self) -> None:
                return None

        monkeypatch.setattr(upload_server.threading, "Thread", _Recording)
        monkeypatch.setattr(upload_server, "_bind_addresses", lambda _h: ["127.0.0.1"])
        monkeypatch.setattr(upload_server, "_NoFqdnHTTPServer", _FakeServer)
        monkeypatch.setattr(
            upload_server, "_pid_path", lambda port: tmp_path / f"upload-{port}.pid"
        )

        with pytest.raises(KeyboardInterrupt):
            upload_server.run_server(port=0, config_path="C:/cfg/magent.config.json")

        reaper = [
            args
            for target, args in started
            if target is upload_server._supervise_idle_reap
        ]
        assert len(reaper) == 1
        path, stop = reaper[0]
        assert path == "C:/cfg/magent.config.json"
        assert isinstance(stop, threading.Event)
        assert stop.is_set()  # the finally stopped it
