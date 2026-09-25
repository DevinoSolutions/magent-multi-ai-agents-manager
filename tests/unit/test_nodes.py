"""nodes.py -- pure data + policy for running a project on a pool machine."""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import MISSING
from pathlib import Path, PurePosixPath

import pytest

from magent import nodes, psmux, remote_mux
from magent.config import (
    DEFAULT_TOOLS,
    MagentConfig,
    NodeConfig,
    ProjectConfig,
    Settings,
)
from magent.lockfile import LockHeld
from magent.nodes import (
    LoadSample,
    LocalGitState,
    Node,
    NodeConfigError,
    NodeMapEntry,
    Recipe,
    RepoSpec,
    node_for_nick,
)
from magent.sessions import IDE_TOOLS, is_ide_tool
from tests.conftest import REAL_MAGENT_DIR

NODE = Node(nick="second", host="devino-second", user="amin", root="~/magent")


class TestTheDataShapes:
    def test_a_node_targets_user_at_host(self):
        assert NODE.target == "amin@devino-second"

    @pytest.mark.parametrize(
        "shape",
        [
            NODE,
            RepoSpec(url="u", branch="main", remote_dir="~/magent/x"),
            LocalGitState(
                path=Path("x"),
                url="u",
                branch="main",
                dirty=False,
                unpushed=False,
                detached=False,
            ),
            Recipe(
                project="x",
                sid="x",
                repos=(),
                push_files=(),
                memory_dir=None,
                remote_root="~/magent/x",
            ),
            LoadSample(
                ts=0.0,
                nproc=1,
                load1=0.0,
                load5=0.0,
                load15=0.0,
                mem_total_mb=1,
                mem_avail_mb=1,
                my_sessions=0,
            ),
        ],
    )
    def test_every_shape_is_frozen(self, shape):
        field_name = dataclasses.fields(shape)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(shape, field_name, "changed")

    def test_a_git_state_carries_no_ignored_listing_by_default(self):
        state = LocalGitState(
            path=Path("x"),
            url="u",
            branch="main",
            dirty=False,
            unpushed=False,
            detached=False,
        )
        assert state.ignored == ()

    def test_a_recipe_carries_no_warnings_by_default(self):
        recipe = Recipe(
            project="x",
            sid="x",
            repos=(),
            push_files=(),
            memory_dir=None,
            remote_root="~/magent/x",
        )
        assert recipe.warnings == ()


class TestTheNodeStoreLayout:
    def test_the_store_lives_under_magent_nodes(self):
        # tests/conftest.py has monkeypatched both constants on the LIVE module
        # (they are import-bound), so reading nodes.NODES_DIR here would pin
        # the patch. A fresh, unregistered load of the module re-runs the
        # PRODUCT's own binding -- under the redirected home, so Path.home() is
        # tmp -- without touching sys.modules or spawning an interpreter.
        spec = importlib.util.find_spec("magent.nodes")
        assert spec is not None
        assert spec.loader is not None
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        store_dir, map_path = fresh.NODES_DIR, fresh.NODE_MAP_PATH
        assert store_dir == Path.home() / ".magent" / "nodes"
        assert map_path == store_dir / "node-map.json"

    @pytest.mark.parametrize("name", ["NODES_DIR", "NODE_MAP_PATH"])
    def test_no_test_can_reach_the_real_store(self, name):
        assert not getattr(nodes, name).is_relative_to(REAL_MAGENT_DIR)


class TestEncodedProjectDir:
    def test_it_delegates_to_the_one_encoder(self, monkeypatch):
        # An inlined copy of the encoder would still match it on any input;
        # only a patched encoder proves the wrapper calls through.
        monkeypatch.setattr(nodes, "encode_claude_project_path", lambda p: "sentinel")
        assert nodes.encoded_project_dir("/anything") == "sentinel"

    def test_a_node_side_path_encodes_by_the_same_rule(self):
        assert (
            nodes.encoded_project_dir("/home/amin/magent/sendly")
            == "-home-amin-magent-sendly"
        )


ENTRY = NodeMapEntry(
    nick="second",
    sid="api",
    placed_ts=1727200000.0,
    attached_existing=False,
    remote_root="/home/amin/magent/api",
)


# The five fields every node-map.json written by the first release carries.
V1_FIELDS = ("nick", "sid", "placed_ts", "attached_existing", "remote_root")


def _entry_text(placed_ts: str) -> str:
    """ENTRY as raw JSON text with ``placed_ts`` spelled literally -- NaN,
    Infinity and a 400-digit int are text json.dumps cannot be asked for."""
    fields = {**dataclasses.asdict(ENTRY), "placed_ts": "@TS@"}
    return json.dumps(fields).replace('"@TS@"', placed_ts)


class _Busy:
    """A stand-in NODE_MAP_PATH whose read_text raises ``errors`` in order,
    then serves ``text`` -- the Windows reader racing an os.replace."""

    def __init__(self, errors: list[OSError], text: str = "{}") -> None:
        self.errors = list(errors)
        self.text = text
        self.reads = 0

    def read_text(self, encoding: str) -> str:
        self.reads += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.text


@pytest.fixture
def node_map(tmp_path, monkeypatch):
    path = tmp_path / "nodes" / "node-map.json"
    monkeypatch.setattr(nodes, "NODE_MAP_PATH", path)
    return path


class TestTheNodeMap:
    def test_what_is_written_reads_back(self, node_map):
        other = dataclasses.replace(
            ENTRY, nick="third", sid="web", attached_existing=True
        )
        nodes.write_node_map({"api": ENTRY, "web": other})
        assert nodes.read_node_map() == {"api": ENTRY, "web": other}

    def test_the_file_is_keyed_by_project_name(self, node_map):
        # The on-disk shape D writes and E/G read -- pinned, not implied.
        nodes.write_node_map({"api": ENTRY})
        assert json.loads(node_map.read_text(encoding="utf-8")) == {
            "api": {
                "nick": "second",
                "sid": "api",
                "placed_ts": 1727200000.0,
                "attached_existing": False,
                "remote_root": "/home/amin/magent/api",
                "target": "",
                "cwd": "",
            }
        }

    def test_a_missing_file_is_an_empty_map(self, node_map):
        assert not node_map.exists()
        assert nodes.read_node_map() == {}

    def test_a_torn_file_is_an_empty_map(self, node_map):
        node_map.parent.mkdir(parents=True)
        node_map.write_text('{"api": {"nick": "sec', encoding="utf-8")
        assert nodes.read_node_map() == {}

    @pytest.mark.parametrize("text", ["[1, 2]", '"api"', "null", ""])
    def test_a_file_that_is_not_an_object_is_an_empty_map(self, node_map, text):
        node_map.parent.mkdir(parents=True)
        node_map.write_text(text, encoding="utf-8")
        assert nodes.read_node_map() == {}

    @pytest.mark.parametrize(
        "bad",
        [
            '{"nick": "third"}',
            _entry_text("true"),
            _entry_text("NaN"),
            _entry_text("Infinity"),
            _entry_text("-Infinity"),
            _entry_text("1e400"),
            # float() of a 309+-digit int raises OverflowError, not inf.
            _entry_text("1" + "0" * 400),
        ],
        ids=["partial", "bool-ts", "nan", "inf", "-inf", "1e400", "huge-int"],
    )
    def test_a_malformed_entry_is_dropped_alone(self, node_map, bad):
        node_map.parent.mkdir(parents=True)
        node_map.write_text(
            f'{{"api": {_entry_text("1727200000.0")}, "db": {bad}}}',
            encoding="utf-8",
        )
        assert nodes.read_node_map() == {"api": ENTRY}
        assert nodes.load_node_map_strict() == {"api": ENTRY}

    def test_a_hand_written_v1_file_reads_back(self, node_map):
        # Every other read test builds its input from asdict(ENTRY), which
        # moves with the dataclass. This is the first release's file, frozen.
        node_map.parent.mkdir(parents=True)
        node_map.write_text(
            '{"api": {"nick": "second", "sid": "api", "placed_ts": 1727200000.0,'
            ' "attached_existing": false, "remote_root": "/home/amin/magent/api"}}',
            encoding="utf-8",
        )
        assert nodes.read_node_map() == {"api": ENTRY}

    def test_every_field_after_v1_has_a_default(self):
        # An older file lacks every later field; without a default it can't load.
        for f in dataclasses.fields(NodeMapEntry):
            if f.name not in V1_FIELDS:
                assert f.default is not MISSING or f.default_factory is not MISSING, (
                    f.name
                )

    def test_an_unknown_field_is_ignored(self, node_map):
        # A newer magent may add a (defaulted) field; an older reader keeps working.
        node_map.parent.mkdir(parents=True)
        node_map.write_text(
            json.dumps({"api": {**dataclasses.asdict(ENTRY), "future": 1}}),
            encoding="utf-8",
        )
        assert nodes.read_node_map() == {"api": ENTRY}

    def test_a_non_finite_timestamp_is_never_written(self, node_map):
        nodes.write_node_map({"api": ENTRY})
        bad = dataclasses.replace(ENTRY, placed_ts=float("nan"))
        with pytest.raises(ValueError):
            nodes.write_node_map({"web": bad})
        assert nodes.read_node_map() == {"api": ENTRY}
        assert [p.name for p in node_map.parent.iterdir()] == ["node-map.json"]

    def test_a_write_replaces_the_whole_map(self, node_map):
        nodes.write_node_map({"api": ENTRY})
        nodes.write_node_map({"web": dataclasses.replace(ENTRY, sid="web")})
        assert set(nodes.read_node_map()) == {"web"}

    def test_a_write_leaves_no_temp_file(self, node_map):
        nodes.write_node_map({"api": ENTRY})
        assert [p.name for p in node_map.parent.iterdir()] == ["node-map.json"]

    def test_a_failed_write_keeps_the_old_map(self, node_map, monkeypatch):
        nodes.write_node_map({"api": ENTRY})

        def refuse(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(nodes.os, "replace", refuse)
        with pytest.raises(OSError, match="disk full"):
            nodes.write_node_map({"web": ENTRY})
        # read_node_map never calls os.replace, so the patch doesn't blind it.
        assert nodes.read_node_map() == {"api": ENTRY}
        assert [p.name for p in node_map.parent.iterdir()] == ["node-map.json"]

    def test_two_writes_in_flight_never_share_a_temp_file(self, node_map, monkeypatch):
        # Threads share a pid: a pid-derived temp name let one writer's
        # os.replace move (or clobber) the other's half-written file.
        held: list[Path] = []
        monkeypatch.setattr(
            nodes.os, "replace", lambda src, dst: held.append(Path(src))
        )
        nodes.write_node_map({"api": ENTRY})
        nodes.write_node_map({"web": ENTRY})
        assert len(set(held)) == 2
        assert all(p.exists() and p.parent == node_map.parent for p in held)

    @pytest.mark.parametrize(
        "field", [f.name for f in dataclasses.fields(NodeMapEntry)]
    )
    def test_every_field_is_frozen(self, field):
        # setattr with a parametrized name: no literal attribute, so no
        # suppression comment is needed (DECISION-26 iv).
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(ENTRY, field, "x")


class TestTheStrictRead:
    """load_node_map_strict: ``{}`` means "no file" and nothing else."""

    def test_a_missing_file_is_an_empty_map(self, node_map):
        assert nodes.load_node_map_strict() == {}

    def test_a_torn_file_raises(self, node_map):
        node_map.parent.mkdir(parents=True)
        node_map.write_text('{"api": {"nick": "sec', encoding="utf-8")
        with pytest.raises(ValueError):
            nodes.load_node_map_strict()

    @pytest.mark.parametrize("text", ["[1, 2]", '"api"', "null"])
    def test_a_file_that_is_not_an_object_raises(self, node_map, text):
        node_map.parent.mkdir(parents=True)
        node_map.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError, match="not a JSON object"):
            nodes.load_node_map_strict()

    def test_a_briefly_busy_map_is_retried_not_read_as_empty(self, monkeypatch):
        full = json.dumps({"api": dataclasses.asdict(ENTRY)})
        busy = _Busy([PermissionError(13, "busy")] * 3, text=full)
        sleeps: list[float] = []
        monkeypatch.setattr(nodes, "NODE_MAP_PATH", busy)
        monkeypatch.setattr(nodes.time, "sleep", sleeps.append)
        assert nodes.load_node_map_strict() == {"api": ENTRY}
        assert busy.reads == 4
        assert sleeps == [nodes._BUSY_SLEEP_S] * 3

    def test_a_map_that_stays_busy_raises_after_the_retries(self, monkeypatch):
        busy = _Busy([PermissionError(13, "busy")] * 100)
        monkeypatch.setattr(nodes, "NODE_MAP_PATH", busy)
        monkeypatch.setattr(nodes.time, "sleep", lambda s: None)
        with pytest.raises(PermissionError):
            nodes.load_node_map_strict()
        assert busy.reads == nodes._BUSY_RETRIES + 1

    def test_the_tolerant_read_turns_a_busy_map_into_empty(self, monkeypatch):
        # read_node_map's contract: the map never stops a launch.
        monkeypatch.setattr(nodes, "NODE_MAP_PATH", _Busy([PermissionError()] * 100))
        monkeypatch.setattr(nodes.time, "sleep", lambda s: None)
        assert nodes.read_node_map() == {}

    def test_any_other_os_error_is_not_retried(self, monkeypatch):
        busy = _Busy([OSError(5, "I/O error")])
        sleeps: list[float] = []
        monkeypatch.setattr(nodes, "NODE_MAP_PATH", busy)
        monkeypatch.setattr(nodes.time, "sleep", sleeps.append)
        with pytest.raises(OSError, match="I/O error"):
            nodes.load_node_map_strict()
        assert (busy.reads, sleeps) == (1, [])


