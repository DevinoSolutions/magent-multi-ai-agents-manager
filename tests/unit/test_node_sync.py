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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from magent import agent_state, launch, log, node_sync, nodes, remote_mux
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
from magent.log import get_logger, heartbeat_age, heartbeat_fresh, write_heartbeat
from magent.nodes import NodeMapEntry, encoded_project_dir
from tests.unit._pull_reply import SAMPLE, pull_meta, pull_reply

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

    def test_a_node_removed_while_down_warns_again_when_readded_still_down(
        self, placed, caplog
    ):
        """A node that leaves the pool is forgotten: coming back unreachable
        is a new state to report, not the old one continuing."""
        _capture_nodes_log(caplog)

        def pull(node, _sids):
            if node.nick == "second":
                raise remote_mux.RemoteError(255, REFUSED, ("ssh",))
            return _snapshot()

        third_only = _config(
            pool={"third": POOL["third"]},
            projects=[ProjectConfig(path="web", node="third")],
        )
        syncer = node_sync.NodeSyncer(_config(), pull=pull)
        try:
            syncer.tick()
            syncer.reconfigure(third_only)
            syncer.tick()
            syncer.reconfigure(_config())
            syncer.tick()
        finally:
            syncer.close()
        assert (
            _node_warnings(caplog, "second")
            == [f"node second: unreachable ({REFUSED.strip()})"] * 2
        )


def _second_only(**sync) -> MagentConfig:
    return _config(
        pool={"second": POOL["second"]},
        projects=[ProjectConfig(path="api", node="second")],
        **sync,
    )


def _marks() -> dict[str, object]:
    return json.loads(nodes.pull_marks_path("second").read_text(encoding="utf-8"))


def _snapshot(**over) -> remote_mux.NodeSnapshot:
    fields: dict[str, object] = {
        "now": 9000.0,
        "sessions": (),
        "sample": None,
        "realpaths": {},
        "state_files": {},
        "files": (),
        "failed_sids": frozenset(),
    }
    fields.update(over)
    return remote_mux.NodeSnapshot(**fields)


class TestTheWatermark:
    def test_the_watermark_walks_from_zero_to_the_nodes_clock(self, placed, fake_ssh):
        _answer(
            fake_ssh,
            "devino-second",
            meta=pull_meta(realpaths={"api": "/home/amin/magent/api"}),
        )
        syncer = node_sync.NodeSyncer(_second_only())
        syncer.tick()
        assert _marks() == {"api": {"since": 0.0, "realpath": "/home/amin/magent/api"}}
        syncer.tick()
        assert _payload(fake_ssh.calls()[-1])["sids"]["api"] == {
            "roots": ["~/magent/api"],
            "project_dir": encoded_project_dir("/home/amin/magent/api"),
            "since": 0.0,
        }
        assert _marks() == {
            "api": {"since": 4999.0, "realpath": "/home/amin/magent/api"}
        }
        syncer.tick()
        assert _payload(fake_ssh.calls()[-1])["sids"]["api"]["since"] == 4999.0

    def test_a_moved_directory_starts_its_transcripts_over(self, placed, fake_ssh):
        nodes.write_json_atomic(
            nodes.pull_marks_path("second"),
            {"api": {"since": 4999.0, "realpath": "/old"}},
        )
        _answer(
            fake_ssh,
            "devino-second",
            meta=pull_meta(realpaths={"api": "/home/amin/magent/api"}),
        )
        node_sync.NodeSyncer(_second_only()).tick()
        assert _marks() == {"api": {"since": 0.0, "realpath": "/home/amin/magent/api"}}

    def test_a_session_whose_files_could_not_be_stored_keeps_its_watermark_and_its_state(
        self, placed
    ):
        nodes.write_json_atomic(
            nodes.pull_marks_path("second"), {"api": {"since": 10.0, "realpath": "/r"}}
        )
        state = nodes.state_dir("second", "api")
        state.mkdir(parents=True)
        (state / "gone.json").write_text("{}", encoding="utf-8")

        def pull(_node, _sids):
            return _snapshot(
                realpaths={"api": "/r"},
                state_files={"api": ()},
                failed_sids=frozenset({"api"}),
            )

        node_sync.NodeSyncer(_second_only(), pull=pull).tick()
        assert _marks() == {"api": {"since": 10.0, "realpath": "/r"}}
        assert (state / "gone.json").exists()

    def test_marks_are_dropped_for_sessions_no_longer_placed(self, placed, fake_ssh):
        nodes.write_json_atomic(
            nodes.pull_marks_path("second"), {"gone": {"since": 5.0, "realpath": "/g"}}
        )
        _answer(fake_ssh, "devino-second")
        node_sync.NodeSyncer(_second_only()).tick()
        assert set(_marks()) == {"api"}


