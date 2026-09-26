"""The launch-side half of running a project on a pool node (PR-D): the D7
refusals, the per-node lock, the map write, the window -- with the node itself
faked at remote_mux's seam, so nothing here dials anything."""

from __future__ import annotations

import dataclasses
import errno
import logging
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from magent import attach_client, launch, lockfile, nodes, remote_mux
from magent.config import MagentConfig, NodeConfig, ProjectConfig, Settings
from magent.nodes import LocalGitState, NodeMapEntry
from magent.remote_mux import BringUpResult, RemoteError
from tests.conftest import FakePlatform

if TYPE_CHECKING:
    import os

_TOOLS = {"claude": "claude --continue"}


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
        _hold("second")
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
        self, rig, api, tmp_path
    ):
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
        outcome = launch.bring_up_node_project(_config(api), api)
        assert (outcome.ok, outcome.attached_existing) == (True, True)
        assert any("--allow-dirty" in w for w in outcome.warnings)
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
        x_api, y_api, _web = _twin_apis(tmp_path, rig)
        y_api.node = "auto"
        nodes.update_node_map(
            "api-y",
            NodeMapEntry(
                nick="third",
                sid="api-y",
                placed_ts=1.0,
                attached_existing=False,
                remote_root="~/magent/api",
                target="amin@devino-third",
            ),
        )
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
        self, rig, tmp_path, monkeypatch
    ):
        # One node project's folder this user may not stat (another profile, a
        # deny ACL) must not take `up` of any other project down with it --
        # the fleet scan reads every folder, asked for or not.
        (good,) = _projects(tmp_path, rig, [("a1", "second")])
        locked = tmp_path / "locked" / "z"
        locked.mkdir(parents=True)
        rig.states[locked] = _state(locked)
        z = ProjectConfig(path=str(locked), node="second")
        real_stat = Path.stat

        def stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
            if str(self) == str(locked):
                raise PermissionError(errno.EACCES, "Access is denied", str(self))
            return real_stat(self, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(Path, "stat", stat)
        alone = _batch(_config(good, z), only=["a1"])
        assert [(o.sid, o.ok) for o in alone] == [("a1", True)]
        both = _batch(_config(good, z))
        assert [(o.sid, o.ok) for o in both] == [("a1", True), ("z", False)]
        assert "Access is denied" in (both[1].error or "")

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


def _hold(nick: str) -> None:
    nodes.update_node_map(
        "api",
        NodeMapEntry(
            nick=nick,
            sid="api",
            placed_ts=1.0,
            attached_existing=False,
            remote_root="~/magent/api",
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
        _hold("third")
        rig.live = True
        rig.states[tmp_path / "api"] = _state(tmp_path / "api", dirty=True)
        outcome = launch.bring_up_node_project(_config(api), api)
        assert (outcome.ok, outcome.attached_existing) == (False, False)
        assert rig.decorated == []

    def test_a_probe_that_failed_is_not_a_live_session(self, rig, api, tmp_path):
        _hold("second")
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
        _hold("second")
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
        # so the outcome is ok with a repair warning naming the cause (6688a69).
        assert (outcome.ok, outcome.node, outcome.error) == (True, "second", None)
        (warning,) = [w for w in outcome.warnings if "not recorded" in w]
        assert "held by another writer" in warning
        assert not launch._bring_up_lock("second").locked()

    def test_an_unreadable_push_file_is_an_outcome(self, rig, api):
        rig.error = PermissionError(13, "Permission denied")
        outcome = launch.bring_up_node_project(_config(api), api)
        assert outcome.ok is False
        assert "Permission denied" in (outcome.error or "")

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
