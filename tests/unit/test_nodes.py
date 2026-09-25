"""nodes.py -- pure data + policy for running a project on a pool machine."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

from magent import nodes
from magent.nodes import (
    LoadSample,
    LocalGitState,
    Node,
    NodeMapEntry,
    Recipe,
    RepoSpec,
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
