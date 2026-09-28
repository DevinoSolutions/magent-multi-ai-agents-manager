"""The launch-side half of running a project on a pool node (PR-D): the D7
refusals, the per-node lock, the map write, the window -- with the node itself
faked at remote_mux's seam, so nothing here dials anything."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import subprocess
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from magent import (
    attach_client,
    cli,
    launch,
    lockfile,
    node_scripts,
    node_sync,
    nodes,
    psmux,
    remote_mux,
)
from magent.config import (
    SCHEMA_VERSION,
    MagentConfig,
    NodeConfig,
    ProjectConfig,
    Settings,
)
from magent.nodes import LocalGitState, NodeMapEntry
from magent.remote_mux import BringUpResult, RemoteError
from tests.conftest import FakePlatform
from tests.unit._deny_stat import deny_stat
from tests.unit._fake_ssh import gh_auth_status

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# The real body, captured before any NodeRig replaces it: the tests below put
# it back to drive the whole chain through THE fake ssh.
real_provision_node = remote_mux.provision_node
real_bring_up = remote_mux.bring_up
real_decorate = remote_mux.decorate

_TOOLS = {"claude": "claude --continue"}
# What a final pull that brought the last turn home returns: None is
# "never placed", which `down` must not read as pulled.
_PULLED = remote_mux.PullResult(files=(), since=0.0)


def _config(*projects: ProjectConfig) -> MagentConfig:
    return MagentConfig(
        projects=list(projects),
        settings=Settings(
            tools=dict(_TOOLS),
            psmux=False,
            upload_server=False,
            nodes={
                "second": NodeConfig(nick="second", host="devino-second", user="amin"),
                "third": NodeConfig(nick="third", host="devino-third", user="amin"),
            },
        ),
    )


def _state(path: Path, **kw: bool) -> LocalGitState:
    return LocalGitState(
        path=path,
        url="git@github.com:me/api.git",
        branch="main",
        dirty=kw.get("dirty", False),
        unpushed=kw.get("unpushed", False),
        detached=False,
    )


class NodeRig:
    """Fakes remote_mux's four node calls and records what reached them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.recipes: list[tuple[str, object]] = []
        self.decorated: list[tuple[str, str]] = []
        self.windows: list[tuple[str, str, str, str]] = []
        self.live = False
        self.states: dict[Path, LocalGitState] = {}
        monkeypatch.setattr(nodes, "NODE_MAP_PATH", tmp_path / "node-map.json")
        monkeypatch.setattr(remote_mux, "bring_up", self._bring_up)
        monkeypatch.setattr(remote_mux, "has_session", lambda node, sid: self.live)
        monkeypatch.setattr(
            remote_mux,
            "decorate",
            lambda node, sid, nick: self.decorated.append((sid, nick)),
        )
        monkeypatch.setattr(
            launch,
            "node_git_states",
            lambda config, proj: [
                s for p, s in self.states.items() if str(p) == proj.path
            ],
        )
        monkeypatch.setattr("magent.attach_client.spawn_attach_window", self._window)
        self.error: Exception | None = None
        self.provisioned: list[str] = []
        self.provision_timeouts: list[float] = []
        monkeypatch.setattr(remote_mux, "provision_node", self._provision)
        monkeypatch.setattr(launch, "_PROVISIONED", set())

    def _bring_up(self, node, recipe, *, allow_dirty=False, resume_id=None):
        self.recipes.append((node.nick, recipe))
        if self.error is not None:
            raise self.error
        return BringUpResult(
            sid=recipe.sid,
            attached_existing=False,
            cwd=f"/home/amin/magent/{Path(recipe.remote_root).name}",
        )

    def _window(self, target, sid, *, mux, remote=None, reconnect=True):
        # The real spawn derives the pane's command from ``mux`` when
        # ``remote`` is left out; record what the pane would really run.
        self.windows.append(
            (target, sid, mux, remote or attach_client.remote_attach_command(sid, mux))
        )
        return f"magent:{sid}"

    def _provision(self, node, config, *, home, timeout_s, force=False):
        self.provisioned.append(node.nick)
        self.provision_timeouts.append(timeout_s)
        return remote_mux.ProvisionReport(())


@pytest.fixture
def rig(monkeypatch, tmp_path):
    return NodeRig(monkeypatch, tmp_path)


@pytest.fixture
def api(tmp_path, rig):
    folder = tmp_path / "api"
    folder.mkdir()
    rig.states[folder] = _state(folder)
    return ProjectConfig(path=str(folder), node="second")


class TestACleanProjectComesUpOnItsNode:
    def test_it_is_brought_up_and_recorded_in_the_map(self, rig, api):
        outcome = launch.bring_up_node_project(_config(api), api)
        assert outcome == launch.NodeBringUpOutcome(ok=True, sid="api", node="second")
        entry = nodes.read_node_map()["api"]
        assert (entry.nick, entry.sid, entry.target, entry.cwd, entry.remote_root) == (
            "second",
            "api",
            "amin@devino-second",
            "/home/amin/magent/api",
            "~/magent/api",
        )

    def test_an_explicit_resume_id_reaches_the_node_and_none_lets_it_pick(
        self, rig, api, monkeypatch
    ):
        seen: list[str | None] = []

        def fake(node, recipe, *, allow_dirty=False, resume_id=None):
            seen.append(resume_id)
            return BringUpResult(sid=recipe.sid, attached_existing=False, cwd="/n/api")

        monkeypatch.setattr(remote_mux, "bring_up", fake)
        uuid = "8f14e45f-ceea-467a-9b36-0c4b0f8d2c11"
        launch.bring_up_node_project(_config(api), api, resume_id=uuid)
        launch.bring_up_node_project(_config(api), api)
        assert seen == [uuid, None]

    def test_the_recipe_carries_the_tool_its_command_and_the_fresh_form(
        self, rig, api, tmp_path
    ):
        launch.bring_up_node_project(_config(api), api)
        nick, recipe = rig.recipes[0]
        assert nick == "second"
        assert (recipe.tool, recipe.command, recipe.fresh_command) == (
            "claude",
            "claude --continue",
            "claude",
        )
        assert recipe.local_root == tmp_path / "api"

    def test_no_window_unless_asked(self, rig, api, monkeypatch):
        monkeypatch.setattr(
            launch, "get_platform", lambda: FakePlatform(supports_attach_windows=True)
        )
        launch.bring_up_node_project(_config(api), api)
        assert rig.windows == []

    def test_the_window_attaches_through_the_supervisor(self, rig, api, monkeypatch):
        monkeypatch.setattr(
            launch, "get_platform", lambda: FakePlatform(supports_attach_windows=True)
        )
        launch.bring_up_node_project(_config(api), api, window=True)
        assert rig.windows == [
            ("amin@devino-second", "api", "tmux", "tmux -L magent attach -t '=api'")
        ]

    def test_a_platform_without_attach_windows_opens_none(self, rig, api, monkeypatch):
        monkeypatch.setattr(launch, "get_platform", FakePlatform)
        assert launch.bring_up_node_project(_config(api), api, window=True).ok
        assert rig.windows == []

    def test_a_window_that_cannot_spawn_does_not_fail_the_bring_up(
        self, rig, api, monkeypatch
    ):
        monkeypatch.setattr(
            launch, "get_platform", lambda: FakePlatform(supports_attach_windows=True)
        )

        def no_wt(*_a: object, **_k: object) -> str:
            raise FileNotFoundError("wt")

        monkeypatch.setattr("magent.attach_client.spawn_attach_window", no_wt)
        assert launch.bring_up_node_project(_config(api), api, window=True).ok
        assert "api" in nodes.read_node_map()


class TestTheWindowTitleIsTheOneTheSpawnUsed:
    """``_open_node_window`` hands back the title C's real spawn opened the
    window under -- the value tiling (Task 12) matches on -- and None when the
    platform cannot open attach windows. The real ``spawn_attach_window`` runs;
    only its ``wt`` Popen and the supervisor lookup are faked."""

    _NODE = nodes.Node(
        nick="second", host="devino-second", user="amin", root="~/magent"
    )

    def _spawned(self, monkeypatch) -> list[list[str]]:
        argvs: list[list[str]] = []
        monkeypatch.setattr(attach_client, "client_exe", lambda: None)
        monkeypatch.setattr(
            attach_client.subprocess, "Popen", lambda a, **k: argvs.append(a)
        )
        return argvs

    def test_it_returns_the_title_the_window_opened_under(self, monkeypatch):
        argvs = self._spawned(monkeypatch)
        monkeypatch.setattr(
            launch, "get_platform", lambda: FakePlatform(supports_attach_windows=True)
        )
        title = launch._open_node_window(self._NODE, "api")
        assert title == "magent:api"
        (argv,) = argvs
        assert argv[argv.index("--title") + 1] == title

    def test_no_attach_windows_means_no_title_and_no_spawn(self, monkeypatch):
        argvs = self._spawned(monkeypatch)
        monkeypatch.setattr(launch, "get_platform", FakePlatform)
        assert launch._open_node_window(self._NODE, "api") is None
        assert argvs == []


class TestTheOutcomeCarriesTheWindowsTitle:
    """Tiling (Task 12) places node windows by the title the spawn used, so
    the outcome hands it on -- and None whenever no window opened."""

    @pytest.fixture
    def windows(self, monkeypatch):
        monkeypatch.setattr(
            launch, "get_platform", lambda: FakePlatform(supports_attach_windows=True)
        )

    def test_a_fresh_bring_up_names_the_window_it_opened(self, rig, api, windows):
        outcome = launch.bring_up_node_project(_config(api), api, window=True)
        assert outcome.title == "magent:api"

    def test_attaching_instead_names_the_window_too(self, rig, api, tmp_path, windows):
        _hold("api")
        rig.live = True
        rig.states[tmp_path / "api"] = _state(tmp_path / "api", dirty=True)
        outcome = launch.bring_up_node_project(_config(api), api, window=True)
        assert (outcome.attached_existing, outcome.title) == (True, "magent:api")

    def test_no_window_asked_is_no_title(self, rig, api, windows):
        assert launch.bring_up_node_project(_config(api), api).title is None

    def test_a_window_that_failed_to_spawn_is_no_title(
        self, rig, api, monkeypatch, windows
    ):
        def no_wt(*_a: object, **_k: object) -> str:
            raise FileNotFoundError("wt")

        monkeypatch.setattr("magent.attach_client.spawn_attach_window", no_wt)
        outcome = launch.bring_up_node_project(_config(api), api, window=True)
        assert (outcome.ok, outcome.title) == (True, None)

    def test_a_failed_bring_up_is_no_title(self, rig, api, windows):
        rig.error = RemoteError(5, "magent: clone failed", ("bring_up",))
        outcome = launch.bring_up_node_project(_config(api), api, window=True)
        assert (outcome.ok, outcome.title) == (False, None)
        assert rig.windows == []


class TestD7RefusesWhatTheNodeCouldNotReproduce:
    def test_a_dirty_tree_names_allow_dirty_and_nothing_is_dialed(
        self, rig, api, tmp_path
    ):
        rig.states[tmp_path / "api"] = _state(tmp_path / "api", dirty=True)
        outcome = launch.bring_up_node_project(_config(api), api)
        assert not outcome.ok
        assert outcome.error is not None
        assert "dirty" in outcome.error
        assert "--allow-dirty" in outcome.error
        assert rig.recipes == []
        assert nodes.read_node_map() == {}

    def test_unpushed_commits_name_the_push(self, rig, api, tmp_path):
        rig.states[tmp_path / "api"] = _state(tmp_path / "api", unpushed=True)
        outcome = launch.bring_up_node_project(_config(api), api)
        assert outcome.error is not None
        assert "git push -u origin main" in outcome.error

    def test_allow_dirty_goes_through(self, rig, api, tmp_path):
        rig.states[tmp_path / "api"] = _state(
            tmp_path / "api", dirty=True, unpushed=True
        )
        assert launch.bring_up_node_project(_config(api), api, allow_dirty=True).ok

    def test_provisioning_never_runs_for_a_refused_project(
        self, rig, api, tmp_path, monkeypatch
    ):
        seen: list[str] = []
        monkeypatch.setattr(
            launch, "_provision_once", lambda node, config: seen.append(node.nick)
        )
        rig.states[tmp_path / "api"] = _state(tmp_path / "api", dirty=True)
        launch.bring_up_node_project(_config(api), api)
        assert seen == []
        rig.states[tmp_path / "api"] = _state(tmp_path / "api")
        launch.bring_up_node_project(_config(api), api)
        assert seen == ["second"]

    def test_a_refused_tree_with_its_session_alive_attaches_instead(
        self, rig, api, tmp_path, caplog
    ):
        from magent.log import get_logger

        # The running session is not affected by what is uncommitted HERE.
        nodes.update_node_map(
            "api",
            NodeMapEntry(
                nick="second",
                sid="api",
                placed_ts=1.0,
                attached_existing=False,
                remote_root="~/magent/api",
                target="amin@devino-second",
            ),
        )
        rig.live = True
        rig.states[tmp_path / "api"] = _state(tmp_path / "api", dirty=True)
        get_logger("nodes")  # sets the level; caplog must come after
        caplog.set_level("WARNING", logger="magent.nodes")
        outcome = launch.bring_up_node_project(_config(api), api)
        assert (outcome.ok, outcome.attached_existing) == (True, True)
        # The refusal alone: a readable map says nothing about the map, on
        # screen or in nodes.log.
        (dirty,) = [w for w in outcome.warnings if "--allow-dirty" in w]
        assert outcome.warnings == (dirty,)
        assert not [
            r.getMessage()
            for r in caplog.records
            if r.name == "magent.nodes" and "looked up on its pin" in r.getMessage()
        ]
        assert rig.decorated == [("api", "second")]
        assert rig.recipes == []

    def test_a_project_with_no_repo_is_refused(self, rig, api, tmp_path):
        del rig.states[tmp_path / "api"]
        outcome = launch.bring_up_node_project(_config(api), api)
        assert outcome.error is not None
        assert "no git repository" in outcome.error

    def test_a_folder_missing_on_this_pc_is_refused(self, rig, tmp_path):
        gone = ProjectConfig(path=str(tmp_path / "gone"), node="second")
        outcome = launch.bring_up_node_project(_config(gone), gone)
        assert outcome.error is not None
        assert "not found on this PC" in outcome.error

    def test_an_unknown_tool_is_refused(self, rig, tmp_path):
        folder = tmp_path / "x"
        folder.mkdir()
        rig.states[folder] = _state(folder)
        proj = ProjectConfig(path=str(folder), node="second", tool="aider")
        outcome = launch.bring_up_node_project(_config(proj), proj)
        assert (
            outcome.error
            == f"{folder}: unknown tool 'aider' (add under settings.tools)"
        )


class TestAFailureIsAnOutcomeNeverACrash:
    def test_a_node_side_refusal_reads_as_its_own_message(self, rig, api):
        rig.error = RemoteError(
            3,
            "magent: ~/magent/api has uncommitted changes on the node; ... or pass --allow-dirty",
            ("bring_up",),
        )
        outcome = launch.bring_up_node_project(_config(api), api)
        assert outcome.error is not None
        assert outcome.error.startswith("~/magent/api has uncommitted changes")
        assert nodes.read_node_map() == {}

    def test_an_unreachable_node_is_an_outcome(self, rig, api):
        rig.error = RemoteError(
            None, "ssh: connect to host devino-second: timed out", ("ssh",)
        )
        outcome = launch.bring_up_node_project(_config(api), api)
        assert (outcome.ok, outcome.node) == (False, "second")
        assert "timed out" in (outcome.error or "")

    def test_an_auto_project_with_no_placement_fails_with_the_resolve_text(
        self, rig, tmp_path
    ):
        # G-C12: `up` does not place; until PR-G's placer lands this is the answer.
        folder = tmp_path / "web"
        folder.mkdir()
        proj = ProjectConfig(path=str(folder), node="auto")
        outcome = launch.bring_up_node_project(_config(proj), proj)
        assert "needs a placement" in (outcome.error or "")

    def test_an_auto_project_goes_where_the_map_placed_it(self, rig, tmp_path):
        folder = tmp_path / "web"
        folder.mkdir()
        rig.states[folder] = _state(folder)
        nodes.update_node_map(
            "web",
            NodeMapEntry(
                nick="third",
                sid="web",
                placed_ts=1.0,
                attached_existing=False,
                remote_root="~/magent/web",
            ),
        )
        proj = ProjectConfig(path=str(folder), node="auto")
        assert launch.bring_up_node_project(_config(proj), proj).node == "third"
        assert rig.recipes[0][0] == "third"


class TestASessionThatCameUpButWasNotRecordedIsUp:
    """The node said yes, then the map write failed (another writer held the
    lock past its wait, or the map was unreadable): the session IS running
    there, so the outcome says so -- ok, with a warning naming what was lost
    and how to repair it -- never a failure that invites a second bring-up."""

    @pytest.mark.parametrize(
        "exc",
        [
            lockfile.LockHeld("node-map is held"),
            ValueError("node-map.json: not valid JSON"),
        ],
    )
    def test_it_is_ok_with_a_repair_warning(self, rig, api, monkeypatch, exc):
        def no_map(*_a: object, **_k: object) -> dict[str, NodeMapEntry]:
            raise exc

        monkeypatch.setattr(nodes, "update_node_map", no_map)
        outcome = launch.bring_up_node_project(_config(api), api)
        assert outcome.ok is True
        assert outcome.error is None
        (warning,) = [w for w in outcome.warnings if "not recorded" in w]
        assert warning.startswith("up on @second but not recorded")
        assert "re-run magent up" in warning
        # Unrecorded, a re-run has no held session to attach to, so a dirty
        # tree would be refused: the repair says how not to be.
        assert "clean tree or --allow-dirty" in warning
        assert warning.isascii()

    @pytest.mark.parametrize(
        ("exc", "cls"),
        [
            (
                ValueError(r"C:\Users\me\.magent\nodes\node-map.json: Expecting value"),
                "ValueError",
            ),
            (
                PermissionError(13, "Access is denied", r"C:\x\node-map.json"),
                "PermissionError",
            ),
            (lockfile.LockHeld("node-map is held"), "LockHeld"),
        ],
        ids=["torn", "busy", "held"],
    )
    def test_the_screen_gets_the_class_and_nodes_log_the_error(
        self, rig, api, monkeypatch, caplog, exc, cls
    ):
        from magent.log import get_logger

        def no_map(*_a: object, **_k: object) -> dict[str, NodeMapEntry]:
            raise exc

        monkeypatch.setattr(nodes, "update_node_map", no_map)
        get_logger("nodes")  # sets the level; caplog must come after
        caplog.set_level("WARNING", logger="magent.nodes")
        outcome = launch.bring_up_node_project(_config(api), api)
        (warning,) = [w for w in outcome.warnings if "not recorded" in w]
        assert warning == (
            f"up on @second but not recorded ({cls}); re-run magent up from a"
            " clean tree or --allow-dirty"
        )
        logged = [r.getMessage() for r in caplog.records if r.name == "magent.nodes"]
        assert f"node second: api up but not recorded in the node map: {exc}" in logged

    def test_the_window_still_opens(self, rig, api, monkeypatch):
        def no_map(*_a: object, **_k: object) -> dict[str, NodeMapEntry]:
            raise lockfile.LockHeld("node-map is held")

        monkeypatch.setattr(nodes, "update_node_map", no_map)
        monkeypatch.setattr(
            launch, "get_platform", lambda: FakePlatform(supports_attach_windows=True)
        )
        assert launch.bring_up_node_project(_config(api), api, window=True).ok
        assert [w[1] for w in rig.windows] == ["api"]


