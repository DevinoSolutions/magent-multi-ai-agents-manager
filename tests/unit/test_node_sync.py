"""The node sync daemon (magent.node_sync) and everything that keeps it alive.

Nothing here dials a real machine: node calls go to THE fake ssh
(tests/unit/_fake_ssh.py) or through the ``pull=`` seam, and MAGENT_NODE_SYNC
is 0 for every test that does not set it back.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from magent import launch, node_sync, nodes
from magent.config import (
    SCHEMA_VERSION,
    MagentConfig,
    NodeConfig,
    NodeSyncConfig,
    ProjectConfig,
    Settings,
)
from magent.env import get_env
from magent.lockfile import LockHeld, exclusive_lock
from magent.log import get_logger, heartbeat_age, write_heartbeat
from magent.nodes import NodeMapEntry

if TYPE_CHECKING:
    from collections.abc import Iterator

_TESTS = Path(__file__).resolve().parents[1]
_BOOST_PIN = re.compile(r'MAGENT_PSMUX_BOOST"[^\n]*"0"')
# Exemptions, each for its own reason:
_EXEMPT = {
    # Pins the boost pin itself -- a setenv of BOOST "0" in-process, with no
    # sync twin to check, since this file is about the boost, not the sync.
    "test_psmux_boost.py",
    # This file's own regex literals (in _BOOST_PIN and this docstring) would
    # self-match and corrupt the scan.
    "test_node_sync.py",
}
# Two sentinels from different tiers: if a regex drift silently stopped
# matching real explicit-env sites, these named files would still be expected
# to show up, so their absence fails loudly instead of the scan just shrinking.
_SENTINELS = {
    "e2e/test_up.py",
    "dist/test_packaged_serve.py",
}


class TestNoTestRunsTheRealDaemon:
    """Every explicit child ``env=`` dict that turns the psmux boost off must
    turn node sync off on the line immediately after -- a comment or a
    docstring that merely *mentions* the sync pin does not satisfy this law,
    only a real adjacent assignment does.

    The scan is indexed on the MAGENT_PSMUX_BOOST pin: every explicit child
    env in this suite already pins the boost (the other reaches-past-HOME
    law), so a fixture that forgets the boost pin also escapes this law along
    with its sync twin. Only the ``env["NAME"] = "0"`` / ``setenv("NAME",
    "0")`` assignment form is matched -- the keyword form
    ``env.update(MAGENT_PSMUX_BOOST="0")`` is not.

    ``tests/conftest.py`` is the one legitimate non-adjacent site: its sync
    pin sits several lines after its boost pin, separated by the
    SESSION0_POLICY block, and it is dropped from this scan because it is
    already covered by ``test_the_suite_runs_with_node_sync_off`` below via
    ``get_env()``.
    """

    def test_the_suite_runs_with_node_sync_off(self, monkeypatch):
        monkeypatch.setattr("magent.env._cached_env", None)
        assert get_env().node_sync is False

    def test_every_boost_pin_is_immediately_followed_by_its_sync_twin(self):
        scanned: set[str] = set()
        missing: list[str] = []
        for path in sorted(_TESTS.rglob("*.py")):
            if path.name in _EXEMPT or path.name == "conftest.py":
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            rel = path.relative_to(_TESTS).as_posix()
            for i, line in enumerate(lines):
                if line.lstrip().startswith("#"):
                    continue
                if not _BOOST_PIN.search(line):
                    continue
                scanned.add(rel)
                twin = line.replace("MAGENT_PSMUX_BOOST", "MAGENT_NODE_SYNC")
                next_line = lines[i + 1] if i + 1 < len(lines) else None
                if next_line != twin:
                    missing.append(rel)
        for sentinel in sorted(_SENTINELS):
            assert sentinel in scanned, (
                f"expected {sentinel} to carry an explicit-env boost pin -- "
                "has the pin's shape drifted, or was the file removed?"
            )
        assert not missing, (
            "these files turn MAGENT_PSMUX_BOOST off for a child but the very "
            "next line does not turn MAGENT_NODE_SYNC off the same way (a "
            "real serve there would ssh into real machines): "
            + ", ".join(sorted(set(missing)))
        )


POOL = {
    "second": NodeConfig(nick="second", host="devino-second", user="amin"),
    "third": NodeConfig(nick="third", host="devino-third", user="amin"),
}


def _config(*, pool=None, projects=None, **sync) -> MagentConfig:
    return MagentConfig(
        projects=(
            projects
            if projects is not None
            else [
                ProjectConfig(path="api", node="second"),
                ProjectConfig(path="web", node="third"),
            ]
        ),
        settings=Settings(
            nodes=dict(POOL if pool is None else pool), node_sync=NodeSyncConfig(**sync)
        ),
    )


def _entry(nick: str, sid: str) -> NodeMapEntry:
    return NodeMapEntry(
        nick=nick,
        sid=sid,
        placed_ts=1.0,
        attached_existing=False,
        remote_root=f"~/magent/{sid}",
    )


@pytest.fixture
def placed(tmp_path, monkeypatch):
    """The node map: api placed on second, web on third."""
    root = tmp_path / "nodes"
    monkeypatch.setattr(nodes, "NODES_DIR", root)
    monkeypatch.setattr(nodes, "NODE_MAP_PATH", root / "node-map.json")
    nodes.write_node_map(
        {"api": _entry("second", "api"), "web": _entry("third", "web")}
    )
    return root


class TestWhenTheDaemonIsWanted:
    def test_a_project_on_a_pool_node_wants_it(self):
        assert node_sync.wanted(_config())

    def test_no_node_project_does_not(self):
        assert not node_sync.wanted(_config(projects=[ProjectConfig(path="api")]))

    def test_an_empty_pool_does_not(self):
        assert not node_sync.wanted(_config(pool={}))

    def test_a_disabled_node_project_alone_does_not(self):
        assert not node_sync.wanted(
            _config(projects=[ProjectConfig(path="api", node="second", enabled=False)])
        )

    def test_a_cloud_project_alone_does_not(self):
        assert not node_sync.wanted(
            _config(projects=[ProjectConfig(path="api", node="cloud")])
        )

    def test_the_tick_is_the_shorter_of_the_two_intervals(self):
        assert (
            node_sync.tick_interval_s(_config(pull_interval_s=30, sample_interval_s=60))
            == 30.0
        )
        assert (
            node_sync.tick_interval_s(_config(pull_interval_s=90, sample_interval_s=60))
            == 60.0
        )


class TestStateStores:
    def test_each_placed_session_is_a_store_named_by_its_project(self, placed):
        assert node_sync.state_stores() == [
            ("api", "@second", nodes.state_dir("second", "api")),
            ("web", "@third", nodes.state_dir("third", "web")),
        ]


class TestTheNodeLock:
    def test_a_held_node_is_waited_for_then_refused(self):
        naps: list[float] = []
        clock = iter([0.0, 0.1, 0.3, 0.6])
        with (
            exclusive_lock("node-pull-second"),
            pytest.raises(LockHeld),
            node_sync.node_lock(
                "second", wait_s=0.5, sleep=naps.append, now=lambda: next(clock)
            ),
        ):
            pass
        assert naps == [node_sync.NODE_LOCK_RETRY_S] * 2

    def test_an_error_inside_the_lock_is_not_mistaken_for_contention(self):
        def never(_s: float) -> None:
            raise AssertionError("slept")

        with (
            pytest.raises(LockHeld, match="inner"),
            node_sync.node_lock("second", sleep=never),
        ):
            raise LockHeld("inner")
        with node_sync.node_lock("second"):
            pass


# An edit that is visibly different from tmp_config's empty project list.
_ONE_PROJECT = json.dumps({"version": SCHEMA_VERSION, "projects": [{"path": "api"}]})


class TestConfigWatch:
    def test_the_config_is_reloaded_only_when_the_file_changes(
        self, tmp_config, monkeypatch
    ):
        path = Path(tmp_config({"version": SCHEMA_VERSION, "projects": []}))
        loads: list[str] = []
        real = node_sync.load_config

        def counting(p: str) -> MagentConfig:
            loads.append(p)
            return real(p)

        monkeypatch.setattr(node_sync, "load_config", counting)
        watch = node_sync.ConfigWatch(path)
        first = watch.current()
        assert first is not None
        assert watch.current() is first
        assert len(loads) == 1
        os.utime(path, (1, 1))
        watch.current()
        assert len(loads) == 2

    def test_a_broken_edit_keeps_the_last_good_config(self, tmp_config, caplog):
        caplog.set_level(logging.WARNING, logger="magent.nodes")
        path = Path(tmp_config({"version": SCHEMA_VERSION, "projects": []}))
        watch = node_sync.ConfigWatch(path)
        good = watch.current()
        path.write_text("{not json", encoding="utf-8")
        os.utime(path, (1, 1))
        assert watch.current() is good
        assert any("did not load" in r.getMessage() for r in caplog.records)

    def test_a_missing_file_with_nothing_loaded_is_none(self, tmp_path):
        assert node_sync.ConfigWatch(tmp_path / "nope.json").current() is None

    def test_an_edit_between_the_callers_load_and_the_watch_is_seen(self, tmp_config):
        """The caller stamps the file BEFORE it loads; a watch that stamped at
        construction would take an edit made in between for the loaded one."""
        path = Path(tmp_config({"version": SCHEMA_VERSION, "projects": []}))
        stamp = node_sync.config_stamp(path)
        loaded = node_sync.load_config(str(path))
        path.write_text(_ONE_PROJECT, encoding="utf-8")
        watch = node_sync.ConfigWatch(path, loaded, stamp=stamp)
        fresh = watch.current()
        assert fresh is not loaded
        assert fresh is not None
        assert [p.path for p in fresh.projects] == ["api"]

    def test_a_rewrite_with_the_same_mtime_but_a_new_size_is_seen(self, tmp_config):
        path = Path(tmp_config({"version": SCHEMA_VERSION, "projects": []}))
        watch = node_sync.ConfigWatch(path)
        first = watch.current()
        before = path.stat()
        path.write_text(_ONE_PROJECT, encoding="utf-8")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert path.stat().st_mtime_ns == before.st_mtime_ns
        fresh = watch.current()
        assert fresh is not first
        assert fresh is not None
        assert [p.path for p in fresh.projects] == ["api"]


class TestTheDaemonsPidFile:
    def test_no_pid_file_is_no_daemon(self):
        assert node_sync.daemon_pid() is None
        assert node_sync.stop_daemon() is False

    def test_a_live_pid_is_the_daemon(self):
        node_sync._PID_PATH.parent.mkdir(parents=True, exist_ok=True)
        node_sync._PID_PATH.write_text(str(os.getpid()))
        assert node_sync.daemon_pid() == os.getpid()

    def test_a_recycled_pid_is_never_killed(self, unrelated):
        """The lock is free, so no daemon exists and the pid file names a
        stranger: clear the leftovers, kill nothing."""
        _record_pid(unrelated.pid)
        write_heartbeat(node_sync.HEARTBEAT_NAME)  # a crash marker
        assert node_sync.stop_daemon() is True
        assert unrelated.poll() is None
        assert not node_sync._PID_PATH.exists()
        assert heartbeat_age(node_sync.HEARTBEAT_NAME) is None

    def test_a_sigterm_is_given_time_to_land(self, daemon_lock, monkeypatch):
        """POSIX SIGTERM is asynchronous: the pid can still answer right after
        the kill. The stop waits for it instead of reporting a failure."""
        answers = iter([True, True, False])
        monkeypatch.setattr(node_sync, "pid_alive", lambda _pid: next(answers))
        kills: list[int] = []
        monkeypatch.setattr(node_sync, "_kill", lambda pid: kills.append(pid) or True)
        naps: list[float] = []
        _record_pid(424242)
        write_heartbeat(node_sync.HEARTBEAT_NAME)
        assert node_sync.stop_daemon(sleep=naps.append) is True
        assert kills == [424242]
        assert naps == [node_sync.STOP_POLL_S]
        assert heartbeat_age(node_sync.HEARTBEAT_NAME) is None

    def test_a_daemon_that_outlives_the_settle_is_not_called_stopped(
        self, daemon_lock, monkeypatch
    ):
        monkeypatch.setattr(node_sync, "pid_alive", lambda _pid: True)
        monkeypatch.setattr(node_sync, "_kill", lambda _pid: True)
        clock = iter([0.0, 1.0, node_sync.STOP_SETTLE_S + 0.5])
        _record_pid(424242)
        assert (
            node_sync.stop_daemon(sleep=lambda _s: None, now=lambda: next(clock))
            is False
        )
        assert node_sync._PID_PATH.exists()


class TestTheImportLaw:
    def test_node_sync_never_imports_cli_launch_or_upload_server(self):
        """Spec §5 / DECISION-19: node_sync is a leaf. launch and upload_server
        import IT (the supervisor), and cli imports everything; walking the whole
        tree catches an in-body import too."""
        tree = ast.parse(Path(node_sync.__file__).read_text(encoding="utf-8"))
        names = [
            a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
        ]
        names += [
            n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
        ]
        names += [
            f"magent.{a.name}"
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module == "magent"
            for a in n.names
        ]
        banned = ("magent.cli", "magent.launch", "magent.upload_server")
        assert [m for m in names if m.startswith(banned)] == []


class TestTheDaemonHasOneName:
    def test_the_literal_occurs_exactly_once_in_src(self):
        """DECISION-17 / DECISION-26 i: HEARTBEAT_NAME is the only "node-sync"
        string literal in src/. Docstrings and f-string pieces are other
        constants, so only a real second copy of the name trips this."""
        src = Path(node_sync.__file__).parent
        hits = [
            f"{path.relative_to(src).as_posix()}:{n.lineno}"
            for path in sorted(src.rglob("*.py"))
            for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(n, ast.Constant) and n.value == "node-sync"
        ]
        assert len(hits) == 1 and hits[0].startswith("node_sync.py:"), hits

    def test_every_lock_pid_and_thread_name_derives_from_it(self):
        name = node_sync.HEARTBEAT_NAME
        assert name == node_sync.LOCK_NAME
        assert f"{name}-supervisor" == node_sync.SUPERVISOR_LOCK_NAME
        assert node_sync._PID_PATH.name == f"{name}.pid"


def _record_pid(pid: int) -> None:
    node_sync._PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    node_sync._PID_PATH.write_text(str(pid))


def _capture_nodes_log(caplog: pytest.LogCaptureFixture) -> None:
    """Capture the nodes log at DEBUG. get_logger sets the configured level on
    FIRST use, which would undo caplog's level if it ran afterwards, so the
    logger is configured before caplog lowers it."""
    get_logger(node_sync.LOG_NAME)
    caplog.set_level(logging.DEBUG, logger=f"magent.{node_sync.LOG_NAME}")


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.fixture
def unrelated() -> Iterator[subprocess.Popen[bytes]]:
    """A live process that is NOT the daemon -- what a recycled pid names."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        yield child
    finally:
        child.kill()
        child.wait(timeout=10)