class TestTheMapRecordsHowToReachASession:
    def test_target_and_cwd_round_trip(self, node_map):
        entry = dataclasses.replace(
            ENTRY, target="amin@devino-second", cwd="/home/amin/magent/api"
        )
        nodes.write_node_map({"api": entry})
        assert nodes.read_node_map() == {"api": entry}

    def test_an_entry_written_before_pr_d_reads_with_empty_target_and_cwd(
        self, node_map
    ):
        node_map.parent.mkdir(parents=True)
        old = {
            k: v
            for k, v in dataclasses.asdict(ENTRY).items()
            if k not in ("target", "cwd")
        }
        node_map.write_text(json.dumps({"api": old}), encoding="utf-8")
        assert nodes.read_node_map()["api"].target == ""
        assert nodes.read_node_map()["api"].cwd == ""

    def test_a_non_string_target_reads_as_empty_not_as_a_dropped_entry(self, node_map):
        node_map.parent.mkdir(parents=True)
        raw = {**dataclasses.asdict(ENTRY), "target": 7, "cwd": ["x"]}
        node_map.write_text(json.dumps({"api": raw}), encoding="utf-8")
        assert nodes.read_node_map()["api"].target == ""
        assert nodes.read_node_map()["api"].cwd == ""

    def test_an_update_changes_one_project_and_keeps_the_rest(self, node_map):
        nodes.write_node_map({"web": dataclasses.replace(ENTRY, sid="web")})
        assert set(nodes.update_node_map("api", ENTRY)) == {"api", "web"}
        assert set(nodes.read_node_map()) == {"api", "web"}

    def test_an_update_with_none_removes_that_project(self, node_map):
        nodes.write_node_map(
            {"api": ENTRY, "web": dataclasses.replace(ENTRY, sid="web")}
        )
        nodes.update_node_map("api", None)
        assert set(nodes.read_node_map()) == {"web"}

    def test_removing_an_absent_project_writes_nothing(self, node_map):
        assert nodes.update_node_map("api", None) == {}
        assert not node_map.exists()

    def test_sixteen_concurrent_updates_all_land(self, node_map):
        # PR-D's bring-ups finish on a thread pool; the whole-map writer alone
        # would let two finishing together erase each other.
        threads = [
            threading.Thread(
                target=nodes.update_node_map,
                args=(f"p{i}", dataclasses.replace(ENTRY, sid=f"p{i}")),
            )
            for i in range(16)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert set(nodes.read_node_map()) == {f"p{i}" for i in range(16)}


class TestTheMapWriterNeverGuesses:
    """The four ways B's landed map changed the writer (plan D, Task 3's
    forward correction): read strictly, a persistent blocking sidecar, a
    retried replace, and a sweep of what a killed writer stranded."""

    def test_a_torn_map_is_refused_not_overwritten(self, node_map):
        # read_node_map would call this {} and the write would erase every
        # placement; the strict read raises and the file stays as it was.
        node_map.parent.mkdir(parents=True)
        torn = '{"api": {"nick": "sec'
        node_map.write_text(torn, encoding="utf-8")
        with pytest.raises(ValueError):
            nodes.update_node_map("web", ENTRY)
        assert node_map.read_text(encoding="utf-8") == torn

    def test_a_replace_a_reader_blocks_is_retried(self, node_map, monkeypatch):
        # Windows: a reader holding node-map.json makes os.replace fail with
        # PermissionError for as long as it holds the file.
        real_replace = os.replace
        refusals = [PermissionError(13, "held by a reader")] * 3
        sleeps: list[float] = []

        def flaky(src, dst):
            if refusals:
                raise refusals.pop()
            real_replace(src, dst)

        monkeypatch.setattr(nodes.os, "replace", flaky)
        monkeypatch.setattr(nodes.time, "sleep", sleeps.append)
        assert nodes.update_node_map("api", ENTRY) == {"api": ENTRY}
        assert nodes.read_node_map() == {"api": ENTRY}
        assert sleeps == [nodes._REPLACE_SLEEP_S] * 3
        assert [p.name for p in node_map.parent.iterdir()] == ["node-map.json"]

    def test_a_replace_that_stays_refused_raises_and_keeps_the_old_map(
        self, node_map, monkeypatch
    ):
        nodes.write_node_map({"api": ENTRY})
        attempts: list[str] = []

        def refuse(src, dst):
            attempts.append(src)
            raise PermissionError(13, "held by a reader")

        monkeypatch.setattr(nodes.os, "replace", refuse)
        monkeypatch.setattr(nodes.time, "sleep", lambda s: None)
        with pytest.raises(PermissionError):
            nodes.update_node_map("web", ENTRY)
        assert len(attempts) == nodes._REPLACE_RETRIES + 1
        assert nodes.read_node_map() == {"api": ENTRY}
        assert [p.name for p in node_map.parent.iterdir()] == ["node-map.json"]

    def test_any_other_replace_error_is_not_retried(self, node_map, monkeypatch):
        attempts: list[str] = []

        def refuse(src, dst):
            attempts.append(src)
            raise OSError(28, "disk full")

        monkeypatch.setattr(nodes.os, "replace", refuse)
        with pytest.raises(OSError, match="disk full"):
            nodes.update_node_map("api", ENTRY)
        assert len(attempts) == 1

    def test_a_temp_file_a_killed_writer_stranded_is_swept(self, node_map):
        node_map.parent.mkdir(parents=True)
        (node_map.parent / "node-map.json.k1ll3d.tmp").write_text("{", encoding="utf-8")
        unrelated = node_map.parent / "notes.tmp"
        unrelated.write_text("mine", encoding="utf-8")
        nodes.update_node_map("api", ENTRY)
        assert sorted(p.name for p in node_map.parent.iterdir()) == [
            "node-map.json",
            "notes.tmp",
        ]

    def test_the_sidecar_is_under_the_redirected_home_and_outlives_the_writer(
        self, node_map
    ):
        # exclusive_lock unlinks its file on exit, so a waiter could lock a
        # file the holder was about to delete. The map's sidecar never goes.
        nodes.update_node_map("api", ENTRY)
        assert nodes.map_lock_path() == Path.home() / ".magent" / "node-map.lock"
        assert nodes.map_lock_path().exists()
        with nodes.map_lock():
            assert nodes.map_lock_path().exists()
        assert nodes.map_lock_path().exists()

    def test_a_writer_behind_a_holder_in_this_process_says_so(self, node_map):
        with nodes.map_lock(), pytest.raises(LockHeld):
            nodes.update_node_map("api", ENTRY, wait_s=0.2)
        assert not node_map.exists()


# A second process writing the map: its own interpreter, so its own threading
# lock. It inherits conftest's redirected HOME (so the same sidecar lock), and
# the map path comes in argv.
_MAP_WRITER = """
import dataclasses, sys
from pathlib import Path
from magent import nodes

nodes.NODE_MAP_PATH = Path(sys.argv[1])
prefix, count = sys.argv[2], int(sys.argv[3])
base = nodes.NodeMapEntry(
    nick="second", sid="x", placed_ts=0.0, attached_existing=False, remote_root="~/magent/x"
)
for i in range(count):
    nodes.update_node_map(f"{prefix}{i}", dataclasses.replace(base, sid=f"{prefix}{i}"))
"""

# Holds the map's sidecar lock -- the same map_lock every writer takes -- until
# its stdin closes.
_MAP_HOLDER = """
import sys
from magent import nodes

with nodes.map_lock():
    print("held", flush=True)
    sys.stdin.read()
"""


class TestTheMapWriterIsSerializedAcrossProcesses:
    """DECISION-13: `up`, `down`, G's placement and recall are separate
    processes, and a threading lock cannot see another process. The writer
    holds the ~/.magent/node-map.lock sidecar around read + write. That path
    is resolved per call, not at import, so under conftest's HOME redirect it
    lands in tmp for this process and for the children alike."""

    def test_a_writer_waits_for_another_process_holding_the_map_lock(self, node_map):
        holder = subprocess.Popen(
            [sys.executable, "-c", _MAP_HOLDER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert holder.stdin is not None
        assert holder.stdout is not None
        try:
            assert holder.stdout.readline().strip() == "held"
            with pytest.raises(LockHeld):
                nodes.update_node_map("api", ENTRY, wait_s=0.3)
            assert not node_map.exists()
        finally:
            holder.stdin.close()
            holder.wait(timeout=30)
        assert nodes.update_node_map("api", ENTRY) == {"api": ENTRY}
        assert nodes.read_node_map() == {"api": ENTRY}

    def test_two_processes_writing_disjoint_projects_lose_nothing(self, node_map):
        writers = [
            subprocess.Popen(
                [sys.executable, "-c", _MAP_WRITER, str(node_map), prefix, "25"]
            )
            for prefix in ("a", "b")
        ]
        assert [w.wait(timeout=120) for w in writers] == [0, 0]
        assert set(nodes.read_node_map()) == {
            f"{p}{i}" for p in "ab" for i in range(25)
        }


class TestF2FindsANodeFolder:
    def _entries(self) -> dict[str, NodeMapEntry]:
        return {
            "API": dataclasses.replace(
                ENTRY,
                sid="API",
                target="amin@devino-second",
                cwd="/home/amin/magent/api",
            )
        }

    def test_a_project_name_finds_its_target_and_folder(self):
        assert nodes.open_target("API", self._entries()) == (
            "amin@devino-second",
            "/home/amin/magent/api",
        )

    def test_a_session_id_finds_it_too(self):
        entries = {"My App": dataclasses.replace(self._entries()["API"], sid="My-App")}
        assert nodes.open_target("My-App", entries) is not None

    def test_without_a_cwd_the_remote_root_is_the_folder(self):
        entries = {"API": dataclasses.replace(self._entries()["API"], cwd="")}
        assert nodes.open_target("API", entries) == (
            "amin@devino-second",
            ENTRY.remote_root,
        )

    def test_a_project_no_node_holds_is_none(self):
        assert nodes.open_target("other", self._entries()) is None

    def test_a_cloud_placement_is_none(self):
        entries = {"API": dataclasses.replace(self._entries()["API"], nick="cloud")}
        assert nodes.open_target("API", entries) is None

    def test_an_entry_from_before_pr_d_has_no_target_and_is_none(self):
        assert nodes.open_target("api", {"api": ENTRY}) is None


def _pool(entries: dict[str, NodeConfig] | None = None) -> MagentConfig:
    """A fresh pool per call: MagentConfig and Settings are plain (mutable)
    dataclasses, so one shared module-level config could carry a test's edit
    into the next. Default: ``second`` (explicit user) and ``third`` (none)."""
    if entries is None:
        entries = {
            "second": NodeConfig(nick="second", host="devino-second", user="amin"),
            "third": NodeConfig(nick="third", host="devino-third"),
        }
    return MagentConfig(projects=[], settings=Settings(nodes=entries))


class TestResolve:
    def test_a_pinned_project_resolves_to_its_node(self):
        node = nodes.resolve(
            _pool(), ProjectConfig(path="api", node="second"), local_user="whoever"
        )
        assert node == Node(
            nick="second", host="devino-second", user="amin", root="~/magent"
        )

    def test_no_configured_user_means_the_local_one_lowercased(self):
        node = nodes.resolve(
            _pool(), ProjectConfig(path="api", node="third"), local_user="Amin"
        )
        assert node.user == "amin"

    def test_auto_resolves_to_the_placed_node(self):
        node = nodes.resolve(
            _pool(),
            ProjectConfig(path="api", node="auto"),
            local_user="amin",
            placed="third",
        )
        assert node.nick == "third"

    def test_a_pinned_project_ignores_a_placement(self):
        node = nodes.resolve(
            _pool(),
            ProjectConfig(path="api", node="second"),
            local_user="amin",
            placed="third",
        )
        assert node.nick == "second"

    def test_auto_without_a_placement_is_refused(self):
        with pytest.raises(NodeConfigError, match="placement"):
            nodes.resolve(
                _pool(), ProjectConfig(path="api", node="auto"), local_user="amin"
            )

    def test_a_cloud_project_is_refused_clearly_not_a_key_error(self):
        # The cloud backend (DECISION-8, Task J) resolves its own projects; a
        # caller that hands one to the node resolver gets a named refusal.
        with pytest.raises(NodeConfigError, match="cloud backend"):
            nodes.resolve(
                _pool(), ProjectConfig(path="api", node="cloud"), local_user="amin"
            )

    def test_a_project_without_a_node_is_refused(self):
        with pytest.raises(NodeConfigError, match="not a node project"):
            nodes.resolve(_pool(), ProjectConfig(path="api"), local_user="amin")

    def test_a_nick_missing_from_the_pool_is_refused_naming_it(self):
        # A PINNED nick falls through to node_for_nick's unknown-nick error,
        # prefixed with the project; only an auto placement says "re-place".
        with pytest.raises(
            NodeConfigError,
            match=r"^api: node 'fourth' is not in settings\.nodes; "
            r"known nodes: second, third$",
        ):
            nodes.resolve(
                _pool(), ProjectConfig(path="api", node="fourth"), local_user="amin"
            )

    def test_a_stale_placement_is_refused_saying_it_was_a_placement(self):
        # The pool shrank after placement chose a node: the fix is to re-place,
        # not to edit the project's pin (it has none).
        with pytest.raises(
            NodeConfigError,
            match=r"placement chose 'fourth', which is no longer in settings\.nodes;"
            r" re-place \(known nodes: second, third\)",
        ):
            nodes.resolve(
                _pool(),
                ProjectConfig(path="api", node="auto"),
                local_user="amin",
                placed="fourth",
            )

    def test_an_empty_pool_says_there_are_no_known_nodes(self):
        with pytest.raises(NodeConfigError, match="known nodes: none"):
            nodes.resolve(
                _pool({}), ProjectConfig(path="api", node="second"), local_user="amin"
            )

    @pytest.mark.parametrize("local_user", ["root", "ROOT"])
    def test_an_implicit_root_user_is_refused(self, local_user):
        # Lowercasing happens BEFORE the D4 check, so "ROOT" cannot slip past.
        with pytest.raises(NodeConfigError, match="D4"):
            nodes.resolve(
                _pool(), ProjectConfig(path="api", node="third"), local_user=local_user
            )

    @pytest.mark.parametrize(
        ("local_user", "derived"),
        [("Amin Dhouib", "amin dhouib"), (" ", " "), ("1amin", "1amin")],
    )
    def test_a_derived_user_ssh_cannot_log_in_as_is_refused(self, local_user, derived):
        # A Windows USERNAME may hold a space; whitespace passes `if not user`.
        with pytest.raises(NodeConfigError) as err:
            nodes.resolve(
                _pool(), ProjectConfig(path="api", node="third"), local_user=local_user
            )
        assert "settings.nodes.third.user" in str(err.value)
        assert repr(local_user) in str(err.value)
        assert repr(derived) in str(err.value)

    def test_an_explicit_user_is_not_second_guessed(self):
        pool = _pool({"sixth": NodeConfig(nick="sixth", host="h", user="Svc.Account")})
        node = nodes.resolve(
            pool, ProjectConfig(path="api", node="sixth"), local_user="Amin Dhouib"
        )
        assert node.user == "Svc.Account"

    def test_an_explicit_root_user_is_honoured(self):
        pool = _pool(
            {"fifth": NodeConfig(nick="fifth", host="devino-fifth", user="root")}
        )
        node = nodes.resolve(
            pool, ProjectConfig(path="api", node="fifth"), local_user="amin"
        )
        assert node.user == "root"

    def test_no_user_anywhere_is_refused(self):
        with pytest.raises(NodeConfigError, match=r"settings\.nodes\.third\.user"):
            nodes.resolve(
                _pool(), ProjectConfig(path="api", node="third"), local_user=""
            )


class TestANickResolvesLikeAProject:
    # What a nick shares with a project is pinned through resolve() in
    # TestResolve; this class pins only what differs when there is no project.

    def test_an_unknown_nick_names_the_pool(self):
        with pytest.raises(
            NodeConfigError,
            match=r"^node 'fifth' is not in settings\.nodes; known nodes: second, third$",
        ):
            node_for_nick(_pool(), "fifth", local_user="amin")

    @pytest.mark.parametrize("nick", ["auto", "cloud"])
    def test_a_placement_word_as_a_nick_is_just_an_unknown_nick(self, nick):
        # Config validation keeps the reserved words out of the pool, so the
        # ordinary refusal is the true one; no special case is wanted.
        with pytest.raises(
            NodeConfigError,
            match=rf"^node '{nick}' is not in settings\.nodes; known nodes: second, third$",
        ):
            node_for_nick(_pool(), nick, local_user="amin")

    def test_the_label_prefixes_the_unknown_nick_error(self):
        with pytest.raises(
            NodeConfigError,
            match=r"^api: node 'fifth' is not in settings\.nodes; known nodes: second, third$",
        ):
            node_for_nick(_pool(), "fifth", local_user="amin", label="api")

    def test_an_empty_pool_says_there_are_no_known_nodes(self):
        with pytest.raises(
            NodeConfigError,
            match=r"^node 'fifth' is not in settings\.nodes; known nodes: none$",
        ):
            node_for_nick(_pool({}), "fifth", local_user="amin")

    @pytest.mark.parametrize(
        ("local_user", "derived"),
        [("Amin Dhouib", "amin dhouib"), (" ", " "), ("1amin", "1amin")],
    )
    def test_a_derived_user_ssh_cannot_log_in_as_is_refused_by_nick(
        self, local_user, derived
    ):
        # The login check lives in node_for_nick, not resolve: a caller holding
        # only a nick (one read from the node map) gets the same refusal.
        with pytest.raises(NodeConfigError) as err:
            node_for_nick(_pool(), "third", local_user=local_user)
        assert "settings.nodes.third.user" in str(err.value)
        assert repr(local_user) in str(err.value)
        assert repr(derived) in str(err.value)

    def test_an_empty_label_is_no_label(self):
        with pytest.raises(NodeConfigError, match=r"^node 'fifth' "):
            node_for_nick(_pool(), "fifth", local_user="amin", label="")

    @pytest.mark.parametrize(
        ("local_user", "expected"),
        [
            ("", r"^settings\.nodes\.third\.user is not set"),
            ("root", r"^settings\.nodes\.third: magent is running as root"),
            (
                "Amin Dhouib",
                (
                    r"^settings\.nodes\.third\.user is not set and the local "
                    r"username 'Amin Dhouib' is not a node login"
                ),
            ),
        ],
    )
    def test_the_label_never_prefixes_a_settings_error(self, local_user, expected):
        # These name settings.nodes.<nick>, the thing to fix; the project that
        # led there is not part of the fix.
        with pytest.raises(NodeConfigError, match=expected):
            node_for_nick(_pool(), "third", local_user=local_user, label="api")


class TestTheMirrorLayout:
    def test_every_path_hangs_off_the_nodes_dir(self, tmp_path):
        assert nodes.node_dir("second", nodes_dir=tmp_path) == tmp_path / "second"
        assert nodes.transcripts_dir("second", "api", nodes_dir=tmp_path) == (
            tmp_path / "second" / "api" / "transcripts"
        )
        assert nodes.state_dir("second", "api", nodes_dir=tmp_path) == (
            tmp_path / "second" / "api" / "state"
        )
        assert (
            nodes.sessions_path("second", nodes_dir=tmp_path)
            == tmp_path / "second" / "sessions.json"
        )
        assert (
            nodes.load_path("second", nodes_dir=tmp_path)
            == tmp_path / "second" / "load.jsonl"
        )
        assert (
            nodes.pull_marks_path("second", nodes_dir=tmp_path)
            == tmp_path / "second" / "pull.json"
        )

    def test_the_default_root_is_read_at_call_time(self, tmp_path, monkeypatch):
        monkeypatch.setattr(nodes, "NODES_DIR", tmp_path)
        assert nodes.state_dir("second", "api") == tmp_path / "second" / "api" / "state"


class TestAtomicWrites:
    def test_a_write_lands_whole_with_no_temp_left(self, tmp_path):
        target = tmp_path / "a" / "b.json"
        nodes.write_json_atomic(target, {"x": 1})
        assert json.loads(target.read_text(encoding="utf-8")) == {"x": 1}
        assert [p.name for p in target.parent.iterdir()] == ["b.json"]

    def test_a_failed_write_keeps_the_old_file_and_no_temp(self, tmp_path, monkeypatch):
        target = tmp_path / "b.json"
        nodes.write_text_atomic(target, "old\n")

        def refuse(_src, _dst):
            raise OSError("disk full")

        monkeypatch.setattr(nodes.os, "replace", refuse)
        with pytest.raises(OSError, match="disk full"):
            nodes.write_text_atomic(target, "new\n")
        assert target.read_text(encoding="utf-8") == "old\n"
        assert [p.name for p in tmp_path.iterdir()] == ["b.json"]

    def test_the_temp_file_is_never_a_json_a_reader_could_glob(
        self, tmp_path, monkeypatch
    ):
        # Readers glob `*.json` in a mirror dir; a half-written temp must never
        # match. It is a sibling (os.replace is atomic only within one fs).
        seen: list[Path] = []
        real_replace = nodes.os.replace

        def spy(src, dst):
            seen.append(Path(src))
            real_replace(src, dst)

        monkeypatch.setattr(nodes.os, "replace", spy)
        target = tmp_path / "b.json"
        nodes.write_json_atomic(target, {"x": 1})
        nodes.write_json_atomic(target, {"x": 2})
        assert len(seen) == 2
        assert all(p.parent == tmp_path for p in seen)
        assert all(p.suffix == ".tmp" and not p.name.endswith(".json") for p in seen)
        assert seen[0] != seen[1]

    def test_a_failing_fdopen_closes_the_descriptor_and_leaves_no_temp(
        self, tmp_path, monkeypatch
    ):
        # Between mkstemp and fdopen the raw fd is ours alone: if fdopen raises
        # it must be closed here, or it leaks (and on Windows the open handle
        # would also make the temp's unlink fail, leaving the temp behind).
        made: list[int] = []
        real_mkstemp = nodes.tempfile.mkstemp

        def recording_mkstemp(*args, **kwargs):
            fd, name = real_mkstemp(*args, **kwargs)
            made.append(fd)
            return fd, name

        def broken_fdopen(*_args, **_kwargs):
            raise MemoryError("no buffer")

        monkeypatch.setattr(nodes.tempfile, "mkstemp", recording_mkstemp)
        monkeypatch.setattr(nodes.os, "fdopen", broken_fdopen)
        target = tmp_path / "b.json"
        with pytest.raises(MemoryError, match="no buffer"):
            nodes.write_text_atomic(target, "new\n")
        assert len(made) == 1
        with pytest.raises(OSError):
            os.fstat(made[0])
        assert list(tmp_path.iterdir()) == []

    def test_a_non_finite_number_is_refused_before_anything_is_written(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        with pytest.raises(ValueError, match="JSON compliant"):
            nodes.write_json_atomic(sub / "b.json", {"ts": float("nan")})
        # Not only the target: no temp file either, in the dir it would use.
        assert list(sub.iterdir()) == []


class TestTheSessionsSnapshot:
    def test_a_written_snapshot_reads_back(self, tmp_path):
        nodes.write_json_atomic(
            nodes.sessions_path("second", nodes_dir=tmp_path),
            {"ts": 5.0, "sessions": ["api", "web"]},
        )
        assert nodes.read_sessions("second", nodes_dir=tmp_path) == nodes.NodeSessions(
            ts=5.0, sessions=("api", "web")
        )

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "{torn",
            "[]",
            '{"ts": true, "sessions": []}',
            '{"ts": 1, "sessions": "api"}',
            # A non-finite ts would make sessions_stale() answer "fresh" forever.
            '{"ts": NaN, "sessions": []}',
            '{"ts": Infinity, "sessions": []}',
            # Any non-string entry is corruption, not a name to skip.
            '{"ts": 1, "sessions": ["a", 3, null]}',
            '{"ts": 1, "sessions": ["a", ["b"]]}',
        ],
    )
    def test_an_unusable_snapshot_reads_as_none(self, tmp_path, text):
        path = nodes.sessions_path("second", nodes_dir=tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")
        assert nodes.read_sessions("second", nodes_dir=tmp_path) is None

    def test_a_missing_snapshot_reads_as_none(self, tmp_path):
        assert nodes.read_sessions("second", nodes_dir=tmp_path) is None

    def test_a_snapshot_is_stale_after_two_pull_intervals(self):
        snap = nodes.NodeSessions(ts=100.0, sessions=())
        assert not nodes.sessions_stale(snap, pull_interval_s=30, now=160.0)
        assert nodes.sessions_stale(snap, pull_interval_s=30, now=160.5)
        assert nodes.sessions_stale(None, pull_interval_s=30, now=0.0)

    def test_a_snapshot_from_the_future_reads_stale(self):
        # A backwards wall-clock jump: ts is ahead of now by more than two
        # intervals, and that reads stale too -- never fresh forever.
        snap = nodes.NodeSessions(ts=1_000.0, sessions=("api",))
        assert nodes.sessions_stale(snap, pull_interval_s=30, now=10.0)

    def test_a_future_ts_inside_the_window_still_reads_fresh(self):
        snap = nodes.NodeSessions(ts=100.0, sessions=("api",))
        assert not nodes.sessions_stale(snap, pull_interval_s=30, now=130.0)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real git repo in tmp: this fixture writes it (init/add); the code
    under test only READS it. The home is already redirected (no ~/.gitconfig,
    no global excludes); NOSYSTEM keeps the machine's system gitconfig out too."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "sendly"
    root.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True, timeout=30
        )

    files = {
        ".gitignore": ".env*\nnode_modules/\n.claude/settings.local.json\nCLAUDE.local.md\n",
        ".env": "API_KEY=1\n",
        ".env.example": "API_KEY=\n",
        "apps/web/page.tsx": "export {}\n",
        "apps/web/.env.local": "WEB=1\n",
        ".claude/settings.json": "{}\n",
        ".claude/settings.local.json": "{}\n",
        "CLAUDE.local.md": "notes\n",
        "node_modules/left-pad/.env": "INSIDE=1\n",
        "node_modules/left-pad/index.js": "\n",
        "notes.txt": "untracked, not ignored\n",
        # An untracked directory holding ONLY an ignored file: git may list the
        # directory rather than the file (TestGitsIgnoredListing pins which).
        "config/.env": "CFG=1\n",
    }
    git("init", "-q")
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git("add", ".gitignore", "apps/web/page.tsx", ".claude/settings.json")
    # Tracked although `.env*` matches it: a tracked file is the clone's, never a push.
    git("add", "-f", ".env.example")
    return root


def _state(path: Path, ignored: tuple[str, ...]) -> LocalGitState:
    return LocalGitState(
        path=path,
        url="git@github.com:amin/sendly.git",
        branch="main",
        dirty=False,
        unpushed=False,
        detached=False,
        ignored=ignored,
    )


def _real_state(repo: Path) -> LocalGitState:
    return _state(repo, remote_mux.ignored_paths(repo, timeout_s=30, label="test"))


class TestGitsIgnoredListing:
    def test_a_wholly_ignored_directory_is_one_entry(self, repo):
        listing = remote_mux.ignored_paths(repo, timeout_s=30, label="test")
        assert "node_modules/" in listing
        assert [p for p in listing if p.startswith("node_modules/")] == [
            "node_modules/"
        ]

    def test_tracked_and_merely_untracked_files_are_not_listed(self, repo):
        listing = remote_mux.ignored_paths(repo, timeout_s=30, label="test")
        assert ".env.example" not in listing
        assert "notes.txt" not in listing

    def test_an_untracked_dir_of_only_ignored_files_is_descended(self, repo):
        # git 2.52 lists BOTH `config/` and `config/.env`. The file line is what
        # push_set ships from; if a git ever drops it, this goes red first.
        listing = remote_mux.ignored_paths(repo, timeout_s=30, label="test")
        assert "config/.env" in listing

    def test_a_non_repo_is_git_rc_128_and_the_log_names_the_caller(
        self, tmp_path, monkeypatch, caplog
    ):
        # The ceiling keeps git from finding an enclosing repo above tmp.
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        with (
            caplog.at_level("WARNING", logger="magent.nodes"),
            pytest.raises(remote_mux.RemoteError) as err,
        ):
            remote_mux.ignored_paths(tmp_path, timeout_s=30, label="push-set read")
        assert err.value.rc == 128
        assert "not a git repository" in err.value.stderr_tail
        assert "push-set read failed (rc=128)" in caplog.text

    def test_a_missing_git_is_named_as_git_not_as_the_node_client(
        self, tmp_path, monkeypatch
    ):
        # _spawn maps a FileNotFoundError to the ssh client's rc and wording;
        # a LOCAL git read must say git. find_ssh plays no part here.
        empty = tmp_path / "empty-path"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        with pytest.raises(remote_mux.RemoteError) as err:
            remote_mux.ignored_paths(tmp_path, timeout_s=30, label="test")
        assert err.value.rc is None
        assert err.value.stderr_tail == "git not found on PATH"
        assert err.value.command_redacted[0] == "git"


class TestPushSet:
    def test_gitignored_env_files_ship_at_any_depth(self, repo):
        shipped = nodes.push_set(repo, [_real_state(repo)], home=Path.home())
        assert repo / ".env" in shipped
        assert repo / "apps" / "web" / ".env.local" in shipped

    def test_an_env_file_in_a_dir_of_only_ignored_files_ships(self, repo):
        shipped = nodes.push_set(repo, [_real_state(repo)], home=Path.home())
        assert repo / "config" / ".env" in shipped

    def test_the_local_claude_files_ship(self, repo):
        shipped = nodes.push_set(repo, [_real_state(repo)], home=Path.home())
        assert repo / ".claude" / "settings.local.json" in shipped
        assert repo / "CLAUDE.local.md" in shipped

    def test_a_tracked_env_example_never_ships(self, repo):
        shipped = nodes.push_set(repo, [_real_state(repo)], home=Path.home())
        assert repo / ".env.example" not in shipped

    def test_nothing_under_an_ignored_directory_ships(self, repo):
        shipped = nodes.push_set(repo, [_real_state(repo)], home=Path.home())
        assert not [p for p in shipped if p.is_relative_to(repo / "node_modules")]

    def test_an_untracked_file_git_does_not_ignore_stays_home(self, repo):
        shipped = nodes.push_set(repo, [_real_state(repo)], home=Path.home())
        assert repo / "notes.txt" not in shipped

    def test_a_wholly_ignored_claude_dir_still_ships_its_local_settings(self, tmp_path):
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "settings.local.json").write_text(
            "{}", encoding="utf-8"
        )
        (tmp_path / ".claude" / "other.json").write_text("{}", encoding="utf-8")
        shipped = nodes.push_set(
            tmp_path, [_state(tmp_path, (".claude/",))], home=Path.home()
        )
        assert shipped == (tmp_path / ".claude" / "settings.local.json",)

    def test_a_workspace_roots_own_local_files_ship(self, tmp_path):
        workspace = tmp_path / "ws"
        (workspace / "api").mkdir(parents=True)
        (workspace / ".env").write_text("X=1\n", encoding="utf-8")
        (workspace / "CLAUDE.local.md").write_text("n\n", encoding="utf-8")
        (workspace / "README.md").write_text("r\n", encoding="utf-8")
        shipped = nodes.push_set(
            workspace, [_state(workspace / "api", ())], home=Path.home()
        )
        assert shipped == (workspace / ".env", workspace / "CLAUDE.local.md")

    def test_a_project_reached_through_a_link_never_ships_a_tracked_file(
        self, repo, tmp_path
    ):
        # The project's configured path is a symlink/junction to the repo: the
        # repo is still its workspace, so the root listing (which cannot tell
        # tracked from ignored) must not run.
        link = tmp_path / "sendly-link"
        try:
            os.symlink(repo, link, target_is_directory=True)
        except OSError:
            pytest.skip("this platform/user cannot create symlinks")
        shipped = nodes.push_set(link, [_real_state(repo)], home=Path.home())
        assert not [p for p in shipped if p.name == ".env.example"]

    def test_a_monorepo_subdirectory_project_never_ships_a_tracked_file(self, repo):
        web = repo / "apps" / "web"
        (web / ".env.example").write_text("WEB=\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repo), "add", "-f", "apps/web/.env.example"],
            check=True,
            capture_output=True,
            timeout=30,
        )
        shipped = nodes.push_set(web, [_real_state(repo)], home=Path.home())
        assert not [p for p in shipped if p.name == ".env.example"]

    def test_a_git_hit_that_is_not_a_file_never_ships(self, tmp_path):
        # The listing is a snapshot: the file may be gone, or be a directory.
        (tmp_path / ".env.d").mkdir()
        shipped = nodes.push_set(
            tmp_path, [_state(tmp_path, (".env", ".env.d"))], home=Path.home()
        )
        assert shipped == ()

    def test_a_git_hit_under_a_credential_store_never_ships(self, tmp_path):
        # A dotfiles repo that IS the home dir: git's listing is no exemption.
        home = tmp_path / "home"
        (home / ".ssh").mkdir(parents=True)
        (home / ".ssh" / ".env").write_text("K=1\n", encoding="utf-8")
        assert nodes.push_set(home, [_state(home, (".ssh/.env",))], home=home) == ()

    def test_the_answer_is_sorted_and_unique(self, repo):
        shipped = nodes.push_set(
            repo, [_real_state(repo)], home=Path.home(), extras=[".env"]
        )
        assert list(shipped) == sorted(set(shipped), key=str)


def _secret_under(entry: str) -> str:
    """A file a ``_NEVER_PUSHED`` entry covers: the entry itself, or a file
    inside it when it names a directory (a trailing '/')."""
    return f"{entry}secret" if entry.endswith("/") else entry


class TestPushExtras:
    def test_an_extra_file_ships(self, repo):
        (repo / "apps" / "web" / "gcp-sa.json").write_text("{}", encoding="utf-8")
        shipped = nodes.push_set(
            repo, [_real_state(repo)], home=Path.home(), extras=["apps/web/gcp-sa.json"]
        )
        assert repo / "apps" / "web" / "gcp-sa.json" in shipped

    def test_a_missing_extra_is_a_warning_not_an_error(self, repo):
        assert nodes.push_warnings(repo, ["nope.json"], home=Path.home()) == (
            "push: nope.json does not exist; skipped",
        )
        assert repo / "nope.json" not in nodes.push_set(
            repo, [], home=Path.home(), extras=["nope.json"]
        )

    def test_an_extra_outside_the_project_is_refused(self, repo, tmp_path):
        (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
        assert nodes.push_warnings(repo, ["../outside.txt"], home=Path.home()) == (
            "push: ../outside.txt is outside the project; skipped",
        )

    def test_a_symlink_that_leaves_the_project_is_refused(self, repo, tmp_path):
        # Containment is judged after BOTH sides are resolved: a link inside
        # the project that points out of it is still outside.
        outside = tmp_path / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        link = repo / "linked.txt"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("this platform/user cannot create symlinks")
        assert nodes.push_warnings(repo, ["linked.txt"], home=Path.home()) == (
            "push: linked.txt is outside the project; skipped",
        )
        shipped = nodes.push_set(repo, [], home=Path.home(), extras=["linked.txt"])
        assert link not in shipped
        assert outside not in shipped

    def test_a_symlinked_extra_ships_under_the_name_the_user_wrote(self, repo):
        # The node recreates the path it is handed: the link's name is what the
        # project reads, not wherever the link happens to point on this PC.
        (repo / "real-sa.json").write_text("{}", encoding="utf-8")
        link = repo / "gcp-sa.json"
        try:
            link.symlink_to(repo / "real-sa.json")
        except OSError:
            pytest.skip("this platform/user cannot create symlinks")
        shipped = nodes.push_set(
            repo, [_real_state(repo)], home=Path.home(), extras=["./gcp-sa.json"]
        )
        assert link in shipped
        assert repo / "real-sa.json" not in shipped

    def test_the_users_ssh_keys_are_never_pushed(self, tmp_path):
        # A project that IS the home dir (a dotfiles repo) still cannot ship them.
        home = tmp_path / "home"
        (home / ".ssh").mkdir(parents=True)
        (home / ".ssh" / "id_ed25519").write_text("KEY", encoding="utf-8")
        assert nodes.push_set(home, [], home=home, extras=[".ssh/id_ed25519"]) == ()
        assert nodes.push_warnings(home, [".ssh/id_ed25519"], home=home) == (
            "push: .ssh/id_ed25519 is never pushed (credentials); skipped",
        )

    def test_a_store_that_will_not_resolve_under_a_linked_home_is_refused(
        self, tmp_path, monkeypatch
    ):
        # home is reached through a directory symlink and ~/.ssh alone fails to
        # resolve: judged lexically under the LINK, the key (which resolves
        # under the real home) would not match. The stores sit under the
        # resolved home, so it still does -- fail toward shipping less.
        real_home = tmp_path / "real-home"
        (real_home / ".ssh").mkdir(parents=True)
        (real_home / ".ssh" / "id_rsa").write_text("KEY", encoding="utf-8")
        home = tmp_path / "home-link"
        try:
            os.symlink(real_home, home, target_is_directory=True)
        except OSError:
            pytest.skip("this platform/user cannot create symlinks")
        real_resolve = Path.resolve

        def resolve(self, strict=False):
            if self.name == ".ssh":
                raise OSError(62, "Too many levels of symbolic links", str(self))
            return real_resolve(self, strict=strict)

        monkeypatch.setattr(Path, "resolve", resolve)
        assert nodes.push_set(home, [], home=home, extras=[".ssh/id_rsa"]) == ()
        assert nodes.push_warnings(home, [".ssh/id_rsa"], home=home) == (
            "push: .ssh/id_rsa is never pushed (credentials); skipped",
        )

    def test_the_usual_credential_files_are_on_the_list(self):
        assert set(nodes._NEVER_PUSHED) >= {
            ".ssh/",
            ".claude-swap-backup/",
            ".claude/.credentials.json",
            ".aws/credentials",
            ".netrc",
            ".gnupg/",
            ".config/gh/hosts.yml",
            ".docker/config.json",
            ".kube/config",
        }

    @pytest.mark.parametrize("entry", nodes._NEVER_PUSHED)
    def test_every_credential_store_is_refused(self, tmp_path, entry):
        # Parametrized over the list itself: a future entry is covered here.
        home = tmp_path / "home"
        rel = _secret_under(entry)
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text("SECRET", encoding="utf-8")
        assert nodes.push_set(home, [], home=home, extras=[rel]) == ()
        assert nodes.push_warnings(home, [rel], home=home) == (
            f"push: {rel} is never pushed (credentials); skipped",
        )

    @pytest.mark.parametrize("entry", nodes._NEVER_PUSHED)
    def test_a_case_variant_of_a_credential_store_is_refused(self, tmp_path, entry):
        # Refused on EVERY OS: on a case-insensitive filesystem (APFS, NTFS)
        # the variant IS the store; on a case-sensitive one refusing is cheap.
        home = tmp_path / "home"
        rel = _secret_under(entry).swapcase()
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text("SECRET", encoding="utf-8")
        assert nodes.push_set(home, [], home=home, extras=[rel]) == ()
        assert nodes.push_warnings(home, [rel], home=home) == (
            f"push: {rel} is never pushed (credentials); skipped",
        )

    def test_the_credential_match_ignores_case_even_for_posix_paths(self):
        # WindowsPath already compares case-insensitively; PosixPath does not,
        # and macOS's APFS is case-insensitive under a PosixPath.
        forbidden = [
            (PurePosixPath("/h/.ssh"), True),
            (PurePosixPath("/h/.netrc"), False),
        ]
        assert nodes._is_forbidden(PurePosixPath("/h/.SSH/id_ed25519"), forbidden)
        assert nodes._is_forbidden(PurePosixPath("/h/.NetRC"), forbidden)
        assert not nodes._is_forbidden(PurePosixPath("/h/.sshx/id"), forbidden)
        assert not nodes._is_forbidden(PurePosixPath("/h/.netrc.d/x"), forbidden)

    def test_an_extra_directory_is_a_warning(self, repo):
        assert nodes.push_warnings(repo, ["apps"], home=Path.home()) == (
            "push: apps is a directory; list its files; skipped",
        )

    def test_an_extra_that_will_not_resolve_is_a_warning_not_a_crash(
        self, tmp_path, monkeypatch
    ):
        # A symlink loop raises on resolve (RuntimeError before 3.13, OSError
        # after on some OSes); faked here so every OS and Python sees it.
        (tmp_path / "ok.json").write_text("{}", encoding="utf-8")
        real_resolve = Path.resolve

        def resolve(self, strict=False):
            if self.name == "loop.json":
                raise OSError(62, "Too many levels of symbolic links", str(self))
            return real_resolve(self, strict=strict)

        monkeypatch.setattr(Path, "resolve", resolve)
        extras = ["loop.json", "ok.json"]
        assert nodes.push_warnings(tmp_path, extras, home=Path.home()) == (
            "push: loop.json cannot be resolved; skipped",
        )
        assert nodes.push_set(tmp_path, [], home=Path.home(), extras=extras) == (
            tmp_path / "ok.json",
        )

    def test_a_symlinked_extra_that_leaves_through_the_link_ships_inside(
        self, repo, tmp_path
    ):
        # The project is a link to the repo and the extra is written through
        # the repo's real name: lexically it is outside the project, resolved
        # it is inside, so it ships at its place under the project's own path.
        (repo / "sa.json").write_text("{}", encoding="utf-8")
        link = tmp_path / "sendly-link"
        try:
            os.symlink(repo, link, target_is_directory=True)
        except OSError:
            pytest.skip("this platform/user cannot create symlinks")
        shipped = nodes.push_set(
            link, [_real_state(repo)], home=Path.home(), extras=["../sendly/sa.json"]
        )
        assert link / "sa.json" in shipped
        assert repo / "sa.json" not in shipped

    def test_a_linked_project_ships_one_file_once_under_its_own_name(
        self, repo, tmp_path
    ):
        # git lists repo/.env; the extra names link/.env. One file, one push,
        # under the path the project is configured at.
        link = tmp_path / "sendly-link"
        try:
            os.symlink(repo, link, target_is_directory=True)
        except OSError:
            pytest.skip("this platform/user cannot create symlinks")
        shipped = nodes.push_set(
            link, [_real_state(repo)], home=Path.home(), extras=[".env"]
        )
        assert link / ".env" in shipped
        assert repo / ".env" not in shipped
        assert [p for p in shipped if p.resolve() == (repo / ".env").resolve()] == [
            link / ".env"
        ]


class TestRecipeFor:
    def test_a_repo_project_is_one_repo_at_the_node_root(self, repo):
        recipe = nodes.recipe_for(
            ProjectConfig(path=str(repo), node="second"),
            NODE,
            [_real_state(repo)],
            home=Path.home(),
            project_dir=repo,
        )
        assert (recipe.project, recipe.sid) == ("sendly", "sendly")
        assert recipe.remote_root == "~/magent/sendly"
        assert recipe.repos == (
            RepoSpec(
                url="git@github.com:amin/sendly.git",
                branch="main",
                remote_dir="~/magent/sendly",
            ),
        )

    def test_the_sid_is_psmuxs_session_name_of_the_title(self, tmp_path):
        proj = ProjectConfig(path=str(tmp_path), node="second", title="Sendly v2.0")
        recipe = nodes.recipe_for(
            proj, NODE, [_state(tmp_path, ())], home=Path.home(), project_dir=tmp_path
        )
        assert recipe.project == "Sendly v2.0"
        assert recipe.sid == psmux.session_name("Sendly v2.0") == "Sendly-v2-0"

    def test_a_workspace_puts_each_child_repo_under_it(self, tmp_path):
        workspace = tmp_path / "ws"
        (workspace / "api").mkdir(parents=True)
        (workspace / "web").mkdir()
        recipe = nodes.recipe_for(
            ProjectConfig(path=str(workspace), node="second"),
            NODE,
            [_state(workspace / "api", ()), _state(workspace / "web", ())],
            home=Path.home(),
            project_dir=workspace,
        )
        assert recipe.remote_root == "~/magent/ws"
        assert [r.remote_dir for r in recipe.repos] == [
            "~/magent/ws/api",
            "~/magent/ws/web",
        ]

    def test_a_repo_outside_the_project_is_refused(self, tmp_path):
        with pytest.raises(
            NodeConfigError, match="neither the project nor a direct child"
        ):
            nodes.recipe_for(
                ProjectConfig(path=str(tmp_path / "ws"), node="second"),
                NODE,
                [_state(tmp_path / "elsewhere" / "api", ())],
                home=Path.home(),
                project_dir=tmp_path / "ws",
            )

    def test_the_push_set_rides_along(self, repo):
        state = _real_state(repo)
        recipe = nodes.recipe_for(
            ProjectConfig(path=str(repo), node="second"),
            NODE,
            [state],
            home=Path.home(),
            project_dir=repo,
        )
        assert recipe.push_files == nodes.push_set(repo, [state], home=Path.home())
        # Every push ships to the same place under remote_root that it had
        # under local_root: a consumer takes relative_to(local_root) of each.
        assert recipe.local_root == repo.resolve()
        assert recipe.push_files
        for pushed in recipe.push_files:
            pushed.relative_to(recipe.local_root)

    def test_push_warnings_ride_along(self, repo):
        recipe = nodes.recipe_for(
            ProjectConfig(path=str(repo), node="second", push=["missing.json"]),
            NODE,
            [_real_state(repo)],
            home=Path.home(),
            project_dir=repo,
        )
        assert recipe.warnings == ("push: missing.json does not exist; skipped",)

    def test_the_memory_dir_is_found_under_the_encoded_local_path(self, repo):
        memory = (
            Path.home()
            / ".claude"
            / "projects"
            / nodes.encoded_project_dir(str(repo))
            / "memory"
        )
        memory.mkdir(parents=True)
        recipe = nodes.recipe_for(
            ProjectConfig(path=str(repo), node="second"),
            NODE,
            [_real_state(repo)],
            home=Path.home(),
            project_dir=repo,
        )
        assert recipe.memory_dir == memory

    def test_no_memory_dir_is_none(self, repo):
        recipe = nodes.recipe_for(
            ProjectConfig(path=str(repo), node="second"),
            NODE,
            [_real_state(repo)],
            home=Path.home(),
            project_dir=repo,
        )
        assert recipe.memory_dir is None

    def test_a_project_reached_through_a_link_is_still_its_repo(self, repo, tmp_path):
        # The configured path is a symlink/junction to the repo: compared on
        # resolved paths, the repo IS the project, not "outside" it.
        link = tmp_path / "sendly-link"
        try:
            os.symlink(repo, link, target_is_directory=True)
        except OSError:
            pytest.skip("this platform/user cannot create symlinks")
        # The extra names link/.env while git lists repo/.env: both forms must
        # come out under ONE root, or a relpath consumer would write the other
        # outside remote_root on the node.
        recipe = nodes.recipe_for(
            ProjectConfig(path=str(link), node="second", push=[".env"]),
            NODE,
            [_real_state(repo)],
            home=Path.home(),
            project_dir=link,
        )
        assert [r.remote_dir for r in recipe.repos] == [recipe.remote_root]
        assert recipe.local_root == repo.resolve()
        assert recipe.push_files
        for pushed in recipe.push_files:
            pushed.relative_to(recipe.local_root)
        assert recipe.local_root / ".env" in recipe.push_files

    def test_a_repo_listed_through_a_link_ships_under_the_resolved_root(
        self, repo, tmp_path
    ):
        # The mirror: the project is configured at the repo itself, but git's
        # state names it through a link -- its hits still land under local_root.
        link = tmp_path / "sendly-link"
        try:
            os.symlink(repo, link, target_is_directory=True)
        except OSError:
            pytest.skip("this platform/user cannot create symlinks")
        recipe = nodes.recipe_for(
            ProjectConfig(path=str(repo), node="second"),
            NODE,
            [_real_state(link)],
            home=Path.home(),
            project_dir=repo,
        )
        assert recipe.local_root == repo.resolve()
        assert recipe.push_files
        for pushed in recipe.push_files:
            pushed.relative_to(recipe.local_root)

    def test_a_push_that_would_land_outside_the_node_folder_is_refused(self, tmp_path):
        # A git hit named through a linked repo, inside a directory that
        # itself links out of the project: no spelling of it is under
        # local_root, so the recipe refuses rather than ship it elsewhere.
        project = tmp_path / "proj"
        project.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / ".env").write_text("K=1\n", encoding="utf-8")
        via = tmp_path / "via"
        try:
            os.symlink(project, via, target_is_directory=True)
            os.symlink(outside, project / "sub", target_is_directory=True)
        except OSError:
            pytest.skip("this platform/user cannot create symlinks")
        with pytest.raises(NodeConfigError, match="outside the project"):
            nodes.recipe_for(
                ProjectConfig(path=str(project), node="second"),
                NODE,
                [_state(via, ("sub/.env",))],
                home=tmp_path / "home",
                project_dir=project,
            )

    def test_a_project_inside_a_larger_repo_is_refused_as_such(self, tmp_path):
        # A monorepo subdirectory: push_set stays safe for it, but a node
        # clones whole repos, so the refusal names what is actually wrong.
        web = tmp_path / "mono" / "apps" / "web"
        web.mkdir(parents=True)
        with pytest.raises(
            NodeConfigError, match="the project is inside a larger repo"
        ) as err:
            nodes.recipe_for(
                ProjectConfig(path=str(web), node="second"),
                NODE,
                [_state(tmp_path / "mono", ())],
                home=Path.home(),
                project_dir=web,
            )
        assert str(tmp_path / "mono") in str(err.value)

    def test_a_path_that_will_not_resolve_is_a_config_error(
        self, tmp_path, monkeypatch
    ):
        raised: list[OSError] = []

        def loop(self, strict=False):
            raised.append(OSError(62, "Too many levels of symbolic links", str(self)))
            raise raised[-1]

        with monkeypatch.context() as patched:
            patched.setattr(Path, "resolve", loop)
            with pytest.raises(NodeConfigError, match="cannot be resolved") as err:
                nodes.recipe_for(
                    ProjectConfig(path=str(tmp_path), node="second"),
                    NODE,
                    [_state(tmp_path, ())],
                    home=Path.home(),
                    project_dir=tmp_path,
                )
        assert "Too many levels of symbolic links" in str(err.value)
        # Chained, not re-worded: the traceback still shows the OS's own error.
        assert err.value.__cause__ is raised[0]

    def test_a_nul_in_the_project_path_is_a_config_error_naming_it(self, tmp_path):
        # Path.resolve may or may not reject the NUL (it varies by OS and
        # Python); either way the answer is a NodeConfigError naming the path,
        # never a bare "scandir: embedded null character".
        bad = Path(str(tmp_path / "ws") + "\0x")
        with pytest.raises(NodeConfigError) as err:
            nodes.recipe_for(
                ProjectConfig(path=str(bad), node="second"),
                NODE,
                [_state(bad / "api", ())],
                home=Path.home(),
                project_dir=bad,
            )
        assert str(bad) in str(err.value)

    def test_a_project_with_no_repo_is_refused(self, tmp_path):
        # Nothing to clone means nothing to run: an empty recipe is a caller
        # bug, not a bring-up that silently starts in an empty folder.
        with pytest.raises(NodeConfigError, match="has no git repo") as err:
            nodes.recipe_for(
                ProjectConfig(path=str(tmp_path), node="second"),
                NODE,
                [],
                home=Path.home(),
                project_dir=tmp_path,
            )
        assert str(tmp_path) in str(err.value)

    def test_a_repo_listed_twice_is_cloned_once(self, tmp_path):
        state = _state(tmp_path, ())
        recipe = nodes.recipe_for(
            ProjectConfig(path=str(tmp_path), node="second"),
            NODE,
            [state, state],
            home=Path.home(),
            project_dir=tmp_path,
        )
        assert [r.remote_dir for r in recipe.repos] == [recipe.remote_root]

    @pytest.mark.parametrize(
        ("url", "stripped"),
        [
            (
                "https://user:ghp_SECRET@github.com/org/repo.git",
                "https://github.com/org/repo.git",
            ),
            (
                "https://ghp_SECRET@github.com:8443/org/repo.git",
                "https://github.com:8443/org/repo.git",
            ),
            (
                "HTTP://x-access-token:ghp_SECRET@github.com/org/repo.git",
                "http://github.com/org/repo.git",
            ),
            # Not an http(s) allow-list: every scheme but ssh loses the WHOLE
            # userinfo, because a token-only login IS the credential there.
            (
                "git+https://user:ghp_SECRET@github.com/org/repo.git",
                "git+https://github.com/org/repo.git",
            ),
            (
                "git://ghp_SECRET@github.com/org/repo.git",
                "git://github.com/org/repo.git",
            ),
            (
                "https://user:ghp_SECRET@[2001:db8::1]:8443/org/repo.git",
                "https://[2001:db8::1]:8443/org/repo.git",
            ),
            # ssh-family keeps the login and drops only the password.
            (
                "ssh://user:ghp_SECRET@github.com/org/repo.git",
                "ssh://user@github.com/org/repo.git",
            ),
            (
                "GIT+SSH://git:ghp_SECRET@[2001:db8::1]:22/org/repo.git",
                "git+ssh://git@[2001:db8::1]:22/org/repo.git",
            ),
            (
                "ssh+git://git:ghp_SECRET@github.com/org/repo.git",
                "ssh+git://git@github.com/org/repo.git",
            ),
        ],
    )
    def test_credentials_in_an_origin_never_reach_the_recipe(
        self, tmp_path, url, stripped
    ):
        # The node's .git/config, the Recipe's repr and every log line would
        # otherwise carry this PC's token.
        state = dataclasses.replace(_state(tmp_path, ()), url=url)
        recipe = nodes.recipe_for(
            ProjectConfig(path=str(tmp_path), node="second"),
            NODE,
            [state],
            home=Path.home(),
            project_dir=tmp_path,
        )
        assert recipe.repos[0].url == stripped
        assert "ghp_SECRET" not in repr(recipe)
        assert recipe.warnings == (
            (
                f"repo {recipe.remote_root}: origin URL carried credentials; "
                "stripped -- the node authenticates with its own gh token"
            ),
        )

    @pytest.mark.parametrize(
        ("url", "stripped"),
        [
            # git's remote-helper form `<transport>::<address>`: the address is
            # stripped by the same rules, the transport is re-attached as-is.
            (
                "https::https://u:SECRET@host/r.git",
                "https::https://host/r.git",
            ),
            (
                "HTTPS::HTTPS://u:SECRET@host/r.git",
                "HTTPS::https://host/r.git",
            ),
            # Every layer of a stacked prefix is peeled, none is guessed at.
            (
                "https::https::https://u:SECRET@host/r.git",
                "https::https::https://host/r.git",
            ),
            # An empty ssh login with a password: the whole userinfo goes.
            (
                "ssh://:SECRET@host/r.git",
                "ssh://host/r.git",
            ),
        ],
    )
    def test_a_credential_behind_a_transport_or_an_empty_login_is_stripped(
        self, tmp_path, url, stripped
    ):
        state = dataclasses.replace(_state(tmp_path, ()), url=url)
        recipe = nodes.recipe_for(
            ProjectConfig(path=str(tmp_path), node="second"),
            NODE,
            [state],
            home=Path.home(),
            project_dir=tmp_path,
        )
        assert recipe.repos[0].url == stripped
        assert "SECRET" not in repr(recipe)
        assert recipe.warnings == (
            (
                f"repo {recipe.remote_root}: origin URL carried credentials; "
                "stripped -- the node authenticates with its own gh token"
            ),
        )

    @pytest.mark.parametrize(
        "url",
        [
            # The ext transport's address is a command, not a URL.
            "ext::ssh -i key git@host r.git",
            "https::https://host/r.git",
            # An empty userinfo carries nothing: no rewrite, no warning.
            "https://@host/r.git",
            "https://:@host/r.git",
            "ssh://git:@host/r.git",
        ],
    )
    def test_an_origin_with_nothing_to_strip_is_left_byte_for_byte(self, tmp_path, url):
        state = dataclasses.replace(_state(tmp_path, ()), url=url)
        recipe = nodes.recipe_for(
            ProjectConfig(path=str(tmp_path), node="second"),
            NODE,
            [state],
            home=Path.home(),
            project_dir=tmp_path,
        )
        assert recipe.repos[0].url == url
        assert recipe.warnings == ()

    @pytest.mark.parametrize(
        "url",
        [
            "git@github.com:org/repo.git",
            "ssh://git@github.com/org/repo.git",
            "git+ssh://git@[2001:db8::1]:22/org/repo.git",
            "https://github.com/org/repo.git",
            "git://github.com/org/repo.git",
            # An '@' past the authority is a path character, not a login.
            "https://github.com/org/repo@v1.git",
        ],
    )
    def test_a_login_in_an_ssh_origin_is_not_a_credential(self, tmp_path, url):
        state = dataclasses.replace(_state(tmp_path, ()), url=url)
        recipe = nodes.recipe_for(
            ProjectConfig(path=str(tmp_path), node="second"),
            NODE,
            [state],
            home=Path.home(),
            project_dir=tmp_path,
        )
        assert recipe.repos[0].url == url
        assert recipe.warnings == ()


D_NODE = Node(nick="second", host="devino-second", user="amin", root="~/magent")


def _pool_config(*projects: ProjectConfig) -> MagentConfig:
    return MagentConfig(
        projects=list(projects),
        settings=Settings(
            nodes={
                "second": NodeConfig(nick="second", host="devino-second", user="amin"),
                "third": NodeConfig(nick="third", host="devino-third", user="amin"),
            }
        ),
    )


def _git_state(
    path: Path,
    *,
    url: str = "git@github.com:me/api.git",
    branch: str = "main",
    dirty: bool = False,
    unpushed: bool = False,
    detached: bool = False,
) -> LocalGitState:
    # Not B's `_state(path, ignored)` -- that helper already exists in this file.
    return LocalGitState(
        path=path,
        url=url,
        branch=branch,
        dirty=dirty,
        unpushed=unpushed,
        detached=detached,
    )


class TestANodeProjectIsNamedLikeALocalOne:
    def test_the_title_wins_over_the_folder(self):
        proj = ProjectConfig(path="C:/src/api-service", title="API", node="second")
        assert nodes.project_name(proj) == "API"

    def test_without_a_title_the_folder_leaf_names_it(self):
        proj = ProjectConfig(path="C:/src/api-service", node="second")
        assert nodes.project_name(proj) == "api-service"

    def test_the_session_id_is_the_local_sanitizer_applied_to_that_name(self):
        proj = ProjectConfig(path="C:/src/x", title="My App.v2", node="second")
        assert nodes.node_sid(proj) == "My-App-v2"


class TestWhichProjectsRunOnANode:
    def test_only_enabled_non_ide_projects_pinned_to_a_pool_node_or_auto(self):
        keep = ProjectConfig(path="C:/a/api", node="second")
        auto = ProjectConfig(path="C:/a/web", node="auto")
        config = _pool_config(
            keep,
            ProjectConfig(path="C:/a/local"),
            ProjectConfig(path="C:/a/cloudy", node="cloud"),
            ProjectConfig(path="C:/a/off", node="second", enabled=False),
            ProjectConfig(path="C:/a/ide", node="second", tool="code"),
            auto,
        )
        assert nodes.node_projects(config) == [keep, auto]

    def test_a_group_filter_is_case_insensitive(self):
        a = ProjectConfig(path="C:/a/api", node="second", group="Work")
        b = ProjectConfig(path="C:/a/web", node="second", group="home")
        assert nodes.node_projects(_pool_config(a, b), group="work") == [a]

    def test_an_ide_default_tool_keeps_a_toolless_project_home(self):
        ide = sorted(IDE_TOOLS)[0]
        config = _pool_config(ProjectConfig(path="C:/a/api", node="second"))
        config.settings.default_tool = ide
        assert is_ide_tool(ide)
        assert nodes.node_projects(config) == []

    def test_an_agent_default_tool_sends_a_toolless_project_to_its_node(self):
        agent = next(t for t in DEFAULT_TOOLS if not is_ide_tool(t))
        proj = ProjectConfig(path="C:/a/api", node="second")
        config = _pool_config(proj)
        config.settings.default_tool = agent
        assert nodes.node_projects(config) == [proj]

    def test_a_second_entry_with_the_same_session_id_is_dropped(self):
        a = ProjectConfig(path="C:/a/api", node="second")
        dup = ProjectConfig(path="C:/b/api", node="third")
        assert nodes.node_projects(_pool_config(a, dup)) == [a]


class TestWhereTheProjectLandsOnTheNode:
    def test_the_remote_folder_is_the_root_plus_the_local_folder_name(self):
        assert nodes.remote_root_for(D_NODE, Path("C:/src/api")) == "~/magent/api"

    def test_a_trailing_slash_on_the_root_does_not_double(self):
        node = dataclasses.replace(D_NODE, root="~/magent/")
        assert nodes.remote_root_for(node, Path("C:/src/api")) == "~/magent/api"

    def test_a_drive_root_is_refused_not_placed_at_the_node_root(self, tmp_path):
        # A nameless folder would land AT the node's root, beside every other
        # project -- never a project directory of its own. The anchor is this
        # OS's own root (``C:\`` here, ``/`` on POSIX): on POSIX ``Path("C:/")``
        # is a relative folder NAMED ``C:``, not a root.
        for root in (Path("/"), Path(tmp_path.anchor)):
            assert root.name == ""
            with pytest.raises(
                NodeConfigError, match="a drive root cannot be a node project"
            ):
                nodes.remote_root_for(D_NODE, root)

    @pytest.mark.parametrize("leaf", ["..", "api.", "api ", "a\x1fb", "a\x7fb"])
    def test_a_leaf_that_is_not_a_folder_name_is_refused_and_named(self, leaf):
        # ".." would climb to the node user's HOME; Windows opens "api." and
        # "api " as "api", so the local and remote names would diverge; a
        # control character has no business in a remote path. Refused, never
        # rewritten.
        project_dir = Path("C:/src") / leaf
        assert project_dir.name == leaf
        with pytest.raises(NodeConfigError, match="cannot name a node folder") as err:
            nodes.remote_root_for(D_NODE, project_dir)
        assert repr(leaf) in str(err.value)

    def test_a_dot_leaf_is_refused(self):
        # pathlib collapses a "." part, so a "." leaf reaches here only as
        # Path(".") itself -- a folder with no name, refused like a drive root.
        assert Path(".").name == ""
        with pytest.raises(NodeConfigError):
            nodes.remote_root_for(D_NODE, Path("."))

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("~", "/home/amin"),
            ("~/magent/api", "/home/amin/magent/api"),
            ("/srv/work/api", "/srv/work/api"),
        ],
    )
    def test_a_tilde_expands_against_the_nodes_home(self, path, expected):
        assert nodes.absolute_remote(path, "/home/amin") == expected

    @pytest.mark.parametrize(
        ("path", "home", "expected"),
        [
            ("~", "/home/amin/", "/home/amin"),
            ("~/x", "/home/amin/", "/home/amin/x"),
            ("~", "/", "/"),
            ("~/x", "/", "/x"),
        ],
    )
    def test_a_trailing_slash_on_home_gives_one_spelling(self, path, home, expected):
        # "~" and "~/x" must encode to keys under the same home spelling.
        assert nodes.absolute_remote(path, home) == expected

    def test_recipe_for_carries_the_local_root_and_the_folder_name_rule(self, tmp_path):
        project_dir = tmp_path / "api-service"
        project_dir.mkdir()
        proj = ProjectConfig(path=str(project_dir), title="API", node="second")
        recipe = nodes.recipe_for(
            proj,
            D_NODE,
            [_git_state(project_dir)],
            home=tmp_path / "home",
            project_dir=project_dir,
        )
        assert recipe.local_root == project_dir.resolve()
        assert recipe.remote_root == "~/magent/api-service"
        assert recipe.sid == "API"
        # C1's other three fields are launch.node_recipe's to fill.
        assert (recipe.tool, recipe.command, recipe.fresh_command) == ("", "", None)