class TestTheMirror:
    def test_state_records_mirror_the_node_and_vanish_with_it(self, placed, fake_ssh):
        state = nodes.state_dir("second", "api")
        syncer = node_sync.NodeSyncer(_second_only())
        _answer(
            fake_ssh,
            "devino-second",
            meta=pull_meta(state_files={"api": ["k1.json", "k2.json"]}),
            files={
                "api/state/k1.json": '{"state": "working"}',
                "api/state/k2.json": '{"state": "done"}',
            },
        )
        syncer.tick()
        assert sorted(p.name for p in state.iterdir()) == ["k1.json", "k2.json"]
        _forget_replies(fake_ssh)
        _answer(
            fake_ssh, "devino-second", meta=pull_meta(state_files={"api": ["k2.json"]})
        )
        syncer.tick()
        assert sorted(p.name for p in state.iterdir()) == ["k2.json"]

    def test_transcripts_land_where_recall_reads_them(self, placed, fake_ssh):
        _answer(
            fake_ssh,
            "devino-second",
            files={
                "api/transcripts/0f.jsonl": "{}\n",
                "api/transcripts/0f/subagents/agent-1.jsonl": "{}\n",
            },
        )
        node_sync.NodeSyncer(_second_only()).tick()
        folder = nodes.transcripts_dir("second", "api")
        assert (folder / "0f.jsonl").read_text(encoding="utf-8") == "{}\n"
        assert (folder / "0f" / "subagents" / "agent-1.jsonl").exists()

    def test_load_samples_are_kept_at_the_sample_interval_for_the_history_window(
        self, placed, fake_ssh
    ):
        _answer(fake_ssh, "devino-second")
        clock = iter([1000.0, 1030.0, 1070.0, 4650.0])
        syncer = node_sync.NodeSyncer(
            _second_only(sample_interval_s=60, history_h=1), now=lambda: next(clock)
        )
        for _ in range(3):
            syncer.tick()
        rows = [
            json.loads(x)
            for x in nodes.load_path("second").read_text(encoding="utf-8").splitlines()
        ]
        assert [r["ts"] for r in rows] == [1000.0, 1070.0]
        syncer.tick()
        rows = [
            json.loads(x)
            for x in nodes.load_path("second").read_text(encoding="utf-8").splitlines()
        ]
        assert rows == [{**SAMPLE, "ts": 1070.0}, {**SAMPLE, "ts": 4650.0}]


def _recording_pull(seen: list[tuple[str, list[str]]]):
    def pull(node, sids):
        seen.append((node.nick, sorted(sids)))
        return _snapshot()

    return pull


