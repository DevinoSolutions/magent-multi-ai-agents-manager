"""The node sync daemon (magent.node_sync) and everything that keeps it alive.

Nothing here dials a real machine: node calls go to THE fake ssh
(tests/unit/_fake_ssh.py) or through the ``pull=`` seam, and MAGENT_NODE_SYNC
is 0 for every test that does not set it back.
"""

from __future__ import annotations

import ast
import contextlib
import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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
from magent.nodes import LoadSample, NodeMapEntry, encoded_project_dir
from tests.unit._fake_ssh import FLOOD_CAP
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
    "second": NodeConfig(nick="second", host="box-second", user="demo"),
    "third": NodeConfig(nick="third", host="box-third", user="demo"),
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

    @pytest.mark.parametrize("nick", ["../x", "\x1b[31mx"], ids=["traversal", "escape"])
    def test_a_nick_config_would_refuse_is_no_store_and_no_label(self, placed, nick):
        # The label is what `magent watch` prints; the dir is under NODES_DIR.
        # Round 2: the entry is malformed, so the map is unreadable -- the
        # error the engine holds its last records on, as for a torn map.
        nodes.write_node_map(
            {"api": _entry("second", "api"), "evil": _entry(nick, "evil")}
        )
        with pytest.raises(ValueError) as caught:
            node_sync.state_stores()
        assert "'evil'" in str(caught.value)
        assert "\x1b" not in str(caught.value)


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

    def test_a_daemon_between_its_lock_and_its_pid_is_waited_for_and_killed(
        self, daemon_lock, monkeypatch
    ):
        # The daemon takes its lock, then writes its pid (run_sync_loop). A
        # stop in between waits for the pid instead of calling it stuck.
        naps: list[float] = []

        def sleep(s: float) -> None:
            naps.append(s)
            if len(naps) == 5:
                _record_pid(4242)

        kills: list[int] = []
        monkeypatch.setattr(
            node_sync, "pid_alive", lambda pid: pid == 4242 and not kills
        )
        monkeypatch.setattr(node_sync, "_kill", lambda pid: kills.append(pid) or True)
        write_heartbeat(node_sync.HEARTBEAT_NAME)
        assert node_sync.stop_daemon(sleep=sleep, now=lambda: sum(naps)) is True
        assert kills == [4242]
        assert naps == [node_sync.STOP_POLL_S] * 5
        assert not node_sync._PID_PATH.exists()
        assert heartbeat_age(node_sync.HEARTBEAT_NAME) is None

    def test_a_daemon_whose_pid_never_comes_is_not_stopped_by_the_deadline(
        self, daemon_lock, monkeypatch
    ):
        kills: list[int] = []
        monkeypatch.setattr(node_sync, "_kill", lambda pid: kills.append(pid) or True)
        naps: list[float] = []
        assert node_sync.stop_daemon(sleep=naps.append, now=lambda: sum(naps)) is False
        assert kills == []
        assert set(naps) == {node_sync.STOP_POLL_S}
        assert sum(naps) == pytest.approx(node_sync.STOP_SETTLE_S, abs=0.11)


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


def _errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Every ERROR record from the nodes log, and only from it."""
    name = f"magent.{node_sync.LOG_NAME}"
    return [r for r in caplog.records if r.name == name and r.levelno >= logging.ERROR]


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
def sync_on(monkeypatch, placed):
    """Serve would spawn a daemon: the env allows it and a session is placed
    on a node (``node_sync.expected``)."""
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

    def test_a_lock_that_would_not_open_is_unknown_and_nothing_is_spawned(
        self, sync_on, spawned, monkeypatch
    ):
        """Windows answers EACCES while the lock file is pending delete, so
        whether a daemon runs is unknown: never "not running" (a spawn could
        double a live daemon), and never the bare PermissionError, which a
        caller could not tell from a refused spawn."""
        denied = PermissionError(13, "Access is denied")
        real_lock = node_sync.exclusive_lock

        def lock(name: str) -> contextlib.AbstractContextManager[None]:
            if name == node_sync.LOCK_NAME:
                raise denied
            return real_lock(name)

        monkeypatch.setattr(node_sync, "exclusive_lock", lock)
        try:
            got: object = launch.ensure_node_sync(_config(), "cfg.json")
        except (OSError, node_sync.DaemonLockUnknown) as exc:
            got = exc
        assert isinstance(got, node_sync.DaemonLockUnknown), got
        assert got.error is denied
        assert got.__cause__ is denied
        assert spawned == []

    def test_an_unknown_lock_is_still_an_oserror(self):
        """Every caller that contains an OSError of ensure_node_sync -- the
        bring-up's best-effort start is one -- still contains an unknown lock.
        Only a caller that names it (serve's supervisor) tells it apart."""
        assert issubclass(node_sync.DaemonLockUnknown, OSError)

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


def _ensure_on_nodes(monkeypatch) -> None:
    """serve's tick runs the REAL ensure_node_sync, on a config with node
    projects (the config file serve reads in these tests names none)."""
    monkeypatch.setattr(launch, "_node_sync_report", launch._NodeSyncReport())
    real = launch.ensure_node_sync
    monkeypatch.setattr(
        launch,
        "ensure_node_sync",
        lambda _cfg, config_path=None: real(_config(), config_path),
    )


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


class TestDownHoldsTheSupervisorLock:
    """``down --all``'s side of serve's supervisor lock: held for the body,
    waited on briefly, never a reason to skip the stop."""

    def test_a_free_lock_is_held_for_the_body_and_let_go_after(self):
        with node_sync.supervisor_held() as contended:
            assert contended is False
            with (
                pytest.raises(LockHeld),
                exclusive_lock(node_sync.SUPERVISOR_LOCK_NAME),
            ):
                pass
        with exclusive_lock(node_sync.SUPERVISOR_LOCK_NAME):
            pass

    def test_a_lock_that_will_not_open_is_retried_then_run_without(
        self, monkeypatch, caplog
    ):
        # Windows answers EACCES while a lock file is pending delete: retried
        # like a held lock, and past the wait the body runs unprotected.
        _capture_nodes_log(caplog)
        tries: list[str] = []

        def unopenable(name):
            tries.append(name)
            raise PermissionError(13, "Access is denied")

        monkeypatch.setattr(node_sync, "exclusive_lock", unopenable)
        clock = [0.0]

        def sleep(s: float) -> None:
            clock[0] += s

        ran: list[bool] = []
        with node_sync.supervisor_held(sleep=sleep, now=lambda: clock[0]) as c:
            ran.append(c)
        # Unprotected, so a restart cannot be ruled out: reads as contended.
        assert ran == [True]
        assert len(tries) > 1
        assert set(tries) == {node_sync.SUPERVISOR_LOCK_NAME}
        (warning,) = _warnings(caplog)
        assert "could not take serve's supervisor lock" in warning
        assert "Access is denied" in warning

    def test_a_lock_that_opens_on_a_retry_is_held_after_all(self, monkeypatch):
        real = node_sync.exclusive_lock
        answers = iter([PermissionError(13, "Access is denied")])

        def once_pending(name):
            exc = next(answers, None)
            if exc is not None:
                raise exc
            return real(name)

        monkeypatch.setattr(node_sync, "exclusive_lock", once_pending)
        with node_sync.supervisor_held(sleep=lambda _s: None) as c:
            assert c is True
            with (
                pytest.raises(LockHeld),
                exclusive_lock(node_sync.SUPERVISOR_LOCK_NAME),
            ):
                pass

    def test_a_probe_that_will_not_open_does_not_end_the_late_look(self, monkeypatch):
        answers = iter([PermissionError(13, "denied"), False, True])

        def running():
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr(node_sync, "daemon_running", running)
        monkeypatch.setattr(node_sync, "daemon_pid", lambda: 4242)
        assert node_sync.await_late_daemon(sleep=lambda _s: None)

    def test_a_held_lock_is_waited_on_in_steps_up_to_the_settle(
        self, monkeypatch, caplog
    ):
        _capture_nodes_log(caplog)
        clock = [0.0]
        slept: list[float] = []

        def sleep(s: float) -> None:
            slept.append(s)
            clock[0] += s

        with (
            exclusive_lock(node_sync.SUPERVISOR_LOCK_NAME),
            node_sync.supervisor_held(sleep=sleep, now=lambda: clock[0]) as c,
        ):
            assert c is True
        assert set(slept) == {node_sync.SUPERVISOR_RETRY_S}
        assert clock[0] == pytest.approx(node_sync.STOP_SETTLE_S, abs=0.06)
        (warning,) = _warnings(caplog)
        assert "stayed held past 2s" in warning

    def test_the_late_daemon_wait_is_bounded_and_ends_at_the_lock(self, monkeypatch):
        clock = [0.0]

        def sleep(s: float) -> None:
            clock[0] += s

        assert not node_sync.await_late_daemon(sleep=sleep, now=lambda: clock[0])
        assert clock[0] == pytest.approx(node_sync.STOP_SETTLE_S, abs=0.06)
        clock[0] = 0.0
        _record_pid(os.getpid())
        with exclusive_lock(node_sync.LOCK_NAME):
            assert node_sync.await_late_daemon(sleep=sleep, now=lambda: clock[0])
        assert clock[0] == 0.0

    def test_the_late_daemon_wait_ends_at_the_deadline_it_is_given(self):
        # down's contended hold passes its own deadline: a cold start past
        # the hold, not STOP_SETTLE_S from now.
        clock = [0.0]

        def sleep(s: float) -> None:
            clock[0] += s

        assert not node_sync.await_late_daemon(
            until=7.0, sleep=sleep, now=lambda: clock[0]
        )
        assert clock[0] == pytest.approx(7.0, abs=0.06)

    def test_a_deadline_already_past_still_looks_once(self, monkeypatch):
        monkeypatch.setattr(node_sync, "daemon_running", lambda: True)
        monkeypatch.setattr(node_sync, "daemon_pid", lambda: 4242)
        naps: list[float] = []
        assert node_sync.await_late_daemon(
            until=-1.0, sleep=naps.append, now=lambda: 0.0
        )
        assert naps == []

    def test_a_daemon_between_its_lock_and_its_pid_is_waited_for(self, monkeypatch):
        # The daemon locks first and writes its pid after; the stop kills by
        # pid, so the look lasts until the pid is there too.
        pids = iter([None, None, 4242])
        monkeypatch.setattr(node_sync, "daemon_running", lambda: True)
        monkeypatch.setattr(node_sync, "daemon_pid", lambda: next(pids))
        naps: list[float] = []
        assert node_sync.await_late_daemon(sleep=naps.append)
        assert naps == [node_sync.SUPERVISOR_RETRY_S] * 2

    def test_a_held_lock_with_no_pid_by_the_deadline_still_reads_as_a_daemon(
        self, monkeypatch
    ):
        clock = [0.0]

        def sleep(s: float) -> None:
            clock[0] += s

        monkeypatch.setattr(node_sync, "daemon_running", lambda: True)
        monkeypatch.setattr(node_sync, "daemon_pid", lambda: None)
        assert node_sync.await_late_daemon(sleep=sleep, now=lambda: clock[0])
        assert clock[0] == pytest.approx(node_sync.STOP_SETTLE_S, abs=0.06)


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

    @pytest.mark.parametrize("where", ["supervisor-lock", "daemon-lock"])
    def test_a_lock_pending_delete_is_a_warning_that_skips_one_tick(
        self, sync_on, tmp_config, monkeypatch, caplog, where
    ):
        """Windows answers EACCES while a lock file its last holder deleted is
        still pending delete: serve's own lock, or the daemon's under the REAL
        ensure_node_sync. A known transient -- a WARNING naming the class and
        the errno, never an exception-level record (Sentry's), and the next
        tick runs."""
        from magent import upload_server

        _capture_nodes_log(caplog)
        path = tmp_config({"version": SCHEMA_VERSION, "projects": []})
        stop = threading.Event()
        _ensure_on_nodes(monkeypatch)
        module = upload_server if where == "supervisor-lock" else node_sync
        refused = (
            node_sync.SUPERVISOR_LOCK_NAME
            if where == "supervisor-lock"
            else node_sync.LOCK_NAME
        )
        real_lock = module.exclusive_lock
        refusals = [PermissionError(13, "Access is denied")]

        def lock(name: str) -> contextlib.AbstractContextManager[None]:
            if name == refused and refusals:
                raise refusals.pop()
            return real_lock(name)

        monkeypatch.setattr(module, "exclusive_lock", lock)
        spawned: list[list[str]] = []

        def spawn(argv: list[str]) -> None:
            spawned.append(argv)
            stop.set()

        monkeypatch.setattr(launch, "spawn_detached", spawn)
        _run_supervisor(path, stop)
        assert refusals == []
        # The refused tick spawned nothing; the next one did.
        assert spawned == [launch.node_sync_argv(path)]
        assert _errors(caplog) == []
        warnings = _warnings(caplog)
        assert len(warnings) == 1, warnings
        assert "PermissionError" in warnings[0]
        assert "errno 13" in warnings[0]

    def test_a_refused_spawn_is_a_failed_check_not_a_skipped_tick(
        self, sync_on, tmp_config, monkeypatch, caplog
    ):
        """Only a lock file that would not open is the known transient. A
        PermissionError from anywhere else in the tick -- here the spawn -- can
        persist, so it stays at exception level (Sentry's)."""
        _capture_nodes_log(caplog)
        path = tmp_config({"version": SCHEMA_VERSION, "projects": []})
        _ensure_on_nodes(monkeypatch)
        spawns: list[list[str]] = []

        def refuse(argv: list[str]) -> None:
            spawns.append(argv)
            raise PermissionError(13, "Access is denied")

        monkeypatch.setattr(launch, "spawn_detached", refuse)
        _run_supervisor(path, _one_tick())
        assert spawns == [launch.node_sync_argv(path)]
        errors = _errors(caplog)
        assert [r.getMessage() for r in errors] == [
            "node sync supervisor: check failed"
        ]
        assert errors[0].exc_info is not None
        assert not any("tick skipped" in m for m in _warnings(caplog))

    def test_any_other_error_of_a_tick_is_still_logged_at_exception_level(
        self, sync_on, tmp_config, monkeypatch, caplog
    ):
        _capture_nodes_log(caplog)
        path = tmp_config({"version": SCHEMA_VERSION, "projects": []})

        def ensure(_config, _config_path=None):
            raise OSError(5, "Input/output error")

        monkeypatch.setattr(launch, "ensure_node_sync", ensure)
        _run_supervisor(path, _one_tick())
        errors = _errors(caplog)
        assert [r.getMessage() for r in errors] == [
            "node sync supervisor: check failed"
        ]
        assert errors[0].exc_info is not None
        assert _warnings(caplog) == ["node sync supervisor: check failed"]

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
            "cwd": "/home/demo/magent/api",
            "session_id": "s",
        }
        (store / "k.json").write_text(json.dumps(record), encoding="utf-8")
        views = engine_from_config(_config()).poll()
        assert [(v.name, v.cwd, v.state) for v in views] == [
            ("api", "@second:/home/demo/magent/api", "needs-input")
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


REFUSED = "ssh: connect to host box-second port 22: Connection refused\n"


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
    return [c for c in fake.calls() if c.argv[-2] == f"demo@{host}"]


class TestOneTick:
    def test_one_tick_is_one_ssh_per_node(self, placed, fake_ssh):
        _answer(fake_ssh, "box-second")
        _answer(fake_ssh, "box-third")
        results = node_sync.NodeSyncer(_config()).tick()
        assert results == {"second": (node_sync.OK, ""), "third": (node_sync.OK, "")}
        assert sorted(c.argv[-2] for c in fake_ssh.calls()) == [
            "demo@box-second",
            "demo@box-third",
        ]

    def test_a_node_with_nothing_placed_on_it_is_still_asked_for_its_load(
        self, placed, fake_ssh
    ):
        nodes.write_node_map({"api": _entry("second", "api")})
        _answer(fake_ssh, "box-second")
        _answer(fake_ssh, "box-third")
        node_sync.NodeSyncer(_config()).tick()
        (call,) = _calls_to(fake_ssh, "box-third")
        assert _payload(call)["sids"] == {}

    def test_the_session_list_is_mirrored_on_this_pcs_clock(self, placed, fake_ssh):
        _answer(fake_ssh, "box-second", meta=pull_meta(sessions=["api", "other"]))
        _answer(fake_ssh, "box-third")
        node_sync.NodeSyncer(_config(), now=lambda: 777.0).tick()
        assert nodes.read_sessions("second") == nodes.NodeSessions(
            ts=777.0, sessions=("api", "other")
        )

    def test_each_placed_session_is_asked_for_from_the_beginning(
        self, placed, fake_ssh
    ):
        _answer(fake_ssh, "box-second")
        _answer(fake_ssh, "box-third")
        node_sync.NodeSyncer(_config()).tick()
        (call,) = _calls_to(fake_ssh, "box-second")
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
            "box-second",
            meta=pull_meta(sessions=[]),
            files={"api/transcripts/abc.jsonl": "x\n"},
        )
        _answer(fake_ssh, "box-third")
        assert node_sync.NodeSyncer(_config()).tick()["second"] == (node_sync.OK, "")
        (call,) = _calls_to(fake_ssh, "box-second")
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
        _answer(fake_ssh, "box-second")
        _answer(fake_ssh, "box-third")
        syncer = node_sync.NodeSyncer(_config())
        syncer.tick()
        syncer.tick()
        assert all(
            set(_payload(c)["sids"]) == {"api"}
            for c in _calls_to(fake_ssh, "box-second")
        )
        assert [
            r.getMessage()
            for r in caplog.records
            if "cannot be mirrored" in r.getMessage()
        ] == ["node second: session 'CON' cannot be mirrored on this PC; skipping it"]

    def test_a_transport_failure_is_unreachable(self, placed, fake_ssh):
        _answer(fake_ssh, "box-second", rc=255, stderr=REFUSED)
        _answer(fake_ssh, "box-third")
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
        _answer(fake_ssh, "box-second")
        _answer(fake_ssh, "box-third")
        syncer.tick()
        _forget_replies(fake_ssh)
        _answer(fake_ssh, "box-second", rc=255, stderr=REFUSED)
        _answer(fake_ssh, "box-third")
        results = syncer.tick()
        assert results["second"][0] == node_sync.UNREACHABLE
        assert nodes.read_sessions("second").ts == 100.0
        assert nodes.read_sessions("third").ts == 200.0

    def test_a_node_that_stays_down_is_logged_once_and_once_again_when_it_returns(
        self, placed, fake_ssh, caplog
    ):
        _capture_nodes_log(caplog)
        syncer = node_sync.NodeSyncer(_config())
        _answer(fake_ssh, "box-second", rc=255, stderr=REFUSED)
        _answer(fake_ssh, "box-third")
        for _ in range(3):
            syncer.tick()
        _forget_replies(fake_ssh)
        _answer(fake_ssh, "box-second")
        _answer(fake_ssh, "box-third")
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
        fake_ssh.set_reply("box-second", stdout="hello\n")
        _answer(fake_ssh, "box-third")
        results = node_sync.NodeSyncer(_config()).tick()
        assert results["second"] == (
            node_sync.FAILED,
            "no MAGENT-PULL header in the reply",
        )
        assert results["third"] == (node_sync.OK, "")

    def test_a_missing_ssh_client_fails_every_node_without_raising(self, placed):
        results = node_sync.NodeSyncer(_config()).tick()
        assert results == {
            "second": (node_sync.FAILED, "no ssh client found"),
            "third": (node_sync.FAILED, "no ssh client found"),
        }

    def test_a_node_that_would_run_as_root_is_misconfigured_and_never_dialled(
        self, placed, fake_ssh
    ):
        pool = {
            "second": NodeConfig(nick="second", host="box-second"),
            "third": POOL["third"],
        }
        _answer(fake_ssh, "box-third")
        results = node_sync.NodeSyncer(_config(pool=pool), local_user="root").tick()
        assert results["second"][0] == node_sync.MISCONFIGURED
        assert "(D4)" in results["second"][1]
        assert _calls_to(fake_ssh, "box-second") == []

    def test_a_node_being_pulled_elsewhere_is_skipped_silently(
        self, placed, fake_ssh, caplog
    ):
        _capture_nodes_log(caplog)
        _answer(fake_ssh, "box-second")
        _answer(fake_ssh, "box-third")
        with exclusive_lock("node-pull-second"):
            results = node_sync.NodeSyncer(_config()).tick()
        assert results["second"][0] == node_sync.LOCKED
        assert results["third"] == (node_sync.OK, "")
        assert _calls_to(fake_ssh, "box-second") == []
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
            "box-second",
            meta=pull_meta(realpaths={"api": "/home/demo/magent/api"}),
        )
        syncer = node_sync.NodeSyncer(_second_only())
        syncer.tick()
        assert _marks() == {"api": {"since": 0.0, "realpath": "/home/demo/magent/api"}}
        syncer.tick()
        assert _payload(fake_ssh.calls()[-1])["sids"]["api"] == {
            "roots": ["~/magent/api"],
            "project_dir": encoded_project_dir("/home/demo/magent/api"),
            "since": 0.0,
        }
        assert _marks() == {
            "api": {"since": 4999.0, "realpath": "/home/demo/magent/api"}
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
            "box-second",
            meta=pull_meta(realpaths={"api": "/home/demo/magent/api"}),
        )
        node_sync.NodeSyncer(_second_only()).tick()
        assert _marks() == {"api": {"since": 0.0, "realpath": "/home/demo/magent/api"}}

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

    @pytest.mark.parametrize(
        ("resume", "held"),
        [({"api": 50.0}, math.nextafter(50.0, -math.inf)), ({}, 10.0)],
        ids=["resume", "no-resume"],
    )
    def test_a_truncated_tick_asks_again_for_the_files_it_still_owes(
        self, placed, resume, held
    ):
        """cq-G14 I-R2-1: the daemon shares remote_mux.next_since's rule --
        just under the first owed file, or held without a resume point."""
        nodes.write_json_atomic(
            nodes.pull_marks_path("second"), {"api": {"since": 10.0, "realpath": "/r"}}
        )
        asked: list[float] = []
        replies = iter(
            [
                _snapshot(
                    realpaths={"api": "/r"},
                    truncated={"api": ("api/transcripts/a.jsonl",)},
                    resume=resume,
                ),
                _snapshot(realpaths={"api": "/r"}),
            ]
        )

        def pull(_node, sids):
            asked.append(sids["api"].since)
            return next(replies)

        syncer = node_sync.NodeSyncer(_second_only(), pull=pull)
        syncer.tick()
        assert _marks() == {"api": {"since": held, "realpath": "/r"}}
        syncer.tick()
        assert asked == [10.0, held]

    def test_marks_are_dropped_for_sessions_no_longer_placed(self, placed, fake_ssh):
        nodes.write_json_atomic(
            nodes.pull_marks_path("second"), {"gone": {"since": 5.0, "realpath": "/g"}}
        )
        _answer(fake_ssh, "box-second")
        node_sync.NodeSyncer(_second_only()).tick()
        assert set(_marks()) == {"api"}