def _recipe_at(project: str, remote_root: str) -> Recipe:
    return Recipe(
        project=project,
        sid=psmux.session_name(project),
        repos=(),
        push_files=(),
        memory_dir=None,
        remote_root=remote_root,
    )


class TestTwoProjectsNeverShareANodeFolder:
    """Two local folders with the same leaf name (``C:/a/api``, ``C:/b/api``)
    would both land at ``<root>/api`` -- one clone overwriting the other. The
    leaf is unique across the whole fleet: ``auto`` placement may later put any
    two projects on one node."""

    def test_different_leaves_under_one_root_pass(self):
        nodes.assert_distinct_remote_roots(
            [_recipe_at("api", "~/magent/api"), _recipe_at("web", "~/magent/web")]
        )

    def test_one_leaf_under_different_roots_is_refused(self):
        recipes = [
            _recipe_at("api", "~/magent/api"),
            _recipe_at("api-b", "/srv/work/api"),
        ]
        with pytest.raises(NodeConfigError) as caught:
            nodes.assert_distinct_remote_roots(recipes)
        text = str(caught.value)
        assert "'api'" in text
        assert "'api-b'" in text
        assert "~/magent/api" in text
        assert "/srv/work/api" in text

    def test_no_recipes_pass(self):
        nodes.assert_distinct_remote_roots([])

    def test_a_shared_remote_root_names_both_projects_and_the_folder(self):
        recipes = [
            _recipe_at("API", "~/magent/api"),
            _recipe_at("web", "~/magent/web"),
            _recipe_at("api-b", "~/magent/api"),
        ]
        with pytest.raises(NodeConfigError) as caught:
            nodes.assert_distinct_remote_roots(recipes)
        text = str(caught.value)
        assert "'API'" in text
        assert "'api-b'" in text
        assert "~/magent/api" in text