def _projects(
    tmp_path: Path, rig: NodeRig, spec: list[tuple[str, str]]
) -> list[ProjectConfig]:
    out = []
    for name, nick in spec:
        folder = tmp_path / name
        folder.mkdir()
        rig.states[folder] = _state(folder)
        out.append(ProjectConfig(path=str(folder), node=nick))
    return out


class TestManyNodeProjectsAtOnce:
    def test_outcomes_come_back_in_config_order(self, rig, tmp_path):
        projs = _projects(
            tmp_path, rig, [("a1", "second"), ("b1", "third"), ("a2", "second")]
        )
        outcomes = launch.bring_up_node_projects(_config(*projs))
        assert [o.sid for o in outcomes] == ["a1", "b1", "a2"]

    def test_only_is_a_list_of_session_ids(self, rig, tmp_path):
        projs = _projects(tmp_path, rig, [("a1", "second"), ("b1", "third")])
        outcomes = launch.bring_up_node_projects(
            _config(*projs), only=["b1", "local-api"]
        )
        assert [o.sid for o in outcomes] == ["b1"]

    def test_nothing_to_do_is_an_empty_list(self, rig):
        assert launch.bring_up_node_projects(_config()) == []

    def test_one_node_is_serial_and_two_nodes_are_parallel(
        self, rig, tmp_path, monkeypatch
    ):
        projs = _projects(
            tmp_path, rig, [("a1", "second"), ("a2", "second"), ("b1", "third")]
        )
        active: dict[str, int] = defaultdict(int)
        peak: dict[str, int] = defaultdict(int)
        guard = threading.Lock()
        # a1 (second) and b1 (third) must be inside bring_up AT THE SAME TIME:
        # a global lock would time the barrier out.
        barrier = threading.Barrier(2, timeout=10)

        def fake(node, recipe, *, allow_dirty=False, resume_id=None):
            with guard:
                active[node.nick] += 1
                peak[node.nick] = max(peak[node.nick], active[node.nick])
            try:
                if recipe.sid in ("a1", "b1"):
                    barrier.wait()
                time.sleep(0.05)
            finally:
                with guard:
                    active[node.nick] -= 1
            return BringUpResult(
                sid=recipe.sid, attached_existing=False, cwd=f"/n/{recipe.sid}"
            )

        monkeypatch.setattr(remote_mux, "bring_up", fake)
        outcomes = launch.bring_up_node_projects(_config(*projs))
        assert all(o.ok for o in outcomes)
        assert peak == {"second": 1, "third": 1}
        # ...and all three map entries survived the concurrent writes.
        assert set(nodes.read_node_map()) == {"a1", "a2", "b1"}

    def test_window_reaches_every_bring_up_in_the_batch(
        self, rig, tmp_path, monkeypatch
    ):
        # `up` never asks for windows; `--go` (Task 12) does, through here.
        projs = _projects(tmp_path, rig, [("a1", "second"), ("b1", "third")])
        monkeypatch.setattr(
            launch, "get_platform", lambda: FakePlatform(supports_attach_windows=True)
        )
        outcomes = launch.bring_up_node_projects(_config(*projs), window=True)
        assert [(o.sid, o.title) for o in outcomes] == [
            ("a1", "magent:a1"),
            ("b1", "magent:b1"),
        ]
        assert sorted(sid for _, sid, _, _ in rig.windows) == ["a1", "b1"]

    def test_a_fanned_out_batch_leaves_the_nodes_log_one_handler(self, rig, tmp_path):
        # The workers make the batch's first get_logger("nodes") calls, all at
        # once (conftest's log.reset_logging() hands every test an unconfigured
        # logger); a stacked handler per worker would write every line 8 times.
        logger = logging.getLogger("magent.nodes")
        assert logger.handlers == []
        spec = [(f"p{i}", ("second", "third")[i % 2]) for i in range(8)]
        outcomes = _batch(_config(*_projects(tmp_path, rig, spec)))
        assert all(o.ok for o in outcomes)
        assert len(logger.handlers) == 1


def _no_contact_for(monkeypatch: pytest.MonkeyPatch, rig: NodeRig, *sids: str) -> None:
    """Fail loudly on anything a bring-up does for ``sids`` past the fleet
    check -- the ssh calls, the local git read -- while every other project
    still reaches the rig's fakes."""
    refused = set(sids)

    def guard(sid: str) -> None:
        if sid in refused:
            raise AssertionError(f"{sid} must be refused before any bring-up")

    real_git = launch.node_git_states

    def git_states(config: MagentConfig, proj: ProjectConfig) -> list[LocalGitState]:
        guard(nodes.node_sid(proj))
        return real_git(config, proj)

    def bring_up(node, recipe, *, allow_dirty=False, resume_id=None):
        guard(recipe.sid)
        return rig._bring_up(node, recipe, allow_dirty=allow_dirty, resume_id=resume_id)

    def has_session(node: nodes.Node, sid: str) -> bool:
        guard(sid)
        return rig.live

    def decorate(node: nodes.Node, sid: str, nick: str) -> None:
        guard(sid)
        rig.decorated.append((sid, nick))

    monkeypatch.setattr(launch, "node_git_states", git_states)
    monkeypatch.setattr(remote_mux, "bring_up", bring_up)
    monkeypatch.setattr(remote_mux, "has_session", has_session)
    monkeypatch.setattr(remote_mux, "decorate", decorate)


def _twin_apis(tmp_path: Path, rig: NodeRig) -> list[ProjectConfig]:
    """Two projects whose LOCAL folders share the leaf name ``api`` -- on two
    different nodes, because the rule is fleet-wide (auto may co-locate
    them later) -- plus a bystander with a folder of its own."""
    out = []
    for parent, title, nick in (("x", "api-x", "second"), ("y", "api-y", "third")):
        folder = tmp_path / parent / "api"
        folder.mkdir(parents=True)
        rig.states[folder] = _state(folder)
        out.append(ProjectConfig(path=str(folder), node=nick, title=title))
    (bystander,) = _projects(tmp_path, rig, [("web", "second")])
    return [*out, bystander]


def _record(name: str, nick: str, remote_root: str) -> None:
    """``name`` recorded in the node map as running on ``nick`` in
    ``remote_root`` -- the holder of that folder, as far as the map knows."""
    nodes.update_node_map(
        name,
        NodeMapEntry(
            nick=nick,
            sid=name,
            placed_ts=1.0,
            attached_existing=False,
            remote_root=remote_root,
            target=f"amin@devino-{nick}",
        ),
    )


def _looked_up_on_its_pin(caplog: pytest.LogCaptureFixture) -> Callable[[], int]:
    """With the map already unreadable: a counter of the magent.nodes records
    carrying the WHOLE error a reader meets (its words, not only its class)
    for the pinned project api looked up on second."""
    from magent.log import get_logger

    with pytest.raises((OSError, ValueError)) as err:
        nodes.load_node_map_strict()
    line = f"node second: api looked up on its pin, node map unreadable: {err.value}"
    get_logger("nodes")  # sets the level; caplog must come after
    caplog.set_level("WARNING", logger="magent.nodes")
    return lambda: sum(
        r.name == "magent.nodes" and r.getMessage() == line for r in caplog.records
    )


def _attached_when_live(monkeypatch: pytest.MonkeyPatch, rig: NodeRig) -> None:
    """The node's own answer for a session already running: bring_up.sh
    attaches to it instead of starting one (``attached_existing``)."""
    real = remote_mux.bring_up

    def bring_up(node, recipe, **kw):
        return dataclasses.replace(real(node, recipe, **kw), attached_existing=rig.live)

    monkeypatch.setattr(remote_mux, "bring_up", bring_up)


def _batch(config: MagentConfig, **kw: list[str]) -> list[launch.NodeBringUpOutcome]:
    """``bring_up_node_projects`` with its contract as an assertion: a batch
    never raises -- a project the check cannot place is an outcome, not a
    crash that takes its siblings down with it."""
    try:
        return launch.bring_up_node_projects(config, **kw)
    except Exception as exc:
        raise AssertionError(f"a node batch must never raise: {exc!r}") from exc


class TestTwoProjectsThatWouldShareANodeFolderAreRefusedFirst:
    """X3: before the fan-out, the batch is checked against the WHOLE fleet's
    node folders (``nodes.remote_root_collisions``) -- one clone would
    otherwise overwrite the other's folder, now or on a later ``up``. Only a
    batch project in a colliding group is refused; the rest come up."""

    def test_the_colliding_members_are_refused_and_the_rest_come_up(
        self, rig, tmp_path, monkeypatch
    ):
        projs = _twin_apis(tmp_path, rig)
        _no_contact_for(monkeypatch, rig, "api-x", "api-y")
        outcomes = _batch(_config(*projs))
        assert [(o.sid, o.ok) for o in outcomes] == [
            ("api-x", False),
            ("api-y", False),
            ("web", True),
        ]
        for o in outcomes[:2]:
            assert o.error is not None
            assert "'api-x' and 'api-y' would share the node folder name 'api'" in (
                o.error
            )
            assert "rename one of them" in o.error
        assert [o.node for o in outcomes] == ["second", "third", "second"]
        assert [recipe.sid for _, recipe in rig.recipes] == ["web"]
        assert set(nodes.read_node_map()) == {"web"}

    def test_a_refusal_keeps_its_place_in_the_batch(self, rig, tmp_path, monkeypatch):
        # Refused before the fan-out, but reported where the batch put them.
        x_api, y_api, web = _twin_apis(tmp_path, rig)
        _no_contact_for(monkeypatch, rig, "api-x", "api-y")
        outcomes = _batch(_config(web, x_api, y_api))
        assert [(o.sid, o.ok) for o in outcomes] == [
            ("web", True),
            ("api-x", False),
            ("api-y", False),
        ]

    def test_an_auto_project_the_map_placed_collides_like_a_pinned_one(
        self, rig, tmp_path
    ):
        # The map places api-y on third, under a folder it no longer uses --
        # a record of the very folder would make api-y its holder instead.
        x_api, y_api, _web = _twin_apis(tmp_path, rig)
        y_api.node = "auto"
        _record("api-y", "third", "~/magent/old-api")
        outcomes = _batch(_config(x_api, y_api))
        assert [(o.sid, o.ok, o.node) for o in outcomes] == [
            ("api-x", False, "second"),
            ("api-y", False, "third"),
        ]
        assert rig.recipes == []

    def test_a_title_that_is_not_a_session_id_is_still_refused(self, rig, tmp_path):
        # The refusal is keyed by session id, which a title only becomes once
        # sanitized: "API X" runs as API-X.
        x_api, y_api, _web = _twin_apis(tmp_path, rig)
        x_api.title, y_api.title = "API X", "API Y"
        outcomes = _batch(_config(x_api, y_api))
        assert [(o.sid, o.ok) for o in outcomes] == [("API-X", False), ("API-Y", False)]
        assert rig.recipes == []

    @pytest.mark.parametrize(
        "make",
        [
            lambda tmp: ProjectConfig(path=str(tmp / "gone"), node="second"),
            lambda tmp: ProjectConfig(path=str(tmp / "web2"), node="auto"),
        ],
        ids=["missing-folder", "unplaced-auto"],
    )
    def test_a_project_the_scan_skips_does_not_hide_a_later_pair(
        self, rig, tmp_path, make
    ):
        (tmp_path / "web2").mkdir()
        x_api, y_api, _web = _twin_apis(tmp_path, rig)
        outcomes = _batch(_config(make(tmp_path), x_api, y_api))
        assert [o.ok for o in outcomes] == [False, False, False]
        assert ["would share" in (o.error or "") for o in outcomes] == [
            False,
            True,
            True,
        ]
        assert rig.recipes == []

    def test_a_folder_the_scan_cannot_read_is_that_projects_outcome(
        self, rig, tmp_path, monkeypatch, caplog
    ):
        # One node project's folder this user may not stat (another profile, a
        # deny ACL) must not take `up` of any other project down with it --
        # the fleet scan reads every folder, asked for or not.
        from magent.log import get_logger

        get_logger("nodes")  # sets the level; caplog must come after
        caplog.set_level("WARNING", logger="magent.nodes")
        (good,) = _projects(tmp_path, rig, [("a1", "second")])
        locked = tmp_path / "locked" / "z"
        locked.mkdir(parents=True)
        rig.states[locked] = _state(locked)
        z = ProjectConfig(path=str(locked), node="second")
        deny_stat(monkeypatch, locked)
        alone = _batch(_config(good, z), only=["a1"])
        assert [(o.sid, o.ok) for o in alone] == [("a1", True)]
        both = _batch(_config(good, z))
        assert [(o.sid, o.ok) for o in both] == [("a1", True), ("z", False)]
        # Named as unreadable -- not "not found on this PC", which is what
        # Python 3.14's Path.is_dir alone would have said -- by its class: the
        # OS's words carry the folder's path, and those are nodes.log's.
        assert both[1].error == "local error: PermissionError; see nodes.log"
        assert "Permission denied" not in both[1].error
        assert str(locked) not in both[1].error
        # str(OSError) reprs its filename.
        assert any(
            "Permission denied" in r.getMessage()
            and repr(str(locked)) in r.getMessage()
            for r in caplog.records
            if r.name == "magent.nodes" and r.levelno == logging.WARNING
        )

    def test_a_collision_with_a_project_outside_the_batch_still_refuses(
        self, rig, tmp_path, monkeypatch
    ):
        # `magent up api-x` today and `magent up api-y` tomorrow would each be
        # a batch of one: only the fleet sees that they share a folder.
        projs = _twin_apis(tmp_path, rig)
        _no_contact_for(monkeypatch, rig, "api-x", "api-y")
        outcomes = _batch(_config(*projs), only=["api-x"])
        assert [(o.sid, o.ok) for o in outcomes] == [("api-x", False)]
        assert "'api-y'" in (outcomes[0].error or "")
        assert rig.recipes == []
        assert nodes.read_node_map() == {}

    def test_a_live_holder_keeps_attaching_and_only_the_newcomer_is_refused(
        self, rig, tmp_path, monkeypatch
    ):
        # api-x already runs in ~/magent/api; the user then adds api-y, whose
        # folder has the same name. Only api-y's clone could overwrite
        # anything, so only api-y is refused -- api-x attaches, and is told
        # of the clash as a warning.
        x_api, y_api, _web = _twin_apis(tmp_path, rig)
        _record("api-x", "second", "~/magent/api")
        rig.live = True
        _no_contact_for(monkeypatch, rig, "api-y")
        _attached_when_live(monkeypatch, rig)
        outcomes = _batch(_config(x_api, y_api))
        holder, newcomer = outcomes
        assert (holder.sid, holder.ok, holder.attached_existing) == (
            "api-x",
            True,
            True,
        )
        assert holder.error is None
        assert len(holder.warnings) == 1
        assert "'api-x' and 'api-y' would share" in holder.warnings[0]
        assert (newcomer.sid, newcomer.ok) == ("api-y", False)
        assert "'api-x'" in (newcomer.error or "")
        assert [(n, r.sid, r.remote_root) for n, r in rig.recipes] == [
            ("second", "api-x", "~/magent/api")
        ]

    def test_a_live_holder_brought_up_alone_still_attaches(
        self, rig, tmp_path, monkeypatch
    ):
        projs = _twin_apis(tmp_path, rig)
        _record("api-x", "second", "~/magent/api")
        _attached_when_live(monkeypatch, rig)
        rig.live = True
        (o,) = _batch(_config(*projs), only=["api-x"])
        assert (o.sid, o.ok, o.attached_existing) == ("api-x", True, True)
        assert len(o.warnings) == 1
        assert "'api-y'" in o.warnings[0]

    def test_a_holder_whose_session_is_gone_is_restarted_in_its_own_folder(
        self, rig, tmp_path, monkeypatch
    ):
        # The folder is the holder's recorded placement and the newcomer is
        # refused, so a restart there overwrites no one; it is still told.
        x_api, y_api, _web = _twin_apis(tmp_path, rig)
        _record("api-x", "second", "~/magent/api")
        _no_contact_for(monkeypatch, rig, "api-y")
        holder, newcomer = _batch(_config(x_api, y_api))
        assert (holder.sid, holder.ok, holder.attached_existing) == (
            "api-x",
            True,
            False,
        )
        assert holder.error is None
        assert len(holder.warnings) == 1
        assert "'api-x' and 'api-y' would share" in holder.warnings[0]
        assert (newcomer.sid, newcomer.ok) == ("api-y", False)
        assert "'api-x'" in (newcomer.error or "")
        assert [(n, r.sid, r.remote_root) for n, r in rig.recipes] == [
            ("second", "api-x", "~/magent/api")
        ]
        assert set(nodes.read_node_map()) == {"api-x"}

    def test_a_record_on_another_node_does_not_make_a_holder(
        self, rig, tmp_path, monkeypatch
    ):
        # api-x ran in ~/magent/api on second, then was re-pinned to third,
        # where api-y holds ~/magent/api. On third api-x is a newcomer: taking
        # it for a holder would dial both into one folder.
        x_api, y_api, _web = _twin_apis(tmp_path, rig)
        x_api.node = "third"
        _record("api-x", "second", "~/magent/api")
        _record("api-y", "third", "~/magent/api")
        _no_contact_for(monkeypatch, rig, "api-x")
        newcomer, holder = _batch(_config(x_api, y_api))
        assert (newcomer.sid, newcomer.ok, newcomer.node) == ("api-x", False, "third")
        assert "'api-y'" in (newcomer.error or "")
        assert (holder.sid, holder.ok) == ("api-y", True)
        assert [(n, r.sid, r.remote_root) for n, r in rig.recipes] == [
            ("third", "api-y", "~/magent/api")
        ]

    def test_a_record_of_another_folder_does_not_make_a_holder(
        self, rig, tmp_path, monkeypatch
    ):
        # api-x ran under another folder name once; that session holds
        # nothing of ~/magent/api, so it is a newcomer to it like api-y.
        projs = _twin_apis(tmp_path, rig)
        _record("api-x", "second", "~/magent/old-api")
        rig.live = True
        _no_contact_for(monkeypatch, rig, "api-x", "api-y")
        outcomes = _batch(_config(*projs), only=["api-x", "api-y"])
        assert [(o.sid, o.ok) for o in outcomes] == [("api-x", False), ("api-y", False)]
        assert rig.decorated == []

    def test_a_collision_entirely_outside_the_batch_blocks_nothing(self, rig, tmp_path):
        projs = _twin_apis(tmp_path, rig)
        outcomes = _batch(_config(*projs), only=["web"])
        assert [(o.sid, o.ok, o.error) for o in outcomes] == [("web", True, None)]

    def test_a_batch_project_the_fleet_does_not_list_is_still_checked(
        self, rig, tmp_path, monkeypatch
    ):
        # The fleet is the enabled node projects; a caller handing over one it
        # does not list (here a disabled one) must not slip past the check.
        x_api, y_api, _web = _twin_apis(tmp_path, rig)
        x_api.enabled = False
        _no_contact_for(monkeypatch, rig, "api-x")
        outcomes = launch._run_node_bring_ups(
            _config(x_api, y_api), [x_api], allow_dirty=False, window=False
        )
        assert [(o.sid, o.ok) for o in outcomes] == [("api-x", False)]
        assert "'api-y'" in (outcomes[0].error or "")
        assert rig.recipes == []

    def test_the_rule_is_asked_once_over_the_whole_fleet(
        self, rig, tmp_path, monkeypatch
    ):
        projs = _projects(
            tmp_path, rig, [("a1", "second"), ("b1", "third"), ("a2", "second")]
        )
        calls: list[list[str]] = []
        real = nodes.remote_root_collisions

        def spy(recipes):
            calls.append([r.remote_root for r in recipes])
            return real(recipes)

        monkeypatch.setattr(nodes, "remote_root_collisions", spy)
        outcomes = _batch(_config(*projs), only=["b1"])
        assert [o.sid for o in outcomes] == ["b1"]
        assert calls == [["~/magent/a1", "~/magent/b1", "~/magent/a2"]]

    def test_up_prints_the_collision_and_counts_only_the_pair_failed(
        self, rig, tmp_path, monkeypatch, capsys
    ):
        projs = _twin_apis(tmp_path, rig)
        _no_contact_for(monkeypatch, rig, "api-x", "api-y")
        monkeypatch.setattr("magent.psmux.bring_up", lambda cfg, only, group: ([], []))
        assert launch.bring_up_psmux(_config(*projs)) == (["web"], ["api-x", "api-y"])
        out = capsys.readouterr().out
        assert "api-x: projects 'api-x' and 'api-y' would share" in out
        assert "web @second started" in out

    @pytest.mark.parametrize(
        ("make", "reason"),
        [
            (
                lambda tmp: ProjectConfig(path=str(tmp / "gone"), node="second"),
                "not found on this PC",
            ),
            (
                lambda tmp: ProjectConfig(path=str(tmp / "web2"), node="auto"),
                "needs a placement",
            ),
            (
                lambda tmp: ProjectConfig(path=tmp.anchor, node="second", title="rt"),
                "no git repository",
            ),
        ],
        ids=["missing-folder", "unplaced-auto", "drive-root"],
    )
    def test_a_project_the_check_cannot_place_fails_on_its_own(
        self, rig, tmp_path, make, reason
    ):
        # No folder name to compare is not a collision: that project's own
        # bring-up names its reason, and the rest of the batch goes ahead.
        (tmp_path / "web2").mkdir()
        (good,) = _projects(tmp_path, rig, [("a1", "second")])
        odd = make(tmp_path)
        outcomes = _batch(_config(odd, good))
        assert [o.ok for o in outcomes] == [False, True]
        assert reason in (outcomes[0].error or "")
        assert [nick for nick, _ in rig.recipes] == ["second"]


