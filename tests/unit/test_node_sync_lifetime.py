"""How long the node sync daemon lives, and which config it lives by.

It exits on its own once no session has been placed on a node for
``IDLE_EXIT_S`` (serve's supervisor and every status reader ask the same
``expected`` question, so nothing respawns it in the meantime), and once the
config file it was started on is gone. While it runs it records that file,
so `magent status` names a daemon following a config other than the one it
read -- never a silent pin."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from magent import cli, log, node_sync, nodes
from magent.config import MagentConfig, NodeConfig, ProjectConfig, Settings
from magent.lockfile import exclusive_lock
from magent.nodes import NodeMapEntry
from magent.remote_mux import NodeSnapshot

POOL = {"second": NodeConfig(nick="second", host="box-second", user="demo")}


def _config(*, projects=None) -> MagentConfig:
    return MagentConfig(
        projects=(
            projects
            if projects is not None
            else [ProjectConfig(path="api", node="second")]
        ),
        settings=Settings(nodes=dict(POOL)),
    )


def _entry(nick: str, sid: str = "api") -> NodeMapEntry:
    return NodeMapEntry(
        nick=nick,
        sid=sid,
        placed_ts=1.0,
        attached_existing=False,
        remote_root=f"~/magent/{sid}",
    )


def _pull(seen: list[str], then=None):
    def pull(node, _sids):
        seen.append(node.nick)
        if then is not None:
            then(len(seen))
        return NodeSnapshot(
            now=9000.0,
            sessions=(),
            sample=None,
            realpaths={},
            state_files={},
            files=(),
            failed_sids=frozenset(),
        )

    return pull


class _Clock:
    """Advanced by the loop's own sleep: every tick is one interval later."""

    def __init__(self) -> None:
        self.at = 0.0

    def __call__(self) -> float:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.at += seconds


class TestIsAnythingPlaced:
    def test_an_empty_map_is_not(self):
        assert not node_sync.placed_on_pool(_config())

    def test_a_session_on_a_pool_node_is(self):
        nodes.write_node_map({"api": _entry("second")})
        assert node_sync.placed_on_pool(_config())

    def test_a_session_on_a_node_that_left_the_pool_is_not(self):
        nodes.write_node_map({"api": _entry("gone")})
        assert not node_sync.placed_on_pool(_config())

    def test_an_unreadable_map_is_unknown_so_it_counts(self):
        nodes.NODE_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        nodes.NODE_MAP_PATH.write_text("{torn", encoding="utf-8")
        assert node_sync.placed_on_pool(_config())

    def test_expected_needs_both_a_node_project_and_a_placed_session(self):
        assert not node_sync.expected(_config())
        nodes.write_node_map({"api": _entry("second")})
        assert node_sync.expected(_config())
        assert not node_sync.expected(_config(projects=[ProjectConfig(path="api")]))

    def test_serve_asks_the_same_question(self, monkeypatch):
        from magent import launch

        monkeypatch.setenv("MAGENT_NODE_SYNC", "1")
        assert not launch.node_sync_enabled(_config())
        nodes.write_node_map({"api": _entry("second")})
        assert launch.node_sync_enabled(_config())


