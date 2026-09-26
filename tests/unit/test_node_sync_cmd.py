"""`magent node sync`: the shell over node_sync -- exit codes and lines, no logic."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from magent import cli, log, node_sync, nodes
from magent.cli import node_cmd
from magent.config import SCHEMA_VERSION
from magent.env import get_env
from magent.lockfile import exclusive_lock
from magent.nodes import NodeMapEntry
from tests.unit._pull_reply import pull_meta, pull_reply

if TYPE_CHECKING:
    from collections.abc import Iterator

    from magent.config import MagentConfig


@pytest.fixture
def pool_config(tmp_path, tmp_config, monkeypatch):
    root = tmp_path / "nodes"
    monkeypatch.setattr(nodes, "NODES_DIR", root)
    monkeypatch.setattr(nodes, "NODE_MAP_PATH", root / "node-map.json")
    nodes.write_node_map(
        {
            "api": NodeMapEntry("second", "api", 1.0, False, "~/magent/api"),
            "web": NodeMapEntry("third", "web", 1.0, False, "~/magent/web"),
        }
    )
    return tmp_config(
        {
            "version": SCHEMA_VERSION,
            "settings": {
                "nodes": {
                    "second": {"host": "devino-second", "user": "amin"},
                    "third": {"host": "devino-third", "user": "amin"},
                }
            },
            "projects": [
                {"path": "api", "node": "second"},
                {"path": "web", "node": "third"},
            ],
        }
    )


@pytest.fixture
def daemon_lock() -> Iterator[None]:
    """The daemon's lock, held by this test: a daemon is alive
    (``node_sync.daemon_running`` asks the lock, never the pid file)."""
    with exclusive_lock(node_sync.LOCK_NAME):
        yield


@pytest.fixture
def stranger() -> Iterator[subprocess.Popen[bytes]]:
    """A live process that is NOT the daemon -- what a recycled pid names."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        yield child
    finally:
        child.kill()
        child.wait(timeout=10)


def _record_pid(pid: int) -> None:
    node_sync._PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    node_sync._PID_PATH.write_text(str(pid))


def _pid_is_mine() -> None:
    _record_pid(os.getpid())


def _child(
    *, exit_code: int | None = None, pid: int = 4242, signals: list[str] | None = None
) -> SimpleNamespace:
    """What spawn_detached hands back: the detached child's Popen, alive
    (``poll()`` is None) unless an exit code is given. Any kill/terminate
    lands in ``signals`` -- a slow child must never be killed for it."""
    sent = signals if signals is not None else []
    return SimpleNamespace(
        pid=pid,
        poll=lambda: exit_code,
        kill=lambda: sent.append("kill"),
        terminate=lambda: sent.append("terminate"),
        send_signal=lambda sig: sent.append(f"signal {sig}"),
    )


@pytest.fixture(autouse=True)
def _no_endless_loop(monkeypatch):
    """No test here runs the foreground loop without --ticks, so reaching it
    unbounded is a failure in a second -- not a test that hangs CI until its
    timeout (a flag the shell ignores falls through to exactly that loop).
    A bounded run (--ticks) still gets the real loop."""
    real_loop = node_sync.run_sync_loop

    def bounded(cfg: object, *, max_ticks: int | None = None, **kw: object) -> int:
        if max_ticks is None:
            pytest.fail("fell through to the endless foreground sync loop")
        return real_loop(cfg, max_ticks=max_ticks, **kw)

    monkeypatch.setattr(node_sync, "run_sync_loop", bounded)