class TestTheLoop:
    def test_the_loop_beats_and_records_its_pid_while_it_runs(self, placed):
        during: list[tuple[bool, int | None]] = []

        def nap(_s: float) -> None:
            during.append(
                (heartbeat_fresh(node_sync.HEARTBEAT_NAME), node_sync.daemon_pid())
            )

        assert (
            node_sync.run_sync_loop(
                _config(), max_ticks=2, sleep=nap, pull=_recording_pull([])
            )
            == 0
        )
        assert during == [(True, os.getpid())]
        assert heartbeat_age(node_sync.HEARTBEAT_NAME) is None
        assert node_sync.daemon_pid() is None

    def test_the_daemon_beats_at_least_every_10_seconds(self, placed, monkeypatch):
        """Readers call a heartbeat older than 30 s stale (DECISION-17). One
        write per pull (30 s by default) would flap between ok and stale, so the
        beat runs on its own thread at log.HEARTBEAT_INTERVAL."""
        beats: list[str] = []

        def beat(name, stop) -> None:
            beats.append(name)
            stop.wait(5)

        monkeypatch.setattr(node_sync, "run_heartbeat", beat)
        node_sync.run_sync_loop(_config(), max_ticks=1, pull=_recording_pull([]))
        assert beats == [node_sync.HEARTBEAT_NAME]
        assert (
            log.HEARTBEAT_INTERVAL * 3 <= log.HEARTBEAT_MAX_AGE
        )  # >= 3 beats per max age

    def test_a_second_daemon_exits_without_pulling(self, placed):
        seen: list[tuple[str, list[str]]] = []
        with exclusive_lock(node_sync.LOCK_NAME):
            assert (
                node_sync.run_sync_loop(
                    _config(), max_ticks=1, pull=_recording_pull(seen)
                )
                == 0
            )
        assert seen == []

    def test_a_crash_keeps_the_heartbeat_as_its_marker(self, placed, monkeypatch):
        """The crash is raised by the tick itself, not by a pull: a node's
        failure is reduced to an outcome inside _sync_node, so only a bug in
        the loop's own machinery escapes -- and that must crash the loop."""

        def broken(_self, *, wait_s=None):
            raise ValueError("boom")

        monkeypatch.setattr(node_sync.NodeSyncer, "tick", broken)
        with pytest.raises(ValueError, match="boom"):
            node_sync.run_sync_loop(_config(), max_ticks=1, pull=_recording_pull([]))
        assert heartbeat_age(node_sync.HEARTBEAT_NAME) is not None
        assert node_sync.daemon_pid() is None

    def test_a_changed_config_is_picked_up_between_ticks(self, placed):
        seen: list[tuple[str, list[str]]] = []
        smaller = _second_only()
        node_sync.run_sync_loop(
            _config(),
            max_ticks=2,
            sleep=lambda _s: None,
            reload=lambda: smaller,
            pull=_recording_pull(seen),
        )
        assert sorted(nick for nick, _ in seen) == ["second", "second", "third"]

    def test_the_loop_ends_when_no_project_runs_on_a_node(self, placed):
        seen: list[tuple[str, list[str]]] = []
        empty = _config(projects=[ProjectConfig(path="api")])
        node_sync.run_sync_loop(
            _config(),
            max_ticks=5,
            sleep=lambda _s: None,
            reload=lambda: empty,
            pull=_recording_pull(seen),
        )
        assert len(seen) == 2
        assert heartbeat_age(node_sync.HEARTBEAT_NAME) is None

    def test_a_reload_that_fails_keeps_the_config(self, placed):
        seen: list[tuple[str, list[str]]] = []
        node_sync.run_sync_loop(
            _config(),
            max_ticks=2,
            sleep=lambda _s: None,
            reload=lambda: None,
            pull=_recording_pull(seen),
        )
        assert len(seen) == 4

    def test_one_tick_refuses_while_the_daemon_holds_the_lock(self, placed):
        with exclusive_lock(node_sync.LOCK_NAME), pytest.raises(LockHeld):
            node_sync.run_once(_config(), pull=_recording_pull([]))


def _blocking_pull(release: threading.Event, dialled: list[str]):
    """``second`` hangs until the test sets ``release``; ``third`` answers at
    once. The wait is bounded, so a test that forgets to release still ends."""

    def pull(node, _sids):
        dialled.append(node.nick)
        if node.nick == "second":
            assert release.wait(30), "the test never released the hung pull"
        return _snapshot()

    return pull


class _RecordingExecutor(ThreadPoolExecutor):
    """A real pool that remembers its size and every shutdown call."""

    made: list[_RecordingExecutor]

    def __init__(self, max_workers=None, *args, **kwargs) -> None:
        super().__init__(max_workers, *args, **kwargs)
        self.max_workers = max_workers
        self.shutdowns: list[tuple[bool, bool]] = []
        self.made.append(self)

    def shutdown(self, wait=True, *, cancel_futures=False) -> None:
        self.shutdowns.append((wait, cancel_futures))
        super().shutdown(wait=wait, cancel_futures=cancel_futures)


