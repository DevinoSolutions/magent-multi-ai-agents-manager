"""The launch-side half of running a project on a pool node (PR-D): the D7
refusals, the per-node lock, the map write, the window -- with the node itself
faked at remote_mux's seam, so nothing here dials anything."""

from __future__ import annotations

from pathlib import Path

import pytest

from magent import attach_client, launch, lockfile, nodes, remote_mux
from magent.config import MagentConfig, NodeConfig, ProjectConfig, Settings
from magent.nodes import LocalGitState, NodeMapEntry
from magent.remote_mux import BringUpResult, RemoteError
from tests.conftest import FakePlatform

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