@pytest.fixture
def daemon_lock() -> Iterator[None]:
    """The daemon's lock, held by this test: the daemon is alive."""
    with exclusive_lock(node_sync.LOCK_NAME):
        yield


@pytest.fixture
def sync_on(monkeypatch):
    monkeypatch.setenv("MAGENT_NODE_SYNC", "1")
    monkeypatch.setattr("magent.env._cached_env", None)


@pytest.fixture
def spawned(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(launch, "spawn_detached", calls.append)
    return calls


class TestEnsureNodeSync:
    @pytest.fixture(autouse=True)
    def _no_wedge_on_record(self, monkeypatch):
        monkeypatch.setattr(launch, "_node_sync_report", launch._NodeSyncReport())

    def test_the_argv_runs_node_sync_with_the_same_config(self):
        assert launch.node_sync_argv("C:/cfg.json") == [
            sys.executable,
            "-m",
            "magent",
            "--config",
            "C:/cfg.json",
            "node",
            "sync",
        ]
        assert launch.node_sync_argv(None) == [
            sys.executable,
            "-m",
            "magent",
            "node",
            "sync",
        ]

    def test_an_env_that_goes_bad_is_reported_as_the_node_sync_supervisors(
        self, monkeypatch, caplog
    ):
        """Fail-open like every supervisor (the config gate still applies), and
        the log line names THIS supervisor in THIS subsystem's log."""
        from pydantic import ValidationError

        def _bad():
            raise ValidationError.from_exception_data("MagentEnv", [])

        monkeypatch.setattr("magent.env.get_env", _bad)
        caplog.set_level(logging.DEBUG)
        assert launch.node_sync_env_enabled() is True
        (record,) = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert record.name == "magent.nodes"
        assert record.getMessage().startswith(
            "node sync supervisor: environment did not validate"
        )

    def test_nothing_is_spawned_when_the_env_says_no(self, spawned):
        assert launch.ensure_node_sync(_config(), "cfg.json") is False
        assert spawned == []

    def test_nothing_is_spawned_when_no_project_runs_on_a_node(self, sync_on, spawned):
        assert (
            launch.ensure_node_sync(_config(projects=[ProjectConfig(path="api")]))
            is False
        )
        assert spawned == []

    def test_a_dead_daemon_is_started_with_the_callers_config(self, sync_on, spawned):
        assert launch.ensure_node_sync(_config(), "cfg.json") is True
        assert spawned == [launch.node_sync_argv("cfg.json")]

    def test_a_recycled_pid_is_no_daemon(self, sync_on, spawned, unrelated, caplog):
        """After a crash or a reboot the pid file survives and Windows hands
        the number to an unrelated process. The lock is free, so there is no
        daemon: spawn one, and do not call the stranger a wedged daemon."""
        _capture_nodes_log(caplog)
        _record_pid(unrelated.pid)
        assert launch.ensure_node_sync(_config(), "cfg.json") is True
        assert spawned == [launch.node_sync_argv("cfg.json")]
        assert _warnings(caplog) == []

    def test_a_live_daemon_is_never_re_aimed_and_says_nothing(
        self, sync_on, spawned, daemon_lock, caplog
    ):
        """The probe's LockHeld is the answer "a daemon is alive", caught
        inside ensure_node_sync: it never escapes to the supervisor, which
        would blame it on another server."""
        _capture_nodes_log(caplog)
        _record_pid(os.getpid())
        write_heartbeat(node_sync.HEARTBEAT_NAME)
        assert launch.ensure_node_sync(_config(), "other.json") is True
        assert spawned == []
        assert _warnings(caplog) == []

    def test_a_live_daemon_with_a_stale_heartbeat_is_reported_not_replaced(
        self, sync_on, spawned, daemon_lock, caplog
    ):
        _capture_nodes_log(caplog)
        _record_pid(os.getpid())
        assert launch.ensure_node_sync(_config()) is False
        assert spawned == []
        (warning,) = _warnings(caplog)
        assert "heartbeat is stale" in warning
        assert str(os.getpid()) in warning

    def test_a_wedge_is_reported_once_and_its_recovery_once(
        self, sync_on, spawned, daemon_lock, caplog
    ):
        _capture_nodes_log(caplog)
        _record_pid(os.getpid())
        launch.ensure_node_sync(_config())
        launch.ensure_node_sync(_config())
        assert len(_warnings(caplog)) == 1
        write_heartbeat(node_sync.HEARTBEAT_NAME)
        launch.ensure_node_sync(_config())
        launch.ensure_node_sync(_config())
        infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert len([m for m in infos if "fresh again" in m]) == 1
        assert spawned == []


def _run_supervisor(
    config_path: str | None, stop: threading.Event, *, interval: float = 0.0
) -> None:
    """Run serve's node sync supervisor on its own thread, bounded: a loop
    that never reaches its stop fails the test instead of hanging it."""
    from magent import upload_server

    thread = threading.Thread(
        target=upload_server._supervise_node_sync,
        args=(config_path, stop),
        kwargs={"interval": interval},
        daemon=True,
    )
    thread.start()
    thread.join(timeout=10)
    alive = thread.is_alive()
    stop.set()  # release a runaway loop before failing
    assert not alive, "the supervisor loop never stopped"


def _one_tick() -> threading.Event:
    """A stop that is already set: the loop runs exactly one iteration."""
    stop = threading.Event()
    stop.set()
    return stop


class TestServeSupervisesTheDaemon:
    def test_the_supervisor_stands_down_when_the_env_says_no(
        self, tmp_config, monkeypatch
    ):
        monkeypatch.setenv("MAGENT_NODE_SYNC", "0")
        monkeypatch.setattr("magent.env._cached_env", None)
        path = tmp_config({"version": SCHEMA_VERSION, "projects": []})
        monkeypatch.setattr(
            launch, "ensure_node_sync", lambda *a, **k: pytest.fail("ensured")
        )
        _run_supervisor(path, threading.Event(), interval=999)

    def test_the_supervisor_ensures_the_daemon_with_serves_config(
        self, sync_on, tmp_config, monkeypatch
    ):
        path = tmp_config({"version": SCHEMA_VERSION, "projects": []})
        stop = threading.Event()
        seen: list[str | None] = []

        def ensure(_config, config_path=None):
            seen.append(config_path)
            stop.set()
            return False

        monkeypatch.setattr(launch, "ensure_node_sync", ensure)
        _run_supervisor(path, stop)
        assert seen == [path]

    def test_the_config_is_read_once_until_it_changes(
        self, sync_on, tmp_config, monkeypatch
    ):
        path = tmp_config({"version": SCHEMA_VERSION, "projects": []})
        stop = threading.Event()
        loads: list[str] = []
        real = node_sync.load_config
        monkeypatch.setattr(
            node_sync, "load_config", lambda p: loads.append(p) or real(p)
        )
        seen: list[object] = []

        def ensure(config, config_path=None):
            seen.append(config)
            if len(seen) == 2:
                stop.set()
            return False

        monkeypatch.setattr(launch, "ensure_node_sync", ensure)
        _run_supervisor(path, stop)
        assert len(seen) == 2
        assert len(loads) == 1

    def test_a_lock_held_inside_the_ensure_is_not_blamed_on_another_server(
        self, sync_on, tmp_config, monkeypatch, caplog
    ):
        """Only the SUPERVISOR lock means another serve is supervising. Any
        other LockHeld is a failed check and is logged as one."""
        _capture_nodes_log(caplog)
        path = tmp_config({"version": SCHEMA_VERSION, "projects": []})

        def ensure(_config, _config_path=None):
            raise LockHeld("some other lock is held by another process")

        monkeypatch.setattr(launch, "ensure_node_sync", ensure)
        _run_supervisor(path, _one_tick())
        messages = [r.getMessage() for r in caplog.records]
        assert "node sync supervisor: check failed" in messages
        assert not any("another server" in m for m in messages)

    def test_another_supervisor_is_still_named_as_such(
        self, sync_on, tmp_config, monkeypatch, caplog
    ):
        _capture_nodes_log(caplog)
        path = tmp_config({"version": SCHEMA_VERSION, "projects": []})
        monkeypatch.setattr(
            launch, "ensure_node_sync", lambda *a, **k: pytest.fail("ensured")
        )
        with exclusive_lock(node_sync.SUPERVISOR_LOCK_NAME):
            _run_supervisor(path, _one_tick())
        assert any("another server" in r.getMessage() for r in caplog.records)

    def test_a_config_lookup_that_raises_is_logged_and_survived(
        self, sync_on, monkeypatch, caplog
    ):
        """serve's cwd deleted and no --config: Path.cwd() raises on POSIX.
        That is one failed tick, logged, not a thread that dies in silence."""
        _capture_nodes_log(caplog)

        def gone(_path=None):
            raise FileNotFoundError("the working directory is gone")

        monkeypatch.setattr("magent.paths.find_config", gone)
        _run_supervisor(None, _one_tick())
        assert any(
            r.getMessage() == "node sync supervisor: check failed"
            for r in caplog.records
        )

    def test_serve_starts_the_node_sync_supervisor(self, monkeypatch):
        from magent import upload_server

        started: list[object] = []
        real_thread = threading.Thread

        class _Recording(real_thread):
            """Records WHAT run_server wanted to run and runs none of it."""

            def __init__(self, *args, target=None, **kwargs) -> None:
                super().__init__(*args, target=target, **kwargs)
                self.recorded_target = target

            def start(self) -> None:
                started.append(self.recorded_target)

        monkeypatch.setattr(upload_server.threading, "Thread", _Recording)
        monkeypatch.setattr(upload_server, "_bind_addresses", lambda _h: ["127.0.0.1"])

        class _FakeServer:
            def __init__(self, addr, _handler) -> None:
                self.server_address = addr

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def shutdown(self) -> None:
                return None

            def server_close(self) -> None:
                return None

        monkeypatch.setattr(upload_server, "_NoFqdnHTTPServer", _FakeServer)
        with pytest.raises(KeyboardInterrupt):
            upload_server.run_server(port=0)
        assert upload_server._supervise_node_sync in started
