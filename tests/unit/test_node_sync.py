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
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from magent import agent_state, launch, node_sync, nodes, remote_mux
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
from tests.unit._pull_reply import pull_meta, pull_reply

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

    def test_no_map_file_is_no_stores(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nodes, "NODE_MAP_PATH", tmp_path / "node-map.json")
        assert node_sync.state_stores() == []

    def test_a_torn_map_is_an_error_not_an_empty_listing(self, placed):
        """An empty listing would make the attention engine drop every node
        row for the tick; the error lets it hold the last records instead."""
        nodes.NODE_MAP_PATH.write_text("{", encoding="utf-8")
        with pytest.raises(ValueError):
            node_sync.state_stores()

    @pytest.mark.parametrize("bad", ["/etc", "../x"])
    def test_a_sid_that_is_no_directory_name_here_is_skipped_and_named(
        self, placed, caplog, monkeypatch, bad
    ):
        """state_dir joins a sid VERBATIM, so ``/etc`` would read a store
        outside the nodes dir. Warned once, however many ticks ask again."""
        monkeypatch.setattr(node_sync, "_UNPULLABLE_WARNED", set())
        _capture_nodes_log(caplog)
        nodes.write_node_map(
            {"api": _entry("second", "api"), "evil": _entry("second", bad)}
        )
        assert node_sync.state_stores() == [
            ("api", "@second", nodes.state_dir("second", "api")),
        ]
        node_sync.state_stores()
        (warning,) = _warnings(caplog)
        assert repr(bad) in warning
        assert "evil" in warning