class TestTheDaemonWindsDownWhenNothingIsPlaced:
    def test_it_exits_after_the_idle_grace(self, caplog):
        caplog.set_level(logging.INFO, logger="magent.nodes")
        clock = _Clock()
        seen: list[str] = []
        cap = 1000
        assert (
            node_sync.run_sync_loop(
                _config(),
                max_ticks=cap,
                sleep=clock.sleep,
                clock=clock,
                pull=_pull(seen),
            )
            == 0
        )
        interval = node_sync.tick_interval_s(_config())
        assert len(seen) < cap
        assert clock.at >= node_sync.IDLE_EXIT_S
        assert clock.at <= node_sync.IDLE_EXIT_S + interval
        assert log.heartbeat_age(node_sync.HEARTBEAT_NAME) is None
        assert any("no session is placed" in r.getMessage() for r in caplog.records)

    def test_a_placement_during_the_grace_keeps_it_running(self):
        clock = _Clock()
        seen: list[str] = []

        def place(n: int) -> None:
            if n == 3:
                nodes.write_node_map({"api": _entry("second")})

        ticks = int(node_sync.IDLE_EXIT_S // node_sync.tick_interval_s(_config())) * 2
        node_sync.run_sync_loop(
            _config(),
            max_ticks=ticks,
            sleep=clock.sleep,
            clock=clock,
            pull=_pull(seen, then=place),
        )
        assert len(seen) == ticks

    def test_the_grace_starts_again_when_the_last_session_goes(self):
        nodes.write_node_map({"api": _entry("second")})
        clock = _Clock()
        seen: list[str] = []

        def down(n: int) -> None:
            if n == 5:
                nodes.write_node_map({})

        node_sync.run_sync_loop(
            _config(),
            max_ticks=1000,
            sleep=clock.sleep,
            clock=clock,
            pull=_pull(seen, then=down),
        )
        interval = node_sync.tick_interval_s(_config())
        assert clock.at >= 4 * interval + node_sync.IDLE_EXIT_S


class TestTheDaemonStopsWhenItsConfigIsGone:
    def test_the_watch_says_gone_only_on_consecutive_misses(self, tmp_path):
        path = tmp_path / "magent.config.json"
        path.write_text('{"projects": []}', encoding="utf-8")
        watch = node_sync.ConfigWatch(path)
        watch.current()
        assert not watch.gone()
        path.unlink()
        watch.current()
        assert not watch.gone()
        watch.current()
        assert watch.gone()
        path.write_text('{"projects": []}', encoding="utf-8")
        watch.current()
        assert not watch.gone()

    def test_the_loop_ends_and_says_why(self, caplog):
        caplog.set_level(logging.WARNING, logger="magent.nodes")
        nodes.write_node_map({"api": _entry("second")})
        seen: list[str] = []
        misses = iter([False, True])
        node_sync.run_sync_loop(
            _config(),
            max_ticks=10,
            sleep=lambda _s: None,
            reload=lambda: None,
            gone=lambda: next(misses, True),
            follows=Path("x.json"),
            pull=_pull(seen),
        )
        assert len(seen) == 2
        assert any("is gone" in r.getMessage() for r in caplog.records)


class TestTheDaemonNamesItsConfig:
    def test_it_is_recorded_while_it_runs_and_cleared_after(self, tmp_path):
        nodes.write_node_map({"api": _entry("second")})
        path = tmp_path / "magent.config.json"
        during: list[Path | None] = []
        node_sync.run_sync_loop(
            _config(),
            max_ticks=2,
            sleep=lambda _s: during.append(node_sync.followed_config()),
            follows=path,
            pull=_pull([]),
        )
        assert during == [path.resolve()]
        assert node_sync.followed_config() is None

    def test_no_daemon_follows_nothing(self, tmp_path):
        node_sync._write_follows(tmp_path / "left-over.json")
        assert node_sync.followed_config() is None

    def test_status_names_a_daemon_on_another_config(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("magent.cli.status._health_check", lambda port: True)
        other = tmp_path / "elsewhere.json"
        cfgpath = tmp_config(
            {"projects": [], "settings": {"nodes": {"second": {"host": "h"}}}}
        )
        node_sync._write_follows(other)
        with exclusive_lock(node_sync.LOCK_NAME):
            result = runner.invoke(cli.main, ["--config", cfgpath, "status"])
        lines = [ln for ln in result.stdout.splitlines() if "node sync follows" in ln]
        assert len(lines) == 1
        assert str(other.resolve()) in lines[0]

    def test_status_is_quiet_when_it_follows_this_config(
        self, runner, tmp_config, monkeypatch
    ):
        monkeypatch.setattr("magent.cli.status._health_check", lambda port: True)
        cfgpath = tmp_config(
            {"projects": [], "settings": {"nodes": {"second": {"host": "h"}}}}
        )
        node_sync._write_follows(Path(cfgpath))
        with exclusive_lock(node_sync.LOCK_NAME):
            result = runner.invoke(cli.main, ["--config", cfgpath, "status"])
        assert "node sync follows" not in result.stdout


@pytest.fixture(autouse=True)
def _no_psmux(monkeypatch):
    monkeypatch.setattr("magent.psmux.find_psmux", lambda: None)
