"""The launch-side half of running a project on a pool node (PR-D): the D7
refusals, the per-node lock, the map write, the window -- with the node itself
faked at remote_mux's seam, so nothing here dials anything."""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

import pytest

from magent import attach_client, launch, lockfile, node_scripts, nodes, remote_mux
from magent.config import MagentConfig, NodeConfig, ProjectConfig, Settings
from magent.nodes import LocalGitState, NodeMapEntry
from magent.remote_mux import BringUpResult, RemoteError
from tests.conftest import FakePlatform

# The real body, captured before any NodeRig replaces it: the tests below put
# it back to drive the whole chain through THE fake ssh.
real_provision_node = remote_mux.provision_node

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
        self.provisioned: list[str] = []
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


def _web(tmp_path: Path, rig: NodeRig) -> ProjectConfig:
    folder = tmp_path / "web"
    folder.mkdir()
    rig.states[folder] = _state(folder)
    return ProjectConfig(path=str(folder), node="second")


class TestTheBringUpProvisionsFirst:
    def test_the_node_is_provisioned_before_the_session_comes_up(
        self, rig, api, fake_ssh, monkeypatch
    ):
        monkeypatch.setattr(remote_mux, "provision_node", real_provision_node)
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
        assert call.stdin.startswith(node_scripts.script("provision").encode("utf-8"))
        assert "second" in launch._PROVISIONED

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
        self, rig, api, fake_ssh, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(remote_mux, "provision_node", real_provision_node)
        fake_ssh.set_reply(
            "bash -s", stderr="ssh: connect to host devino-second: No route\n", rc=255
        )
        web = _web(tmp_path, rig)
        config = _config(api, web)
        outcome = launch.bring_up_node_project(config, api)
        assert not outcome.ok
        assert "No route" in (outcome.error or "")
        assert rig.recipes == []
        assert "second" not in launch._PROVISIONED
        # The node answers again: the NEXT project on it provisions, rather
        # than taking the failed attempt as done.
        (fake_ssh.base / "replies.json").unlink()
        assert launch.bring_up_node_project(config, web).ok
        assert len(fake_ssh.calls()) == 2
        assert "second" in launch._PROVISIONED

    def test_a_fail_row_is_logged_and_the_session_still_comes_up(
        self, rig, api, fake_ssh, monkeypatch, caplog
    ):
        monkeypatch.setattr(remote_mux, "provision_node", real_provision_node)
        fake_ssh.set_reply("bash -s", stdout="fail\tgh\tgh is not installed\n", rc=1)
        with caplog.at_level(logging.WARNING, logger="magent.nodes"):
            outcome = launch.bring_up_node_project(_config(api), api)
        assert outcome.ok
        assert [nick for nick, _ in rig.recipes] == ["second"]
        assert "second" in launch._PROVISIONED
        assert (
            "magent.nodes",
            logging.WARNING,
            "provision second: gh: gh is not installed",
        ) in caplog.record_tuples

    # D-MERGE: test_dry_run_provisions_nothing (plan F Task 12A) lands with
    # sub-plan D's Task 12: it needs D12's `desk` / `no_sleep` fixtures and its
    # "would provision <nick>" dry-run line, neither of which exists yet.