# A pinned project's refusal when its only folder rivals are auto projects
# an unreadable map hides.
_FOLDER_UNKNOWN = (
    "the node map could not be read ({cls}), so whether 'api' on {nick} is"
    " already in use is unknown; not brought up"
)


class TestAnUnreadableMapPlacesNothingByGuess:
    """A torn or busy node map is UNKNOWN, never "nothing is placed". Read as
    ``{}``, an ``auto`` project running on a node looks unplaced: the fleet
    check leaves it out, and a newcomer sharing its folder name is dialed
    into that very folder. Every placement decision reads the map strictly;
    unreadable, an ``auto`` project is refused naming the map, a pinned one
    keeps its pin, and the rest of the batch comes up."""

    @pytest.fixture(params=["torn", "busy"])
    def unreadable_map(self, request, rig, monkeypatch):
        """Make the map unreadable -- AFTER the test recorded what it holds --
        and return the error class a reader then meets."""

        def make() -> str:
            if request.param == "torn":
                nodes.NODE_MAP_PATH.write_text("{ torn", encoding="utf-8")
                return "ValueError"

            def busy() -> dict[str, NodeMapEntry]:
                raise PermissionError(13, "The process cannot access the file")

            monkeypatch.setattr(nodes, "load_node_map_strict", busy)
            return "PermissionError"

        return make

    def test_a_newcomer_is_not_dialed_into_the_folder_an_auto_project_may_hold(
        self, rig, tmp_path, unreadable_map
    ):
        # api-y (auto) runs on second in ~/magent/api. The map that says so is
        # unreadable, so where api-y runs is unknown -- which must never read
        # as "nowhere": api-x shares its folder name and stays refused.
        x_api, y_api, web = _twin_apis(tmp_path, rig)
        y_api.node = "auto"
        _record("api-y", "second", "~/magent/api")
        rig.live = True
        cls = unreadable_map()
        outcomes = _batch(_config(x_api, y_api, web), only=["api-x", "web"])
        assert [(o.sid, o.ok) for o in outcomes] == [("api-x", False), ("web", True)]
        # Its only rival is hidden by the map, so the map is the reason.
        assert outcomes[0].error == _FOLDER_UNKNOWN.format(cls=cls, nick="second")
        assert [(n, r.sid) for n, r in rig.recipes] == [("second", "web")]

    def test_an_auto_project_is_refused_naming_the_map_and_nothing_is_dialed(
        self, rig, tmp_path, unreadable_map
    ):
        (web,) = _projects(tmp_path, rig, [("web", "auto")])
        _record("web", "third", "~/magent/web")
        rig.live = True
        cls = unreadable_map()
        outcome = launch.bring_up_node_project(_config(web), web)
        assert (outcome.ok, outcome.sid, outcome.node) == (False, "web", "")
        error = outcome.error or ""
        assert "node map could not be read" in error
        assert f"({cls})" in error
        # The error CLASS only: never the OS's or the parser's words.
        assert "torn" not in error
        assert "cannot access" not in error
        assert "\n" not in error
        assert rig.recipes == []
        assert rig.decorated == []

    def test_in_a_batch_the_auto_project_names_the_map_and_its_twin_the_clash(
        self, rig, tmp_path, unreadable_map
    ):
        x_api, y_api, web = _twin_apis(tmp_path, rig)
        y_api.node = "auto"
        _record("api-y", "second", "~/magent/api")
        cls = unreadable_map()
        outcomes = _batch(_config(x_api, y_api, web))
        assert [(o.sid, o.ok) for o in outcomes] == [
            ("api-x", False),
            ("api-y", False),
            ("web", True),
        ]
        assert outcomes[0].error == _FOLDER_UNKNOWN.format(cls=cls, nick="second")
        assert "node map could not be read" in (outcomes[1].error or "")
        assert f"({cls})" in (outcomes[1].error or "")
        assert [(n, r.sid) for n, r in rig.recipes] == [("second", "web")]

    def test_a_holder_refused_only_for_an_unknown_auto_twin_names_the_map(
        self, rig, tmp_path, unreadable_map
    ):
        # api-x holds second:~/magent/api and is live; api-y (auto) holds
        # third:~/magent/api. Without the map api-x cannot prove it is the
        # holder, so it stays refused -- but the reason is the map, never
        # "rename one of them" for a fleet that is fine once it reads again.
        x_api, y_api, web = _twin_apis(tmp_path, rig)
        y_api.node = "auto"
        _record("api-x", "second", "~/magent/api")
        _record("api-y", "third", "~/magent/api")
        rig.live = True
        cls = unreadable_map()
        outcomes = _batch(_config(x_api, y_api, web), only=["api-x", "web"])
        assert [(o.sid, o.ok) for o in outcomes] == [("api-x", False), ("web", True)]
        assert outcomes[0].error == _FOLDER_UNKNOWN.format(cls=cls, nick="second")
        assert [(n, r.sid) for n, r in rig.recipes] == [("second", "web")]

    def test_a_clash_with_a_known_project_keeps_the_rename_text(
        self, rig, tmp_path, unreadable_map
    ):
        # api-z (pinned, known) shares the leaf too: that clash is real
        # whatever the map says, so its text is the X3 one.
        x_api, y_api, _web = _twin_apis(tmp_path, rig)
        y_api.node = "auto"
        z_dir = tmp_path / "z" / "api"
        z_dir.mkdir(parents=True)
        rig.states[z_dir] = _state(z_dir)
        z_api = ProjectConfig(path=str(z_dir), node="third", title="api-z")
        unreadable_map()
        (outcome,) = _batch(_config(x_api, y_api, z_api), only=["api-x"])
        assert (outcome.sid, outcome.ok) == ("api-x", False)
        error = outcome.error or ""
        assert "'api-x', 'api-y' and 'api-z' would share" in error
        assert "rename one of them" in error
        assert "node map could not be read" not in error
        assert rig.recipes == []

    def test_two_hidden_auto_rivals_still_name_the_map(
        self, rig, tmp_path, unreadable_map
    ):
        x_api, y_api, web = _twin_apis(tmp_path, rig)
        y_api.node = "auto"
        w_dir = tmp_path / "w" / "api"
        w_dir.mkdir(parents=True)
        rig.states[w_dir] = _state(w_dir)
        w_api = ProjectConfig(path=str(w_dir), node="auto", title="api-w")
        _record("api-x", "second", "~/magent/api")
        _record("api-y", "third", "~/magent/api")
        _record("api-w", "second", "~/magent/api")
        rig.live = True
        cls = unreadable_map()
        outcomes = _batch(_config(x_api, y_api, w_api, web), only=["api-x", "web"])
        assert [(o.sid, o.ok) for o in outcomes] == [("api-x", False), ("web", True)]
        assert outcomes[0].error == _FOLDER_UNKNOWN.format(cls=cls, nick="second")
        assert [(n, r.sid) for n, r in rig.recipes] == [("second", "web")]

    def test_the_holder_and_its_hidden_auto_twin_in_one_batch_both_name_the_map(
        self, rig, tmp_path, unreadable_map
    ):
        x_api, y_api, web = _twin_apis(tmp_path, rig)
        y_api.node = "auto"
        _record("api-x", "second", "~/magent/api")
        _record("api-y", "third", "~/magent/api")
        rig.live = True
        cls = unreadable_map()
        outcomes = _batch(_config(x_api, y_api, web))
        assert [(o.sid, o.ok) for o in outcomes] == [
            ("api-x", False),
            ("api-y", False),
            ("web", True),
        ]
        assert outcomes[0].error == _FOLDER_UNKNOWN.format(cls=cls, nick="second")
        assert outcomes[1].error == (
            f"the node map could not be read ({cls}), so where this auto project"
            " runs is unknown; not brought up"
        )
        # Its node is unknown, and the outcome never guesses one.
        assert outcomes[1].node == ""
        assert [(n, r.sid) for n, r in rig.recipes] == [("second", "web")]

    def test_two_pinned_twins_keep_the_rename_text(self, rig, tmp_path, unreadable_map):
        # No auto project in the group: the clash is real whatever the map says.
        x_api, y_api, web = _twin_apis(tmp_path, rig)
        _record("api-x", "second", "~/magent/api")
        rig.live = True
        unreadable_map()
        outcomes = _batch(_config(x_api, y_api, web), only=["api-x", "web"])
        assert [(o.sid, o.ok) for o in outcomes] == [("api-x", False), ("web", True)]
        error = outcomes[0].error or ""
        assert "'api-x' and 'api-y' would share" in error
        assert "rename one of them" in error
        assert "node map could not be read" not in error

    def test_a_readable_map_keeps_the_holder_up_beside_its_auto_twin(
        self, rig, tmp_path, monkeypatch
    ):
        # The same fleet with the map readable: api-x proves it holds
        # second:~/magent/api and attaches, told of the clash as a warning.
        x_api, y_api, web = _twin_apis(tmp_path, rig)
        y_api.node = "auto"
        _record("api-x", "second", "~/magent/api")
        _record("api-y", "third", "~/magent/api")
        rig.live = True
        _attached_when_live(monkeypatch, rig)
        outcomes = _batch(_config(x_api, y_api, web), only=["api-x", "web"])
        holder = outcomes[0]
        assert (holder.sid, holder.ok, holder.attached_existing) == (
            "api-x",
            True,
            True,
        )
        (warning,) = holder.warnings
        assert "'api-x' and 'api-y' would share" in warning
        assert "node map could not be read" not in warning

    def test_only_a_known_member_is_given_the_map_reason(self):
        # Two auto projects of unknown node sharing a folder name: neither is
        # a member whose node is known, so neither gets the map text (whose
        # "on <node>" would name no node) -- each keeps the X3 text.
        def unknown(sid: str) -> tuple[str, nodes.Recipe, bool]:
            return (
                "",
                nodes.Recipe(
                    project=sid,
                    sid=sid,
                    repos=(),
                    push_files=(),
                    memory_dir=None,
                    remote_root=f"{nodes.UNKNOWN_NODE_ROOT}/api",
                ),
                False,
            )

        placed = {"api-y": unknown("api-y"), "api-w": unknown("api-w")}
        clash = launch._folder_clashes(placed, ValueError("torn"))
        text = nodes.remote_root_collision_text(
            (placed["api-y"][1], placed["api-w"][1])
        )
        assert clash == {"api-y": text, "api-w": text}

    def test_a_dirty_pinned_project_attaches_to_its_running_session(
        self, rig, api, tmp_path, monkeypatch, caplog, unreadable_map
    ):
        # The map cannot say it runs there, so its pin and its own session id
        # are asked: running, the uncommitted tree here does not touch it.
        _record("api", "second", "~/magent/api")
        rig.states[tmp_path / "api"] = _state(tmp_path / "api", dirty=True)
        probes: list[tuple[str, str]] = []

        def has_session(node: nodes.Node, sid: str) -> bool | None:
            probes.append((node.nick, sid))
            return True

        monkeypatch.setattr(remote_mux, "has_session", has_session)
        cls = unreadable_map()
        full = _looked_up_on_its_pin(caplog)
        outcome = launch.bring_up_node_project(_config(api), api)
        assert (outcome.ok, outcome.node, outcome.attached_existing) == (
            True,
            "second",
            True,
        )
        assert outcome.error is None
        (dirty,) = [w for w in outcome.warnings if "--allow-dirty" in w]
        # The map is named on screen by its class, once; nodes.log has it all.
        assert outcome.warnings == (
            dirty,
            (
                f"the node map could not be read ({cls}); attached to api on its"
                " pin @second"
            ),
        )
        assert full() == 1
        assert probes == [("second", "api")]
        assert rig.decorated == [("api", "second")]
        assert rig.recipes == []

    @pytest.mark.parametrize("live", [False, None], ids=["not-running", "no-answer"])
    def test_a_dirty_pinned_project_not_found_running_is_refused_naming_the_map(
        self, rig, api, tmp_path, monkeypatch, caplog, unreadable_map, live
    ):
        _record("api", "second", "~/magent/api")
        rig.states[tmp_path / "api"] = _state(tmp_path / "api", dirty=True)
        probes: list[tuple[str, str]] = []

        def has_session(node: nodes.Node, sid: str) -> bool | None:
            probes.append((node.nick, sid))
            return live

        monkeypatch.setattr(remote_mux, "has_session", has_session)
        cls = unreadable_map()
        full = _looked_up_on_its_pin(caplog)
        outcome = launch.bring_up_node_project(_config(api), api)
        assert (outcome.ok, outcome.node) == (False, "second")
        # The screen names the class; the error it stands for is in nodes.log.
        assert full() == 1
        error = outcome.error or ""
        assert "--allow-dirty" in error
        assert error.endswith(
            f"; the node map could not be read ({cls}), and api was not found"
            " running on @second"
        )
        assert "torn" not in error
        assert "cannot access" not in error
        assert probes == [("second", "api")]
        assert rig.decorated == []
        assert rig.recipes == []

    def test_a_pinned_project_keeps_its_pin(self, rig, api, unreadable_map):
        _record("api", "third", "~/magent/api")
        unreadable_map()
        (outcome,) = _batch(_config(api))
        assert (outcome.ok, outcome.node) == (True, "second")
        assert [(n, r.sid) for n, r in rig.recipes] == [("second", "api")]

    def test_the_local_fleet_and_the_pinned_node_projects_still_come_up(
        self, rig, tmp_path, monkeypatch, capsys, unreadable_map
    ):
        pinned, auto = _projects(tmp_path, rig, [("a1", "second"), ("w1", "auto")])
        _record("w1", "third", "~/magent/w1")
        unreadable_map()
        monkeypatch.setattr(
            "magent.psmux.bring_up", lambda cfg, only, group: (["loc"], [])
        )
        assert launch.bring_up_psmux(_config(pinned, auto)) == (["loc", "a1"], ["w1"])
        out = capsys.readouterr().out
        (line,) = [ln for ln in out.splitlines() if ln.lstrip().startswith("x w1:")]
        assert "node map could not be read" in line