class TestNodeSync:
    def test_once_reports_every_node_and_fails_when_one_is_down(
        self, runner, pool_config, fake_ssh
    ):
        fake_ssh.set_reply("devino-second", stdout=pull_reply(pull_meta()))
        fake_ssh.set_reply(
            "devino-third",
            stderr="ssh: connect to host devino-third port 22: Connection refused\n",
            rc=255,
        )
        result = runner.invoke(
            cli.main, ["--config", pool_config, "node", "sync", "--once"]
        )
        assert result.exit_code == 1, result.output
        assert "@second  ok" in result.stdout
        assert (
            "@third  unreachable  ssh: connect to host devino-third port 22: "
            "Connection refused"
        ) in result.stdout

    def test_once_fails_when_the_first_node_fails_and_the_last_is_ok(
        self, runner, pool_config, fake_ssh
    ):
        """The exit code aggregates every node, not whichever printed last."""
        fake_ssh.set_reply(
            "devino-second",
            stderr="ssh: connect to host devino-second port 22: Connection refused\n",
            rc=255,
        )
        fake_ssh.set_reply("devino-third", stdout=pull_reply(pull_meta()))
        result = runner.invoke(
            cli.main, ["--config", pool_config, "node", "sync", "--once"]
        )
        assert result.exit_code == 1, result.output
        assert "@second  unreachable" in result.stdout
        assert "@third  ok" in result.stdout

    def test_once_while_the_daemon_runs_defers_to_its_tick(
        self, runner, pool_config, fake_ssh, daemon_lock
    ):
        result = runner.invoke(
            cli.main, ["--config", pool_config, "node", "sync", "--once"]
        )
        assert result.exit_code == 0, result.output
        assert "its own next tick is this one" in result.stdout
        assert fake_ssh.calls() == []

    def test_nothing_to_sync_is_said_and_is_not_an_error(self, runner, tmp_config):
        path = tmp_config({"version": SCHEMA_VERSION, "projects": [{"path": "api"}]})
        result = runner.invoke(cli.main, ["--config", path, "node", "sync", "--once"])
        assert result.exit_code == 0
        assert "Nothing to sync: no project runs on a node." in result.stdout

    def test_stop_when_nothing_runs(self, runner):
        result = runner.invoke(cli.main, ["node", "sync", "--stop"])
        assert result.exit_code == 0
        assert "Node sync daemon was not running." in result.stdout

    def test_stop_does_only_the_stop(self, runner, pool_config):
        """--stop prints its one line and returns; it never goes on to load
        the pool and sync (the autouse stub fails the test if it does)."""
        result = runner.invoke(
            cli.main, ["--config", pool_config, "node", "sync", "--stop"]
        )
        assert result.exit_code == 0, result.output
        assert result.stdout == "  - Node sync daemon was not running.\n"

    def test_a_daemon_whose_pid_is_unknown_is_not_reported_as_nothing_running(
        self, runner, daemon_lock
    ):
        """stop_daemon() is False both for "nothing ran" and for "a daemon
        holds the lock and could not be stopped"; only the lock tells them
        apart. With no pid file there is nothing to kill."""
        result = runner.invoke(cli.main, ["node", "sync", "--stop"])
        assert result.exit_code == 1, result.output
        assert "was not running" not in result.stdout
        assert "Could not stop the node sync daemon (pid unknown)." in result.stdout

    def test_a_refused_kill_is_not_reported_as_nothing_running(
        self, runner, monkeypatch, daemon_lock, stranger
    ):
        """A daemon started by another user or in Session 0: the kill is
        refused (Access denied / EPERM) and the daemon is still there."""
        _record_pid(stranger.pid)
        monkeypatch.setattr(node_sync, "_kill", lambda pid: False)
        result = runner.invoke(cli.main, ["node", "sync", "--stop"])
        assert result.exit_code == 1, result.output
        assert "was not running" not in result.stdout
        assert f"Could not stop the node sync daemon (pid {stranger.pid})." in (
            result.stdout
        )

    def test_the_daemon_flag_spawns_the_foreground_loop_detached(
        self, runner, pool_config, monkeypatch
    ):
        spawned: list[list[str]] = []

        def spawn(argv: list[str]) -> SimpleNamespace:
            spawned.append(argv)
            _pid_is_mine()
            return _child()

        monkeypatch.setattr("magent.launch.spawn_detached", spawn)
        result = runner.invoke(
            cli.main, ["--config", pool_config, "node", "sync", "-d"]
        )
        assert result.exit_code == 0, result.output
        assert spawned == [
            [sys.executable, "-m", "magent", "--config", pool_config, "node", "sync"]
        ]
        assert f"(pid {os.getpid()})" in result.stdout

    def test_a_child_that_exited_is_a_failure_at_once(
        self, runner, pool_config, monkeypatch
    ):
        """A child that died never becomes the daemon: say so on the next
        poll, not after the whole start budget."""
        slept: list[float] = []
        signals: list[str] = []
        monkeypatch.setattr(
            "magent.launch.spawn_detached",
            lambda argv: _child(exit_code=1, signals=signals),
        )
        monkeypatch.setattr(node_cmd, "time", SimpleNamespace(sleep=slept.append))
        result = runner.invoke(
            cli.main, ["--config", pool_config, "node", "sync", "-d"]
        )
        assert result.exit_code == 1
        assert "node sync daemon failed to start" in result.stdout
        assert "~/.magent/logs/nodes.log" in result.stdout  # where to look next
        assert len(slept) <= 2
        assert signals == []

    def test_a_child_still_alive_at_the_deadline_is_not_a_failure(
        self, runner, pool_config, monkeypatch
    ):
        """A cold child can take longer than any budget (measured 13.1 s once,
        60 ms after the poll gave up). Alive but not yet holding the lock is
        "still starting", never a failure the daemon then contradicts."""
        slept: list[float] = []
        signals: list[str] = []
        monkeypatch.setattr(
            "magent.launch.spawn_detached", lambda argv: _child(signals=signals)
        )
        monkeypatch.setattr(node_cmd, "time", SimpleNamespace(sleep=slept.append))
        result = runner.invoke(
            cli.main, ["--config", pool_config, "node", "sync", "-d"]
        )
        assert result.exit_code == 0, result.output
        assert "failed to start" not in result.stdout
        assert (
            "still starting (pid 4242) -- see ~/.magent/logs/nodes.log"
        ) in result.stdout
        assert len(slept) == node_cmd._START_POLLS  # waited out the budget first
        assert signals == []  # a slow child is not a failed one: never killed

    @pytest.mark.parametrize("seconds", [6.0, 9.0])
    def test_a_child_slow_to_start_is_waited_for(
        self, runner, pool_config, monkeypatch, seconds
    ):
        """A cold child spends seconds importing before it takes the lock and
        writes its pid (measured 2.5-9.5 s on a loaded desktop). The poll must
        outlast that and name the daemon's own pid."""
        slept: list[float] = []

        def nap(s: float) -> None:
            slept.append(s)
            if sum(slept) >= seconds:
                _pid_is_mine()

        monkeypatch.setattr("magent.launch.spawn_detached", lambda argv: _child())
        monkeypatch.setattr(node_cmd, "time", SimpleNamespace(sleep=nap))
        result = runner.invoke(
            cli.main, ["--config", pool_config, "node", "sync", "-d"]
        )
        assert result.exit_code == 0, result.output
        assert f"Node sync daemon running (pid {os.getpid()})" in result.stdout
        assert sum(slept) < seconds + 0.5  # still returns as soon as it appears

    def test_a_running_daemon_is_reported_not_doubled(
        self, runner, pool_config, monkeypatch, daemon_lock
    ):
        _pid_is_mine()
        monkeypatch.setattr(
            "magent.launch.spawn_detached", lambda argv: pytest.fail("spawned")
        )
        result = runner.invoke(
            cli.main, ["--config", pool_config, "node", "sync", "-d"]
        )
        assert result.exit_code == 0
        assert "Node sync daemon already running" in result.stdout
        assert f"(pid {os.getpid()})" in result.stdout

    def test_a_recycled_pid_is_no_daemon_and_is_never_reported_as_ours(
        self, runner, pool_config, monkeypatch, stranger
    ):
        """After a crash or a reboot the pid file survives and the number is
        handed to an unrelated process. The lock is free, so there is no
        daemon: spawn one, and report the pid the NEW daemon writes -- never
        the stranger still named by the leftover file."""
        _record_pid(stranger.pid)
        spawned: list[list[str]] = []
        naps: list[float] = []

        def spawn(argv: list[str]) -> SimpleNamespace:
            spawned.append(argv)
            return _child()

        def nap(s: float) -> None:
            # The child takes a moment to write its pid file: the first poll
            # still reads the leftover, and must not take it for the daemon.
            naps.append(s)
            if len(naps) == 2:
                _pid_is_mine()

        monkeypatch.setattr("magent.launch.spawn_detached", spawn)
        monkeypatch.setattr(node_cmd, "time", SimpleNamespace(sleep=nap))
        result = runner.invoke(
            cli.main, ["--config", pool_config, "node", "sync", "-d"]
        )
        assert result.exit_code == 0, result.output
        assert len(spawned) == 1
        assert f"(pid {os.getpid()})" in result.stdout
        assert f"(pid {stranger.pid})" not in result.stdout

    @pytest.mark.parametrize(
        "flags",
        [["--stop", "-d"], ["--once", "-d"], ["--ticks", "1", "-d"]],
        ids=["stop-with-daemon", "once-with-daemon", "ticks-with-daemon"],
    )
    def test_contradictory_flags_are_a_usage_error(
        self, runner, pool_config, monkeypatch, flags
    ):
        """Each pair asks for two different runs; doing only one of them
        silently would leave the user guessing which."""
        monkeypatch.setattr(node_sync, "stop_daemon", lambda: pytest.fail("stopped"))
        monkeypatch.setattr(node_sync, "run_once", lambda cfg: pytest.fail("ran once"))
        monkeypatch.setattr(
            "magent.launch.spawn_detached", lambda argv: pytest.fail("spawned")
        )
        result = runner.invoke(
            cli.main, ["--config", pool_config, "node", "sync", *flags]
        )
        assert result.exit_code == 2, result.output
        assert "cannot be combined with -d" in result.output

    def test_the_foreground_run_without_ticks_is_unbounded(
        self, runner, pool_config, monkeypatch
    ):
        """The foreground run is also the detached daemon's body: a bound
        slipped in here (``ticks or 1``) would make the daemon exit quietly
        after one tick. The autouse guard cannot see that -- this can."""
        seen: list[int | None] = []

        def loop(cfg, *, max_ticks=None, reload=None) -> int:
            seen.append(max_ticks)
            return 0

        monkeypatch.setattr(node_sync, "run_sync_loop", loop)
        result = runner.invoke(cli.main, ["--config", pool_config, "node", "sync"])
        assert result.exit_code == 0, result.output
        assert seen == [None]

    def test_a_bare_node_command_exits_0(self, runner, pool_config):
        """The group is invoke_without_command: G's node table fills the bare
        `magent node` later, without touching the declaration."""
        result = runner.invoke(cli.main, ["--config", pool_config, "node"])
        assert result.exit_code == 0, result.output

    def test_an_edit_between_the_load_and_the_watch_is_picked_up(
        self, runner, pool_config, monkeypatch
    ):
        """The loop's reload is a ConfigWatch stamped BEFORE the command loaded
        the config. Stamped after (or at construction), an edit landing in
        between would read as the file already loaded, and the daemon would run
        the old pool until the next edit."""
        real_load = node_cmd._load_config_or_exit

        def load_then_edit(path):
            cfg = real_load(path)
            # A different size, so the stamp changes whatever the mtime tick.
            Path(pool_config).write_text(
                json.dumps({"version": SCHEMA_VERSION, "projects": [{"path": "api"}]}),
                encoding="utf-8",
            )
            return cfg

        reloaded: list[MagentConfig | None] = []

        def loop(cfg, *, max_ticks=None, reload=None) -> int:
            assert node_sync.wanted(cfg)  # the command ran on the file it loaded
            assert reload is not None
            reloaded.append(reload())
            return 0

        monkeypatch.setattr(node_cmd, "_load_config_or_exit", load_then_edit)
        monkeypatch.setattr(node_sync, "run_sync_loop", loop)
        result = runner.invoke(cli.main, ["--config", pool_config, "node", "sync"])
        assert result.exit_code == 0, result.output
        (fresh,) = reloaded
        assert fresh is not None
        assert not node_sync.wanted(fresh)  # the edit, not the loaded config