class TestTheMirror:
    def test_state_records_mirror_the_node_and_vanish_with_it(self, placed, fake_ssh):
        state = nodes.state_dir("second", "api")
        syncer = node_sync.NodeSyncer(_second_only())
        _answer(
            fake_ssh,
            "box-second",
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
            fake_ssh, "box-second", meta=pull_meta(state_files={"api": ["k2.json"]})
        )
        syncer.tick()
        assert sorted(p.name for p in state.iterdir()) == ["k2.json"]

    def test_transcripts_land_where_recall_reads_them(self, placed, fake_ssh):
        _answer(
            fake_ssh,
            "box-second",
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
        _answer(fake_ssh, "box-second")
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
        # 1000 is past the 1 h window at 4650 but inside its slack (360 s):
        # still an append. The trim itself: TestTheLoadFileIsAppendedTo.
        assert rows == [
            {**SAMPLE, "ts": 1000.0},
            {**SAMPLE, "ts": 1070.0},
            {**SAMPLE, "ts": 4650.0},
        ]


def _seed_marks(nick: str = "second", **marks: tuple[float, str]) -> bytes:
    path = nodes.pull_marks_path(nick)
    nodes.write_json_atomic(
        path, {sid: {"since": s, "realpath": r} for sid, (s, r) in marks.items()}
    )
    return path.read_bytes()


def _two_on_second() -> MagentConfig:
    nodes.write_node_map({"api": _entry("second", "api"), "db": _entry("second", "db")})
    return _config(
        pool={"second": POOL["second"]},
        projects=[
            ProjectConfig(path="api", node="second"),
            ProjectConfig(path="db", node="second"),
        ],
    )


class TestMarksMoveOnlyAfterAPull:
    """pull.json is written AFTER a pull lands, never before: a mark advanced
    optimistically and then left behind by a failed pull would skip files."""

    def test_an_unreachable_node_leaves_the_marks_byte_identical(
        self, placed, fake_ssh
    ):
        before = _seed_marks(api=(10.0, "/home/demo/magent/api"))
        _answer(fake_ssh, "box-second", rc=255, stderr=REFUSED)
        results = node_sync.NodeSyncer(_second_only()).tick()
        assert results["second"][0] == node_sync.UNREACHABLE
        assert nodes.pull_marks_path("second").read_bytes() == before

    def test_a_reply_cut_off_before_its_trailer_leaves_the_marks_byte_identical(
        self, placed, fake_ssh
    ):
        before = _seed_marks(api=(10.0, "/home/demo/magent/api"))
        reply = pull_reply(
            pull_meta(realpaths={"api": "/home/demo/magent/api"}),
            {"api/transcripts/a.jsonl": "x\n"},
        )
        cut = reply[: reply.rindex(remote_mux.PULL_TRAILER.decode("ascii"))]
        fake_ssh.set_reply("box-second", stdout=cut)
        results = node_sync.NodeSyncer(_second_only()).tick()
        assert results["second"][0] == node_sync.FAILED
        assert nodes.pull_marks_path("second").read_bytes() == before


def _make_unreadable(state: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Torn on disk, one entry the strict read refuses, or intact but still
    locked after the strict read's retries (a Windows reader racing a
    replace)."""
    if state == "torn":
        nodes.NODE_MAP_PATH.write_text("{ torn", encoding="utf-8")
        return
    if state == "malformed":
        # Round-2 ruling 3: one malformed entry makes the whole map
        # unreadable, so it pauses the sync exactly as a torn file does.
        entry = {
            "nick": "second",
            "sid": "api",
            "placed_ts": "soon",
            "attached_existing": False,
            "remote_root": "~/magent/api",
        }
        nodes.NODE_MAP_PATH.write_text(json.dumps({"api": entry}), encoding="utf-8")
        return

    def busy() -> dict[str, NodeMapEntry]:
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(nodes, "load_node_map_strict", busy)


_UNREADABLE = pytest.mark.parametrize(
    ("state", "cls"),
    [
        ("torn", "ValueError"),
        ("malformed", "ValueError"),
        ("busy", "PermissionError"),
    ],
)


class TestAnUnreadableMapPullsNothing:
    """A tick behind a map it could not read knows nothing about placement.
    Pulling every node with no session, as if the map were empty, wrote each
    node's marks as ``{}``: the next readable tick pulled every session from
    zero. So it pulls nothing and writes nothing, and says so once."""

    @staticmethod
    def _pull(asked: list[str]):
        def pull(node, _sids):
            asked.append(node.nick)
            return _snapshot(realpaths={"api": "/r"})

        return pull

    def test_a_readable_map_records_marks(self, placed):
        asked: list[str] = []
        node_sync.NodeSyncer(_second_only(), pull=self._pull(asked)).tick()
        assert asked == ["second"]
        assert _marks() == {"api": {"since": 0.0, "realpath": "/r"}}

    @_UNREADABLE
    def test_the_marks_stay_byte_identical_and_no_node_is_dialled(
        self, placed, monkeypatch, state, cls
    ):
        before = _seed_marks(api=(10.0, "/r"))
        _make_unreadable(state, monkeypatch)
        asked: list[str] = []
        results = node_sync.NodeSyncer(_config(), pull=self._pull(asked)).tick()
        assert asked == []
        assert nodes.pull_marks_path("second").read_bytes() == before
        assert not nodes.pull_marks_path("third").exists()
        assert not nodes.sessions_path("second").exists()
        detail = f"the node map could not be read ({cls})"
        assert results == {
            "second": (node_sync.FAILED, detail),
            "third": (node_sync.FAILED, detail),
        }

    @_UNREADABLE
    def test_one_warning_carries_the_maps_error_and_the_recovery_once(
        self, placed, monkeypatch, caplog, state, cls
    ):
        _capture_nodes_log(caplog)
        _seed_marks(api=(10.0, "/r"))
        real = nodes.load_node_map_strict
        _make_unreadable(state, monkeypatch)
        asked: list[str] = []
        syncer = node_sync.NodeSyncer(_second_only(), pull=self._pull(asked))
        syncer.tick()
        syncer.tick()
        warnings = _warnings(caplog)
        assert len(warnings) == 1, warnings
        # Our words and the class, then the map's own error: its path and the
        # parser's (or the OS's) words are for nodes.log -- the tick's answer,
        # which reaches the screen, names the class only.
        assert warnings[0].startswith(
            f"node sync: the node map could not be read ({cls}); pulling nothing: "
        )
        if state == "torn":
            assert f"{nodes.NODE_MAP_PATH}: Expecting" in warnings[0]
        elif state == "malformed":
            assert f"{nodes.NODE_MAP_PATH}: entry 'api' is malformed" in (warnings[0])
        else:
            assert "The process cannot access the file" in warnings[0]
        # Readable again: one line says so, and the pull resumes from the
        # marks the unreadable ticks left alone.
        monkeypatch.setattr(nodes, "load_node_map_strict", real)
        nodes.write_node_map({"api": _entry("second", "api")})
        syncer.tick()
        syncer.tick()
        assert asked == ["second", "second"]
        assert _warnings(caplog) == warnings
        again = [
            r.getMessage() for r in caplog.records if "reads again" in r.getMessage()
        ]
        assert again == ["node sync: the node map reads again"]

    def test_one_shot_behind_an_unreadable_map_is_a_failure_not_a_silence(self, placed):
        # `magent node sync --once` exits 1 on any non-OK node: an empty
        # answer would have printed nothing and exited 0.
        nodes.NODE_MAP_PATH.write_text("{ torn", encoding="utf-8")
        results = node_sync.run_once(_config(), pull=self._pull([]))
        assert set(results) == {"second", "third"}
        assert {outcome for outcome, _ in results.values()} == {node_sync.FAILED}

    def test_a_new_class_mid_episode_warns_again(self, placed, monkeypatch, caplog):
        # Torn, then busy: a different refusal is news, not a repeat.
        _capture_nodes_log(caplog)
        syncer = node_sync.NodeSyncer(_second_only(), pull=self._pull([]))
        _make_unreadable("torn", monkeypatch)
        syncer.tick()
        _make_unreadable("busy", monkeypatch)
        syncer.tick()
        syncer.tick()
        assert [w.split("; ")[0] for w in _warnings(caplog)] == [
            "node sync: the node map could not be read (ValueError)",
            "node sync: the node map could not be read (PermissionError)",
        ]

    @pytest.mark.usefixtures("healthy_pulls_end_first")
    def test_a_pull_in_flight_outlives_the_tick_and_the_next_readable_one_collects_it(
        self, placed, monkeypatch, executors
    ):
        # Ruling (c): an unreadable tick leaves a running pull alone -- its
        # marks untouched, never dialled a second time while it runs -- and
        # the first readable tick after it ends collects it as any laggard.
        release = threading.Event()
        dialled: list[str] = []
        clock = _Clock()
        syncer = node_sync.NodeSyncer(
            _config(), pull=_blocking_pull(release, dialled), clock=clock
        )
        before = _seed_marks(api=(10.0, "/r"))
        real = nodes.load_node_map_strict
        try:
            first = syncer.tick(wait_s=0.2)
            _make_unreadable("busy", monkeypatch)
            unread = syncer.tick(wait_s=0.2)
            marks_while_unread = nodes.pull_marks_path("second").read_bytes()
            monkeypatch.setattr(nodes, "load_node_map_strict", real)
            clock.at = 7.0
            third = syncer.tick(wait_s=0.2)
            release.set()
            _wait_out_the_laggard(syncer, "second")
            clock.at = 8.0
            fourth = syncer.tick(wait_s=10)
        finally:
            release.set()
            syncer.close()
            _drain(executors)
        ok = (node_sync.OK, "")
        assert first == {"second": _running(0), "third": ok}
        assert unread["second"][0] == node_sync.FAILED
        assert marks_while_unread == before
        assert third == {"second": _running(7), "third": ok}
        assert fourth == {"second": ok, "third": ok}
        # Once for the hung pull, once after it was collected; never between.
        assert sorted(dialled) == ["second", "second", "third", "third", "third"]

    @pytest.mark.usefixtures("healthy_pulls_end_first")
    def test_what_a_pull_in_flight_raises_still_surfaces_after_the_tick(
        self, placed, monkeypatch, executors
    ):
        release = threading.Event()
        _hang_then_raise(monkeypatch, release, ValueError("late"))
        syncer = node_sync.NodeSyncer(_config(), pull=self._pull([]), clock=_Clock())
        real = nodes.load_node_map_strict
        try:
            syncer.tick(wait_s=0.2)
            _make_unreadable("busy", monkeypatch)
            syncer.tick(wait_s=0.2)
            monkeypatch.setattr(nodes, "load_node_map_strict", real)
            release.set()
            _wait_out_the_laggard(syncer, "second")
            with pytest.raises(ValueError, match="late"):
                syncer.tick(wait_s=10)
        finally:
            release.set()
            syncer.close()
            _drain(executors)


class TestMarkAndPruneScope:
    def test_a_failed_session_keeps_its_mark_while_its_neighbour_advances(self, placed):
        _seed_marks(api=(10.0, "/ra"), db=(20.0, "/rd"))

        def pull(_node, _sids):
            return _snapshot(
                now=9000.0,
                realpaths={"api": "/ra", "db": "/rd"},
                failed_sids=frozenset({"api"}),
            )

        node_sync.NodeSyncer(_two_on_second(), pull=pull).tick()
        assert _marks() == {
            "api": {"since": 10.0, "realpath": "/ra"},
            "db": {"since": 8999.0, "realpath": "/rd"},
        }

    def test_pruning_one_session_touches_no_other_nodes_mirror_and_no_local_record(
        self, placed, tmp_path, monkeypatch
    ):
        local = tmp_path / "local-state"
        monkeypatch.setattr(agent_state, "STATE_DIR", local)
        local.mkdir()
        (local / "x.json").write_text("{}", encoding="utf-8")
        other = nodes.state_dir("third", "api")
        other.mkdir(parents=True)
        (other / "x.json").write_text("{}", encoding="utf-8")
        mine = nodes.state_dir("second", "api")
        mine.mkdir(parents=True)
        (mine / "x.json").write_text("{}", encoding="utf-8")

        def pull(_node, _sids):
            return _snapshot(realpaths={"api": "/r"}, state_files={"api": ()})

        node_sync.NodeSyncer(_second_only(), pull=pull).tick()
        assert not (mine / "x.json").exists()
        assert (other / "x.json").exists()
        assert (local / "x.json").exists()

    def test_two_nodes_sampled_in_one_tick_each_get_their_row(self, placed):
        """The throttle is per node. Third's pull waits until second's row is
        on disk (plus a beat for the throttle's bookkeeping), so second has
        always stored first: a throttle shared across nodes would then drop
        third's row every time, not only when the threads happen to race."""

        def pull(node, _sids):
            if node.nick == "third":
                deadline = time.monotonic() + 10
                while not nodes.load_path("second").exists():
                    assert time.monotonic() < deadline, "second never stored"
                    time.sleep(0.01)
                time.sleep(0.2)
            return _snapshot(sample=LoadSample(**SAMPLE))

        node_sync.NodeSyncer(_config(), pull=pull, now=lambda: 1000.0).tick()
        for nick in ("second", "third"):
            rows = nodes.load_path(nick).read_text(encoding="utf-8").splitlines()
            assert [json.loads(r)["ts"] for r in rows] == [1000.0]


class TestHostileClocksAndValues:
    def test_a_nan_or_infinite_mark_reads_as_absent(self, placed):
        path = nodes.pull_marks_path("second")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"api": {"since": NaN, "realpath": "/r"},'
            ' "db": {"since": Infinity, "realpath": "/r"},'
            ' "ok": {"since": 5.0, "realpath": "/r"}}',
            encoding="utf-8",
        )
        assert node_sync._read_marks("second") == {
            "ok": node_sync.Mark(since=5.0, realpath="/r")
        }

    def test_a_node_clock_that_went_back_past_the_mark_starts_over(self, placed):
        """A node clock that jumped forward left a mark in its future. When
        the clock comes back, files stamped before that mark would never be
        asked for again: the transcripts start over from zero instead."""
        _seed_marks(api=(4999.0, "/r"))

        def pull(_node, _sids):
            return _snapshot(now=3000.0, realpaths={"api": "/r"})

        node_sync.NodeSyncer(_second_only(), pull=pull).tick()
        assert _marks() == {"api": {"since": 0.0, "realpath": "/r"}}

    def test_a_truncated_reply_moves_the_mark_to_its_resume_point(self, placed):
        """E8's rule through the tick (remote_mux.next_since): holding the
        mark would ask for the same files, cut the same way, every tick."""
        _seed_marks(api=(10.0, "/r"))

        def pull(_node, _sids):
            return _snapshot(
                realpaths={"api": "/r"},
                truncated={"api": ("api/transcripts/b.jsonl",)},
                resume={"api": 500.0},
            )

        node_sync.NodeSyncer(_second_only(), pull=pull).tick()
        assert _marks() == {
            "api": {"since": math.nextafter(500.0, -math.inf), "realpath": "/r"}
        }

    def test_a_pc_clock_that_steps_back_does_not_starve_samples(self, placed):
        clock = iter([1000.0, 900.0])

        def pull(_node, _sids):
            return _snapshot(sample=LoadSample(**SAMPLE))

        syncer = node_sync.NodeSyncer(
            _second_only(sample_interval_s=60), pull=pull, now=lambda: next(clock)
        )
        syncer.tick()
        syncer.tick()
        rows = nodes.load_path("second").read_text(encoding="utf-8").splitlines()
        # The 900 sample is kept, not starved. The 1000 row now lies in this
        # PC's future, so the trim that sample triggers drops it
        # (TestTheTrimNeverStalls).
        assert [json.loads(r)["ts"] for r in rows] == [900.0]

    def test_a_sample_that_is_not_json_is_logged_and_the_pull_still_counts(
        self, placed, caplog
    ):
        _capture_nodes_log(caplog)

        def pull(_node, _sids):
            return _snapshot(
                realpaths={"api": "/r"},
                sample=LoadSample(**{**SAMPLE, "load1": float("nan")}),
            )

        results = node_sync.NodeSyncer(_second_only(), pull=pull).tick()
        assert results["second"] == (node_sync.OK, "")
        assert not nodes.load_path("second").exists()
        assert set(_marks()) == {"api"}
        assert [m for m in _warnings(caplog) if "load sample" in m]

    def test_a_load_file_that_cannot_be_written_does_not_fail_the_tick(
        self, placed, caplog
    ):
        _capture_nodes_log(caplog)
        nodes.load_path("second").mkdir(parents=True)

        def pull(_node, _sids):
            return _snapshot(realpaths={"api": "/r"}, sample=LoadSample(**SAMPLE))

        results = node_sync.NodeSyncer(_second_only(), pull=pull).tick()
        assert results["second"] == (node_sync.OK, "")
        assert _marks() == {"api": {"since": 0.0, "realpath": "/r"}}
        assert nodes.read_sessions("second") is not None
        assert [m for m in _warnings(caplog) if "node second: load sample" in m]


WINDOW_S = 3600.0  # history_h=1
SLACK_S = 360.0  # max(10% of the window, the 60 s sample interval)


def _load_ts(nick: str = "second") -> list[float]:
    out: list[float] = []
    for line in nodes.load_path(nick).read_text(encoding="utf-8").splitlines():
        out.append(json.loads(line)["ts"])
    return out


@pytest.fixture
def rewrites(monkeypatch) -> list[Path]:
    """Every atomic rewrite of a load.jsonl (pull.json and sessions.json go
    through the same writer and are not counted)."""
    seen: list[Path] = []
    real = nodes.write_text_atomic

    def spy(path: Path, text: str) -> None:
        if path.name == "load.jsonl":
            seen.append(path)
        real(path, text)

    monkeypatch.setattr(nodes, "write_text_atomic", spy)
    return seen


def _sample_at(at: float) -> None:
    node_sync._append_sample(
        "second", LoadSample(**SAMPLE), at=at, history_h=1, interval_s=60
    )


class TestTheLoadFileIsAppendedTo:
    def test_rows_are_appended_without_a_rewrite_inside_the_slack(
        self, placed, rewrites
    ):
        for at in (1000.0, 1060.0, 1000.0 + WINDOW_S + SLACK_S):
            _sample_at(at)
        assert _load_ts() == [1000.0, 1060.0, 1000.0 + WINDOW_S + SLACK_S]
        assert rewrites == []

    def test_a_trim_happens_past_the_slack_and_keeps_only_the_window(
        self, placed, rewrites
    ):
        for at in (1000.0, 1060.0, 4650.0):
            _sample_at(at)
        at = 1000.0 + WINDOW_S + SLACK_S + 1
        _sample_at(at)
        assert len(rewrites) == 1
        assert _load_ts() == [4650.0, at]
        assert min(_load_ts()) >= at - WINDOW_S

    def test_a_long_run_stays_bounded_and_rarely_rewrites(self, placed, rewrites):
        samples = 600  # ten hours at one sample a minute
        for i in range(samples):
            at = 1000.0 + 60 * i
            _sample_at(at)
            rows = _load_ts()
            assert rows[-1] == at
            assert at - rows[0] <= WINDOW_S + SLACK_S
        assert 0 < len(rewrites) <= samples // 5

    def test_an_unreadable_first_line_is_trimmed_away(self, placed, rewrites):
        path = nodes.load_path("second")
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\xff not json\n")
        _sample_at(1000.0)
        assert len(rewrites) == 1
        assert _load_ts() == [1000.0]

    def test_a_torn_last_row_does_not_swallow_the_next_one(self, placed):
        path = nodes.load_path("second")
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({**SAMPLE, "ts": 900.0}) + '\n{"ts": 95', encoding="utf-8"
        )
        _sample_at(1000.0)
        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines[1] == '{"ts": 95'
        assert json.loads(lines[2])["ts"] == 1000.0