class TestUpBringsUpNodeProjectsToo:
    def test_local_and_node_results_are_merged_and_node_lines_printed(
        self, rig, tmp_path, monkeypatch, capsys
    ):
        projs = _projects(tmp_path, rig, [("a1", "second"), ("a2", "second")])
        rig.states[tmp_path / "a2"] = _state(tmp_path / "a2", dirty=True)
        monkeypatch.setattr(
            "magent.psmux.bring_up", lambda cfg, only, group: (["loc"], ["bad"])
        )
        created, failed = launch.bring_up_psmux(_config(*projs))
        assert (created, failed) == (["loc", "a1"], ["bad", "a2"])
        out = capsys.readouterr().out
        assert "a1 @second started" in out
        assert "a2: " in out
        assert "--allow-dirty" in out

    def test_an_attached_session_reads_attached_and_its_warnings_print(
        self, rig, api, tmp_path, monkeypatch, capsys
    ):
        # D10: a refusal made moot by a live session is a warning, as is a
        # session that came up but was not recorded -- both reach the user
        # only through this echo.
        nodes.update_node_map(
            "api",
            NodeMapEntry(
                nick="second",
                sid="api",
                placed_ts=1.0,
                attached_existing=False,
                remote_root="~/magent/api",
                target="amin@devino-second",
            ),
        )
        rig.live = True
        rig.states[tmp_path / "api"] = _state(tmp_path / "api", dirty=True)
        monkeypatch.setattr("magent.psmux.bring_up", lambda cfg, only, group: ([], []))
        assert launch.bring_up_psmux(_config(api)) == (["api"], [])
        out = capsys.readouterr().out
        assert "api @second attached" in out
        (warning,) = [line for line in out.splitlines() if "--allow-dirty" in line]
        assert warning.lstrip().startswith("! ")

    def test_an_unreadable_push_file_fails_only_its_own_project(
        self, rig, tmp_path, monkeypatch, capsys, caplog
    ):
        # Raising for a push candidate that cannot be read is contained: that
        # project's outcome is a failure, the other node project and the
        # local fleet still come up, and nothing raises out of `up`.
        from magent.log import get_logger

        get_logger("nodes")  # sets the level; caplog must come after
        caplog.set_level("WARNING", logger="magent.nodes")
        good, locked = _projects(tmp_path, rig, [("a1", "second"), ("z", "second")])
        secret = tmp_path / "z" / "sa.json"
        secret.write_text("{}", encoding="utf-8")
        locked = dataclasses.replace(locked, push=["sa.json"])
        deny_stat(monkeypatch, secret.resolve())
        monkeypatch.setattr(
            "magent.psmux.bring_up", lambda cfg, only, group: (["loc"], [])
        )
        created, failed = launch.bring_up_psmux(_config(good, locked))
        assert (created, failed) == (["loc", "a1"], ["z"])
        assert [recipe.sid for _, recipe in rig.recipes] == ["a1"]
        out = capsys.readouterr().out
        assert "  x z: local error: PermissionError; see nodes.log\n" in out
        # Neither the OS's words nor the file's path reach the screen; the log
        # has both.
        assert "Permission denied" not in out
        assert "sa.json" not in out
        logged = [
            r.getMessage()
            for r in caplog.records
            if r.name == "magent.nodes" and r.levelno == logging.WARNING
        ]
        assert any("Permission denied" in m and "sa.json" in m for m in logged)

    def test_allow_dirty_reaches_the_node_bring_up(self, rig, tmp_path, monkeypatch):
        projs = _projects(tmp_path, rig, [("a1", "second")])
        rig.states[tmp_path / "a1"] = _state(tmp_path / "a1", dirty=True)
        monkeypatch.setattr("magent.psmux.bring_up", lambda cfg, only, group: ([], []))
        assert launch.bring_up_psmux(_config(*projs), allow_dirty=True) == (["a1"], [])

    def test_up_never_opens_a_window(self, rig, tmp_path, monkeypatch):
        # `up` is the host side of attach, often run over ssh.
        projs = _projects(tmp_path, rig, [("a1", "second")])
        monkeypatch.setattr("magent.psmux.bring_up", lambda cfg, only, group: ([], []))
        monkeypatch.setattr(
            launch, "get_platform", lambda: FakePlatform(supports_attach_windows=True)
        )
        launch.bring_up_psmux(_config(*projs))
        assert rig.windows == []

    def test_only_reaches_the_node_half(self, rig, tmp_path, monkeypatch):
        # `magent up` without --all and the menu's `u` pass the down-list: a
        # node project outside it is never dialed, cloned or provisioned.
        projs = _projects(tmp_path, rig, [("a1", "second"), ("b1", "third")])
        monkeypatch.setattr("magent.psmux.bring_up", lambda cfg, only, group: ([], []))
        assert launch.bring_up_psmux(_config(*projs), only=["b1"]) == (["b1"], [])
        assert [recipe.sid for _, recipe in rig.recipes] == ["b1"]
        assert launch.bring_up_psmux(_config(*projs), only=["local-x"]) == ([], [])
        assert [recipe.sid for _, recipe in rig.recipes] == ["b1"]

    def test_group_reaches_the_node_half(self, rig, tmp_path, monkeypatch):
        # `up --group work` brings up that group's node projects and no other.
        work, other = _projects(tmp_path, rig, [("w1", "second"), ("o1", "third")])
        work.group = "work"
        monkeypatch.setattr("magent.psmux.bring_up", lambda cfg, only, group: ([], []))
        assert launch.bring_up_psmux(_config(work, other), group="WORK") == (["w1"], [])
        assert [recipe.sid for _, recipe in rig.recipes] == ["w1"]

    def test_node_session_ids_follow_the_group_filter(self, rig, tmp_path):
        a = ProjectConfig(path=str(tmp_path / "a"), node="second", group="work")
        b = ProjectConfig(path=str(tmp_path / "b"), node="second")
        assert launch.node_session_ids(_config(a, b), group="WORK") == ["a"]


def _hold(name: str, nick: str = "second", sid: str | None = None) -> None:
    nodes.update_node_map(
        name,
        NodeMapEntry(
            nick=nick,
            sid=sid or name,
            placed_ts=1.0,
            attached_existing=False,
            remote_root=f"~/magent/{name}",
            target=f"amin@devino-{nick}",
        ),
    )


class TestTheNodeLockCoversTheDialNotTheWindow:
    def test_the_bring_up_runs_under_its_nodes_lock(self, rig, api, monkeypatch):
        held: list[bool] = []
        real = rig._bring_up

        def spy(node, recipe, **kw):
            held.append(launch._bring_up_lock(node.nick).locked())
            return real(node, recipe, **kw)

        monkeypatch.setattr(remote_mux, "bring_up", spy)
        assert launch.bring_up_node_project(_config(api), api).ok
        assert held == [True]
        assert not launch._bring_up_lock("second").locked()

    def test_the_window_opens_after_the_lock_is_released(self, rig, api, monkeypatch):
        monkeypatch.setattr(
            launch, "get_platform", lambda: FakePlatform(supports_attach_windows=True)
        )
        held: list[bool] = []
        real = rig._window

        def spy(target, sid, **kw):
            held.append(launch._bring_up_lock("second").locked())
            return real(target, sid, **kw)

        monkeypatch.setattr("magent.attach_client.spawn_attach_window", spy)
        assert launch.bring_up_node_project(_config(api), api, window=True).ok
        assert held == [False]

    def test_a_failed_bring_up_releases_the_lock(self, rig, api):
        rig.error = RemoteError(5, "magent: clone failed", ("bring_up",))
        assert not launch.bring_up_node_project(_config(api), api).ok
        assert not launch._bring_up_lock("second").locked()

    def test_one_lock_per_node(self):
        assert launch._bring_up_lock("second") is launch._bring_up_lock("second")
        assert launch._bring_up_lock("second") is not launch._bring_up_lock("third")


class TestTheAttachInsteadPathIsNarrow:
    def test_a_session_held_on_another_node_does_not_excuse_a_dirty_tree(
        self, rig, api, tmp_path
    ):
        _hold("api", nick="third")
        rig.live = True
        rig.states[tmp_path / "api"] = _state(tmp_path / "api", dirty=True)
        outcome = launch.bring_up_node_project(_config(api), api)
        assert (outcome.ok, outcome.attached_existing) == (False, False)
        assert rig.decorated == []

    def test_a_probe_that_failed_is_not_a_live_session(self, rig, api, tmp_path):
        _hold("api")
        rig.live = None  # has_session's "the PROBE failed" answer
        rig.states[tmp_path / "api"] = _state(tmp_path / "api", dirty=True)
        outcome = launch.bring_up_node_project(_config(api), api)
        assert (outcome.ok, outcome.attached_existing) == (False, False)
        assert rig.decorated == []

    def test_attaching_instead_opens_the_window_on_the_held_session(
        self, rig, api, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            launch, "get_platform", lambda: FakePlatform(supports_attach_windows=True)
        )
        _hold("api")
        rig.live = True
        rig.states[tmp_path / "api"] = _state(tmp_path / "api", dirty=True)
        outcome = launch.bring_up_node_project(_config(api), api, window=True)
        assert outcome.attached_existing
        assert rig.windows == [
            ("amin@devino-second", "api", "tmux", "tmux -L magent attach -t '=api'")
        ]


class TestEveryNodeFailureIsAnOutcomeButABugIsNot:
    def test_a_map_held_past_its_wait_is_an_outcome(self, rig, api, monkeypatch):
        def held(*_a: object, **_k: object) -> None:
            raise lockfile.LockHeld("the node map is held by another writer")

        monkeypatch.setattr(nodes, "update_node_map", held)
        outcome = launch.bring_up_node_project(_config(api), api)
        # The node said yes before the map write failed: the session is up,
        # so the outcome is ok with a repair warning naming the cause (6688a69)
        # -- by its class; its words are nodes.log's.
        assert (outcome.ok, outcome.node, outcome.error) == (True, "second", None)
        (warning,) = [w for w in outcome.warnings if "not recorded" in w]
        assert "(LockHeld)" in warning
        assert "held by another writer" not in warning
        assert not launch._bring_up_lock("second").locked()

    def test_an_unreadable_push_file_is_an_outcome(self, rig, api, caplog):
        # The class on screen; the OS's words and the path are nodes.log's.
        from magent.log import get_logger

        get_logger("nodes")  # sets the level; caplog must come after
        caplog.set_level("WARNING", logger="magent.nodes")
        rig.error = PermissionError(13, "Permission denied", r"C:\Users\amin\sa.json")
        outcome = launch.bring_up_node_project(_config(api), api)
        assert outcome.ok is False
        assert outcome.error == "local error: PermissionError; see nodes.log"
        assert "Permission denied" not in outcome.error
        assert "sa.json" not in outcome.error
        (record,) = [r for r in caplog.records if "failed" in r.getMessage()]
        assert record.getMessage().endswith(f"failed: {rig.error}")

    def test_a_config_error_logs_the_os_error_under_it(self, rig, api, caplog):
        # nodes' own words on screen, the chained OS error in nodes.log.
        from magent.log import get_logger

        get_logger("nodes")
        caplog.set_level("WARNING", logger="magent.nodes")
        error = nodes.NodeConfigError(r"C:\ws\api: cannot be resolved (OSError)")
        error.__cause__ = OSError(62, "Too many levels of symbolic links")
        rig.error = error
        outcome = launch.bring_up_node_project(_config(api), api)
        assert outcome.error == r"C:\ws\api: cannot be resolved (OSError)"
        # The configured project path is named; the OS's words are not.
        assert "symbolic links" not in outcome.error
        (record,) = [r for r in caplog.records if "failed" in r.getMessage()]
        assert record.getMessage().endswith(
            r"C:\ws\api: cannot be resolved (OSError):"
            " [Errno 62] Too many levels of symbolic links"
        )

    def test_the_recipes_warnings_reach_the_outcome(self, rig, api, monkeypatch):
        real = launch.node_recipe
        monkeypatch.setattr(
            launch,
            "node_recipe",
            lambda *a: dataclasses.replace(real(*a), warnings=("push: .env skipped",)),
        )
        assert launch.bring_up_node_project(_config(api), api).warnings == (
            "push: .env skipped",
        )

    def test_a_bug_is_not_swallowed_into_an_outcome(self, rig, api):
        rig.error = TypeError("a real bug")
        with pytest.raises(TypeError):
            launch.bring_up_node_project(_config(api), api)

    def test_an_unexpected_value_error_is_logged_with_its_traceback(
        self, rig, api, caplog
    ):
        # A plain ValueError is an outcome (a recipe that cannot be framed) but
        # also possibly a bug: the log keeps the traceback to tell them apart.
        from magent.log import get_logger

        get_logger("nodes")  # sets the level; caplog must come after
        caplog.set_level("WARNING", logger="magent.nodes")
        rig.error = ValueError("cannot frame the recipe")
        assert not launch.bring_up_node_project(_config(api), api).ok
        (record,) = [r for r in caplog.records if "failed" in r.getMessage()]
        assert record.exc_info

    @pytest.mark.parametrize(
        "exc",
        [
            nodes.NodeConfigError("second: unknown node"),
            RemoteError(5, "magent: clone failed", ("bring_up",)),
            PermissionError(13, "Permission denied"),
        ],
    )
    def test_an_expected_failure_is_logged_without_one(self, rig, api, caplog, exc):
        from magent.log import get_logger

        get_logger("nodes")
        caplog.set_level("WARNING", logger="magent.nodes")
        rig.error = exc
        assert not launch.bring_up_node_project(_config(api), api).ok
        (record,) = [r for r in caplog.records if "failed" in r.getMessage()]
        assert not record.exc_info

    def test_the_nodes_last_stderr_line_is_the_reason(self, rig, api):
        rig.error = RemoteError(
            5,
            "Cloning into 'api'...\nfatal: repository not found\n"
            "magent: git clone of api failed",
            ("bring_up",),
        )
        outcome = launch.bring_up_node_project(_config(api), api)
        assert outcome.error == "git clone of api failed"


@pytest.fixture
def no_sleep(monkeypatch):
    # Same device as test_launch.py's fake_sleep: tiling's retry loop and the
    # launch delay both sleep through the shared `time` module.
    monkeypatch.setattr(time, "sleep", lambda _s: None)


@pytest.fixture
def desk(monkeypatch):
    plat = FakePlatform(supports_attach_windows=True)
    monkeypatch.setattr(launch, "get_platform", lambda: plat)
    return plat