class TestTheRefusalNamesTheFix:
    """D7: a tree the node could not reproduce is refused, and the text says
    exactly what to run. magent never runs it for the user."""

    def test_a_clean_pushed_tree_is_not_refused(self, tmp_path):
        assert nodes.refusal_for(_git_state(tmp_path)) is None

    def test_no_origin_is_refused_even_with_allow_dirty(self, tmp_path):
        text = nodes.refusal_for(_git_state(tmp_path, url=""), allow_dirty=True)
        assert text is not None
        assert "no 'origin' remote" in text

    def test_a_detached_head_is_refused_even_with_allow_dirty(self, tmp_path):
        text = nodes.refusal_for(
            _git_state(tmp_path, detached=True, branch=""), allow_dirty=True
        )
        assert text is not None
        assert "git switch <branch>" in text

    def test_a_dirty_tree_names_allow_dirty(self, tmp_path):
        text = nodes.refusal_for(_git_state(tmp_path, dirty=True))
        assert text is not None
        assert "dirty" in text
        assert "--allow-dirty" in text

    def test_unpushed_commits_name_the_exact_push(self, tmp_path):
        text = nodes.refusal_for(_git_state(tmp_path, unpushed=True, branch="feat/x"))
        assert text is not None
        assert "git push -u origin feat/x" in text
        assert "--allow-dirty" in text

    def test_a_whitespace_only_origin_is_no_origin(self, tmp_path):
        text = nodes.refusal_for(_git_state(tmp_path, url="  \t"), allow_dirty=True)
        assert text is not None
        assert "no 'origin' remote" in text

    def test_an_empty_branch_is_refused_as_detached_not_as_a_blank_push(self, tmp_path):
        # "git push -u origin , or pass" names no fix at all.
        state = _git_state(tmp_path, unpushed=True, branch="")
        for allow_dirty in (False, True):
            text = nodes.refusal_for(state, allow_dirty=allow_dirty)
            assert text is not None
            assert "git switch <branch>" in text
            assert "git push" not in text

    def test_allow_dirty_lets_dirty_and_unpushed_through(self, tmp_path):
        state = _git_state(tmp_path, dirty=True, unpushed=True)
        assert nodes.refusal_for(state, allow_dirty=True) is None