class TestTheHistoryKeepsTheClaudeToken:
    """Placement reads whether a node has its Claude token off the synced
    history, so the row must carry it -- and only when the node said so."""

    @pytest.mark.parametrize("ready", [True, False])
    def test_a_known_token_is_written_and_read_back(self, ready):
        sample = LoadSample(**SAMPLE, claude_auth=ready)

        node_sync._append_sample(
            "second", sample, at=1000.0, history_h=1, interval_s=60
        )

        (line,) = nodes.load_path("second").read_text(encoding="utf-8").splitlines()
        assert json.loads(line)["claude_auth"] is ready
        (read,) = nodes.read_load_history("second")
        assert read.claude_auth is ready

    def test_an_unknown_token_leaves_the_row_as_it_always_was(self):
        node_sync._append_sample(
            "second", LoadSample(**SAMPLE), at=1000.0, history_h=1, interval_s=60
        )

        (line,) = nodes.load_path("second").read_text(encoding="utf-8").splitlines()
        assert json.loads(line) == {**SAMPLE, "ts": 1000.0}


class TestATornLoadRowAcrossTicks:
    def test_a_tick_after_a_torn_row_appends_intact_and_the_trim_drops_the_fragment(
        self, placed
    ):
        """A crash mid-append leaves a partial last line. The next tick's row
        must start its own line, and the trim pass must skip the fragment."""
        torn = '{"ts": 95'
        path = nodes.load_path("second")
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({**SAMPLE, "ts": 900.0}) + "\n" + torn, encoding="utf-8"
        )
        clock = iter([1000.0, 5000.0])

        def pull(_node, _sids):
            return _snapshot(sample=LoadSample(**SAMPLE))

        syncer = node_sync.NodeSyncer(
            _second_only(sample_interval_s=60, history_h=1),
            pull=pull,
            now=lambda: next(clock),
        )
        syncer.tick()
        lines = path.read_text(encoding="utf-8").splitlines()
        assert [ln for ln in lines if ln == torn] == [torn]
        assert [json.loads(ln) for ln in lines if ln != torn] == [
            {**SAMPLE, "ts": 900.0},
            {**SAMPLE, "ts": 1000.0},
        ]
        syncer.tick()  # 900 is now past the window + slack: a trim
        lines = path.read_text(encoding="utf-8").splitlines()
        assert [json.loads(ln) for ln in lines] == [{**SAMPLE, "ts": 5000.0}]