class TestGoBringsNodeProjectsUp:
    def test_a_node_project_is_listed_with_its_node_badge_and_brought_up(
        self, rig, api, desk, no_sleep, capsys
    ):
        rc = launch.run_magent(_config(api), launch.RunOpts(retile_all=True))
        assert rc == 0
        out = capsys.readouterr().out
        assert "[@second]" in out
        assert "api @second started" in out
        assert rig.windows == [
            ("amin@devino-second", "api", "tmux", "tmux -L magent attach -t '=api'")
        ]
        assert "api" in nodes.read_node_map()

    def test_a_dirty_tree_is_refused_with_no_ssh_and_no_map_entry(
        self, rig, api, desk, no_sleep, tmp_path, capsys
    ):
        # R-D2.
        rig.states[tmp_path / "api"] = _state(tmp_path / "api", dirty=True)
        assert launch.run_magent(_config(api), launch.RunOpts()) == 0
        out = capsys.readouterr().out
        assert "dirty" in out
        assert "--allow-dirty" in out
        assert rig.recipes == []
        assert rig.windows == []
        assert nodes.read_node_map() == {}

    def test_allow_dirty_reaches_the_bring_up(self, rig, api, desk, no_sleep, tmp_path):
        rig.states[tmp_path / "api"] = _state(tmp_path / "api", dirty=True)
        launch.run_magent(_config(api), launch.RunOpts(allow_dirty=True))
        assert [nick for nick, _ in rig.recipes] == ["second"]

    def test_dry_run_names_the_target_folder_and_touches_nothing(
        self, api, desk, no_sleep, tmp_path, monkeypatch, capsys
    ):
        # R-D3 + R-D4: <root>/<local folder name>, no ssh, no git, no map.
        def forbidden(*_a: object, **_k: object) -> None:
            raise AssertionError("--dry-run must not start a process or provision")

        monkeypatch.setattr(nodes, "NODE_MAP_PATH", tmp_path / "node-map.json")
        monkeypatch.setattr(subprocess, "Popen", forbidden)
        monkeypatch.setattr(subprocess, "run", forbidden)
        monkeypatch.setattr(launch, "_provision_once", forbidden)
        assert launch.run_magent(_config(api), launch.RunOpts(dry_run=True)) == 0
        out = capsys.readouterr().out
        assert "-> amin@devino-second:~/magent/api" in out
        # DECISION-24: what the real run would do first, said and not done.
        assert "would provision second" in out
        assert not (tmp_path / "node-map.json").exists()

    def test_dry_run_prints_why_an_auto_project_has_no_target(
        self, desk, no_sleep, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(nodes, "NODE_MAP_PATH", tmp_path / "node-map.json")
        proj = ProjectConfig(path=str(tmp_path), node="auto")
        launch.run_magent(_config(proj), launch.RunOpts(dry_run=True))
        assert "needs a placement" in capsys.readouterr().out

    def test_dry_run_names_a_folder_this_user_may_not_read(
        self, desk, no_sleep, tmp_path, monkeypatch, capsys, caplog
    ):
        # The preview says why, as the real run's outcome would, rather than
        # raising out of `--dry-run` -- by class, with the OS's words logged.
        from magent.log import get_logger

        get_logger("nodes")
        caplog.set_level("WARNING", logger="magent.nodes")
        monkeypatch.setattr(nodes, "NODE_MAP_PATH", tmp_path / "node-map.json")
        locked = tmp_path / "locked"
        locked.mkdir()
        deny_stat(monkeypatch, locked)
        proj = ProjectConfig(path=str(locked), node="second")
        assert launch.run_magent(_config(proj), launch.RunOpts(dry_run=True)) == 0
        out = capsys.readouterr().out
        assert "      x local error: PermissionError; see nodes.log\n" in out
        assert "Permission denied" not in out
        assert str(locked) not in out
        # str(OSError) reprs its filename.
        assert any(
            "Permission denied" in r.getMessage()
            and repr(str(locked)) in r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
        )

    def test_dry_run_says_a_config_error_in_printable_ascii(
        self, desk, no_sleep, tmp_path, monkeypatch, capsys
    ):
        # A folder name off a POSIX disk can hold a lone surrogate; the preview
        # must not die of it any more than the run's own row does.
        monkeypatch.setattr(nodes, "NODE_MAP_PATH", tmp_path / "node-map.json")

        def refuse(*_a: object, **_k: object) -> object:
            raise nodes.NodeConfigError("caf\udce9\x1b[2J: no usable name")

        monkeypatch.setattr(nodes, "resolve", refuse)
        proj = ProjectConfig(path=str(tmp_path), node="second")
        assert launch.run_magent(_config(proj), launch.RunOpts(dry_run=True)) == 0
        assert "      x caf??[2J: no usable name\n" in capsys.readouterr().out

    def test_a_window_already_open_is_not_brought_up_again(
        self, rig, api, no_sleep, monkeypatch
    ):
        plat = FakePlatform(supports_attach_windows=True)
        plat._register_window("magent:api")
        monkeypatch.setattr(launch, "get_platform", lambda: plat)
        launch.run_magent(_config(api), launch.RunOpts())
        assert rig.recipes == []

    def test_an_ide_project_with_a_node_stays_local(
        self, rig, tmp_path, desk, no_sleep, capsys
    ):
        folder = tmp_path / "docs"
        folder.mkdir()
        proj = ProjectConfig(path=str(folder), node="second", tool="code")
        launch.run_magent(_config(proj), launch.RunOpts(dry_run=True))
        assert "[@second]" not in capsys.readouterr().out
        assert rig.recipes == []


class TestTheGoFlagIsPlumbed:
    def test_allow_dirty_reaches_run_opts(self, tmp_config, monkeypatch):
        seen: list[launch.RunOpts] = []
        monkeypatch.setattr(
            launch, "run_magent", lambda cfg, opts: seen.append(opts) or 0
        )
        path = tmp_config({"version": SCHEMA_VERSION, "projects": [{"path": "."}]})
        result = CliRunner().invoke(
            cli.main, ["--config", path, "--go", "--all", "--allow-dirty"]
        )
        assert result.exit_code == 0, result.output
        assert seen[0].allow_dirty is True


def _spawn_titled(desk: FakePlatform, title: str):
    """A ``spawn_attach_window`` stand-in whose window really appears on the
    desk, under ``title`` -- deliberately NOT ``make_title(sid)``, so a tile
    pass that rebuilt the title from the sid would hunt a window that does not
    exist and print "not found"."""

    def spawn(target, sid, *, mux, remote=None, reconnect=True):
        desk._register_window(title)
        return title

    return spawn


def _placed_names(out: str) -> list[str]:
    return [
        line.split()[1]
        for line in out.splitlines()
        if line.lstrip().startswith("+ ") and "-> screen" in line
    ]


class TestGoTilesNodeWindowsByTheTitleTheSpawnReturned:
    """D12: a node window is placed by the title ``spawn_attach_window``
    handed back through ``NodeBringUpOutcome.title``, never one rebuilt from
    the session id -- and a project with no window coming is not waited on."""

    def test_the_window_is_placed_by_the_returned_title(
        self, rig, api, desk, no_sleep, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            "magent.attach_client.spawn_attach_window",
            _spawn_titled(desk, "magent:api-at-second"),
        )
        assert launch.run_magent(_config(api), launch.RunOpts(retile_all=True)) == 0
        out = capsys.readouterr().out
        assert "not found" not in out
        assert _placed_names(out) == ["api"]
        assert [h for h, _rect in desk.moved] == [desk._windows["magent:api-at-second"]]

    def test_a_badged_title_still_places_it(
        self, rig, api, desk, no_sleep, monkeypatch, capsys
    ):
        # The attention daemon may badge the window before the tile pass runs;
        # matching by parsed name, like every magent window, survives that.
        def spawn(target, sid, *, mux, remote=None, reconnect=True):
            desk._register_window("magent:[!] api")
            return "magent:api"

        monkeypatch.setattr("magent.attach_client.spawn_attach_window", spawn)
        launch.run_magent(_config(api), launch.RunOpts(retile_all=True))
        assert "not found" not in capsys.readouterr().out
        assert len(desk.moved) == 1

    def test_a_title_outside_the_grammar_is_placed_by_exact_match(
        self, rig, api, desk, no_sleep, monkeypatch, capsys
    ):
        monkeypatch.setattr(
            "magent.attach_client.spawn_attach_window",
            _spawn_titled(desk, "api on second"),
        )
        launch.run_magent(_config(api), launch.RunOpts(retile_all=True))
        assert "not found" not in capsys.readouterr().out
        assert [h for h, _rect in desk.moved] == [desk._windows["api on second"]]

    def test_a_refused_project_is_not_waited_for(
        self, rig, api, desk, no_sleep, tmp_path, capsys
    ):
        rig.states[tmp_path / "api"] = _state(tmp_path / "api", dirty=True)
        launch.run_magent(_config(api), launch.RunOpts(retile_all=True))
        assert "not found" not in capsys.readouterr().out
        assert desk.moved == []

    def test_a_window_that_failed_to_open_is_not_waited_for(
        self, rig, api, desk, no_sleep, monkeypatch, capsys
    ):
        def no_wt(*_a: object, **_k: object) -> str:
            raise FileNotFoundError("wt")

        monkeypatch.setattr("magent.attach_client.spawn_attach_window", no_wt)
        launch.run_magent(_config(api), launch.RunOpts(retile_all=True))
        out = capsys.readouterr().out
        assert "api @second started" in out
        assert "not found" not in out

    def test_a_platform_without_attach_windows_waits_for_none(
        self, rig, api, no_sleep, monkeypatch, capsys
    ):
        monkeypatch.setattr(launch, "get_platform", FakePlatform)
        launch.run_magent(_config(api), launch.RunOpts(retile_all=True))
        out = capsys.readouterr().out
        assert "api @second started" in out
        assert "not found" not in out

    def test_the_node_window_keeps_its_config_order_slot(
        self, rig, api, desk, no_sleep, tmp_path, monkeypatch, capsys
    ):
        # Config order is slot order: the rebuilt node target stays where the
        # launch loop put it, between the two local windows.
        before = ProjectConfig(path=str(tmp_path / "a-local"))
        after = ProjectConfig(path=str(tmp_path / "z-local"))
        for proj in (before, after):
            Path(proj.path).mkdir()
        monkeypatch.setattr(
            "magent.attach_client.spawn_attach_window",
            _spawn_titled(desk, "magent:api-at-second"),
        )
        launch.run_magent(_config(before, api, after), launch.RunOpts(retile_all=True))
        assert _placed_names(capsys.readouterr().out) == ["a-local", "api", "z-local"]


class TestGoWarnsOnceWhenNodeWindowsCannotReconnect:
    """``spawn_attach_window`` degrades a missing supervisor to a bare-ssh
    pane silently, by design: the batch caller -- ``--go``'s node phase --
    says so once, whatever the number of windows."""

    def test_one_warning_for_the_whole_batch(
        self, rig, tmp_path, desk, no_sleep, monkeypatch, capsys
    ):
        projs = _projects(tmp_path, rig, [("a1", "second"), ("b1", "third")])
        monkeypatch.setattr(attach_client, "client_exe", lambda: None)
        launch.run_magent(_config(*projs), launch.RunOpts())
        out = capsys.readouterr().out
        assert out.count("will not auto-reconnect") == 1
        assert attach_client.CLIENT_EXE_NAME in out
        assert len(rig.windows) == 2

    def test_no_warning_when_the_supervisor_is_there(
        self, rig, api, desk, no_sleep, monkeypatch, capsys
    ):
        monkeypatch.setattr(attach_client, "client_exe", lambda: "C:/x/client.exe")
        launch.run_magent(_config(api), launch.RunOpts())
        assert "auto-reconnect" not in capsys.readouterr().out

    def test_no_warning_where_no_window_can_open(
        self, rig, api, no_sleep, monkeypatch, capsys
    ):
        monkeypatch.setattr(launch, "get_platform", FakePlatform)
        monkeypatch.setattr(attach_client, "client_exe", lambda: None)
        launch.run_magent(_config(api), launch.RunOpts())
        assert "auto-reconnect" not in capsys.readouterr().out

    def test_no_warning_without_a_node_bring_up(
        self, rig, api, no_sleep, monkeypatch, capsys
    ):
        plat = FakePlatform(supports_attach_windows=True)
        plat._register_window("magent:api")
        monkeypatch.setattr(launch, "get_platform", lambda: plat)
        monkeypatch.setattr(attach_client, "client_exe", lambda: None)
        launch.run_magent(_config(api), launch.RunOpts())
        assert "auto-reconnect" not in capsys.readouterr().out


class TestADryRunPreviewsOnlyWhatTheRunWillDo:
    """cq-D12 I1/M2: the preview is the real run's first step, so it appears
    exactly where the real run would queue a bring-up -- never for a window
    already open, never under a re-tile -- and it reads the map's placement."""

    def test_an_open_window_is_not_previewed(
        self, rig, api, no_sleep, monkeypatch, capsys
    ):
        plat = FakePlatform(supports_attach_windows=True)
        plat._register_window("magent:api")
        monkeypatch.setattr(launch, "get_platform", lambda: plat)
        launch.run_magent(_config(api), launch.RunOpts(dry_run=True, retile_all=True))
        out = capsys.readouterr().out
        assert "would provision" not in out
        assert "-> amin@devino-second" not in out

    def test_a_retile_is_not_previewed(self, rig, api, desk, no_sleep, capsys):
        launch.run_magent(
            _config(api), launch.RunOpts(dry_run=True, retile_all=True, tile_only=True)
        )
        assert "would provision" not in capsys.readouterr().out

    def test_an_auto_project_is_badged_and_previewed_on_its_placed_node(
        self, rig, tmp_path, desk, no_sleep, capsys
    ):
        folder = tmp_path / "web"
        folder.mkdir()
        rig.states[folder] = _state(folder)
        nodes.update_node_map(
            "web",
            NodeMapEntry(
                nick="third",
                sid="web",
                placed_ts=0.0,
                attached_existing=False,
                remote_root="~/magent/web",
                target="amin@devino-third",
                cwd="/home/amin/magent/web",
            ),
        )
        proj = ProjectConfig(path=str(folder), node="auto")
        launch.run_magent(_config(proj), launch.RunOpts(dry_run=True))
        out = capsys.readouterr().out
        assert "[@third]" in out
        assert "-> amin@devino-third:~/magent/web" in out
        assert "would provision third" in out

    def test_a_relative_path_under_base_dir_names_its_folder(
        self, rig, tmp_path, desk, no_sleep, capsys
    ):
        base = tmp_path / "base"
        (base / "svc").mkdir(parents=True)
        proj = ProjectConfig(path="svc", node="second", title="renamed")
        cfg = _config(proj)
        cfg.base_dir = str(base)
        launch.run_magent(cfg, launch.RunOpts(dry_run=True))
        assert "-> amin@devino-second:~/magent/svc" in capsys.readouterr().out


class TestARetileOrAnOpenWindowDialsNoNode:
    """cq-D12 I3/M1: "tile what is open" never starts remote work, and the
    already-open probe keys on the SANITIZED sid -- the name the window is
    titled with -- so a spaced title cannot re-bring-up a live project."""

    def test_a_tile_only_run_never_brings_a_node_project_up(
        self, rig, api, desk, no_sleep
    ):
        launch.run_magent(_config(api), launch.RunOpts(retile_all=True, tile_only=True))
        assert rig.recipes == []
        assert rig.windows == []

    def test_a_spaced_title_probes_the_sid_window(
        self, rig, tmp_path, no_sleep, monkeypatch
    ):
        folder = tmp_path / "gh"
        folder.mkdir()
        rig.states[folder] = _state(folder)
        proj = ProjectConfig(path=str(folder), node="second", title="GitHub Ads")
        assert nodes.node_sid(proj) != "GitHub Ads"
        plat = FakePlatform(supports_attach_windows=True)
        plat._register_window("magent:" + nodes.node_sid(proj))
        monkeypatch.setattr(launch, "get_platform", lambda: plat)
        launch.run_magent(_config(proj), launch.RunOpts(retile_all=True))
        assert rig.recipes == []


class TestTheNodePhaseSaysWhatHappened:
    """cq-D12 I2/M4: a bring-up that came up but whose window did not open is
    named on screen (tiling's "not found" no longer says it, by design), and a
    fan-out that can run for minutes announces itself first."""

    def test_a_window_that_did_not_open_is_said_once(
        self, rig, api, desk, no_sleep, monkeypatch, capsys
    ):
        def no_wt(*_a: object, **_k: object) -> str:
            raise FileNotFoundError("wt")

        monkeypatch.setattr("magent.attach_client.spawn_attach_window", no_wt)
        launch.run_magent(_config(api), launch.RunOpts(retile_all=True))
        out = capsys.readouterr().out
        lines = [ln for ln in out.splitlines() if "did not open" in ln]
        assert len(lines) == 1, out
        line = lines[0]
        assert line.lstrip().startswith("! api @second: ")
        assert "nodes.log" in line
        assert "magent --go" in line
        assert line.isascii()

    def test_nothing_is_said_where_no_window_was_meant_to_open(
        self, rig, api, no_sleep, monkeypatch, capsys
    ):
        monkeypatch.setattr(launch, "get_platform", FakePlatform)
        launch.run_magent(_config(api), launch.RunOpts(retile_all=True))
        assert "did not open" not in capsys.readouterr().out

    def test_a_refused_bring_up_is_not_said_to_be_a_missing_window(
        self, rig, api, desk, no_sleep, tmp_path, capsys
    ):
        # Its own "x api: ..." line already says why; no window was ever due.
        rig.states[tmp_path / "api"] = _state(tmp_path / "api", dirty=True)
        launch.run_magent(_config(api), launch.RunOpts(retile_all=True))
        out = capsys.readouterr().out
        assert "api: " in out
        assert "did not open" not in out

    def test_an_opened_window_is_not_said_missing(
        self, rig, api, desk, no_sleep, capsys
    ):
        launch.run_magent(_config(api), launch.RunOpts(retile_all=True))
        assert "did not open" not in capsys.readouterr().out

    def test_the_fan_out_announces_itself_once(
        self, rig, tmp_path, desk, no_sleep, capsys
    ):
        projs = _projects(tmp_path, rig, [("a1", "second"), ("b1", "third")])
        launch.run_magent(_config(*projs), launch.RunOpts())
        out = capsys.readouterr().out
        assert out.count("Bringing up 2 node project(s)...") == 1
        assert out.index("Bringing up") < out.index("a1 @second started")
        assert out.isascii()

    def test_no_node_project_queued_is_no_announcement(
        self, rig, api, no_sleep, monkeypatch, capsys
    ):
        plat = FakePlatform(supports_attach_windows=True)
        plat._register_window("magent:api")
        monkeypatch.setattr(launch, "get_platform", lambda: plat)
        launch.run_magent(_config(api), launch.RunOpts())
        assert "node project(s)" not in capsys.readouterr().out


class TestANodeRowReachesTheScreenPrintable:
    """A bring-up row carries the node's own words (its last stderr line) and
    names read off disk. A lone surrogate there is a UnicodeEncodeError in the
    middle of the bring-up, and a control character is the node writing to
    this terminal: an OSC sequence retitles the window tiling finds by title.
    Every row is one line of printable ASCII."""

    @pytest.mark.parametrize(
        ("text", "shown"),
        [
            ("caf\udce9 gone", "caf? gone"),
            ("café … gone", "caf? ? gone"),
            ("clone \x1b]0;x\x07failed\r\nfatal", "clone ?]0;x?failed??fatal"),
        ],
        ids=["lone-surrogate", "non-ascii", "control"],
    )
    def test_an_error_and_a_warning_are_one_printable_line_each(
        self, capsys, text, shown
    ):
        launch._echo_node_outcomes(
            [
                launch.NodeBringUpOutcome(False, "api", "second", error=text),
                launch.NodeBringUpOutcome(True, "web", "second", warnings=(text,)),
            ]
        )
        lines = capsys.readouterr().out.splitlines()
        assert lines == [
            f"  x api: {shown}",
            "  + web @second started",
            f"    ! {shown}",
        ]
        assert all(line.isascii() and line.isprintable() for line in lines)

    def test_the_nodes_stderr_reaches_the_go_screen_printable(
        self, rig, api, desk, no_sleep, capsys
    ):
        rig.error = RemoteError(5, "magent: clone \x1b]0;x\x07failed", ("bring_up",))
        launch.run_magent(_config(api), launch.RunOpts())
        out = capsys.readouterr().out
        assert "  x api: clone ?]0;x?failed\n" in out
        assert all(line.isprintable() for line in out.splitlines())


class TestADryRunReadsTheNodeMapOnce:
    def test_one_read_for_the_whole_run(
        self, rig, tmp_path, desk, no_sleep, monkeypatch
    ):
        # cq-D12 M5: the badge and the preview of every node project answer
        # from ONE snapshot of the map, not two reads per project.
        projs = _projects(tmp_path, rig, [("a1", "second"), ("b1", "third")])
        reads: list[None] = []
        real = nodes.read_node_map

        def counting() -> dict[str, NodeMapEntry]:
            reads.append(None)
            return real()

        monkeypatch.setattr(nodes, "read_node_map", counting)
        launch.run_magent(_config(*projs), launch.RunOpts(dry_run=True))
        assert len(reads) == 1

    def test_a_run_without_node_projects_never_reads_it(
        self, rig, tmp_path, desk, no_sleep, monkeypatch
    ):
        local = tmp_path / "local"
        local.mkdir()

        def forbidden() -> dict[str, NodeMapEntry]:
            raise AssertionError("no node project: the map is not read")

        monkeypatch.setattr(nodes, "read_node_map", forbidden)
        launch.run_magent(
            _config(ProjectConfig(path=str(local))), launch.RunOpts(dry_run=True)
        )


class TestTheExactFallbackIsExact:
    def test_a_window_merely_containing_the_title_is_not_the_one_moved(
        self, rig, api, desk, no_sleep, monkeypatch
    ):
        # cq-D12 M3: a decoy whose title CONTAINS the returned one is on the
        # desk first; only the window titled exactly that is placed.
        desk._register_window("xx api on second xx")
        monkeypatch.setattr(
            "magent.attach_client.spawn_attach_window",
            _spawn_titled(desk, "api on second"),
        )
        launch.run_magent(_config(api), launch.RunOpts(retile_all=True))
        assert [h for h, _r in desk.moved] == [desk._windows["api on second"]]


class TestUpCommandBringsNodeProjectsUp:
    def _config_file(
        self, tmp_path: Path, folder: Path, group: str | None = None
    ) -> str:
        proj: dict[str, object] = {"path": str(folder), "node": "second"}
        if group:
            proj["group"] = group
        path = tmp_path / "magent.config.json"
        path.write_text(
            json.dumps(
                {
                    "projects": [proj],
                    "settings": {
                        "psmux": False,
                        "uploadServer": False,
                        "tools": _TOOLS,
                        "nodes": {"second": {"host": "devino-second", "user": "amin"}},
                    },
                }
            ),
            encoding="utf-8",
        )
        return str(path)

    def test_a_node_only_config_with_no_psmux_comes_up(
        self, rig, api, tmp_path, monkeypatch
    ):
        # R-D1: exit 0, the sid in "Brought up", the map written, no window.
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: None)
        result = CliRunner().invoke(
            cli.main, ["--config", self._config_file(tmp_path, tmp_path / "api"), "up"]
        )
        assert result.exit_code == 0, result.output
        assert "Brought up 1 session(s): api" in result.stdout
        assert "api" in nodes.read_node_map()
        assert rig.windows == []

    def test_allow_dirty_reaches_the_bring_up(self, rig, api, tmp_path, monkeypatch):
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: None)
        rig.states[tmp_path / "api"] = _state(tmp_path / "api", dirty=True)
        config = self._config_file(tmp_path, tmp_path / "api")
        refused = CliRunner().invoke(cli.main, ["--config", config, "up"])
        assert "--allow-dirty" in refused.stdout
        allowed = CliRunner().invoke(
            cli.main, ["--config", config, "up", "--allow-dirty"]
        )
        assert "Brought up 1 session(s): api" in allowed.stdout

    def test_a_node_session_is_never_decorated_as_a_local_one(
        self, rig, api, tmp_path, monkeypatch
    ):
        decorated: list[list[str]] = []
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: None)
        monkeypatch.setattr(
            "magent.launch.decorate_psmux_sessions",
            lambda names, code_hint=None: decorated.append(list(names)) or [],
        )
        result = CliRunner().invoke(
            cli.main, ["--config", self._config_file(tmp_path, tmp_path / "api"), "up"]
        )
        # The node sid really was created -- so [] means it was filtered out,
        # not that nothing came up to decorate.
        assert result.exit_code == 0, result.output
        assert "Brought up 1 session(s): api" in result.stdout
        assert decorated == [[]]

    def test_a_node_project_outside_the_group_is_out_of_scope(
        self, rig, api, tmp_path, monkeypatch
    ):
        # -g scopes the node half at the shell too: a node project in another
        # group must not turn "all already up" into an empty bring-up run.
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: None)
        monkeypatch.setattr(
            "magent.launch.psmux_status",
            lambda cfg, group=None: ([{"name": "web", "session": "web"}], [], [{}]),
        )
        monkeypatch.setattr("magent.launch.revive_psmux", lambda *a, **k: [])
        monkeypatch.setattr("magent.launch.decorate_psmux_sessions", lambda *a, **k: [])
        config = self._config_file(tmp_path, tmp_path / "api", group="backend")
        result = CliRunner().invoke(
            cli.main, ["--config", config, "up", "-g", "frontend"]
        )
        assert result.exit_code == 0, result.output
        assert "All 1 session(s) already up." in result.stdout
        assert rig.recipes == []

    def test_an_unreachable_node_points_at_the_nodes_log(
        self, rig, api, tmp_path, monkeypatch
    ):
        # A node casualty's reason (and any traceback) is logged by the
        # "nodes" logger, so the casualty line must send the reader there.
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: None)
        rig.error = RemoteError(
            255, "ssh: connect to host devino-second: timed out", ("bring_up",)
        )
        result = CliRunner().invoke(
            cli.main, ["--config", self._config_file(tmp_path, tmp_path / "api"), "up"]
        )
        assert result.exit_code == 0, result.output
        assert "1 session(s) failed to come up: api" in result.stdout
        assert "(see ~/.magent/logs/nodes.log on the host)" in result.stdout
        assert "launch.log" not in result.stdout