@pytest.fixture
def executors(monkeypatch) -> list[_RecordingExecutor]:
    made: list[_RecordingExecutor] = []
    monkeypatch.setattr(_RecordingExecutor, "made", made, raising=False)
    monkeypatch.setattr(node_sync, "ThreadPoolExecutor", _RecordingExecutor)
    return made


def _drain(executors: list[_RecordingExecutor]) -> None:
    """Join every worker (without recording it), so a released pull cannot
    still be writing the mirror after this test's path redirects are undone."""
    for e in executors:
        ThreadPoolExecutor.shutdown(e, wait=True)


class TestAHungNodeDoesNotHoldTheTick:
    """One hung node used to hold every tick for up to PULL_TIMEOUT_S, so every
    healthy node's sessions.json aged past sessions_stale's two pull intervals.
    A tick now waits a bounded time, reports the laggard, and moves on."""

    def test_the_tick_returns_with_the_others_ok_and_the_laggard_still_running(
        self, placed, executors
    ):
        release = threading.Event()
        dialled: list[str] = []
        syncer = node_sync.NodeSyncer(_config(), pull=_blocking_pull(release, dialled))
        try:
            started = time.monotonic()
            first = syncer.tick(wait_s=0.2)
            second = syncer.tick(wait_s=0.2)
            elapsed = time.monotonic() - started
        finally:
            release.set()
            syncer.close()
            _drain(executors)
        running = (node_sync.UNREACHABLE, node_sync.PULL_STILL_RUNNING)
        assert first == second == {"second": running, "third": (node_sync.OK, "")}
        assert elapsed < 10
        # The laggard is not dialled again while its pull is still running.
        assert sorted(dialled) == ["second", "third", "third"]

    def test_the_laggard_reads_ok_on_the_tick_after_it_finishes(
        self, placed, caplog, executors
    ):
        _capture_nodes_log(caplog)
        release = threading.Event()
        syncer = node_sync.NodeSyncer(_config(), pull=_blocking_pull(release, []))
        try:
            syncer.tick(wait_s=0.2)
            syncer.tick(wait_s=0.2)
            release.set()
            after = syncer.tick(wait_s=10)
        finally:
            release.set()
            syncer.close()
            _drain(executors)
        assert after == {"second": (node_sync.OK, ""), "third": (node_sync.OK, "")}
        assert _node_warnings(caplog, "second") == [
            "node second: unreachable (previous pull still running)"
        ]
        assert [
            r.getMessage()
            for r in caplog.records
            if "reachable again" in r.getMessage()
        ] == ["node second: reachable again"]

    def test_a_smaller_pool_gets_a_new_executor(self, placed, executors):
        syncer = node_sync.NodeSyncer(_config(), pull=_recording_pull([]))
        try:
            syncer.tick()
            syncer.reconfigure(_second_only())
            syncer.tick()
        finally:
            syncer.close()
        assert [e.max_workers for e in executors] == [2, 1]
        assert executors[0].shutdowns == [(False, False)]
        assert executors[1].shutdowns == [(False, True)]

    def test_the_loop_shuts_its_executor_down_without_joining_a_hung_pull(
        self, placed, executors
    ):
        release = threading.Event()
        try:
            started = time.monotonic()
            rc = node_sync.run_sync_loop(
                _config(pull_interval_s=1, sample_interval_s=1),
                max_ticks=1,
                pull=_blocking_pull(release, []),
            )
            elapsed = time.monotonic() - started
            shutdowns = [list(e.shutdowns) for e in executors]
        finally:
            release.set()
            _drain(executors)
        assert rc == 0
        assert elapsed < 10
        assert shutdowns == [[(False, True)]]