def _parsed_rows(nick: str = "second") -> list[float | None]:
    """Every line's ts, None for a line that is not a row."""
    out: list[float | None] = []
    for line in nodes.load_path(nick).read_text(encoding="utf-8").splitlines():
        try:
            out.append(float(json.loads(line)["ts"]))
        except (ValueError, KeyError, TypeError):
            out.append(None)
    return out


# The most rows the file may hold: the window plus its slack, one per minute,
# and the row just appended.
MAX_ROWS = int((WINDOW_S + SLACK_S) // 60) + 1


class TestTheTrimNeverStalls:
    """Two first-line states used to switch the trim off for good, and the
    file then grew by one row a sample, without bound."""

    def _run(self, first_line: str, rewrites: list[Path]) -> None:
        path = nodes.load_path("second")
        path.parent.mkdir(parents=True)
        path.write_text(first_line, encoding="utf-8")
        samples = 300
        for i in range(samples):
            _sample_at(1000.0 + 60 * i)
        rows = _parsed_rows()
        assert rewrites, "the trim never ran"
        # Rare, too: a kept future row would force a rewrite every sample.
        assert len(rewrites) <= samples // 5
        assert None not in rows
        assert len(rows) <= MAX_ROWS
        now = 1000.0 + 60 * (samples - 1)
        assert rows[-1] == now
        assert max(rows) == now  # nothing from the future survives

    def test_a_first_row_from_this_pcs_future_is_trimmed(self, placed, rewrites):
        """This PC's clock ran ahead, then stepped back: the rows it stamped
        then are later than now. Waiting for real time to pass them would let
        the file grow for the whole jump."""
        self._run(json.dumps({**SAMPLE, "ts": 1_000_000.0}) + "\n", rewrites)

    def test_a_blank_first_line_is_trimmed(self, placed, rewrites):
        self._run("\n", rewrites)

    def test_a_first_row_whose_ts_overflows_a_float_is_trimmed(self, placed, rewrites):
        """json.loads keeps a 309-digit ts as an int, and float() of it raises
        OverflowError: a bad row like any other, never a failed store."""
        self._run('{"ts": ' + "9" * 309 + "}\n", rewrites)


class TestTheLoadSampleEdges:
    def test_the_slack_is_at_least_one_interval(self, placed, rewrites):
        """With a 600 s interval the slack is 600 s, not 10% of an hour: a
        row exactly one window plus one interval old is still inside it, so
        the append does not rewrite the file."""
        first, at = 1000.0, 1000.0 + WINDOW_S + 600
        for ts in (first, at):
            node_sync._append_sample(
                "second", LoadSample(**SAMPLE), at=ts, history_h=1, interval_s=600
            )
        assert rewrites == []
        assert _load_ts() == [first, at]

    def test_a_failing_sample_is_tried_once_per_interval(self, placed, monkeypatch):
        """The throttle is stamped before the append, so a load file that
        cannot be written is retried per sample interval, not per tick."""
        nodes.load_path("second").mkdir(parents=True)
        tried: list[float] = []
        real = node_sync._append_sample

        def spy(nick, sample, **kw):
            tried.append(kw["at"])
            real(nick, sample, **kw)

        monkeypatch.setattr(node_sync, "_append_sample", spy)
        clock = iter([1000.0, 1010.0])

        def pull(_node, _sids):
            return _snapshot(sample=LoadSample(**SAMPLE))

        syncer = node_sync.NodeSyncer(
            _second_only(sample_interval_s=60), pull=pull, now=lambda: next(clock)
        )
        assert syncer.tick()["second"] == (node_sync.OK, "")
        assert syncer.tick()["second"] == (node_sync.OK, "")
        assert tried == [1000.0]

    def test_a_broken_load_file_warns_once_and_says_when_it_recovers(
        self, placed, caplog
    ):
        """A daemon ticking on a broken load file must not warn every sample
        (a day at one a minute is ~1,440 warnings): one on the way in, one
        INFO on the way out, and nothing while nothing changes."""
        _capture_nodes_log(caplog)
        path = nodes.load_path("second")
        path.mkdir(parents=True)
        clock = iter(1000.0 + 60 * i for i in range(6))

        def pull(_node, _sids):
            return _snapshot(sample=LoadSample(**SAMPLE))

        syncer = node_sync.NodeSyncer(
            _second_only(sample_interval_s=60), pull=pull, now=lambda: next(clock)
        )
        for _ in range(3):
            assert syncer.tick()["second"] == (node_sync.OK, "")
        assert len([m for m in _warnings(caplog) if "load sample" in m]) == 1
        path.rmdir()
        for _ in range(3):
            syncer.tick()
        kept_again = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.INFO and "load samples kept again" in r.getMessage()
        ]
        assert kept_again == ["node second: load samples kept again"]
        assert len([m for m in _warnings(caplog) if "load sample" in m]) == 1
        assert _load_ts() == [1180.0, 1240.0, 1300.0]

    def test_the_broken_load_file_state_is_per_node(self, placed, caplog):
        """One node's broken load file must neither silence another node's
        first warning nor be 'recovered' by another node's good sample."""
        _capture_nodes_log(caplog)
        t = [1000.0]

        def pull(_node, _sids):
            return _snapshot(sample=LoadSample(**SAMPLE))

        def tick() -> None:
            results = syncer.tick()
            assert results == dict.fromkeys(("second", "third"), (node_sync.OK, ""))
            t[0] += 60

        def said(level: int, text: str) -> list[str]:
            return sorted(
                r.getMessage()
                for r in caplog.records
                if r.levelno == level and text in r.getMessage()
            )

        syncer = node_sync.NodeSyncer(
            _config(sample_interval_s=60), pull=pull, now=lambda: t[0]
        )
        nodes.load_path("second").mkdir(parents=True)
        for _ in range(3):  # second broken, third healthy
            tick()
        assert [m.split(":")[0] for m in said(logging.WARNING, "load sample")] == [
            "node second"
        ]
        assert said(logging.INFO, "kept again") == []
        nodes.load_path("third").unlink()  # third breaks too, second still is
        nodes.load_path("third").mkdir()
        tick()
        assert [m.split(":")[0] for m in said(logging.WARNING, "load sample")] == [
            "node second",
            "node third",
        ]
        nodes.load_path("second").rmdir()  # second repaired, third still broken
        for _ in range(2):
            tick()
        assert said(logging.INFO, "kept again") == [
            "node second: load samples kept again"
        ]
        assert len(said(logging.WARNING, "load sample")) == 2

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param("[" * 200_000, id="200k-open"),
            # In the window, and parsed whole on every stack: only the scan
            # makes it "not a row".
            pytest.param(
                '{"ts": 500.0, "junk": ' + "[" * 64 + "]" * 64 + "}",
                id="65-deep-in-window",
            ),
        ],
    )
    def test_a_first_line_nested_too_deep_is_trimmed_not_raised(self, placed, line):
        """Nesting past the bound is "not a row", whatever json.loads would do
        with it on this stack -- never a raise out of the tick."""
        path = nodes.load_path("second")
        path.parent.mkdir(parents=True)
        path.write_text(line + "\n", encoding="utf-8")

        def pull(_node, _sids):
            return _snapshot(sample=LoadSample(**SAMPLE))

        syncer = node_sync.NodeSyncer(
            _second_only(sample_interval_s=60, history_h=1),
            pull=pull,
            now=lambda: 1000.0,
        )
        assert syncer.tick()["second"] == (node_sync.OK, "")
        assert _load_ts() == [1000.0]


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

    def test_a_crash_keeps_the_heartbeat_as_its_marker(
        self, placed, monkeypatch, caplog
    ):
        """The crash is raised by the tick itself, not by a pull: a bug in the
        loop's own machinery must crash the loop, logged once at ERROR with
        its traceback, and leave the heartbeat behind as the marker."""
        _capture_nodes_log(caplog)

        def broken(_self, *, wait_s=None):
            raise ValueError("boom")

        monkeypatch.setattr(node_sync.NodeSyncer, "tick", broken)
        with pytest.raises(ValueError, match="boom"):
            node_sync.run_sync_loop(_config(), max_ticks=1, pull=_recording_pull([]))
        assert heartbeat_age(node_sync.HEARTBEAT_NAME) is not None
        assert node_sync.daemon_pid() is None
        (crash,) = _errors(caplog)
        assert crash.getMessage() == "node sync daemon crashed"
        assert crash.exc_info is not None

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

    def test_the_loop_keeps_a_fixed_cadence(self, placed):
        """The sleep is what is left of the tick interval after the tick, so a
        slow tick does not push every later one back."""
        clock = [0.0]
        slept: list[float] = []

        def pull(node, _sids):
            if node.nick == "second":
                clock[0] += 12.0
            return _snapshot()

        node_sync.run_sync_loop(
            _config(pull_interval_s=30, sample_interval_s=60),
            max_ticks=2,
            sleep=slept.append,
            clock=lambda: clock[0],
            pull=pull,
        )
        assert slept == [18.0]

    def test_a_tick_longer_than_the_interval_does_not_sleep(self, placed):
        clock = [0.0]
        slept: list[float] = []

        def pull(node, _sids):
            if node.nick == "second":
                clock[0] += 45.0
            return _snapshot()

        node_sync.run_sync_loop(
            _config(pull_interval_s=30, sample_interval_s=60),
            max_ticks=2,
            sleep=slept.append,
            clock=lambda: clock[0],
            pull=pull,
        )
        assert slept == [0.0]

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