class TestTheEnvGatesOnlyTheSupervisor:
    """MAGENT_NODE_SYNC=0 (pinned for every test by conftest) stops `serve`
    from spawning the daemon. A sync that a person -- or an e2e test -- asks
    for by name runs anyway."""

    def test_the_suite_runs_with_supervision_off(self, monkeypatch):
        monkeypatch.setattr("magent.env._cached_env", None)
        assert get_env().node_sync is False

    def test_an_explicit_once_still_pulls(self, runner, pool_config, fake_ssh):
        _both_answer(fake_ssh)
        result = runner.invoke(
            cli.main, ["--config", pool_config, "node", "sync", "--once"]
        )
        assert result.exit_code == 0, result.output
        assert _hosts_dialled(fake_ssh) == ["amin@devino-second", "amin@devino-third"]

    def test_an_explicit_daemon_flag_still_spawns(
        self, runner, pool_config, monkeypatch
    ):
        spawned: list[list[str]] = []

        def spawn(argv: list[str]) -> SimpleNamespace:
            spawned.append(argv)
            _pid_is_mine()
            return _child()

        monkeypatch.setattr("magent.launch.spawn_detached", spawn)
        result = runner.invoke(
            cli.main, ["--config", pool_config, "node", "sync", "-d"]
        )
        assert result.exit_code == 0, result.output
        assert len(spawned) == 1

    def test_the_foreground_loop_still_runs(self, runner, pool_config, fake_ssh):
        _both_answer(fake_ssh)
        result = runner.invoke(
            cli.main, ["--config", pool_config, "node", "sync", "--ticks", "1"]
        )
        assert result.exit_code == 0, result.output
        assert _hosts_dialled(fake_ssh) == ["amin@devino-second", "amin@devino-third"]