class TestStoppingNodeSessions:
    """``down``'s node half: each node session is killed ON ITS NODE, once.
    The local session a node project may have left here is ``stop_psmux``'s
    (D9), never this function's."""

    @pytest.fixture(autouse=True)
    def _pulled(self, monkeypatch):
        # Every entry's last turn comes home (Task 16): these pins are about
        # the kill and the unmap, so the pull before them always succeeds.
        monkeypatch.setattr(node_sync, "final_pull", lambda config, name, **_k: _PULLED)

    @pytest.fixture
    def kills(self, monkeypatch):
        calls: list[tuple[str, str]] = []
        answers: dict[str, bool | None] = {}

        def kill(node, sid):
            calls.append((node.nick, sid))
            return answers.get(sid, True)

        monkeypatch.setattr(remote_mux, "kill_session", kill)
        return calls, answers

    @pytest.fixture
    def nodes_log(self, caplog):
        # The survivor line sends the user to nodes.log: what lands THERE.
        from magent.log import get_logger

        get_logger("nodes")  # sets the level; caplog must come after
        caplog.set_level("WARNING", logger="magent.nodes")
        return lambda: [
            r.getMessage() for r in caplog.records if r.name == "magent.nodes"
        ]

    @pytest.fixture
    def torn(self, rig):
        nodes.NODE_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        nodes.NODE_MAP_PATH.write_text("{ torn", encoding="utf-8")

    @pytest.fixture(params=["torn", "busy"])
    def unreadable(self, request, rig, monkeypatch):
        # Torn on disk, or intact but locked by another process (a Windows
        # sharing violation reads as PermissionError): neither is "empty".
        if request.param == "torn":
            request.getfixturevalue("torn")
            return

        def busy() -> dict[str, nodes.NodeMapEntry]:
            raise PermissionError(13, "The process cannot access the file")

        monkeypatch.setattr(nodes, "load_node_map_strict", busy)

    def test_a_killed_session_is_stopped_and_unmapped(self, rig, api, kills):
        _hold("api")
        assert launch.stop_node_sessions(_config(api), ["api"]) == (["api"], [])
        assert kills[0] == [("second", "api")]
        assert nodes.read_node_map() == {}

    def test_a_session_that_was_already_gone_is_neither_and_unmapped(
        self, rig, api, kills
    ):
        _hold("api")
        kills[1]["api"] = False
        assert launch.stop_node_sessions(_config(api), ["api"]) == ([], [])
        assert nodes.read_node_map() == {}

    def test_an_unreachable_node_keeps_the_entry_and_names_the_survivor(
        self, rig, api, kills, nodes_log
    ):
        _hold("api")
        kills[1]["api"] = None
        assert launch.stop_node_sessions(_config(api), ["api"]) == ([], ["api"])
        assert "api" in nodes.read_node_map()
        assert nodes_log() == ["down: api not stopped: node second did not answer"]

    def test_a_pinned_project_nobody_recorded_is_still_asked_and_never_mapped(
        self, rig, api, kills
    ):
        # Another PC (or a lost map) may have started it: the pin says where.
        assert launch.stop_node_sessions(_config(api), ["api"]) == (["api"], [])
        assert kills[0] == [("second", "api")]
        assert nodes.read_node_map() == {}

    def test_an_unrecorded_kill_never_takes_the_map_lock(
        self, rig, api, kills, monkeypatch
    ):
        # No entry, nothing to clear: a torn or held map is never touched.
        writes: list[str] = []
        monkeypatch.setattr(
            nodes, "update_node_map", lambda name, entry, **_k: writes.append(name)
        )
        assert launch.stop_node_sessions(_config(api), ["api"]) == (["api"], [])
        assert writes == []

    def test_an_auto_project_that_was_never_placed_is_skipped(
        self, rig, tmp_path, kills
    ):
        proj = ProjectConfig(path=str(tmp_path / "web"), node="auto")
        assert launch.stop_node_sessions(_config(proj), ["web"]) == ([], [])
        assert kills[0] == []

    def test_an_auto_project_is_killed_where_the_map_placed_it(
        self, rig, tmp_path, kills
    ):
        proj = ProjectConfig(path=str(tmp_path / "web"), node="auto")
        _hold("web", nick="third")
        assert launch.stop_node_sessions(_config(proj), ["web"]) == (["web"], [])
        assert kills[0] == [("third", "web")]

    def test_the_map_wins_over_a_pin_changed_since_the_bring_up(self, rig, api, kills):
        _hold("api", nick="third")
        launch.stop_node_sessions(_config(api), ["api"])
        assert kills[0] == [("third", "api")]

    def test_the_recorded_sid_is_the_one_killed(self, rig, api, kills):
        _hold("api", sid="api-2")
        assert launch.stop_node_sessions(_config(api), ["api"]) == (["api"], [])
        assert kills[0] == [("second", "api-2")]

    def test_ids_outside_the_list_are_left_running(self, rig, api, tmp_path, kills):
        other = ProjectConfig(path=str(tmp_path / "web"), node="second")
        launch.stop_node_sessions(_config(api, other), ["web"])
        assert kills[0] == [("second", "web")]

    def test_a_node_that_failed_once_is_not_dialed_again(self, rig, tmp_path, kills):
        # `down --all` against a powered-off node costs one probe timeout,
        # not one per project on it; another node is still asked.
        a1, a2, b1 = (
            ProjectConfig(path=str(tmp_path / n), node=nick)
            for n, nick in (("a1", "second"), ("a2", "second"), ("b1", "third"))
        )
        kills[1].update({"a1": None, "a2": None})
        assert launch.stop_node_sessions(_config(a1, a2, b1), ["a1", "a2", "b1"]) == (
            ["b1"],
            ["a1", "a2"],
        )
        assert kills[0] == [("second", "a1"), ("third", "b1")]

    def test_a_placement_the_config_can_no_longer_name_is_a_survivor(
        self, rig, tmp_path, kills, nodes_log
    ):
        proj = ProjectConfig(path=str(tmp_path / "web"), node="auto")
        _hold("web", nick="gone")
        assert launch.stop_node_sessions(_config(proj), ["web"]) == ([], ["web"])
        assert kills[0] == []
        assert "web" in nodes.read_node_map()
        lines = nodes_log()
        assert len(lines) == 1, lines
        assert lines[0].startswith("down: web not stopped: ")
        assert "gone" in lines[0]

    @pytest.mark.parametrize(
        "exc",
        [lockfile.LockHeld("the node map is held"), ValueError("node-map.json: torn")],
        ids=["held", "torn"],
    )
    def test_a_map_that_cannot_be_rewritten_does_not_unclaim_the_kill(
        self, rig, api, kills, monkeypatch, exc
    ):
        _hold("api")

        def held(*_a: object, **_k: object) -> None:
            raise exc

        monkeypatch.setattr(nodes, "update_node_map", held)
        outcome: object
        try:
            outcome = launch.stop_node_sessions(_config(api), ["api"])
        except (ValueError, OSError) as escaped:  # `down` crashing after a proven kill
            outcome = escaped
        assert outcome == (["api"], [])

    def test_one_unmap_that_fails_stops_the_rest_from_waiting_on_the_lock(
        self, rig, tmp_path, kills, monkeypatch
    ):
        # A held map lock costs MAP_LOCK_WAIT_S per attempt: one, not one per
        # placed project. Every kill still counts.
        a1, a2 = (
            ProjectConfig(path=str(tmp_path / n), node="second") for n in ("a1", "a2")
        )
        _hold("a1")
        _hold("a2")
        tries: list[str] = []

        def held(name: str, *_a: object, **_k: object) -> None:
            tries.append(name)
            raise lockfile.LockHeld("the node map is held")

        monkeypatch.setattr(nodes, "update_node_map", held)
        assert launch.stop_node_sessions(_config(a1, a2), ["a1", "a2"]) == (
            ["a1", "a2"],
            [],
        )
        assert tries == ["a1"]

    def test_a_retitled_project_is_found_by_its_recorded_sid(
        self, rig, tmp_path, kills
    ):
        # The title changed, its session id did not: the map key is the old
        # title, and the session it records still runs. Found, killed, unmapped.
        proj = ProjectConfig(path=str(tmp_path / "web"), title="my web", node="auto")
        sid = nodes.node_sid(proj)
        _hold("my.web", nick="third", sid=sid)
        assert launch.stop_node_sessions(_config(proj), [sid]) == ([sid], [])
        assert kills[0] == [("third", sid)]
        assert nodes.read_node_map() == {}

    def test_a_placement_an_up_made_during_the_kill_survives_the_unmap(
        self, rig, api, monkeypatch
    ):
        # `up` re-placed api on third while the kill on second was in flight:
        # the unmap clears only the entry it stopped, never the fresh one.
        _hold("api")

        def kill_while_up_replaces(node, sid):
            _hold("api", nick="third")
            return True

        monkeypatch.setattr(remote_mux, "kill_session", kill_while_up_replaces)
        assert launch.stop_node_sessions(_config(api), ["api"]) == (["api"], [])
        assert {k: e.nick for k, e in nodes.read_node_map().items()} == {"api": "third"}

    # An unreadable map: this answer becomes a report, so the map is never
    # read as "nothing placed" -- whatever it might hold is not claimed.

    def test_with_an_unreadable_map_an_auto_project_is_a_survivor_nobody_dials(
        self, rig, tmp_path, kills, unreadable, nodes_log
    ):
        proj = ProjectConfig(path=str(tmp_path / "web"), node="auto")
        assert launch.stop_node_sessions(_config(proj), ["web"]) == ([], ["web"])
        assert kills[0] == []
        assert any("node map unreadable" in m for m in nodes_log())

    def test_with_an_unreadable_map_a_pin_answering_not_there_is_a_survivor(
        self, rig, api, kills, unreadable
    ):
        # It may run where the lost map placed it: "not on the pin" proves nothing.
        kills[1]["api"] = False
        assert launch.stop_node_sessions(_config(api), ["api"]) == ([], ["api"])
        assert kills[0] == [("second", "api")]

    def test_with_an_unreadable_map_a_confirmed_kill_is_stopped(
        self, rig, api, kills, unreadable
    ):
        assert launch.stop_node_sessions(_config(api), ["api"]) == (["api"], [])

    def test_with_an_unreadable_map_a_pinned_kill_says_its_last_turn_was_not_pulled(
        self, rig, api, kills, unreadable, monkeypatch, capsys
    ):
        # The pin is known, so the kill stands. Whether the lost map placed it
        # -- and so whether there was a last turn to pull -- is not: unknown
        # never reads as "nothing to pull", and the map is left as it was.
        def untouched(*_a: object, **_k: object) -> None:
            raise AssertionError("an unreadable map was written")

        monkeypatch.setattr(
            node_sync,
            "final_pull",
            lambda *_a, **_k: pytest.fail("pulled without a readable entry"),
        )
        monkeypatch.setattr(nodes, "update_node_map", untouched)
        assert launch.stop_node_sessions(_config(api), ["api"]) == (["api"], [])
        assert kills[0] == [("second", "api")]
        out = capsys.readouterr().out
        assert out.count("\n") == 1, out
        assert "api: last turn not pulled (the node map could not be read)" in out
        assert (
            "; if the map placed it, `magent node sync --once` fetches it once the"
            " map reads again"
        ) in out
        assert "kept in the node map" not in out

    def test_a_torn_map_is_never_rewritten(self, rig, api, kills, torn):
        launch.stop_node_sessions(_config(api), ["api"])
        assert nodes.NODE_MAP_PATH.read_text(encoding="utf-8") == "{ torn"


def _unreachable(timed_out: bool) -> RemoteError:
    """A pull the node never answered: silent past its bound, or ssh's own
    transport failure (rc 255)."""
    if timed_out:
        return RemoteError(None, "timed out after 120s", ("ssh",), timed_out=True)
    return RemoteError(
        255, "ssh: connect to host devino-second port 22: timed out", ("ssh",)
    )