@pytest.fixture(autouse=True)
def executors(monkeypatch) -> Iterator[list[_RecordingExecutor]]:
    """Every pull pool a syncer in this module builds is recorded, and every
    worker is joined at teardown. The join runs BEFORE monkeypatch undoes the
    NODES_DIR and home redirects (this fixture depends on monkeypatch, so it
    is torn down first), so a late _store can never write into the real
    ~/.magent -- the home-isolation law, for threads that outlive a test."""
    made: list[_RecordingExecutor] = []
    monkeypatch.setattr(_RecordingExecutor, "made", made, raising=False)
    monkeypatch.setattr(node_sync, "ThreadPoolExecutor", _RecordingExecutor)
    yield made
    _drain(made)


def _drain(executors: list[_RecordingExecutor]) -> None:
    """Join every worker (without recording it), so a released pull cannot
    still be writing the mirror after this test's path redirects are undone."""
    for e in executors:
        ThreadPoolExecutor.shutdown(e, wait=True)


class _Clock:
    """The syncer's monotonic clock, set by hand."""

    def __init__(self) -> None:
        self.at = 0.0

    def __call__(self) -> float:
        return self.at


def _running(age: int) -> tuple[str, str]:
    return (node_sync.UNREACHABLE, f"{node_sync.PULL_STILL_RUNNING} after {age}s")


def _wait_out_the_laggard(syncer: node_sync.NodeSyncer, nick: str) -> None:
    """Block (bounded) until ``nick``'s in-flight pull has ended, so the next
    tick sees a DONE previous future and takes its between-ticks branch."""
    (_, pending) = wait([syncer._inflight[nick]], timeout=10)
    assert not pending, f"{nick}'s released pull never ended"


def _hang_then_raise(
    monkeypatch: pytest.MonkeyPatch, release: threading.Event, error: Exception
) -> None:
    """Make _sync_node ITSELF hang for ``second`` until ``release``, then raise
    ``error`` -- once; later calls run the real _sync_node. Raising here, not
    from the pull, keeps the error outside any catch-all inside _sync_node."""
    real = node_sync.NodeSyncer._sync_node
    raised = threading.Event()

    def sync_node(self, nick, entries, local_user, config):
        if nick == "second" and not raised.is_set():
            raised.set()
            assert release.wait(30), "the test never released the hung pull"
            raise error
        return real(self, nick, entries, local_user, config)

    monkeypatch.setattr(node_sync.NodeSyncer, "_sync_node", sync_node)


def _reachable_again(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage() for r in caplog.records if "reachable again" in r.getMessage()
    ]


@pytest.fixture
def healthy_pulls_end_first(monkeypatch) -> None:
    """Every healthy pull has ENDED before the tick's own bounded wait starts.

    ``tick(wait_s=0.2)`` is a wall-clock race for every node, not only the hung
    one: on a loaded box (load 24-37 was seen) the healthy node's pull -- a lock
    and a few atomic writes -- outlasted 0.2 s and read PULL_STILL_RUNNING, the
    laggard's outcome. So before the tick's wait, this waits (bounded, 30 s)
    for the first of its pulls to end. With ``_blocking_pull`` exactly one pull
    is hung until the test releases it, so the first to end is the healthy one,
    whatever the load. The tick's own wait then runs unchanged, and it still
    has to return past the hung pull -- the property these tests pin."""
    real_wait = node_sync.wait

    def settled(fs, timeout=None):
        fs = list(fs)
        done, _ = real_wait(fs, timeout=30, return_when=FIRST_COMPLETED)
        assert done, "no healthy pull ended within 30 s"
        return real_wait(fs, timeout=timeout)

    monkeypatch.setattr(node_sync, "wait", settled)


@pytest.mark.usefixtures("healthy_pulls_end_first")
class TestAHungNodeDoesNotHoldTheTick:
    """One hung node used to hold every tick for up to PULL_TIMEOUT_S, so every
    healthy node's sessions.json aged past sessions_stale's two pull intervals.
    A tick now waits a bounded time, reports the laggard, and moves on."""

    def test_the_tick_returns_with_the_others_ok_and_the_laggard_still_running(
        self, placed, executors
    ):
        release = threading.Event()
        dialled: list[str] = []
        clock = _Clock()
        syncer = node_sync.NodeSyncer(
            _config(), pull=_blocking_pull(release, dialled), clock=clock
        )
        try:
            started = time.monotonic()
            first = syncer.tick(wait_s=0.2)
            clock.at = 7.0
            second = syncer.tick(wait_s=0.2)
            elapsed = time.monotonic() - started
        finally:
            release.set()
            syncer.close()
            _drain(executors)
        ok = (node_sync.OK, "")
        assert first == {"second": _running(0), "third": ok}
        assert second == {"second": _running(7), "third": ok}
        assert elapsed < 10
        # The laggard is not dialled again while its pull is still running.
        assert sorted(dialled) == ["second", "third", "third"]

    def test_a_slow_pull_inside_the_stale_threshold_is_no_news(
        self, placed, caplog, executors
    ):
        """sessions.json only reads stale past two pull intervals; a pull that
        is merely slow until then logs nothing, either way."""
        _capture_nodes_log(caplog)
        release = threading.Event()
        clock = _Clock()
        syncer = node_sync.NodeSyncer(
            _config(pull_interval_s=30), pull=_blocking_pull(release, []), clock=clock
        )
        try:
            syncer.tick(wait_s=0.2)
            clock.at = 60.0  # == 2 * pull_interval_s: not yet past it
            syncer.tick(wait_s=0.2)
            release.set()
            after = syncer.tick(wait_s=10)
        finally:
            release.set()
            syncer.close()
            _drain(executors)
        assert after == {"second": (node_sync.OK, ""), "third": (node_sync.OK, "")}
        assert _node_warnings(caplog, "second") == []
        assert _reachable_again(caplog) == []

    def test_a_pull_past_the_stale_threshold_warns_once_and_recovers_once(
        self, placed, caplog, executors
    ):
        _capture_nodes_log(caplog)
        release = threading.Event()
        clock = _Clock()
        syncer = node_sync.NodeSyncer(
            _config(pull_interval_s=30), pull=_blocking_pull(release, []), clock=clock
        )
        try:
            syncer.tick(wait_s=0.2)
            clock.at = 61.0
            syncer.tick(wait_s=0.2)
            clock.at = 300.0
            syncer.tick(wait_s=0.2)
            release.set()
            after = syncer.tick(wait_s=10)
        finally:
            release.set()
            syncer.close()
            _drain(executors)
        assert after["second"] == (node_sync.OK, "")
        assert _node_warnings(caplog, "second") == [
            "node second: unreachable (pull still running after 61s)"
        ]
        assert _reachable_again(caplog) == ["node second: reachable again"]

    def test_a_laggard_that_ends_failed_is_logged_then_recovers(
        self, placed, caplog, executors
    ):
        """The laggard's own outcome is news once it ends: FAILED logs its
        reason, and the next OK logs the recovery. The laggard is waited out
        BEFORE the next tick, so that tick takes the ended-between-ticks
        branch every time rather than collecting it in its own wait."""
        _capture_nodes_log(caplog)
        release = threading.Event()
        calls: list[str] = []

        def pull(node, _sids):
            calls.append(node.nick)
            if node.nick == "second" and calls.count("second") == 1:
                assert release.wait(30), "the test never released the hung pull"
                raise remote_mux.RemoteError(1, "pull.sh: broke\n", ("ssh",))
            return _snapshot()

        syncer = node_sync.NodeSyncer(_config(), pull=pull, clock=_Clock())
        try:
            syncer.tick(wait_s=0.2)
            release.set()
            _wait_out_the_laggard(syncer, "second")
            syncer.tick(wait_s=10)
        finally:
            release.set()
            syncer.close()
            _drain(executors)
        assert _node_warnings(caplog, "second") == [
            "node second: failed (pull.sh: broke)"
        ]
        # E11 (96c51f8): a node back from a failure other than unreachable is
        # "ok again (was failed)"; "reachable again" names a transport recovery.
        assert _reachable_again(caplog) == []
        assert "node second: ok again (was failed)" in [
            r.getMessage() for r in caplog.records
        ]

    def test_a_stale_pull_noted_mid_error_leaves_the_error_to_its_collector(
        self, placed, caplog, executors, monkeypatch
    ):
        """The E11 x E13 seam: a tick notes a pull past the stale threshold as
        unreachable while its worker is recording an internal error. That note
        must not take the error (it would log "unreachable" at ERROR with a
        traceback that is not an unreachable node's); the tick that collects
        the finished pull logs it, once, at ERROR."""
        _capture_nodes_log(caplog)
        recorded = threading.Event()
        release = threading.Event()
        real = node_sync.NodeSyncer._internal_error

        def internal_error(self, nick, e):
            out = real(self, nick, e)
            if nick == "second":
                recorded.set()
                assert release.wait(30), "the test never released the worker"
            return out

        monkeypatch.setattr(node_sync.NodeSyncer, "_internal_error", internal_error)

        def pull(node, _sids):
            if node.nick == "second":
                raise KeyError("boom")
            return _snapshot()

        clock = _Clock()
        syncer = node_sync.NodeSyncer(
            _config(pull_interval_s=30), pull=pull, clock=clock
        )
        try:
            syncer.tick(wait_s=0.2)
            assert recorded.wait(10), "the worker never recorded its error"
            clock.at = 61.0  # past 2 * pull_interval_s: the running pull is news
            stale = syncer.tick(wait_s=0.2)
            release.set()
            _wait_out_the_laggard(syncer, "second")
            collected = syncer.tick(wait_s=10)
        finally:
            release.set()
            syncer.close()
            _drain(executors)
        assert stale["second"] == _running(61)
        assert _node_warnings(caplog, "second") == [
            "node second: unreachable (pull still running after 61s)"
        ]
        assert collected["second"] == (node_sync.FAILED, "internal error: KeyError")
        (error,) = _node_errors(caplog)
        assert error.getMessage() == "node second: failed (internal error: KeyError)"
        assert error.exc_info is not None

    def test_a_node_readded_while_its_old_pull_runs_is_not_dialled_twice(
        self, placed, executors
    ):
        """A removed node's pull is kept until it ends, so re-adding the node
        finds it instead of starting a second pull beside it."""
        release = threading.Event()
        dialled: list[str] = []
        syncer = node_sync.NodeSyncer(
            _config(), pull=_blocking_pull(release, dialled), clock=_Clock()
        )
        third_only = _config(
            pool={"third": POOL["third"]},
            projects=[ProjectConfig(path="web", node="third")],
        )
        try:
            syncer.tick(wait_s=0.2)
            syncer.reconfigure(third_only)
            syncer.tick(wait_s=0.2)
            syncer.reconfigure(_config())
            back = syncer.tick(wait_s=0.2)
        finally:
            release.set()
            syncer.close()
            _drain(executors)
        assert back["second"] == _running(0)
        assert dialled.count("second") == 1

    def test_a_removed_nodes_pull_that_raises_still_surfaces(
        self, placed, executors, monkeypatch
    ):
        """No outcome is reported for a node that left the pool, but an error
        its worker raised is not swallowed: the next tick re-raises it. Raised
        from _sync_node itself, outside any catch-all inside it, so this pins
        the gone-node re-raise however _sync_node classifies a pull."""
        release = threading.Event()
        _hang_then_raise(monkeypatch, release, ValueError("orphan"))
        syncer = node_sync.NodeSyncer(
            _config(), pull=_recording_pull([]), clock=_Clock()
        )
        third_only = _config(
            pool={"third": POOL["third"]},
            projects=[ProjectConfig(path="web", node="third")],
        )
        try:
            syncer.tick(wait_s=0.2)
            syncer.reconfigure(third_only)
            release.set()
            _wait_out_the_laggard(syncer, "second")
            with pytest.raises(ValueError, match="orphan"):
                syncer.tick(wait_s=10)
        finally:
            release.set()
            syncer.close()
            _drain(executors)

    def test_a_laggard_stores_under_the_config_its_tick_started_with(
        self, placed, executors
    ):
        """A reconfigure while a pull is running must not reach that pull: it
        was dialled under the old pool and is stored under the old settings
        (here the history window, which decides what load.jsonl keeps)."""
        entered = threading.Event()
        release = threading.Event()
        hosts: list[str] = []

        def pull(node, _sids):
            hosts.append(node.host)
            if node.nick == "second":
                entered.set()
                assert release.wait(30), "the test never released the hung pull"
            return _snapshot(sample=nodes.LoadSample(**SAMPLE))

        old_row = json.dumps({**SAMPLE, "ts": 1000.0 - 2 * 3600})
        nodes.write_text_atomic(nodes.load_path("second"), old_row + "\n")
        moved = {"second": NodeConfig(nick="second", host="box-moved", user="demo")}
        syncer = node_sync.NodeSyncer(
            _config(history_h=24), pull=pull, now=lambda: 1000.0
        )
        try:
            syncer.tick(wait_s=0.2)
            assert entered.wait(10)
            syncer.reconfigure(_config(pool=moved, history_h=1))
        finally:
            release.set()
            _drain(executors)
        rows = [
            json.loads(x)["ts"]
            for x in nodes.load_path("second").read_text(encoding="utf-8").splitlines()
        ]
        assert "box-moved" not in hosts
        assert rows == [1000.0 - 2 * 3600, 1000.0]  # the old 24 h window kept it

    def test_a_laggard_that_raises_surfaces_on_the_next_tick(
        self, placed, executors, monkeypatch
    ):
        """A worker that raises after its tick stopped waiting is re-raised by
        the next tick -- the loop then crashes, as for any escaping exception.
        The error is raised by _sync_node itself, outside the try E11
        (96c51f8) adds a catch-all to, so this still pins the laggard re-raise
        after that merge. Only the FIRST call raises: the redial is healthy,
        so only the ended laggard's own result can make this tick raise."""
        release = threading.Event()
        _hang_then_raise(monkeypatch, release, ValueError("late"))
        syncer = node_sync.NodeSyncer(_config(), pull=_recording_pull([]))
        try:
            first = syncer.tick(wait_s=0.2)
            release.set()
            _wait_out_the_laggard(syncer, "second")
            with pytest.raises(ValueError, match="late"):
                syncer.tick(wait_s=10)
        finally:
            release.set()
            syncer.close()
            _drain(executors)
        assert first["second"][0] == node_sync.UNREACHABLE
        assert first["second"][1].startswith(node_sync.PULL_STILL_RUNNING)

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

    def test_ticks_over_the_same_pool_share_one_executor(self, placed, executors):
        syncer = node_sync.NodeSyncer(_config(), pull=_recording_pull([]))
        try:
            syncer.tick()
            syncer.tick()
        finally:
            syncer.close()
        assert len(executors) == 1

    def test_one_shot_waits_for_every_pull(self, placed):
        """run_once has no next tick to collect a laggard, so it joins."""

        def slow(_node, _sids):
            time.sleep(0.3)
            return _snapshot()

        assert node_sync.run_once(_config(), pull=slow) == {
            "second": (node_sync.OK, ""),
            "third": (node_sync.OK, ""),
        }

    def test_the_daemon_waits_half_a_tick_for_its_pulls(self):
        cfg = _config(pull_interval_s=30, sample_interval_s=60)
        assert node_sync.tick_wait_s(cfg) == node_sync.tick_interval_s(cfg) / 2 == 15.0

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