class TestTheFinalPull:
    def test_a_first_final_pull_learns_the_directory_then_pulls_its_transcripts(
        self, placed, fake_ssh
    ):
        _answer(
            fake_ssh,
            "devino-second",
            meta=pull_meta(realpaths={"api": "/home/amin/magent/api"}),
            files={"api/transcripts/abc.jsonl": "x\n"},
        )
        result = node_sync.final_pull(_config(), "api")
        assert result is not None
        assert len(_calls_to(fake_ssh, "devino-second")) == 2
        assert nodes.transcripts_dir("second", "api") / "abc.jsonl" in result.files
        assert result.since == 4999.0
        assert _marks() == {
            "api": {"since": 4999.0, "realpath": "/home/amin/magent/api"}
        }

    def test_a_final_pull_with_a_known_directory_is_one_call(self, placed, fake_ssh):
        nodes.write_json_atomic(
            nodes.pull_marks_path("second"),
            {"api": {"since": 10.0, "realpath": "/home/amin/magent/api"}},
        )
        _answer(
            fake_ssh,
            "devino-second",
            meta=pull_meta(realpaths={"api": "/home/amin/magent/api"}),
        )
        node_sync.final_pull(_config(), "api")
        (call,) = _calls_to(fake_ssh, "devino-second")
        assert _payload(call)["sids"]["api"]["since"] == 10.0

    def test_a_project_that_was_never_placed_has_nothing_to_pull(
        self, placed, fake_ssh
    ):
        assert node_sync.final_pull(_config(), "nowhere") is None
        assert fake_ssh.calls() == []

    def test_a_final_pull_waits_for_the_daemons_tick_then_gives_up(
        self, placed, fake_ssh
    ):
        started = time.monotonic()
        with exclusive_lock("node-pull-second"), pytest.raises(LockHeld):
            node_sync.final_pull(_config(), "api", wait_s=0.5)
        assert time.monotonic() - started >= 0.4
        assert fake_ssh.calls() == []

    def test_an_unreachable_node_raises_for_the_caller_to_report(
        self, placed, fake_ssh
    ):
        _answer(fake_ssh, "devino-second", rc=255, stderr=REFUSED)
        with pytest.raises(remote_mux.RemoteError) as info:
            node_sync.final_pull(_config(), "api")
        assert info.value.rc == 255

    @pytest.mark.parametrize("sid", ["a\nb", "../x", "", "CON"])
    def test_a_session_name_this_pc_cannot_pull_is_refused_before_any_ssh(
        self, placed, fake_ssh, sid
    ):
        nodes.write_node_map({"api": _entry("second", sid)})
        with pytest.raises(remote_mux.RemoteError) as info:
            node_sync.final_pull(_config(), "api")
        assert info.value.rc == 0
        assert info.value.stderr_tail == f"not a pullable session name: {sid!r}"
        assert info.value.command_redacted[0] == "ssh"
        assert "amin@devino-second" in info.value.command_redacted
        assert fake_ssh.calls() == []

    def test_a_session_with_an_empty_remote_root_is_refused_before_any_ssh(
        self, placed, fake_ssh
    ):
        entry = _entry("second", "api")
        nodes.write_node_map(
            {
                "api": NodeMapEntry(
                    nick=entry.nick,
                    sid=entry.sid,
                    placed_ts=entry.placed_ts,
                    attached_existing=entry.attached_existing,
                    remote_root="",
                )
            }
        )
        with pytest.raises(remote_mux.RemoteError) as info:
            node_sync.final_pull(_config(), "api")
        assert info.value.rc == 0
        assert info.value.stderr_tail == (
            "session 'api' has an empty remote_root in the node map"
        )
        assert info.value.command_redacted[0] == "ssh"
        assert fake_ssh.calls() == []

    def test_a_good_session_name_still_pulls(self, placed, fake_ssh):
        nodes.write_node_map({"api": _entry("second", "api-2")})
        _answer(fake_ssh, "devino-second")
        result = node_sync.final_pull(_config(), "api")
        assert result is not None
        (call,) = _calls_to(fake_ssh, "devino-second")
        assert set(_payload(call)["sids"]) == {"api-2"}
