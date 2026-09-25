"""nodes.py -- pure data + policy for running a project on a pool machine."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from magent import nodes
from magent.nodes import LoadSample, LocalGitState, Node, Recipe, RepoSpec
from magent.sessions.claude import encode_claude_project_path
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
        # Path.home() is the test's redirected home; this holds only because
        # tests/conftest.py registers both constants as import-bound.
        # Operand order: ruff SIM300 reads the UPPERCASE attribute as a constant
        # and would flag `nodes.NODES_DIR == ...` as a Yoda condition.
        assert Path.home() / ".magent" / "nodes" == nodes.NODES_DIR
        assert nodes.NODE_MAP_PATH == nodes.NODES_DIR / "node-map.json"

    def test_no_test_can_reach_the_real_store(self):
        assert not nodes.NODE_MAP_PATH.is_relative_to(REAL_MAGENT_DIR)


class TestEncodedProjectDir:
    def test_it_is_claude_codes_own_encoding(self):
        path = r"C:\Users\amind\AppData\Local\Temp\capture_cc"
        assert nodes.encoded_project_dir(path) == encode_claude_project_path(path)

    def test_a_node_side_path_encodes_by_the_same_rule(self):
        assert (
            nodes.encoded_project_dir("/home/amin/magent/sendly")
            == "-home-amin-magent-sendly"
        )