def _pull_shown(sid: str, root: str) -> tuple[str, ...]:
    """The shown form of the first pull final_pull would send for ``sid``
    on second (as local user demo) -- what a refusal must name exactly."""
    node = nodes.node_for_nick(_config(), "second", local_user="demo")
    spec = remote_mux.SidPull(roots=(root,), project_dir=None, since=0.0)
    return remote_mux._run_shown(node, *remote_mux._pull_call({sid: spec}))


class TestTheFinalPull:
    def test_a_first_final_pull_learns_the_directory_then_pulls_its_transcripts(
        self, placed, fake_ssh
    ):
        _answer(
            fake_ssh,
            "box-second",
            meta=pull_meta(realpaths={"api": "/home/demo/magent/api"}),
            files={"api/transcripts/abc.jsonl": "x\n"},
        )
        result = node_sync.final_pull(_config(), "api")
        assert result is not None
        assert len(_calls_to(fake_ssh, "box-second")) == 2
        assert nodes.transcripts_dir("second", "api") / "abc.jsonl" in result.files
        assert result.since == 4999.0
        assert _marks() == {
            "api": {"since": 4999.0, "realpath": "/home/demo/magent/api"}
        }

    def test_a_final_pull_with_a_known_directory_is_one_call(self, placed, fake_ssh):
        nodes.write_json_atomic(
            nodes.pull_marks_path("second"),
            {"api": {"since": 10.0, "realpath": "/home/demo/magent/api"}},
        )
        _answer(
            fake_ssh,
            "box-second",
            meta=pull_meta(realpaths={"api": "/home/demo/magent/api"}),
        )
        node_sync.final_pull(_config(), "api")
        (call,) = _calls_to(fake_ssh, "box-second")
        assert _payload(call)["sids"]["api"]["since"] == 10.0

    def test_a_project_that_was_never_placed_has_nothing_to_pull(
        self, placed, fake_ssh
    ):
        assert node_sync.final_pull(_config(), "nowhere") is None
        assert fake_ssh.calls() == []

    @pytest.mark.parametrize(
        ("state", "cls"),
        [
            ("torn", "ValueError"),
            ("malformed-entry", "ValueError"),
            ("busy", "PermissionError"),
        ],
    )
    def test_an_unreadable_map_raises_and_is_never_read_as_never_placed(
        self, placed, fake_ssh, monkeypatch, state, cls
    ):
        # None is "never placed". A map that could not be read says nothing
        # about placement, so it is an error the caller must handle -- one
        # that names the map's failure by class alone, never its path.
        before = _seed_marks(api=(10.0, "/home/demo/magent/api"))
        if state == "torn":
            nodes.NODE_MAP_PATH.write_text("{ torn", encoding="utf-8")
        elif state == "malformed-entry":
            # Round-2 ruling: dropped, api's entry read as "never placed".
            raw = json.loads(nodes.NODE_MAP_PATH.read_text(encoding="utf-8"))
            raw["api"]["nick"] = "../x"
            nodes.NODE_MAP_PATH.write_text(json.dumps(raw), encoding="utf-8")
        else:
            # Intact on disk, but still locked after the strict read's
            # retries (a Windows reader racing a replace).
            def busy() -> dict[str, NodeMapEntry]:
                raise PermissionError(13, "The process cannot access the file")

            monkeypatch.setattr(nodes, "load_node_map_strict", busy)
        with pytest.raises(OSError) as info:
            node_sync.final_pull(_config(), "api")
        assert type(info.value.__cause__).__name__ == cls
        assert str(info.value) == f"the node map could not be read ({cls})"
        assert fake_ssh.calls() == []
        assert nodes.pull_marks_path("second").read_bytes() == before

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
        _answer(fake_ssh, "box-second", rc=255, stderr=REFUSED)
        with pytest.raises(remote_mux.RemoteError) as info:
            node_sync.final_pull(_config(), "api")
        assert info.value.rc == 255

    @pytest.mark.parametrize("sid", ["a\nb", "../x", "", "CON"])
    def test_a_session_name_this_pc_cannot_pull_is_refused_before_any_ssh(
        self, placed, fake_ssh, sid
    ):
        nodes.write_node_map({"api": _entry("second", sid)})
        with pytest.raises(remote_mux.RemoteError) as info:
            node_sync.final_pull(_config(), "api", local_user="demo")
        assert not isinstance(info.value, node_sync.PullUnfinished)
        # cq-G14 C-R3-1: a refusal made here, never a node's unreadable answer
        # -- the recall goes on after the first and stops on the second.
        assert isinstance(info.value, remote_mux.PullRefused)
        assert not isinstance(info.value, remote_mux.NotAPull)
        assert info.value.rc == 0
        assert info.value.stderr_tail == f"not a pullable session name: {sid!r}"
        assert info.value.command_redacted == _pull_shown(sid, f"~/magent/{sid}")
        assert info.value.command_redacted[0] == "ssh"
        assert "demo@box-second" in info.value.command_redacted
        assert info.value.command_redacted[-1].startswith("<stdin: ")
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
            node_sync.final_pull(_config(), "api", local_user="demo")
        assert info.value.rc == 0
        assert info.value.stderr_tail == (
            "session 'api' has an empty remote_root in the node map"
        )
        assert info.value.command_redacted == _pull_shown("api", "")
        assert info.value.command_redacted[0] == "ssh"
        assert fake_ssh.calls() == []

    def test_a_good_session_name_still_pulls(self, placed, fake_ssh):
        nodes.write_node_map({"api": _entry("second", "api-2")})
        _answer(fake_ssh, "box-second")
        result = node_sync.final_pull(_config(), "api")
        assert result is not None
        (call,) = _calls_to(fake_ssh, "box-second")
        assert set(_payload(call)["sids"]) == {"api-2"}


def _scripted_pull(
    monkeypatch, *snaps: remote_mux.NodeSnapshot, clock: _Clock | None = None
) -> list[dict]:
    """final_pull's pulls answered in order by ``snaps``; returns what each
    call asked for. With ``clock``, each pull takes 60 s of it."""
    asked: list[dict] = []
    replies = iter(snaps)

    def pull(_node, sids):
        asked.append(dict(sids))
        if clock is not None:
            clock.at += 60.0
        return next(replies)

    monkeypatch.setattr(node_sync, "_pull_node", pull)
    return asked


def _cut(resume: float, files: tuple[Path, ...] = ()) -> remote_mux.NodeSnapshot:
    """A reply for ``api`` cut at the pull cap, owing files from ``resume``."""
    return _snapshot(
        realpaths={"api": _REAL},
        files=files,
        truncated={"api": ("api/transcripts/owed.jsonl",)},
        resume={"api": resume},
    )


_REAL = "/home/demo/magent/api"


