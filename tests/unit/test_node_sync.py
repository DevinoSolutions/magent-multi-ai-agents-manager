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
import time
from pathlib import Path

import pytest

from magent import agent_state, node_sync, nodes
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
from magent.nodes import NodeMapEntry

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
        assert naps == [0.2, 0.2]

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


class TestTheDaemonsPidFile:
    def test_no_pid_file_is_no_daemon(self):
        assert node_sync.daemon_pid() is None
        assert node_sync.stop_daemon() is False

    def test_a_live_pid_is_the_daemon(self):
        node_sync._PID_PATH.parent.mkdir(parents=True, exist_ok=True)
        node_sync._PID_PATH.write_text(str(os.getpid()))
        assert node_sync.daemon_pid() == os.getpid()


class TestTheImportLaw:
    def test_node_sync_never_imports_cli_launch_or_upload_server(self):
        """node_sync is a leaf. launch and upload_server import IT (the
        supervisor), and cli imports everything; walking the whole tree
        catches an in-body import too."""
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
        """HEARTBEAT_NAME is the only "node-sync" string literal in src/.
        Docstrings and f-string pieces are other constants, so only a real
        second copy of the name trips this."""
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