class TestTheNodeLock:
    def test_a_held_node_is_waited_for_then_refused(self):
        naps: list[float] = []
        clock = iter([0.0, 0.1, 0.3, 0.6])
        with (
            exclusive_lock("node-pull-second"),
            pytest.raises(node_sync.NodeLockHeld),
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
            pytest.raises(LockHeld, match="inner") as exc,
            node_sync.node_lock("second", sleep=never),
        ):
            raise LockHeld("inner")
        # Only a refused ACQUIRE is the node's own lock: the body's LockHeld
        # is some other lock, and the syncer must not read it as contention.
        assert not isinstance(exc.value, node_sync.NodeLockHeld)
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
        would blame it on another server. A healthy daemon is False: the
        answer reports a spawn, like ensure_upload_server's."""
        _capture_nodes_log(caplog)
        _record_pid(os.getpid())
        write_heartbeat(node_sync.HEARTBEAT_NAME)
        assert launch.ensure_node_sync(_config(), "other.json") is False
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
    that never reaches its stop fails the test instead of hanging it. A loop
    that never reaches its wait cannot be released by ``stop.set()``; the
    daemon thread then outlives the monkeypatch teardown, which only happens
    after the test has already failed."""
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


class TestAttentionSeesNodeSessions:
    @pytest.fixture
    def local_store(self, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_state, "STATE_DIR", tmp_path / "local-state")
        monkeypatch.setattr(agent_state, "_swept_this_process", True)

    def test_a_node_sessions_state_reaches_the_engine_under_its_project(
        self, placed, local_store
    ):
        from magent.cli.attention_cmd import engine_from_config

        store = nodes.state_dir("second", "api")
        store.mkdir(parents=True)
        record = {
            "state": "needs-input",
            "ts": time.time(),
            "cwd": "/home/amin/magent/api",
            "session_id": "s",
        }
        (store / "k.json").write_text(json.dumps(record), encoding="utf-8")
        views = engine_from_config(_config()).poll()
        assert [(v.name, v.cwd, v.state) for v in views] == [
            ("api", "@second:/home/amin/magent/api", "needs-input")
        ]

    def test_without_a_node_project_the_engine_reads_only_this_pc(
        self, placed, local_store
    ):
        from magent.cli.attention_cmd import engine_from_config

        store = nodes.state_dir("second", "api")
        store.mkdir(parents=True)
        (store / "k.json").write_text(
            json.dumps(
                {"state": "done", "ts": time.time(), "cwd": "/x", "session_id": "s"}
            ),
            encoding="utf-8",
        )
        assert (
            engine_from_config(_config(projects=[ProjectConfig(path="api")])).poll()
            == []
        )


REFUSED = "ssh: connect to host devino-second port 22: Connection refused\n"


def _answer(
    fake, host: str, *, meta=None, files=None, rc: int = 0, stderr: str = ""
) -> None:
    stdout = (
        pull_reply(meta if meta is not None else pull_meta(), files) if rc == 0 else ""
    )
    fake.set_reply(host, stdout=stdout, stderr=stderr, rc=rc)


def _forget_replies(fake) -> None:
    (fake.base / "replies.json").unlink(missing_ok=True)


def _payload(call) -> dict[str, object]:
    return json.loads(call.stdin.rsplit(b"\n__MAGENT_PAYLOAD__\n", 1)[1])


def _calls_to(fake, host: str) -> list:
    return [c for c in fake.calls() if c.argv[-2] == f"amin@{host}"]


class TestOneTick:
    def test_one_tick_is_one_ssh_per_node(self, placed, fake_ssh):
        _answer(fake_ssh, "devino-second")
        _answer(fake_ssh, "devino-third")
        results = node_sync.NodeSyncer(_config()).tick()
        assert results == {"second": (node_sync.OK, ""), "third": (node_sync.OK, "")}
        assert sorted(c.argv[-2] for c in fake_ssh.calls()) == [
            "amin@devino-second",
            "amin@devino-third",
        ]

    def test_a_node_with_nothing_placed_on_it_is_still_asked_for_its_load(
        self, placed, fake_ssh
    ):
        nodes.write_node_map({"api": _entry("second", "api")})
        _answer(fake_ssh, "devino-second")
        _answer(fake_ssh, "devino-third")
        node_sync.NodeSyncer(_config()).tick()
        (call,) = _calls_to(fake_ssh, "devino-third")
        assert _payload(call)["sids"] == {}

    def test_the_session_list_is_mirrored_on_this_pcs_clock(self, placed, fake_ssh):
        _answer(fake_ssh, "devino-second", meta=pull_meta(sessions=["api", "other"]))
        _answer(fake_ssh, "devino-third")
        node_sync.NodeSyncer(_config(), now=lambda: 777.0).tick()
        assert nodes.read_sessions("second") == nodes.NodeSessions(
            ts=777.0, sessions=("api", "other")
        )

    def test_each_placed_session_is_asked_for_from_the_beginning(
        self, placed, fake_ssh
    ):
        _answer(fake_ssh, "devino-second")
        _answer(fake_ssh, "devino-third")
        node_sync.NodeSyncer(_config()).tick()
        (call,) = _calls_to(fake_ssh, "devino-second")
        assert _payload(call)["sids"] == {
            "api": {"roots": ["~/magent/api"], "project_dir": None, "since": 0.0}
        }

    def test_a_mapped_session_whose_tmux_is_gone_is_still_pulled(
        self, placed, fake_ssh
    ):
        """DECISION-22: a `down` whose final pull failed kills the session but
        keeps the map entry, so a later tick fetches what is left on disk. A
        sid missing from list-sessions is not-live, never an error."""
        _answer(
            fake_ssh,
            "devino-second",
            meta=pull_meta(sessions=[]),
            files={"api/transcripts/abc.jsonl": "x\n"},
        )
        _answer(fake_ssh, "devino-third")
        assert node_sync.NodeSyncer(_config()).tick()["second"] == (node_sync.OK, "")
        (call,) = _calls_to(fake_ssh, "devino-second")
        assert set(_payload(call)["sids"]) == {"api"}
        sessions = nodes.read_sessions("second")
        assert sessions is not None
        assert sessions.sessions == ()
        assert (nodes.transcripts_dir("second", "api") / "abc.jsonl").read_text() == (
            "x\n"
        )

    def test_a_session_this_pc_cannot_store_is_skipped_with_one_warning(
        self, placed, fake_ssh, caplog
    ):
        _capture_nodes_log(caplog)
        nodes.write_node_map(
            {"api": _entry("second", "api"), "odd": _entry("second", "CON")}
        )
        _answer(fake_ssh, "devino-second")
        _answer(fake_ssh, "devino-third")
        syncer = node_sync.NodeSyncer(_config())
        syncer.tick()
        syncer.tick()
        assert all(
            set(_payload(c)["sids"]) == {"api"}
            for c in _calls_to(fake_ssh, "devino-second")
        )
        assert [
            r.getMessage()
            for r in caplog.records
            if "cannot be mirrored" in r.getMessage()
        ] == ["node second: session 'CON' cannot be mirrored on this PC; skipping it"]

    def test_a_transport_failure_is_unreachable(self, placed, fake_ssh):
        _answer(fake_ssh, "devino-second", rc=255, stderr=REFUSED)
        _answer(fake_ssh, "devino-third")
        results = node_sync.NodeSyncer(_config()).tick()
        assert results["second"] == (node_sync.UNREACHABLE, REFUSED.strip())
        assert results["third"] == (node_sync.OK, "")


def _node_warnings(caplog, nick: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "magent.nodes"
        and r.levelno == logging.WARNING
        and f"node {nick}:" in r.getMessage()
    ]


class TestANodeFailsAlone:
    def test_an_unreachable_node_keeps_its_snapshot_while_the_other_advances(
        self, placed, fake_ssh
    ):
        clock = iter([100.0, 100.0, 200.0, 200.0])
        syncer = node_sync.NodeSyncer(_config(), now=lambda: next(clock))
        _answer(fake_ssh, "devino-second")
        _answer(fake_ssh, "devino-third")
        syncer.tick()
        _forget_replies(fake_ssh)
        _answer(fake_ssh, "devino-second", rc=255, stderr=REFUSED)
        _answer(fake_ssh, "devino-third")
        results = syncer.tick()
        assert results["second"][0] == node_sync.UNREACHABLE
        assert nodes.read_sessions("second").ts == 100.0
        assert nodes.read_sessions("third").ts == 200.0

    def test_a_node_that_stays_down_is_logged_once_and_once_again_when_it_returns(
        self, placed, fake_ssh, caplog
    ):
        _capture_nodes_log(caplog)
        syncer = node_sync.NodeSyncer(_config())
        _answer(fake_ssh, "devino-second", rc=255, stderr=REFUSED)
        _answer(fake_ssh, "devino-third")
        for _ in range(3):
            syncer.tick()
        _forget_replies(fake_ssh)
        _answer(fake_ssh, "devino-second")
        _answer(fake_ssh, "devino-third")
        syncer.tick()
        assert _node_warnings(caplog, "second") == [
            f"node second: unreachable ({REFUSED.strip()})"
        ]
        assert [
            r.getMessage()
            for r in caplog.records
            if "reachable again" in r.getMessage()
        ] == ["node second: reachable again"]
        assert not [r for r in caplog.records if r.getMessage().startswith("node call")]
        assert _node_warnings(caplog, "third") == []

    def test_a_hung_node_counts_as_unreachable_and_warns_once(
        self, placed, fake_ssh, caplog, monkeypatch
    ):
        _capture_nodes_log(caplog)
        monkeypatch.setattr(remote_mux, "PULL_TIMEOUT_S", 1.0)
        cfg = _config(
            pool={"second": POOL["second"]},
            projects=[ProjectConfig(path="api", node="second")],
        )
        fake_ssh.set_mode("timeout")
        syncer = node_sync.NodeSyncer(cfg)
        assert syncer.tick() == {
            "second": (node_sync.UNREACHABLE, "timed out after 1s")
        }
        syncer.tick()
        assert _node_warnings(caplog, "second") == [
            "node second: unreachable (timed out after 1s)"
        ]
        assert not [
            r
            for r in caplog.records
            if r.getMessage().startswith("node call timed out")
        ]

    def test_a_node_that_answers_garbage_fails_alone(self, placed, fake_ssh):
        fake_ssh.set_reply("devino-second", stdout="hello\n")
        _answer(fake_ssh, "devino-third")
        results = node_sync.NodeSyncer(_config()).tick()
        assert results["second"] == (
            node_sync.FAILED,
            "no MAGENT-PULL header in the reply",
        )
        assert results["third"] == (node_sync.OK, "")

    def test_a_missing_ssh_client_fails_every_node_without_raising(self, placed):
        results = node_sync.NodeSyncer(_config()).tick()
        assert results == {
            "second": (node_sync.FAILED, "ssh client not found on PATH"),
            "third": (node_sync.FAILED, "ssh client not found on PATH"),
        }

    def test_a_node_that_would_run_as_root_is_misconfigured_and_never_dialled(
        self, placed, fake_ssh
    ):
        pool = {
            "second": NodeConfig(nick="second", host="devino-second"),
            "third": POOL["third"],
        }
        _answer(fake_ssh, "devino-third")
        results = node_sync.NodeSyncer(_config(pool=pool), local_user="root").tick()
        assert results["second"][0] == node_sync.MISCONFIGURED
        assert "(D4)" in results["second"][1]
        assert _calls_to(fake_ssh, "devino-second") == []

    def test_a_node_being_pulled_elsewhere_is_skipped_silently(
        self, placed, fake_ssh, caplog
    ):
        _capture_nodes_log(caplog)
        _answer(fake_ssh, "devino-second")
        _answer(fake_ssh, "devino-third")
        with exclusive_lock("node-pull-second"):
            results = node_sync.NodeSyncer(_config()).tick()
        assert results["second"][0] == node_sync.LOCKED
        assert results["third"] == (node_sync.OK, "")
        assert _calls_to(fake_ssh, "devino-second") == []
        assert _node_warnings(caplog, "second") == []


def _snap() -> remote_mux.NodeSnapshot:
    return remote_mux.NodeSnapshot(
        now=1.0,
        sessions=(),
        sample=None,
        realpaths={},
        state_files={},
        files=(),
        failed_sids=frozenset(),
    )


def _pull_raising(raises: dict[str, BaseException]):
    """The ``pull=`` seam: raise ``raises[nick]`` for that node (read at call
    time, so a test can change it between ticks), else an empty snapshot."""

    def pull(node, sids):
        exc = raises.get(node.nick)
        if exc is not None:
            raise exc
        return _snap()

    return pull


def _node_errors(caplog) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == "magent.nodes" and r.levelno == logging.ERROR
    ]


class TestABugInOneNodeFailsItAlone:
    def test_a_bug_in_one_nodes_pull_fails_that_node_and_the_tick_goes_on(self, placed):
        syncer = node_sync.NodeSyncer(
            _config(), pull=_pull_raising({"second": KeyError("sid")})
        )
        assert syncer.tick() == {
            "second": (node_sync.FAILED, "internal error: KeyError"),
            "third": (node_sync.OK, ""),
        }
        assert nodes.read_sessions("third") is not None

    def test_a_bug_is_one_error_with_its_traceback_per_state_change(
        self, placed, caplog
    ):
        _capture_nodes_log(caplog)
        syncer = node_sync.NodeSyncer(
            _config(), pull=_pull_raising({"second": KeyError("sid")})
        )
        syncer.tick()
        syncer.tick()
        (record,) = _node_errors(caplog)
        assert record.getMessage() == "node second: failed (internal error: KeyError)"
        assert record.exc_info is not None
        assert record.exc_info[0] is KeyError
        # The outcome names the type only; the exception's own words ride on
        # the record's traceback (and so into Sentry), not into tick's result.
        assert (
            logging.Formatter()
            .formatException(record.exc_info)
            .endswith("KeyError: 'sid'")
        )
        assert _node_warnings(caplog, "second") == []

    def test_a_plain_failure_turning_into_a_bug_is_still_reported(self, placed, caplog):
        _capture_nodes_log(caplog)
        raises: dict[str, BaseException] = {
            "second": remote_mux.RemoteError(0, "no MAGENT-PULL header", ("ssh",))
        }
        syncer = node_sync.NodeSyncer(_config(), pull=_pull_raising(raises))
        syncer.tick()
        raises["second"] = TypeError("bad")
        syncer.tick()
        assert _node_warnings(caplog, "second") == [
            "node second: failed (no MAGENT-PULL header)"
        ]
        (record,) = _node_errors(caplog)
        assert record.getMessage() == "node second: failed (internal error: TypeError)"

    def test_a_lock_held_inside_the_pull_is_a_bug_not_contention(self, placed):
        results = node_sync.NodeSyncer(
            _config(), pull=_pull_raising({"second": LockHeld("some other lock")})
        ).tick()
        assert results["second"] == (node_sync.FAILED, "internal error: LockHeld")

    def test_an_os_error_fails_that_node_alone(self, placed):
        results = node_sync.NodeSyncer(
            _config(), pull=_pull_raising({"second": PermissionError("denied")})
        ).tick()
        assert results == {
            "second": (node_sync.FAILED, "denied"),
            "third": (node_sync.OK, ""),
        }

    def test_a_node_back_from_a_failure_is_ok_again_not_reachable_again(
        self, placed, caplog
    ):
        _capture_nodes_log(caplog)
        raises: dict[str, BaseException] = {
            "second": remote_mux.RemoteError(0, "no MAGENT-PULL header", ("ssh",))
        }
        syncer = node_sync.NodeSyncer(_config(), pull=_pull_raising(raises))
        syncer.tick()
        raises.clear()
        syncer.tick()
        messages = [r.getMessage() for r in caplog.records]
        assert "node second: ok again (was failed)" in messages
        assert not [m for m in messages if "reachable again" in m]


class TestWhatCountsAsUnreachable:
    @pytest.mark.parametrize(
        ("err", "outcome"),
        [
            (
                remote_mux.RemoteError(255, "Connection refused", ("ssh",)),
                "unreachable",
            ),
            (
                remote_mux.RemoteError(
                    None, "timed out after 1s", ("ssh",), timed_out=True
                ),
                "unreachable",
            ),
            # Both rc None, neither a silent node: an over-cap reply is a node
            # that answered too much, a spawn failure never left this PC.
            (
                remote_mux.RemoteError(None, "reply exceeded 5 bytes", ("ssh",)),
                "failed",
            ),
            (remote_mux.RemoteError(None, "Permission denied", ("ssh",)), "failed"),
            (remote_mux.RemoteError(1, "boom", ("ssh",)), "failed"),
        ],
    )
    def test_only_a_transport_failure_or_a_timeout_is_unreachable(self, err, outcome):
        assert node_sync._classify(err)[0] == outcome

    def test_a_node_cannot_write_terminal_escapes_into_the_log(self, placed, caplog):
        _capture_nodes_log(caplog)
        tail = "first\n\x1b]0;pwned\x07\x1b[2Jboom\x7f\tend\x9b"
        err = remote_mux.RemoteError(1, tail, ("ssh",))
        results = node_sync.NodeSyncer(
            _config(), pull=_pull_raising({"second": err})
        ).tick()
        assert results["second"] == (node_sync.FAILED, "?]0;pwned??[2Jboom?\tend?")
        (line,) = _node_warnings(caplog, "second")
        assert not re.search(r"[\x00-\x08\x0a-\x1f\x7f-\x9f]", line)


class TestABadWatermarkIsNoWatermark:
    @pytest.mark.parametrize(
        "since",
        [
            pytest.param("1" + "0" * 309, id="309-digit-int"),
            pytest.param("NaN", id="nan"),
            pytest.param("Infinity", id="inf"),
            pytest.param("-Infinity", id="-inf"),
        ],
    )
    def test_a_since_that_is_not_a_finite_time_is_absent(self, placed, since):
        path = nodes.pull_marks_path("second")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Raw text: json.loads accepts each of these, and none is a time.
        # float() of the 309-digit int raises OverflowError, not ValueError.
        bad = '{"since": ' + since + ', "realpath": "/home/amin/magent/api"}'
        path.write_text(
            '{"api": ' + bad + ', "ok": {"since": 5, "realpath": null}}',
            encoding="utf-8",
        )
        assert node_sync._read_marks("second") == {
            "ok": node_sync.Mark(since=5.0, realpath=None)
        }


class TestAnEmptyRemoteRoot:
    def test_a_session_with_no_remote_root_is_skipped_naming_the_field(
        self, placed, caplog
    ):
        _capture_nodes_log(caplog)
        nodes.write_node_map(
            {
                "api": NodeMapEntry(
                    nick="second",
                    sid="api",
                    placed_ts=1.0,
                    attached_existing=False,
                    remote_root="",
                )
            }
        )
        asked: list[set[str]] = []

        def pull(node, sids):
            asked.append(set(sids))
            return _snap()

        results = node_sync.NodeSyncer(_config(), pull=pull).tick()
        assert results["second"] == (node_sync.OK, "")
        assert asked == [set(), set()]
        assert _node_warnings(caplog, "second") == [
            (
                "node second: session 'api' has an empty remote_root in the "
                "node map; skipping it"
            )
        ]


class TestARemovedNodeIsForgotten:
    def test_a_node_removed_and_re_added_while_down_is_warned_again(
        self, placed, caplog
    ):
        _capture_nodes_log(caplog)
        down = remote_mux.RemoteError(255, REFUSED, ("ssh",))
        syncer = node_sync.NodeSyncer(_config(), pull=_pull_raising({"second": down}))
        syncer.tick()
        syncer.reconfigure(_config(pool={"third": POOL["third"]}))
        syncer.tick()
        syncer.reconfigure(_config())
        syncer.tick()
        assert (
            _node_warnings(caplog, "second")
            == [f"node second: unreachable ({REFUSED.strip()})"] * 2
        )


# 200k nested arrays: json.loads raises RecursionError, not ValueError.
_TOO_DEEP = "[" * 200_000


class TestJsonNestedTooDeeply:
    def test_a_node_whose_pull_metadata_nests_too_deeply_has_failed(
        self, placed, caplog
    ):
        """Node-controlled input: a bad answer (FAILED at WARNING), never an
        internal error at ERROR."""
        _capture_nodes_log(caplog)
        reply = (
            remote_mux.PULL_HEADER
            + _TOO_DEEP.encode("ascii")
            + b"\n"
            + remote_mux.PULL_TRAILER
            + b"0\n"
        )

        def pull(node, sids):
            return remote_mux.parse_pull(
                reply, dest=nodes.node_dir(node.nick), sids=frozenset(sids)
            )

        results = node_sync.NodeSyncer(_config(), pull=pull).tick()
        assert results["second"][0] == node_sync.FAILED
        assert results["second"][1].startswith("unreadable pull metadata")
        assert _node_errors(caplog) == []

    def test_a_node_map_nested_too_deeply_does_not_stop_the_tick(self, placed):
        nodes.NODE_MAP_PATH.write_text(_TOO_DEEP, encoding="utf-8")
        asked: list[set[str]] = []

        def pull(node, sids):
            asked.append(set(sids))
            return _snap()

        results = node_sync.NodeSyncer(_config(), pull=pull).tick()
        assert results == {"second": (node_sync.OK, ""), "third": (node_sync.OK, "")}
        assert asked == [set(), set()]

    def test_the_strict_reader_calls_it_a_bad_file(self, placed):
        """ValueError, the one type every strict caller catches for a bad map
        (state_stores' attention engine holds its last records on it)."""
        nodes.NODE_MAP_PATH.write_text(_TOO_DEEP, encoding="utf-8")
        with pytest.raises(ValueError, match="nested too deeply"):
            nodes.load_node_map_strict()
        assert nodes.read_node_map() == {}

    def test_a_watermark_file_nested_too_deeply_is_no_watermark(self, placed):
        path = nodes.pull_marks_path("second")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_TOO_DEEP, encoding="utf-8")
        assert node_sync._read_marks("second") == {}

    def test_a_sessions_file_nested_too_deeply_is_no_snapshot(self, placed):
        path = nodes.sessions_path("second")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_TOO_DEEP, encoding="utf-8")
        assert nodes.read_sessions("second") is None

    def test_a_node_whose_watermark_file_nests_too_deeply_still_pulls(
        self, placed, caplog
    ):
        """A corrupt local file, not a bug: the node pulls from the beginning,
        and nothing is logged at ERROR (which would be a Sentry event)."""
        _capture_nodes_log(caplog)
        path = nodes.pull_marks_path("second")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_TOO_DEEP, encoding="utf-8")
        asked: dict[str, dict[str, remote_mux.SidPull]] = {}

        def pull(node, sids):
            asked[node.nick] = dict(sids)
            return _snap()

        results = node_sync.NodeSyncer(_config(), pull=pull).tick()
        assert results["second"] == (node_sync.OK, "")
        assert asked["second"]["api"].since == 0.0
        assert _node_errors(caplog) == []