class TestAFinalPullThatDidNotFinish:
    def test_a_file_that_could_not_be_stored_raises(self, placed, fake_ssh):
        (nodes.transcripts_dir("second", "api") / "abc.jsonl").mkdir(parents=True)
        _answer(
            fake_ssh,
            "box-second",
            meta=pull_meta(realpaths={"api": _REAL}),
            files={"api/transcripts/abc.jsonl": "x\n"},
        )
        with pytest.raises(node_sync.PullUnfinished) as info:
            node_sync.final_pull(_config(), "api")
        assert info.value.rc == 0
        assert info.value.stderr_tail == (
            "the pull did not finish: a file of session 'api' could not be stored"
            " on this PC"
        )
        assert info.value.not_stored is True
        assert info.value.command_redacted == ("pull.sh",)

    def test_a_store_failure_on_the_second_call_raises_too(self, placed, monkeypatch):
        _scripted_pull(
            monkeypatch,
            _snapshot(realpaths={"api": _REAL}),
            _snapshot(realpaths={"api": _REAL}, failed_sids=frozenset({"api"})),
        )
        with pytest.raises(node_sync.PullUnfinished, match="could not be stored"):
            node_sync.final_pull(_config(), "api")

    def test_a_store_failure_on_the_first_call_raises_after_a_clean_second(
        self, placed, monkeypatch
    ):
        """Only the second call asks for transcripts, but the first one's
        state records count too: a clean second answer does not absolve it."""
        asked = _scripted_pull(
            monkeypatch,
            _snapshot(realpaths={"api": _REAL}, failed_sids=frozenset({"api"})),
            _snapshot(realpaths={"api": _REAL}),
        )
        with pytest.raises(node_sync.PullUnfinished, match="could not be stored"):
            node_sync.final_pull(_config(), "api")
        assert len(asked) == 2

    def test_cut_replies_are_resumed_until_one_comes_back_whole(
        self, placed, monkeypatch, fake_ssh
    ):
        """A backlog bigger than one pull cap is not a refusal: each cut reply
        is resumed from the mark it advanced, and every file of every chunk
        is installed. Real pull.sh replies, parsed and stored for real; only
        the reply changes between calls."""
        _seed_marks(api=(10.0, _REAL))
        owed = {"api": ["api/transcripts/owed.jsonl"]}
        chunks = iter(
            [
                ({"truncated": owed, "resume": {"api": 500.0}}, "a"),
                ({"truncated": owed, "resume": {"api": 800.0}}, "b"),
                ({}, "c"),
            ]
        )
        asked: list[float] = []
        real_pull = node_sync._pull_node

        def pull(node, sids):
            meta, name = next(chunks)
            _forget_replies(fake_ssh)
            _answer(
                fake_ssh,
                "box-second",
                meta=pull_meta(realpaths={"api": _REAL}, **meta),
                files={f"api/transcripts/{name}.jsonl": f"{name}\n"},
            )
            asked.append(sids["api"].since)
            return real_pull(node, sids)

        monkeypatch.setattr(node_sync, "_pull_node", pull)
        result = node_sync.final_pull(_config(), "api")
        assert asked == [
            10.0,
            math.nextafter(500.0, -math.inf),
            math.nextafter(800.0, -math.inf),
        ]
        assert result is not None
        assert len(result.files) == 3
        home = nodes.transcripts_dir("second", "api")
        for name in "abc":
            assert (home / f"{name}.jsonl").read_text(encoding="utf-8") == f"{name}\n"
        assert _marks() == {
            "api": {"since": 5000.0 - remote_mux.WATERMARK_OVERLAP_S, "realpath": _REAL}
        }
        assert result.since == 5000.0 - remote_mux.WATERMARK_OVERLAP_S

    def test_a_resume_that_stops_moving_the_mark_raises_on_that_call(
        self, placed, monkeypatch, caplog
    ):
        """The first cut reply moves the mark, the second names no resume
        point and does not: asking a third time would get the same reply, so
        the pull is asked for exactly twice, with plenty of deadline left."""
        _capture_nodes_log(caplog)
        _seed_marks(api=(10.0, _REAL))
        asked = _scripted_pull(
            monkeypatch,
            _cut(500.0),
            _snapshot(
                realpaths={"api": _REAL},
                truncated={"api": ("api/transcripts/owed.jsonl",)},
            ),
        )
        with pytest.raises(
            node_sync.PullUnfinished, match="without moving its mark"
        ) as info:
            node_sync.final_pull(_config(), "api", wait_s=1e9)
        assert len(asked) == 2
        # Asked a third time the node would answer the same: no remedy.
        assert info.value.stuck is True
        assert _marks() == {
            "api": {"since": math.nextafter(500.0, -math.inf), "realpath": _REAL}
        }
        # Round-2 ruling: both marks in nodes.log, where the recall points.
        mark = math.nextafter(500.0, -math.inf)
        assert any(
            "without moving its mark" in w
            and f"asked from mark {mark!r}, the node answered {mark!r}" in w
            # ...and what it still owes: files that may share one mtime.
            and "still owed: ('api/transcripts/owed.jsonl',)" in w
            for w in _warnings(caplog)
        ), _warnings(caplog)

    def test_a_resume_that_moves_the_mark_back_says_so_and_stops(
        self, placed, monkeypatch, caplog
    ):
        """A node whose clock went back behind the mark resets it to 0.0
        (``remote_mux.next_since``): that stops the loop like a mark that did
        not move, and the error says which of the two it was."""
        _capture_nodes_log(caplog)
        _seed_marks(api=(10.0, _REAL))
        clock_back = _snapshot(
            now=100.0,
            realpaths={"api": _REAL},
            truncated={"api": ("api/transcripts/owed.jsonl",)},
            resume={"api": 50.0},
        )
        asked = _scripted_pull(monkeypatch, _cut(500.0), clock_back)
        with pytest.raises(node_sync.PullUnfinished) as info:
            node_sync.final_pull(_config(), "api", wait_s=1e9)
        assert info.value.stderr_tail == (
            "the pull did not finish: 1 file(s) did not fit in the reply and are"
            " still on the node (the reply reached the pull cap and moved its mark"
            " back)"
        )
        assert info.value.not_stored is False
        assert info.value.stuck is True
        assert len(asked) == 2
        asked_from = math.nextafter(500.0, -math.inf)
        assert any(
            "and moved its mark back" in w
            and f"asked from mark {asked_from!r}, the node answered 0.0" in w
            and "still owed: ('api/transcripts/owed.jsonl',)" in w
            for w in _warnings(caplog)
        ), _warnings(caplog)

    def test_a_reply_still_cut_at_the_deadline_raises_with_the_mark_at_its_resume(
        self, placed, monkeypatch, caplog
    ):
        """``wait_s`` bounds the resuming too: every pull here takes 60 s of
        a 100 s budget, so the second cut reply is the last one asked for.
        The mark it leaves resumes where that reply stopped."""
        _capture_nodes_log(caplog)
        _seed_marks(api=(10.0, _REAL))
        clock = _Clock()
        asked = _scripted_pull(
            monkeypatch, _cut(500.0), _cut(800.0), _cut(900.0), clock=clock
        )
        with pytest.raises(
            node_sync.PullUnfinished, match="still cut when the deadline passed"
        ) as info:
            node_sync.final_pull(_config(), "api", wait_s=100.0, now=clock)
        assert info.value.rc == 0
        # Cut by the deadline, not stuck: another run carries on from here.
        assert info.value.stuck is False
        assert len(asked) == 2
        # The marks moved: nothing for nodes.log to explain.
        assert not any("asked from mark" in w for w in _warnings(caplog))
        assert _marks() == {
            "api": {"since": math.nextafter(800.0, -math.inf), "realpath": _REAL}
        }

    def test_a_resume_call_that_fails_keeps_the_mark_the_cut_reply_earned(
        self, placed, monkeypatch
    ):
        """Each mark is written before the next call: a resume that dies on
        the wire leaves the next final pull (or tick) at the first resume
        point, not back at the mark this final pull started from."""
        _seed_marks(api=(10.0, _REAL))
        replies = iter([_cut(500.0)])

        def pull(_node, _sids):
            reply = next(replies, None)
            if reply is None:
                raise remote_mux.RemoteError(255, "Connection reset", ("ssh",))
            return reply

        monkeypatch.setattr(node_sync, "_pull_node", pull)
        with pytest.raises(remote_mux.RemoteError, match="Connection reset"):
            node_sync.final_pull(_config(), "api")
        assert _marks() == {
            "api": {"since": math.nextafter(500.0, -math.inf), "realpath": _REAL}
        }

    def test_a_cut_reply_that_does_not_move_the_mark_raises_at_once(
        self, placed, monkeypatch
    ):
        """A cut reply naming no resume point leaves the mark where it was;
        asking again would get the same reply, so final_pull raises after ONE
        call however much of the deadline is left."""
        before = _seed_marks(api=(10.0, _REAL))
        asked = _scripted_pull(
            monkeypatch,
            _snapshot(
                realpaths={"api": _REAL},
                truncated={"api": ("api/transcripts/b.jsonl",)},
            ),
        )
        with pytest.raises(
            node_sync.PullUnfinished, match="without moving its mark"
        ) as info:
            node_sync.final_pull(_config(), "api", wait_s=1e9)
        assert info.value.rc == 0
        assert len(asked) == 1
        assert nodes.pull_marks_path("second").read_bytes() == before

    def test_a_cut_first_call_does_not_count_when_the_second_asks_again(
        self, placed, monkeypatch
    ):
        """The second call asks for everything from 0.0, so only its answer
        says whether every file is home."""
        asked = _scripted_pull(
            monkeypatch,
            _snapshot(
                realpaths={"api": _REAL},
                truncated={"api": ("api/state/x.json",)},
                resume={"api": 1.0},
            ),
            _snapshot(realpaths={"api": _REAL}),
        )
        result = node_sync.final_pull(_config(), "api")
        assert len(asked) == 2
        assert result is not None
        assert result.since == 9000.0 - remote_mux.WATERMARK_OVERLAP_S

    def test_a_root_the_node_cannot_resolve_is_logged_and_the_pull_returns(
        self, placed, monkeypatch, caplog
    ):
        _capture_nodes_log(caplog)
        asked = _scripted_pull(monkeypatch, _snapshot())
        result = node_sync.final_pull(_config(), "api")
        assert result == remote_mux.PullResult(files=(), since=0.0)
        assert len(asked) == 1
        assert _node_warnings(caplog, "second") == [
            (
                "node second: final pull of 'api': the node reported no real "
                "path for its root, so its transcripts were not requested"
            )
        ]

    def test_state_records_are_pruned_after_a_stored_pull(self, placed, monkeypatch):
        nodes.write_json_atomic(
            nodes.pull_marks_path("second"), {"api": {"since": 10.0, "realpath": "/r"}}
        )
        gone = nodes.state_dir("second", "api") / "gone.json"
        gone.parent.mkdir(parents=True)
        gone.write_text("{}", encoding="utf-8")
        _scripted_pull(
            monkeypatch, _snapshot(realpaths={"api": "/r"}, state_files={"api": ()})
        )
        node_sync.final_pull(_config(), "api")
        assert not gone.exists()

    def test_state_records_are_kept_when_the_pull_failed(self, placed, monkeypatch):
        nodes.write_json_atomic(
            nodes.pull_marks_path("second"), {"api": {"since": 10.0, "realpath": "/r"}}
        )
        gone = nodes.state_dir("second", "api") / "gone.json"
        gone.parent.mkdir(parents=True)
        gone.write_text("{}", encoding="utf-8")
        _scripted_pull(
            monkeypatch,
            _snapshot(
                realpaths={"api": "/r"},
                state_files={"api": ()},
                failed_sids=frozenset({"api"}),
            ),
        )
        with pytest.raises(node_sync.PullUnfinished):
            node_sync.final_pull(_config(), "api")
        assert gone.exists()


class TestWhatAFinalPullLeavesBehind:
    def test_another_sessions_mark_survives(self, placed, monkeypatch):
        nodes.write_json_atomic(
            nodes.pull_marks_path("second"),
            {
                "api": {"since": 10.0, "realpath": "/r"},
                "other": {"since": 7.0, "realpath": "/o"},
            },
        )
        _scripted_pull(monkeypatch, _snapshot(realpaths={"api": "/r"}))
        node_sync.final_pull(_config(), "api")
        assert _marks()["other"] == {"since": 7.0, "realpath": "/o"}

    def test_the_second_call_asks_for_the_learned_directory_and_its_files_count(
        self, placed, monkeypatch
    ):
        a, b = Path("a.jsonl"), Path("b.jsonl")
        asked = _scripted_pull(
            monkeypatch,
            _snapshot(realpaths={"api": _REAL}, files=(a,)),
            _snapshot(realpaths={"api": _REAL}, files=(a, b)),
        )
        result = node_sync.final_pull(_config(), "api")
        assert asked[1]["api"].project_dir == encoded_project_dir(_REAL)
        assert asked[1]["api"].since == 0.0
        # Both calls ship the session's state records: each path is listed once.
        assert result == remote_mux.PullResult(files=(a, b), since=8999.0)

    def test_by_default_it_outwaits_a_daemon_pull_holding_the_node(
        self, placed, monkeypatch
    ):
        """The daemon holds the node lock for one whole pull: up to
        PULL_TIMEOUT_S, the reap, then the store."""
        waits: list[float] = []

        @contextlib.contextmanager
        def lock(_nick, *, wait_s):
            waits.append(wait_s)
            yield

        monkeypatch.setattr(node_sync, "node_lock", lock)
        _scripted_pull(monkeypatch, _snapshot(realpaths={"api": "/r"}))
        nodes.write_json_atomic(
            nodes.pull_marks_path("second"), {"api": {"since": 1.0, "realpath": "/r"}}
        )
        node_sync.final_pull(_config(), "api")
        assert waits == [remote_mux.PULL_TIMEOUT_S + 2.0]


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
            "second": (node_sync.FAILED, "local error: PermissionError"),
            "third": (node_sync.OK, ""),
        }

    def test_a_spawn_failure_logs_the_os_words_once_never_the_client_path(
        self, placed, monkeypatch, caplog
    ):
        # pull_node's ssh call is quiet (the caller reports), so _note's
        # WARNING is the one line carrying the OS's words; the detail `node
        # sync --once` prints keeps the class alone.
        _capture_nodes_log(caplog)
        client = "/opt/secret/ssh"

        def denied(*_a: object, **_k: object) -> object:
            raise PermissionError(13, "Permission denied", client)

        monkeypatch.setattr("magent.remote_mux.find_ssh", lambda: client)
        monkeypatch.setattr(remote_mux.subprocess, "Popen", denied)
        syncer = node_sync.NodeSyncer(_config())
        first = syncer.tick()
        syncer.tick()
        shown = "could not start ssh (PermissionError)"
        assert first["second"] == (node_sync.FAILED, shown)
        assert _node_warnings(caplog, "second") == [
            f"node second: failed ({shown}): errno 13, Permission denied"
        ]
        assert not any(client in r.getMessage() for r in caplog.records)

    def test_an_os_errors_own_words_are_logged_once_not_shown(self, placed, caplog):
        # `node sync --once` prints the detail: the class there, and the OS's
        # words (a path on this PC) in nodes.log -- once per state change,
        # like every other failure, not once per tick.
        _capture_nodes_log(caplog)
        denied = PermissionError(13, "Permission denied", r"C:\Users\demo\a.jsonl")
        syncer = node_sync.NodeSyncer(_config(), pull=_pull_raising({"second": denied}))
        first = syncer.tick()
        syncer.tick()
        assert first["second"] == (node_sync.FAILED, "local error: PermissionError")
        assert "Permission denied" not in first["second"][1]
        assert "a.jsonl" not in first["second"][1]
        assert _node_warnings(caplog, "second") == [
            f"node second: failed (local error: PermissionError): {denied}"
        ]
        assert "a.jsonl" in str(denied)

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

    @pytest.mark.usefixtures("stepped_clock")
    def test_an_over_cap_reply_is_reported_by_its_cap_not_the_childs_last_words(
        self, fake_ssh
    ):
        """The real over-cap error: its stderr is ``reply exceeded N bytes``
        and then whatever the child said -- on Linux the killed fake's LAST
        line is its own BrokenPipeError, exactly the noise this rule skips.
        The cap is why the pull failed, so the log detail names it."""
        fake_ssh.set_reply("flood", stderr="boom: disk full\n")
        fake_ssh.set_mode("flood")
        node = nodes.Node(nick="second", host="box-second", user="demo", root="~")
        with pytest.raises(remote_mux.RemoteError) as info:
            remote_mux.run(node, ["flood"], timeout_s=60, max_stdout_bytes=FLOOD_CAP)
        assert "boom: disk full" in info.value.stderr_tail.splitlines()[1:]
        assert node_sync._classify(info.value) == (
            node_sync.FAILED,
            f"reply exceeded {FLOOD_CAP} bytes",
        )

    @pytest.mark.usefixtures("stepped_clock")
    def test_an_over_cap_pull_logs_the_words_the_row_leaves_out(
        self, placed, fake_ssh, monkeypatch
    ):
        # The row is the cap. nodes.log has the rest, escaped on the one
        # line: the child's last words before the flood, and the half line
        # the cap cut (after_cap).
        monkeypatch.setattr(remote_mux, "PULL_MAX_REPLY_BYTES", FLOOD_CAP)
        fake_ssh.set_reply("box-second", stderr="boom: disk full\nwriting \x1bblo")
        fake_ssh.set_mode("flood")
        cap = f"reply exceeded {FLOOD_CAP} bytes"
        syncer = node_sync.NodeSyncer(_second_only())
        try:
            assert syncer.tick() == {"second": (node_sync.FAILED, cap)}
        finally:
            syncer.close()
        (line,) = [
            line
            for line in (log.LOG_DIR / "nodes.log")
            .read_text(encoding="utf-8")
            .splitlines()
            if "node second: failed" in line
        ]
        assert f"({cap}): {cap}\\nboom: disk full" in line
        assert "after the cap: writing \\x1bblo" in line

    def test_a_failure_logs_the_tail_its_one_line_left_out(self, placed, caplog):
        _capture_nodes_log(caplog)
        err = remote_mux.RemoteError(1, "cannot write\nboom", ("ssh",))
        node_sync.NodeSyncer(_config(), pull=_pull_raising({"second": err})).tick()
        assert _node_warnings(caplog, "second") == [
            "node second: failed (boom): cannot write\\nboom"
        ]

    def test_an_unreachable_node_logs_its_last_line_alone(self, placed, caplog):
        # The tail is a FAILED node's extra: an ssh banner ahead of the real
        # refusal stays out of an unreachable node's line.
        _capture_nodes_log(caplog)
        err = remote_mux.RemoteError(
            255, "banner\nPermission denied (publickey).", ("ssh",)
        )
        node_sync.NodeSyncer(_config(), pull=_pull_raising({"second": err})).tick()
        assert _node_warnings(caplog, "second") == [
            "node second: unreachable (Permission denied (publickey).)"
        ]

    def test_only_the_flag_marks_an_over_cap_reply_never_the_text(self):
        """A node whose stderr merely STARTS with the cap's wording is not an
        over-cap reply: the detail stays its last line."""
        err = remote_mux.RemoteError(
            1, "reply exceeded 5 bytes\nthe real error", ("ssh",)
        )
        assert node_sync._classify(err) == (node_sync.FAILED, "the real error")

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


