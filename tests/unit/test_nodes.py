"""nodes.py -- pure data + policy for running a project on a pool machine."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

from magent import nodes
from magent.config import (
    MagentConfig,
    NodeConfig,
    ProjectConfig,
    Settings,
)
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
        # In this process tests/conftest.py has monkeypatched both constants
        # (they are import-bound), so reading them here would pin the patch.
        # A fresh child imports the PRODUCT's own binding; it inherits the
        # redirected home (no explicit env=), so Path.home() there is tmp.
        out = subprocess.run(
            [
                sys.executable,
                "-c",
                "from magent import nodes; print(nodes.NODES_DIR); print(nodes.NODE_MAP_PATH)",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        assert out == [
            str(Path.home() / ".magent" / "nodes"),
            str(Path.home() / ".magent" / "nodes" / "node-map.json"),
        ]

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

    def test_a_malformed_entry_is_dropped_alone(self, node_map):
        node_map.parent.mkdir(parents=True)
        good = dataclasses.asdict(ENTRY)
        node_map.write_text(
            json.dumps(
                {
                    "api": good,
                    "web": {"nick": "third"},
                    "db": {**good, "placed_ts": True},
                }
            ),
            encoding="utf-8",
        )
        assert nodes.read_node_map() == {"api": ENTRY}

    def test_an_unknown_field_is_ignored(self, node_map):
        # A newer magent may add a (defaulted) field; an older reader keeps working.
        node_map.parent.mkdir(parents=True)
        node_map.write_text(
            json.dumps({"api": {**dataclasses.asdict(ENTRY), "future": 1}}),
            encoding="utf-8",
        )
        assert nodes.read_node_map() == {"api": ENTRY}

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

    @pytest.mark.parametrize(
        "field", [f.name for f in dataclasses.fields(NodeMapEntry)]
    )
    def test_every_field_is_frozen(self, field):
        # setattr with a parametrized name: no literal attribute, so no
        # suppression comment is needed (DECISION-26 iv).
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(ENTRY, field, "x")


POOL = MagentConfig(
    projects=[],
    settings=Settings(
        nodes={
            "second": NodeConfig(nick="second", host="devino-second", user="amin"),
            "third": NodeConfig(nick="third", host="devino-third"),
        }
    ),
)


class TestResolve:
    def test_a_pinned_project_resolves_to_its_node(self):
        node = nodes.resolve(
            POOL, ProjectConfig(path="api", node="second"), local_user="whoever"
        )
        assert node == Node(
            nick="second", host="devino-second", user="amin", root="~/magent"
        )

    def test_no_configured_user_means_the_local_one_lowercased(self):
        node = nodes.resolve(
            POOL, ProjectConfig(path="api", node="third"), local_user="Amin"
        )
        assert node.user == "amin"

    def test_auto_resolves_to_the_placed_node(self):
        node = nodes.resolve(
            POOL,
            ProjectConfig(path="api", node="auto"),
            local_user="amin",
            placed="third",
        )
        assert node.nick == "third"

    def test_a_pinned_project_ignores_a_placement(self):
        node = nodes.resolve(
            POOL,
            ProjectConfig(path="api", node="second"),
            local_user="amin",
            placed="third",
        )
        assert node.nick == "second"

    def test_auto_without_a_placement_is_refused(self):
        with pytest.raises(NodeConfigError, match="placement"):
            nodes.resolve(
                POOL, ProjectConfig(path="api", node="auto"), local_user="amin"
            )

    def test_a_cloud_project_is_refused_clearly_not_a_key_error(self):
        # The cloud backend (DECISION-8, Task J) resolves its own projects; a
        # caller that hands one to the node resolver gets a named refusal.
        with pytest.raises(NodeConfigError, match="cloud backend"):
            nodes.resolve(
                POOL, ProjectConfig(path="api", node="cloud"), local_user="amin"
            )

    def test_a_project_without_a_node_is_refused(self):
        with pytest.raises(NodeConfigError, match="not a node project"):
            nodes.resolve(POOL, ProjectConfig(path="api"), local_user="amin")

    def test_a_nick_missing_from_the_pool_is_refused_naming_it(self):
        with pytest.raises(NodeConfigError, match=r"'fourth'.*second, third"):
            nodes.resolve(
                POOL, ProjectConfig(path="api", node="fourth"), local_user="amin"
            )

    def test_an_implicit_root_user_is_refused(self):
        with pytest.raises(NodeConfigError, match="D4"):
            nodes.resolve(
                POOL, ProjectConfig(path="api", node="third"), local_user="root"
            )

    def test_an_explicit_root_user_is_honoured(self):
        pool = MagentConfig(
            projects=[],
            settings=Settings(
                nodes={
                    "fifth": NodeConfig(nick="fifth", host="devino-fifth", user="root")
                }
            ),
        )
        node = nodes.resolve(
            pool, ProjectConfig(path="api", node="fifth"), local_user="amin"
        )
        assert node.user == "root"

    def test_no_user_anywhere_is_refused(self):
        with pytest.raises(NodeConfigError, match=r"settings\.nodes\.third\.user"):
            nodes.resolve(POOL, ProjectConfig(path="api", node="third"), local_user="")


class TestANickResolvesLikeAProject:
    # What a nick shares with a project is pinned through resolve() in
    # TestResolve; this class pins only what differs when there is no project.

    def test_an_unknown_nick_names_the_pool(self):
        with pytest.raises(
            NodeConfigError,
            match=r"^node 'fifth' is not in settings\.nodes \(known: second, third\)$",
        ):
            node_for_nick(POOL, "fifth", local_user="amin")

    @pytest.mark.parametrize("nick", ["auto", "cloud"])
    def test_a_placement_word_as_a_nick_is_just_an_unknown_nick(self, nick):
        # Config validation keeps the reserved words out of the pool, so the
        # ordinary refusal is the true one; no special case is wanted.
        with pytest.raises(
            NodeConfigError,
            match=rf"^node '{nick}' is not in settings\.nodes \(known: second, third\)$",
        ):
            node_for_nick(POOL, nick, local_user="amin")

    def test_the_label_prefixes_the_unknown_nick_error(self):
        with pytest.raises(
            NodeConfigError,
            match=r"^api: node 'fifth' is not in settings\.nodes \(known: second, third\)$",
        ):
            node_for_nick(POOL, "fifth", local_user="amin", label="api")

    def test_an_empty_label_is_no_label(self):
        with pytest.raises(NodeConfigError, match=r"^node 'fifth' "):
            node_for_nick(POOL, "fifth", local_user="amin", label="")

    @pytest.mark.parametrize(
        ("local_user", "expected"),
        [
            ("", r"^settings\.nodes\.third\.user is not set"),
            ("root", r"^settings\.nodes\.third: magent is running as root"),
        ],
    )
    def test_the_label_never_prefixes_a_settings_error(self, local_user, expected):
        # These name settings.nodes.<nick>, the thing to fix; the project that
        # led there is not part of the fix.
        with pytest.raises(NodeConfigError, match=expected):
            node_for_nick(POOL, "third", local_user=local_user, label="api")


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

    def test_a_non_finite_number_is_refused_before_anything_is_written(self, tmp_path):
        target = tmp_path / "b.json"
        with pytest.raises(ValueError, match="JSON compliant"):
            nodes.write_json_atomic(target, {"ts": float("nan")})
        assert list(tmp_path.iterdir()) == []


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