def _both_answer(fake_ssh) -> None:
    fake_ssh.set_reply("devino-second", stdout=pull_reply(pull_meta()))
    fake_ssh.set_reply("devino-third", stdout=pull_reply(pull_meta()))


def _hosts_dialled(fake_ssh) -> list[str]:
    # A first pull may add a realpath round trip, so count hosts, not calls.
    return sorted({c.argv[-2] for c in fake_ssh.calls()})


class TestImportCost:
    def test_node_sync_help_does_not_import_the_sync_subsystem(self):
        """The registration hub imports this module for every `magent`
        invocation, so node_sync (ssh, tar) and remote_mux are imported
        in-body only. A fresh interpreter, because this one has them loaded."""
        code = (
            "import sys\n"
            "from click.testing import CliRunner\n"
            "from magent import cli\n"
            "r = CliRunner().invoke(cli.main, ['node', 'sync', '--help'])\n"
            "assert r.exit_code == 0, r.output\n"
            "print(sorted(m for m in ('magent.node_sync', 'magent.remote_mux')"
            " if m in sys.modules))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "[]"


class TestDaemonState:
    def test_the_heartbeat_age_decides_stopped_ok_or_stale(self, monkeypatch):
        """node_cmd._daemon_state, the one reader of the daemon's heartbeat
        (DECISION-17), over a frozen clock. log.heartbeat_age reads time.time()
        through log's own `time`, so only that module's clock is frozen."""
        clock = SimpleNamespace(time=lambda: 1000.0)
        monkeypatch.setattr(log, "time", clock)
        assert node_cmd._daemon_state() == "stopped"  # never written

        log.write_heartbeat(node_sync.HEARTBEAT_NAME)
        path = log.HEARTBEAT_DIR / f"{node_sync.HEARTBEAT_NAME}.heartbeat"
        os.utime(path, (1000.0, 1000.0))
        clock.time = lambda: 1000.0 + log.HEARTBEAT_MAX_AGE
        assert node_cmd._daemon_state() == "ok"
        clock.time = lambda: 1000.5 + log.HEARTBEAT_MAX_AGE
        assert node_cmd._daemon_state() == "stale"  # a crash leaves it behind

        log.clear_heartbeat(node_sync.HEARTBEAT_NAME)
        assert node_cmd._daemon_state() == "stopped"  # a clean exit clears it