class TestAnEscapedLineIsWhatTheNodeSaid:
    def test_a_literal_escape_and_the_byte_it_names_log_differently(self):
        # A node that printed the four characters \x1b did not send ESC: a
        # backslash is escaped too, so the log tells the two apart.
        assert node_sync.escaped(r"\x1b") == r"\\x1b"
        assert node_sync.escaped("\x1b") == r"\x1b"

    @pytest.mark.parametrize(
        "said",
        [
            pytest.param(r"\x1b", id="literal-escape"),
            pytest.param("\x1b]0;x\x07boom", id="real-esc"),
            pytest.param(r"C:\Users\demo\a.jsonl", id="windows-path"),
            pytest.param("bad \ufffd byte", id="u-fffd"),
            # A lone surrogate is its \ud800 escape, which decodes back to it.
            pytest.param("hi\ud800", id="lone-surrogate"),
            pytest.param("first\n\tsecond", id="newline-and-tab"),
        ],
    )
    def test_an_escaped_line_decodes_back_to_what_the_node_said(self, said):
        line = node_sync.escaped(said)
        assert all(" " <= c <= "~" for c in line)
        assert line.encode("ascii").decode("unicode_escape") == said


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
        bad = '{"since": ' + since + ', "realpath": "/home/demo/magent/api"}'
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


# Past the bound, whatever json.loads would do with it on this stack: 200k
# open arrays (RecursionError on one stack, JSONDecodeError on another), or one
# level past it beside a good file's fields (parsed whole on every stack).
# Every node-JSON reader refuses both before json parses them.
_TOO_DEEP = "[" * 200_000
_DEEP = ["200k-open", "65-deep-beside"]
_A_MARK = {"api": {"since": 100.0, "realpath": "/home/demo/magent/api"}}


def _past(deep: str, good: dict[str, object]) -> str:
    """``good``'s text nested past the bound the way ``deep`` names."""
    if deep == "200k-open":
        return _TOO_DEEP
    return json.dumps({**good, "junk": json.loads("[" * 64 + "]" * 64)})


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
        assert results["second"] == (
            node_sync.FAILED,
            "unreadable pull metadata (nested deeper than 64 levels)",
        )
        assert _node_errors(caplog) == []

    @pytest.mark.parametrize("deep", _DEEP)
    def test_a_node_map_nested_too_deeply_does_not_stop_the_tick(self, placed, deep):
        # It does not raise out of the tick, and -- like any map the tick
        # cannot read -- it is no licence to pull every node with nothing.
        good = json.loads(nodes.NODE_MAP_PATH.read_text(encoding="utf-8"))
        nodes.NODE_MAP_PATH.write_text(_past(deep, good), encoding="utf-8")
        asked: list[set[str]] = []

        def pull(node, sids):
            asked.append(set(sids))
            return _snap()

        results = node_sync.NodeSyncer(_config(), pull=pull).tick()
        detail = "the node map could not be read (ValueError)"
        assert results == {
            "second": (node_sync.FAILED, detail),
            "third": (node_sync.FAILED, detail),
        }
        assert asked == []

    @pytest.mark.parametrize("deep", _DEEP)
    def test_the_strict_reader_calls_it_a_bad_file(self, placed, deep):
        """ValueError, the one type every strict caller catches for a bad map
        (state_stores' attention engine holds its last records on it), in the
        same words on every stack."""
        assert nodes.load_node_map_strict()  # the good map reads
        good = json.loads(nodes.NODE_MAP_PATH.read_text(encoding="utf-8"))
        nodes.NODE_MAP_PATH.write_text(_past(deep, good), encoding="utf-8")
        with pytest.raises(ValueError) as exc:
            nodes.load_node_map_strict()
        assert str(exc.value) == f"{nodes.NODE_MAP_PATH}: nested deeper than 64 levels"
        assert nodes.read_node_map() == {}

    @pytest.mark.parametrize("deep", _DEEP)
    def test_a_watermark_file_nested_too_deeply_is_no_watermark(self, placed, deep):
        path = nodes.pull_marks_path("second")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_A_MARK), encoding="utf-8")
        assert node_sync._read_marks("second")  # the good marks read
        path.write_text(_past(deep, _A_MARK), encoding="utf-8")
        assert node_sync._read_marks("second") == {}

    @pytest.mark.parametrize("deep", _DEEP)
    def test_a_sessions_file_nested_too_deeply_is_no_snapshot(self, placed, deep):
        good = {"ts": 1.0, "sessions": ["api"]}
        path = nodes.sessions_path("second")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(good), encoding="utf-8")
        assert nodes.read_sessions("second") is not None  # the good file reads
        path.write_text(_past(deep, good), encoding="utf-8")
        assert nodes.read_sessions("second") is None

    @pytest.mark.parametrize("deep", _DEEP)
    def test_a_load_row_nested_too_deeply_is_no_row(self, deep):
        assert node_sync._row_ts('{"ts": 1.0}') == 1.0  # the good row reads
        assert node_sync._row_ts(_past(deep, {"ts": 1.0})) is None

    @pytest.mark.parametrize("deep", _DEEP)
    def test_a_node_whose_watermark_file_nests_too_deeply_still_pulls(
        self, placed, caplog, deep
    ):
        """A corrupt local file, not a bug: the node pulls from the beginning,
        and nothing is logged at ERROR (which would be a Sentry event)."""
        _capture_nodes_log(caplog)
        path = nodes.pull_marks_path("second")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_past(deep, _A_MARK), encoding="utf-8")
        asked: dict[str, dict[str, remote_mux.SidPull]] = {}

        def pull(node, sids):
            asked[node.nick] = dict(sids)
            return _snap()

        results = node_sync.NodeSyncer(_config(), pull=pull).tick()
        assert results["second"] == (node_sync.OK, "")
        assert asked["second"]["api"].since == 0.0
        assert _node_errors(caplog) == []


class TestAFinalPullThatDidNotFinishIsNotASuccess:
    """cq-G14 I1: the caller clears a placement after the last pull, and a
    cleared placement is never pulled again -- so a pull that left something
    behind on the node raises instead of returning as if it had finished."""

    @pytest.fixture
    def answers(self, placed, monkeypatch):
        """The node answers every pull with the snapshot fields given; its
        directory is already known, so each final pull is one call."""
        nodes.write_json_atomic(
            nodes.pull_marks_path("second"),
            {"api": {"since": 10.0, "realpath": "/home/demo/magent/api"}},
        )

        def load(**over):
            snap = _snapshot(
                sessions=("api",), realpaths={"api": "/home/demo/magent/api"}, **over
            )
            monkeypatch.setattr(node_sync, "_pull_node", lambda node, sids: snap)

        return load

    def test_a_file_that_could_not_be_stored_here_raises(self, answers):
        answers(failed_sids=frozenset({"api"}))
        # cq-G14 m1: its own type -- rc 0 also means a refusal made on this PC
        # and an answer that was not a pull, which a caller must not confuse.
        with pytest.raises(node_sync.PullUnfinished) as info:
            node_sync.final_pull(_config(), "api")
        assert isinstance(info.value, remote_mux.RemoteError)
        assert info.value.rc == 0
        assert info.value.stderr_tail == (
            "the pull did not finish: a file of session 'api' could not be stored"
            " on this PC"
        )
        # cq-G14 m-R3-2: the bare reason and its cause, for a caller's remedy.
        assert (
            info.value.why == "a file of session 'api' could not be stored on this PC"
        )
        assert info.value.not_stored is True
        assert info.value.stuck is False
        # The watermark held, so the next pull asks for that file again.
        assert _marks()["api"] == {"since": 10.0, "realpath": "/home/demo/magent/api"}

    def test_files_the_reply_had_no_room_for_raise_naming_how_many(self, answers):
        answers(
            truncated={"api": ("api/transcripts/a.jsonl", "api/transcripts/b.jsonl")},
            resume={"api": 50.0},
        )
        with pytest.raises(node_sync.PullUnfinished) as info:
            node_sync.final_pull(_config(), "api")
        assert info.value.rc == 0
        assert (
            "2 file(s) did not fit in the reply and are still on the node"
            in info.value.stderr_tail
        )
        assert info.value.why == (
            "2 file(s) did not fit in the reply and are still on the node"
            " (the reply reached the pull cap without moving its mark)"
        )
        assert info.value.not_stored is False
        assert info.value.stuck is True
        # cq-G14 I-R2-1: saved BEFORE the raise, and just under the first file
        # still owed -- the node's clock would put the owed files behind the
        # watermark, and no later pull would ask for them.
        assert _marks()["api"] == {
            "since": math.nextafter(50.0, -math.inf),
            "realpath": "/home/demo/magent/api",
        }

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ({"failed_sids": frozenset({"api"})}, {}),
            (
                {},
                {"truncated": {"api": ("api/transcripts/a.jsonl",)}},
            ),
        ],
        ids=["first-call", "second-call"],
    )
    def test_either_call_of_a_first_sight_pull_can_leave_it_unfinished(
        self, placed, monkeypatch, first, second
    ):
        """cq-G14 m3 (N3a/N3b): a directory seen for the first time takes two
        calls, and what EITHER left behind makes the pull unfinished."""
        real = "/home/demo/magent/api"
        replies = iter(
            [
                _snapshot(sessions=("api",), realpaths={"api": real}, **first),
                _snapshot(sessions=("api",), realpaths={"api": real}, **second),
            ]
        )
        asked: list[str | None] = []

        def pull(_node, sids):
            asked.append(sids["api"].project_dir)
            return next(replies)

        monkeypatch.setattr(node_sync, "_pull_node", pull)
        with pytest.raises(node_sync.PullUnfinished):
            node_sync.final_pull(_config(), "api")
        assert asked == [None, encoded_project_dir(real)]

    def test_another_sessions_failure_is_not_this_ones(self, answers):
        answers(failed_sids=frozenset({"web"}), truncated={"web": ("w",)})
        assert node_sync.final_pull(_config(), "api") is not None