class TestDownPullsTheLastTurnHomeFirst:
    """R-D6: `down` pulls a placed session's last turn home, THEN kills it,
    THEN unmaps it. A pull that fails never stops the kill; it keeps the map
    entry, so `magent node sync --once` can still fetch the transcript, which
    outlives the tmux session on the node's disk."""

    @pytest.fixture
    def killed(self, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(
            remote_mux, "kill_session", lambda node, sid: calls.append(sid) or True
        )
        return calls

    @staticmethod
    def _pulls(monkeypatch, error: BaseException | None = None) -> list[str]:
        pulled: list[str] = []

        def pull(config: object, name: str, **_k: object) -> remote_mux.PullResult:
            pulled.append(name)
            if error is not None:
                raise error
            return _PULLED

        monkeypatch.setattr(node_sync, "final_pull", pull)
        return pulled

    def test_a_spawn_failure_logs_the_os_words_never_the_client_path(
        self, rig, api, monkeypatch, capsys, caplog, killed
    ):
        # The pull's ssh call is quiet (the caller reports), so down's WARNING
        # is the one line carrying the OS's words; the row keeps the class.
        from magent.log import get_logger

        get_logger("nodes")  # sets the level; caplog must come after
        caplog.set_level("WARNING", logger="magent.nodes")
        client = "/opt/secret/ssh"

        def denied(*_a: object, **_k: object) -> object:
            raise PermissionError(13, "Permission denied", client)

        monkeypatch.setattr("magent.remote_mux.find_ssh", lambda: client)
        monkeypatch.setattr(remote_mux.subprocess, "Popen", denied)
        _hold("api")
        launch.stop_node_sessions(_config(api), ["api"])
        out = capsys.readouterr().out
        assert (
            "api: last turn not pulled (could not start ssh (PermissionError))" in out
        )
        assert "Permission denied" not in out
        assert client not in out
        logged = [
            r.getMessage()
            for r in caplog.records
            if r.name == "magent.nodes" and r.levelno == logging.WARNING
        ]
        (line,) = [m for m in logged if "final pull of api failed" in m]
        assert line.endswith(
            "could not start ssh (PermissionError): errno 13, Permission denied"
        )
        assert not any(client in m for m in logged)

    def test_the_order_is_pull_then_kill_then_unmap(self, rig, api, monkeypatch):
        # The entry is still mapped while both run -- the pull needs it.
        order: list[tuple[str, str, bool]] = []
        _hold("api")
        monkeypatch.setattr(
            node_sync,
            "final_pull",
            lambda config, name, **_k: (
                order.append(("pull", name, "api" in nodes.read_node_map())) or _PULLED
            ),
        )
        monkeypatch.setattr(
            remote_mux,
            "kill_session",
            lambda node, sid: (
                order.append(("kill", sid, "api" in nodes.read_node_map())) or True
            ),
        )
        assert launch.stop_node_sessions(_config(api), ["api"]) == (["api"], [])
        assert order == [("pull", "api", True), ("kill", "api", True)]
        assert nodes.read_node_map() == {}

    @pytest.mark.parametrize(
        "error",
        [
            node_sync.NodeLockHeld("node-pull-second lock is held by another process"),
            RemoteError(255, "ssh: connect to host devino-second: timed out", ("ssh",)),
            RemoteError(None, "reply exceeded 8 bytes", ("ssh",), over_cap=True),
            RemoteError(0, "could not store every pulled file of 'api'", ("pull.sh",)),
            nodes.NodeConfigError("node 'second' is not in settings.nodes"),
            OSError(28, "No space left on device"),
        ],
        ids=["locked", "unreachable", "over-cap", "unstored", "misconfigured", "disk"],
    )
    def test_a_failed_pull_still_kills_and_keeps_the_entry(
        self, rig, api, monkeypatch, capsys, killed, error
    ):
        _hold("api")
        self._pulls(monkeypatch, error)
        assert launch.stop_node_sessions(_config(api), ["api"]) == (["api"], [])
        assert killed == ["api"]
        assert "api" in nodes.read_node_map()
        assert "api: last turn not pulled" in capsys.readouterr().out

    def test_the_line_is_the_nodes_last_word_and_names_the_repair(
        self, rig, api, monkeypatch, capsys, killed
    ):
        # One line, not RemoteError's argv plus twenty lines of stderr.
        _hold("api")
        self._pulls(
            monkeypatch,
            RemoteError(3, "tar: noise\nmagent: python3 not found", ("bash", "-s")),
        )
        launch.stop_node_sessions(_config(api), ["api"])
        out = capsys.readouterr().out
        assert out.count("\n") == 2, out  # the announcement, then this line
        assert "last turn not pulled (python3 not found)" in out
        assert "tar: noise" not in out
        assert "bash -s" not in out
        assert "; kept in the node map for `magent node sync --once`" in out

    def test_the_nodes_words_reach_the_screen_as_ascii(
        self, rig, api, monkeypatch, capsys, killed
    ):
        # The cause is the node's own stderr: a non-ASCII byte there would be a
        # UnicodeEncodeError on a cp1252 pipe, mid-shutdown.
        _hold("api")
        self._pulls(
            monkeypatch,
            RemoteError(3, "magent: caf\u00e9 \u2026 gone", ("bash", "-s")),
        )
        launch.stop_node_sessions(_config(api), ["api"])
        out = capsys.readouterr().out
        assert out.isascii()
        assert "(caf? ? gone)" in out

    def test_the_nodes_control_characters_do_not_reach_the_screen(
        self, rig, api, monkeypatch, capsys, killed
    ):
        # ASCII is not enough: ESC is ASCII, and an OSC sequence in the node's
        # stderr would retitle this terminal in the middle of the shutdown.
        _hold("api")
        self._pulls(
            monkeypatch,
            RemoteError(3, "magent: gone\x1b]0;x\x07 now", ("bash", "-s")),
        )
        launch.stop_node_sessions(_config(api), ["api"])
        out = capsys.readouterr().out
        assert "(gone?]0;x? now)" in out
        assert all(line.isprintable() for line in out.splitlines())

    @pytest.mark.parametrize(
        ("error", "shown", "os_words"),
        [
            (
                PermissionError(
                    13, "Access is denied", "C:\\Users\\amin\\.magent\\pull.json"
                ),
                "(PermissionError)",
                "Access is denied",
            ),
            (
                OSError("could not write C:\\Users\\amin\\x.part"),
                "(OSError)",
                "could not write",
            ),
        ],
        ids=["strerror", "bare"],
    )
    def test_a_local_error_shows_its_kind_and_logs_its_path(
        self, rig, api, monkeypatch, capsys, caplog, killed, error, shown, os_words
    ):
        # A local path is this PC's business, not the screen's, and the OS's
        # words (strerror: localized, per platform) are not ours: the class on
        # screen, the whole error in nodes.log. (integ-D ruling: this overrides
        # D16's strerror on this row.)
        from magent.log import get_logger

        get_logger("nodes")  # sets the level; caplog must come after
        caplog.set_level("WARNING", logger="magent.nodes")
        _hold("api")
        self._pulls(monkeypatch, error)
        launch.stop_node_sessions(_config(api), ["api"])
        out = capsys.readouterr().out
        assert f"api: last turn not pulled {shown}" in out
        assert "amin" not in out
        assert os_words not in out
        logged = [
            r.getMessage()
            for r in caplog.records
            if r.name == "magent.nodes" and r.levelno == logging.WARNING
        ]
        assert any("amin" in m and os_words in m for m in logged)

    def test_the_pulls_announce_themselves_once(
        self, rig, tmp_path, monkeypatch, capsys, killed
    ):
        # One pull can wait out a held lock and then a slow node -- minutes,
        # and they run one after another. Like the bring-up's fan-out, `down`
        # says so before the first, counting only the sessions it will pull.
        a1, a2, a3 = (
            ProjectConfig(path=str(tmp_path / n), node="second")
            for n in ("a1", "a2", "a3")
        )
        for name in ("a1", "a2", "a3"):
            _hold(name)
        self._pulls(
            monkeypatch, RemoteError(0, "could not store every pulled file", ("x",))
        )
        launch.stop_node_sessions(_config(a1, a2, a3), ["a1", "a2"])
        out = capsys.readouterr().out
        assert out.count("Pulling the last turn of 2 node session(s) home...") == 1
        assert out.count("Pulling") == 1
        assert out.index("Pulling") < out.index("a1: last turn not pulled")
        assert out.isascii()

    def test_nothing_to_pull_is_no_announcement(
        self, rig, api, tmp_path, monkeypatch, capsys, killed
    ):
        # A pinned project nobody recorded is killed without a pull: nothing
        # long is coming, so nothing is announced.
        self._pulls(monkeypatch, AssertionError("pulled with no entry"))
        launch.stop_node_sessions(_config(api), ["api"])
        assert "Pulling" not in capsys.readouterr().out

    def test_a_pinned_project_nobody_recorded_is_killed_without_a_pull(
        self, rig, api, monkeypatch, capsys, killed
    ):
        # No entry, nothing to pull from: no lock, no ssh, no warning.
        pulled = self._pulls(monkeypatch, AssertionError("pulled with no entry"))
        assert launch.stop_node_sessions(_config(api), ["api"]) == (["api"], [])
        assert pulled == []
        assert killed == ["api"]
        assert "not pulled" not in capsys.readouterr().out

    def test_an_unreadable_map_pulls_nothing(self, rig, api, monkeypatch, killed):
        nodes.NODE_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        nodes.NODE_MAP_PATH.write_text("{ torn", encoding="utf-8")
        pulled = self._pulls(monkeypatch, AssertionError("pulled a torn map"))
        launch.stop_node_sessions(_config(api), ["api"])
        assert pulled == []

    def test_a_retitled_project_is_pulled_by_its_map_key(
        self, rig, tmp_path, monkeypatch, killed
    ):
        # final_pull looks the entry up by the map key. Asked by the project's
        # CURRENT name it would find nothing and answer "never placed", and
        # the last turn would never come home.
        proj = ProjectConfig(path=str(tmp_path / "web"), title="my web", node="auto")
        sid = nodes.node_sid(proj)
        _hold("my.web", nick="third", sid=sid)
        pulled = self._pulls(monkeypatch)
        assert launch.stop_node_sessions(_config(proj), [sid]) == ([sid], [])
        assert pulled == ["my.web"]
        assert killed == [sid]
        assert nodes.read_node_map() == {}

    def test_a_pull_that_found_no_entry_is_not_a_pull(
        self, rig, api, monkeypatch, capsys, killed
    ):
        # `down` read the entry strictly; final_pull reads the map again and
        # finds no entry (another process unmapped it in between), so it
        # answers None -- "never placed". After a key `down` proved, that is
        # a pull that did not happen, never one that did.
        _hold("api")
        real = nodes.load_node_map_strict
        reads: list[None] = []

        def vanishing() -> dict[str, NodeMapEntry]:
            reads.append(None)
            return real() if len(reads) == 1 else {}

        monkeypatch.setattr(nodes, "load_node_map_strict", vanishing)
        assert launch.stop_node_sessions(_config(api), ["api"]) == (["api"], [])
        assert len(reads) > 1, "final_pull never read the map"
        assert killed == ["api"]
        assert "api" in real()
        out = capsys.readouterr().out
        assert out.count("\n") == 2, out  # the announcement, then this line
        assert (
            "api: last turn not pulled (its node map entry was not found again); "
        ) in out
        assert "magent node sync --once" in out

    def test_a_map_torn_under_the_real_final_pull_names_no_path(
        self, rig, api, monkeypatch, capsys, caplog, killed
    ):
        # No fake final_pull: `down`'s own strict read went through, then the
        # map tore before final_pull's. The line says the map could not be
        # read -- the parser's words and the map's path go to the log.
        from magent.log import get_logger

        get_logger("nodes")  # sets the level; caplog must come after
        caplog.set_level("WARNING", logger="magent.nodes")
        _hold("api")
        real = nodes.load_node_map_strict
        reads: list[None] = []

        def tearing() -> dict[str, NodeMapEntry]:
            reads.append(None)
            if len(reads) > 1:
                raise ValueError(f"{nodes.NODE_MAP_PATH}: Expecting value: line 1")
            return real()

        monkeypatch.setattr(nodes, "load_node_map_strict", tearing)
        assert launch.stop_node_sessions(_config(api), ["api"]) == (["api"], [])
        assert len(reads) > 1, "final_pull never read the map"
        assert killed == ["api"]
        assert "api" in real()
        out = capsys.readouterr().out
        assert out.count("\n") == 2, out  # the announcement, then this line
        assert (
            "api: last turn not pulled (the node map could not be read (ValueError)); "
        ) in out
        assert "node-map.json" not in out
        assert "Expecting value" not in out
        (logged,) = [
            r.getMessage()
            for r in caplog.records
            if "final pull of api failed" in r.getMessage()
        ]
        assert f"{nodes.NODE_MAP_PATH}: Expecting value" in logged

    def test_a_map_busy_under_the_real_final_pull_keeps_the_entry(
        self, rig, api, monkeypatch, capsys, caplog, killed
    ):
        # No fake final_pull: the real one, with the map a Windows reader
        # finds locked (a sync tick or an `up` replacing it) after `down`'s
        # own strict read went through. The OS's words go to the log only.
        from magent.log import get_logger

        get_logger("nodes")  # sets the level; caplog must come after
        caplog.set_level("WARNING", logger="magent.nodes")
        _hold("api")
        real_read = Path.read_text
        reads: list[None] = []
        busy = [True]

        def read_text(self: Path, *a: object, **k: object) -> str:
            if self == nodes.NODE_MAP_PATH and busy[0]:
                reads.append(None)
                if len(reads) > 1:
                    raise PermissionError(13, "The process cannot access the file")
            return real_read(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", read_text)
        assert launch.stop_node_sessions(_config(api), ["api"]) == (["api"], [])
        busy[0] = False
        assert len(reads) > 1, "final_pull never read the map"
        assert killed == ["api"]
        assert "api" in nodes.load_node_map_strict()
        out = capsys.readouterr().out
        assert (
            "api: last turn not pulled (the node map could not be read"
            " (PermissionError)); "
        ) in out
        assert "cannot access" not in out
        (logged,) = [
            r.getMessage()
            for r in caplog.records
            if "final pull of api failed" in r.getMessage()
        ]
        assert "PermissionError" in logged
        assert "The process cannot access the file" in logged

    def test_an_unreadable_map_is_not_the_nodes_failure(
        self, rig, tmp_path, monkeypatch, killed
    ):
        # A hung node's later sessions skip their pull; a map that could not
        # be read once is no fact about the node, so its next session is
        # still pulled -- and only the one that was not keeps its entry.
        a1, a2 = (
            ProjectConfig(path=str(tmp_path / n), node="second") for n in ("a1", "a2")
        )
        _hold("a1")
        _hold("a2")
        pulled: list[str] = []

        def pull(config: object, name: str, **_k: object) -> remote_mux.PullResult:
            pulled.append(name)
            if name == "a1":
                raise node_sync.NodeMapUnreadable(
                    nodes.map_unread_text(PermissionError(13, "busy"))
                )
            return _PULLED

        monkeypatch.setattr(node_sync, "final_pull", pull)
        assert launch.stop_node_sessions(_config(a1, a2), ["a1", "a2"]) == (
            ["a1", "a2"],
            [],
        )
        assert pulled == ["a1", "a2"]
        assert killed == ["a1", "a2"]
        assert set(nodes.read_node_map()) == {"a1"}

    @pytest.mark.parametrize("timed_out", [True, False], ids=["silent", "rc255"])
    def test_a_node_that_did_not_answer_the_pull_is_not_pulled_again(
        self, rig, tmp_path, monkeypatch, capsys, killed, timed_out
    ):
        # One pull timeout per node, not one per project on it -- but every
        # session there is still killed, and each is told it was not pulled.
        a1, a2 = (
            ProjectConfig(path=str(tmp_path / n), node="second") for n in ("a1", "a2")
        )
        _hold("a1")
        _hold("a2")
        pulled = self._pulls(monkeypatch, _unreachable(timed_out))
        assert launch.stop_node_sessions(_config(a1, a2), ["a1", "a2"]) == (
            ["a1", "a2"],
            [],
        )
        assert pulled == ["a1"]
        assert killed == ["a1", "a2"]
        assert set(nodes.read_node_map()) == {"a1", "a2"}
        out = capsys.readouterr().out
        # The first says what ssh said (rc 255 is a refused key too); the
        # rest only that the node did not answer.
        first = _unreachable(timed_out).stderr_tail
        assert f"a1: last turn not pulled ({first})" in out
        assert "a2: last turn not pulled (node second did not answer the pull)" in out

    @pytest.mark.parametrize(
        "error",
        [
            RemoteError(None, "reply exceeded 8 bytes", ("ssh",), over_cap=True),
            RemoteError(0, "could not store every pulled file", ("pull.sh",)),
            OSError(28, "No space left on device"),
            lockfile.LockHeld("node-marks lock is held by another process"),
            nodes.NodeConfigError("node 'second' is not in settings.nodes"),
        ],
        ids=["over-cap", "unstored", "disk", "other-lock", "misconfigured"],
    )
    def test_a_node_that_answered_is_pulled_for_its_next_session(
        self, rig, tmp_path, monkeypatch, killed, error
    ):
        # Reachability reads timed_out and ssh's rc 255 only: a reply over the
        # cap is a node that answered (outcome_unknown is for mutations). Of
        # the OSErrors only NodeLockHeld is the node's: a plain LockHeld is some
        # other lock taken inside the pull (node_sync.NodeLockHeld's own
        # docstring), and this PC failing to write a pulled file is this
        # session's. So is a config error.
        a1, a2 = (
            ProjectConfig(path=str(tmp_path / n), node="second") for n in ("a1", "a2")
        )
        _hold("a1")
        _hold("a2")
        pulled = self._pulls(monkeypatch, error)
        launch.stop_node_sessions(_config(a1, a2), ["a1", "a2"])
        assert pulled == ["a1", "a2"]

    def test_a_node_whose_pull_lock_is_held_is_waited_on_once(
        self, rig, tmp_path, monkeypatch, capsys, killed
    ):
        # Another magent process holds node-pull-second past the wait (a sync
        # tick, a bring-up, another down): the first session pays that wait,
        # the node's other sessions do not -- N sessions, one FINAL_PULL_WAIT_S.
        # The real final_pull, so the wait counted is the lock's own.
        waits: list[object] = []

        @contextlib.contextmanager
        def held(nick: str, **k: object) -> Iterator[None]:
            waits.append(k.get("wait_s"))
            raise node_sync.NodeLockHeld(
                f"node-pull-{nick} lock is held by another process"
            )
            yield

        monkeypatch.setattr(node_sync, "node_lock", held)
        a1, a2 = (
            ProjectConfig(path=str(tmp_path / n), node="second") for n in ("a1", "a2")
        )
        _hold("a1")
        _hold("a2")
        assert launch.stop_node_sessions(_config(a1, a2), ["a1", "a2"]) == (
            ["a1", "a2"],
            [],
        )
        assert waits == [node_sync.FINAL_PULL_WAIT_S]
        assert killed == ["a1", "a2"]
        assert set(nodes.read_node_map()) == {"a1", "a2"}
        out = capsys.readouterr().out
        busy = (
            "(node second is busy: another magent process held its pull lock past 122s)"
        )
        assert f"a1: last turn not pulled {busy}" in out
        assert f"a2: last turn not pulled {busy}" in out
        assert "node-pull-" not in out

    def test_a_node_that_failed_its_pull_does_not_stop_another_nodes(
        self, rig, tmp_path, monkeypatch, killed
    ):
        a1, b1 = (
            ProjectConfig(path=str(tmp_path / n), node=nick)
            for n, nick in (("a1", "second"), ("b1", "third"))
        )
        _hold("a1")
        _hold("b1", nick="third")
        pulled = self._pulls(monkeypatch, _unreachable(True))
        launch.stop_node_sessions(_config(a1, b1), ["a1", "b1"])
        assert pulled == ["a1", "b1"]


class TestABringUpHoldsTheNodesSyncLock:
    """Spec section 13: the daemon takes the same per-node lock, so a sync
    tick never races a bring-up -- across PROCESSES, which the threading lock
    cannot cover."""

    def test_the_bring_up_runs_inside_node_lock(self, rig, api, monkeypatch):
        held: list[str] = []
        waits: list[object] = []

        @contextlib.contextmanager
        def lock(nick: str, **k: object) -> Iterator[None]:
            waits.append(k.get("wait_s"))
            held.append(nick)
            yield
            held.append("released")

        inside: list[list[str]] = []
        real = rig._bring_up
        monkeypatch.setattr(node_sync, "node_lock", lock)
        monkeypatch.setattr(
            remote_mux,
            "bring_up",
            lambda node, recipe, **k: (
                inside.append(list(held)) or real(node, recipe, **k)
            ),
        )
        assert launch.bring_up_node_project(_config(api), api).ok
        assert inside == [["second"]]
        assert held == ["second", "released"]
        # DECISION-19: a bring-up waits one pull, then says the node is busy.
        assert waits == [remote_mux.PULL_TIMEOUT_S]

    def test_a_node_busy_syncing_is_a_named_outcome(self, rig, api, monkeypatch):
        @contextlib.contextmanager
        def busy(nick: str, **_k: object) -> Iterator[None]:
            raise node_sync.NodeLockHeld(
                f"node-pull-{nick} lock is held by another process"
            )
            yield

        monkeypatch.setattr(node_sync, "node_lock", busy)
        outcome = launch.bring_up_node_project(_config(api), api)
        assert not outcome.ok
        # Not "a node sync pull": a bring-up or a down holds this lock too.
        assert outcome.error == (
            "node second is busy: another magent process held its pull lock past"
            " 120s; re-run to try again"
        )
        assert rig.recipes == []
        assert nodes.read_node_map() == {}


class TestABringUpKeepsTheSyncDaemonRunning:
    @pytest.fixture
    def ensured(self, monkeypatch):
        seen: list[str | None] = []
        monkeypatch.setattr(
            launch,
            "ensure_node_sync",
            lambda config, config_path=None: seen.append(config_path) or True,
        )
        monkeypatch.setattr("magent.psmux.bring_up", lambda cfg, only, group: ([], []))
        return seen

    def test_up_starts_it_on_the_same_config_file(self, rig, api, ensured):
        launch.bring_up_psmux(_config(api), config_path="/cfg/magent.config.json")
        assert ensured == ["/cfg/magent.config.json"]

    def test_a_bring_up_with_nothing_up_starts_nothing(
        self, rig, api, ensured, tmp_path
    ):
        rig.states[tmp_path / "api"] = _state(tmp_path / "api", dirty=True)
        launch.bring_up_psmux(_config(api))
        assert ensured == []

    def test_a_config_without_node_projects_never_asks(self, rig, ensured):
        launch.bring_up_psmux(_config())
        assert ensured == []

    def test_go_starts_it_too(self, rig, api, ensured, desk, no_sleep):
        launch.run_magent(_config(api), launch.RunOpts(config_path="/cfg/m.json"))
        assert ensured == ["/cfg/m.json"]

    def test_go_with_nothing_up_starts_nothing(
        self, rig, api, ensured, desk, no_sleep, tmp_path
    ):
        rig.states[tmp_path / "api"] = _state(tmp_path / "api", dirty=True)
        launch.run_magent(_config(api), launch.RunOpts(config_path="/cfg/m.json"))
        assert ensured == []

    def test_go_without_a_config_file_passes_none(
        self, rig, api, ensured, desk, no_sleep
    ):
        launch.run_magent(_config(api), launch.RunOpts())
        assert ensured == [None]

    @pytest.fixture(params=["spawn-refused", "lock-unknown"])
    def cannot_start(self, monkeypatch, caplog, request):
        # The spawn refused, or the daemon's lock file would not open (whether
        # one runs is unknown): after the sessions came up, which must still
        # be reported.
        denied = PermissionError(13, "Access is denied")
        error: Exception = (
            denied
            if request.param == "spawn-refused"
            else node_sync.DaemonLockUnknown(denied)
        )

        def ensure(config: object, config_path: object = None) -> bool:
            raise error

        monkeypatch.setattr(launch, "ensure_node_sync", ensure)
        monkeypatch.setattr("magent.psmux.bring_up", lambda cfg, only, group: ([], []))
        from magent.log import get_logger

        get_logger("nodes")  # sets the level; caplog must come after
        caplog.set_level("WARNING", logger="magent.nodes")
        return lambda: [
            r.getMessage() for r in caplog.records if r.name == "magent.nodes"
        ]

    def test_up_survives_a_daemon_that_cannot_start(self, rig, api, cannot_start):
        # An escaping error is a failed ASSERTION here, not a crash of the
        # test: a guard gone and a broken rig must not read the same.
        try:
            got: object = launch.bring_up_psmux(_config(api))
        except (OSError, node_sync.DaemonLockUnknown) as exc:
            got = exc
        assert got == (["api"], [])
        assert any("node sync daemon not started" in m for m in cannot_start())

    def test_go_survives_a_daemon_that_cannot_start(
        self, rig, api, cannot_start, desk, no_sleep
    ):
        try:
            got: object = launch.run_magent(_config(api), launch.RunOpts())
        except (OSError, node_sync.DaemonLockUnknown) as exc:
            got = exc
        assert got == 0
        assert any("node sync daemon not started" in m for m in cannot_start())

    def test_the_up_command_hands_it_the_file_it_read(
        self, rig, api, tmp_path, monkeypatch
    ):
        seen: list[str | None] = []
        monkeypatch.setattr(
            launch,
            "ensure_node_sync",
            lambda config, config_path=None: seen.append(config_path) or True,
        )
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: None)
        config = TestUpCommandBringsNodeProjectsUp()._config_file(
            tmp_path, tmp_path / "api"
        )
        result = CliRunner().invoke(cli.main, ["--config", config, "up"])
        assert result.exit_code == 0, result.output
        assert seen == [str(Path(config))]


def _web(tmp_path: Path, rig: NodeRig) -> ProjectConfig:
    folder = tmp_path / "web"
    folder.mkdir()
    rig.states[folder] = _state(folder)
    return ProjectConfig(path=str(folder), node="second")


class TestTheBringUpProvisionsFirst:
    def test_the_node_is_provisioned_before_the_session_comes_up(
        self, rig, api, fake_ssh, monkeypatch
    ):
        from tests.unit.test_node_provision import _sent, _unpack

        monkeypatch.setattr(remote_mux, "provision_node", real_provision_node)
        # conftest points the home at tmp: the scope must be built from IT.
        settings = Path.home() / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
        seen_at_bring_up: list[int] = []

        def bring_up(node, recipe, **kw):
            seen_at_bring_up.append(len(fake_ssh.calls()))
            return rig._bring_up(node, recipe, **kw)

        monkeypatch.setattr(remote_mux, "bring_up", bring_up)
        outcome = launch.bring_up_node_project(_config(api), api)
        assert outcome.ok
        assert seen_at_bring_up == [1]
        (call,) = fake_ssh.calls()
        assert any("devino-second" in arg for arg in call.argv)
        # A bring-up provisions what changed; only `node setup` forces.
        assert "--force" not in call.argv[-1]
        assert call.stdin.startswith(node_scripts.script("provision").encode("utf-8"))
        _, _, data = _unpack(_sent(call))
        assert json.loads(data["settings.json"]) == {"model": "opus"}
        assert "second" in launch._PROVISIONED

    def test_provisioning_gets_its_own_budget(self, rig, api):
        assert launch.bring_up_node_project(_config(api), api).ok
        assert rig.provision_timeouts == [remote_mux.PROVISION_TIMEOUT_S]

    def test_a_second_project_on_the_same_node_does_not_provision_again(
        self, rig, api, fake_ssh, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(remote_mux, "provision_node", real_provision_node)
        web = _web(tmp_path, rig)
        config = _config(api, web)
        assert launch.bring_up_node_project(config, api).ok
        assert launch.bring_up_node_project(config, web).ok
        assert len(fake_ssh.calls()) == 1
        assert [nick for nick, _ in rig.recipes] == ["second", "second"]

    def test_an_unreachable_node_fails_only_that_project_and_is_retried(
        self, rig, api, fake_ssh, monkeypatch, tmp_path, caplog
    ):
        monkeypatch.setattr(remote_mux, "provision_node", real_provision_node)
        fake_ssh.set_reply(
            "bash -s", stderr="ssh: connect to host devino-second: No route\n", rc=255
        )
        web = _web(tmp_path, rig)
        config = _config(api, web)
        with caplog.at_level(logging.WARNING, logger="magent.nodes"):
            outcome = launch.bring_up_node_project(config, api)
        assert not outcome.ok
        assert outcome.error == (
            "provisioning: ssh: connect to host devino-second: No route"
        )
        assert rig.recipes == []
        assert "second" not in launch._PROVISIONED
        assert any(
            message.startswith("provision @second: failed (")
            for _, level, message in caplog.record_tuples
            if level == logging.WARNING
        )
        # The node answers again: the NEXT project on it provisions, rather
        # than taking the failed attempt as done.
        (fake_ssh.base / "replies.json").unlink()
        assert launch.bring_up_node_project(config, web).ok
        assert len(fake_ssh.calls()) == 2
        assert "second" in launch._PROVISIONED

    def test_a_timed_out_provision_fails_that_project_and_is_never_retried(
        self, rig, api, fake_ssh, monkeypatch, tmp_path, caplog
    ):
        # A timeout proves the node answered, but not what the apply did: it
        # may still be running there, so a retry would start a second one.
        attempts: list[str] = []

        def provision_node(node, config, **kw):
            attempts.append(node.nick)
            return real_provision_node(node, config, **kw)

        monkeypatch.setattr(remote_mux, "provision_node", provision_node)
        monkeypatch.setattr(remote_mux, "PROVISION_TIMEOUT_S", 1.0)
        fake_ssh.set_mode("timeout")
        web = _web(tmp_path, rig)
        config = _config(api, web)
        with caplog.at_level(logging.WARNING, logger="magent.nodes"):
            first = launch.bring_up_node_project(config, api)
        assert not first.ok
        assert first.error == "provisioning: timed out after 1s"
        assert rig.recipes == []
        assert "second" in launch._PROVISIONED
        assert any(
            message.startswith("provision @second: outcome unknown (")
            and message.endswith("); not retrying this run")
            for _, level, message in caplog.record_tuples
            if level == logging.WARNING
        )
        # Counted at the seam: on Windows the fake's recorder is orphaned when
        # a timeout kills it, so fake_ssh.calls() undercounts.
        assert launch.bring_up_node_project(config, web).ok
        assert attempts == ["second"]
        assert [nick for nick, _ in rig.recipes] == ["second"]

    def test_an_over_cap_provision_fails_that_project_and_is_never_retried(
        self, rig, api, monkeypatch, tmp_path, caplog
    ):
        # RemoteError.outcome_unknown, not timed_out alone: an over-cap reply
        # killed the local ssh mid-apply too, and a retry would start a second
        # applier beside the first.
        attempts: list[str] = []

        def provision_node(node, config, **kw):
            attempts.append(node.nick)
            raise RemoteError(None, "reply exceeded 8 bytes", ("ssh",), over_cap=True)

        monkeypatch.setattr(remote_mux, "provision_node", provision_node)
        web = _web(tmp_path, rig)
        config = _config(api, web)
        with caplog.at_level(logging.WARNING, logger="magent.nodes"):
            first = launch.bring_up_node_project(config, api)
        assert not first.ok
        assert first.error == "provisioning: reply exceeded 8 bytes"
        assert "second" in launch._PROVISIONED
        assert any(
            message.startswith("provision @second: outcome unknown (")
            for _, level, message in caplog.record_tuples
            if level == logging.WARNING
        )
        assert launch.bring_up_node_project(config, web).ok
        assert attempts == ["second"]
        assert [nick for nick, _ in rig.recipes] == ["second"]

    def test_a_fail_row_is_logged_and_the_session_still_comes_up(
        self, rig, api, fake_ssh, monkeypatch, caplog
    ):
        monkeypatch.setattr(remote_mux, "provision_node", real_provision_node)
        fake_ssh.set_reply(
            "bash -s",
            stdout=(
                "ok\tgit\tgit 2.43\n"
                "warn\tplugin\tmarketplace slow\n"
                "skip\tsettings\tunchanged\n"
                "fail\tgh\tgh is not installed\n"
            ),
            rc=1,
        )
        with caplog.at_level(logging.DEBUG, logger="magent.nodes"):
            outcome = launch.bring_up_node_project(_config(api), api)
        assert outcome.ok
        assert [nick for nick, _ in rig.recipes] == ["second"]
        assert "second" in launch._PROVISIONED
        # The warn and fail rows are news for nodes.log, in the node's order;
        # ok/skip rows are node doctor's. The screen shows none of them.
        assert [
            (logger, level, message)
            for logger, level, message in caplog.record_tuples
            if message.startswith("provision second:")
        ] == [
            (
                "magent.nodes",
                logging.WARNING,
                "provision second: warn plugin: marketplace slow",
            ),
            (
                "magent.nodes",
                logging.WARNING,
                "provision second: fail gh: gh is not installed",
            ),
        ]

    def test_this_pcs_gh_warn_row_reaches_nodes_log_on_the_go_path(
        self, rig, api, fake_ssh, fake_gh, monkeypatch, caplog, capsys
    ):
        # The gh row is this PC's, not the node's: a login gh could not verify
        # (offline) is shared anyway, and says so. `node setup` prints it; a
        # bring-up keeps F17's quiet screen, so nodes.log is where it lands.
        token = "gho_FAKE0123456789abcdefTOKEN"
        monkeypatch.setattr(remote_mux, "provision_node", real_provision_node)
        fake_gh.set_reply(
            "auth status",
            stdout=gh_auth_status(None, accounts=[("amin", True, "timeout")]),
        )
        fake_gh.set_reply("auth token", stdout=token + "\n")
        with caplog.at_level(logging.WARNING, logger="magent.nodes"):
            outcome = launch.bring_up_node_project(_config(api), api)
        assert outcome.ok
        assert (
            "magent.nodes",
            logging.WARNING,
            f"provision second: warn gh: {remote_mux.GH_SHARED_UNVERIFIED}",
        ) in caplog.record_tuples
        assert token not in caplog.text
        assert remote_mux.GH_SHARED_UNVERIFIED not in capsys.readouterr().out

    def test_dry_run_provisions_nothing(
        self, rig, api, fake_ssh, desk, no_sleep, capsys
    ):
        # Plan F Task 12A: the preview says what the real run would do first,
        # and neither provisions nor dials anything.
        assert launch.run_magent(_config(api), launch.RunOpts(dry_run=True)) == 0
        assert "would provision second" in capsys.readouterr().out
        assert rig.provisioned == []
        assert fake_ssh.calls() == []


class TestTextWithNoUtf8FormReachesTheRowInOurWords:
    """A command or a project title with no UTF-8 form (json reads a
    ``\\udXXX`` escape into one) cannot be framed for the node. The row says
    so in our words and the class; the codec's own words -- Python's, naming
    the character and its position -- are nodes.log's, at WARNING, and no
    line is an ERROR. The real bring-up and decoration run, through THE fake
    ssh."""

    @pytest.fixture
    def real_node(self, rig, fake_ssh, monkeypatch, caplog):
        from magent.log import get_logger

        monkeypatch.setattr(remote_mux, "bring_up", real_bring_up)
        monkeypatch.setattr(remote_mux, "decorate", real_decorate)
        monkeypatch.setattr(remote_mux, "PROBE_TIMEOUT_S", 60.0)
        monkeypatch.setattr(psmux, "code_on_path", lambda: False)
        fake_ssh.set_reply("printenv HOME", stdout="/home/amin\n")
        get_logger("nodes")  # sets the level; caplog must come after
        caplog.set_level("WARNING", logger="magent.nodes")
        return fake_ssh

    @staticmethod
    def _logged(caplog) -> list[str]:
        records = [r for r in caplog.records if r.name == "magent.nodes"]
        assert [r for r in records if r.levelno >= logging.ERROR] == []
        return [r.getMessage() for r in records]

    @staticmethod
    def _nothing_sent(fake_ssh) -> None:
        # The HOME probe, if it ran, is the only call: a refusal hoisted
        # before it passes too.
        calls = [" ".join(c.argv) for c in fake_ssh.calls()]
        assert [c for c in calls if "printenv HOME" not in c] == []

    def test_a_command_with_no_utf_8_form(self, real_node, api, caplog):
        base = _config(api)
        config = dataclasses.replace(
            base,
            settings=dataclasses.replace(
                base.settings, tools={"claude": "claude --continue \ud83d"}
            ),
        )
        outcome = launch.bring_up_node_project(config, api)
        assert (outcome.ok, outcome.error) == (
            False,
            (
                "the project's repo, node folder or command has text with no "
                "UTF-8 form (UnicodeEncodeError)"
            ),
        )
        (failed,) = self._logged(caplog)
        assert "bring-up of api failed: " in failed
        assert failed.endswith("surrogates not allowed")
        self._nothing_sent(real_node)

    def test_a_title_with_no_utf_8_form(self, real_node, api, caplog):
        titled = dataclasses.replace(api, title="api\ud83d")
        outcome = launch.bring_up_node_project(_config(titled), titled)
        assert (outcome.ok, outcome.error) == (
            False,
            (
                "the project's session name has text with no UTF-8 form "
                "(UnicodeEncodeError)"
            ),
        )
        (failed,) = self._logged(caplog)
        assert failed.endswith("surrogates not allowed")
        self._nothing_sent(real_node)

    def test_the_failed_line_lands_escaped_in_nodes_log(
        self, real_node, api, tmp_path, capsys
    ):
        # The sid reaches that WARNING raw (%s). f512ba7's handler is what
        # writes it as escape text on disk; a strict one would drop the line.
        from magent import log

        # real_node's get_logger made LOG_DIR already. That was safe: conftest's
        # autouse _isolate_magent_home, which points LOG_DIR here, runs first.
        assert log.LOG_DIR.is_relative_to(tmp_path)
        titled = dataclasses.replace(api, title="api\ud83d")
        assert not launch.bring_up_node_project(_config(titled), titled).ok
        path = log.LOG_DIR / "nodes.log"
        logged = path.read_text(encoding="utf-8") if path.exists() else ""
        lines = [line for line in logged.splitlines() if "bring-up of" in line]
        assert len(lines) == 1, logged
        assert " WARNING " in lines[0]
        assert (
            "node second: bring-up of api\\ud83d failed: the project's session "
            "name has text with no UTF-8 form (UnicodeEncodeError): "
        ) in lines[0]
        assert "Logging error" not in capsys.readouterr().err

    def test_attaching_to_a_session_so_named_still_attaches(
        self, real_node, rig, api, tmp_path, caplog
    ):
        # Decoration is cosmetic: one that cannot be sent never fails the
        # attach it rides, and never reaches the row.
        titled = dataclasses.replace(api, title="api\ud83d")
        _hold("api\ud83d")
        rig.live = True
        rig.states[tmp_path / "api"] = _state(tmp_path / "api", dirty=True)
        outcome = launch.bring_up_node_project(_config(titled), titled)
        assert (outcome.ok, outcome.attached_existing, outcome.error) == (
            True,
            True,
            None,
        )
        assert not [w for w in outcome.warnings if "codec" in w]
        (message,) = self._logged(caplog)
        assert message.startswith(
            "decoration of 'api\\ud83d' on second not sent (UnicodeEncodeError): "
        )
        assert real_node.calls() == []
