"""The launch-side half of running a project on a pool node (PR-D): the D7
refusals, the per-node lock, the map write, the window -- with the node itself
faked at remote_mux's seam, so nothing here dials anything."""

from __future__ import annotations

import threading
import time
from collections import defaultdict
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


def _no_node_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly on anything a bring-up does past the batch check: the ssh
    calls, the local git read, provisioning."""

    def forbidden(*_a: object, **_k: object) -> None:
        raise AssertionError("a colliding batch must refuse before any bring-up")

    for name in ("bring_up", "has_session", "decorate"):
        monkeypatch.setattr(remote_mux, name, forbidden)
    monkeypatch.setattr(launch, "node_git_states", forbidden)
    monkeypatch.setattr(launch, "_provision_once", forbidden)


def _twin_apis(tmp_path: Path, rig: NodeRig) -> list[ProjectConfig]:
    """Two projects whose LOCAL folders share the leaf name ``api`` -- on two
    different nodes, because the check is fleet-wide (auto may co-locate
    them later) -- plus a bystander with a folder of its own."""
    out = []
    for parent, title, nick in (("x", "api-x", "second"), ("y", "api-y", "third")):
        folder = tmp_path / parent / "api"
        folder.mkdir(parents=True)
        rig.states[folder] = _state(folder)
        out.append(ProjectConfig(path=str(folder), node=nick, title=title))
    (bystander,) = _projects(tmp_path, rig, [("web", "second")])
    return [*out, bystander]


def _batch(config: MagentConfig) -> list[launch.NodeBringUpOutcome]:
    """``bring_up_node_projects`` with its contract as an assertion: a batch
    never raises -- a project the check cannot place is an outcome, not a
    crash that takes its siblings down with it."""
    try:
        return launch.bring_up_node_projects(config)
    except Exception as exc:
        raise AssertionError(f"a node batch must never raise: {exc!r}") from exc


class TestTwoProjectsThatWouldShareANodeFolderAreRefusedFirst:
    """X3: ``nodes.assert_distinct_remote_roots`` runs ONCE over the whole
    batch, before the fan-out -- one clone would otherwise overwrite the
    other's folder, and which one wins would depend on thread timing."""

    def test_a_colliding_batch_refuses_before_any_ssh(self, rig, tmp_path, monkeypatch):
        projs = _twin_apis(tmp_path, rig)
        _no_node_contact(monkeypatch)
        outcomes = launch.bring_up_node_projects(_config(*projs))
        assert [(o.sid, o.ok) for o in outcomes] == [
            ("api-x", False),
            ("api-y", False),
            ("web", False),
        ]
        for o in outcomes:
            assert o.error is not None
            assert "'api-x' and 'api-y' would share the node folder name 'api'" in (
                o.error
            )
            assert "rename one of them" in o.error
        assert [o.node for o in outcomes] == ["second", "third", "second"]
        assert rig.recipes == []
        assert nodes.read_node_map() == {}

    def test_the_check_runs_once_over_every_project_in_the_batch(
        self, rig, tmp_path, monkeypatch
    ):
        projs = _projects(
            tmp_path, rig, [("a1", "second"), ("b1", "third"), ("a2", "second")]
        )
        calls: list[list[str]] = []
        real = nodes.assert_distinct_remote_roots

        def spy(recipes):
            calls.append([r.remote_root for r in recipes])
            real(recipes)

        monkeypatch.setattr(nodes, "assert_distinct_remote_roots", spy)
        outcomes = launch.bring_up_node_projects(_config(*projs))
        assert all(o.ok for o in outcomes)
        assert calls == [["~/magent/a1", "~/magent/b1", "~/magent/a2"]]

    def test_up_prints_the_collision_and_counts_every_project_failed(
        self, rig, tmp_path, monkeypatch, capsys
    ):
        projs = _twin_apis(tmp_path, rig)
        _no_node_contact(monkeypatch)
        monkeypatch.setattr("magent.psmux.bring_up", lambda cfg, only, group: ([], []))
        assert launch.bring_up_psmux(_config(*projs)) == (
            [],
            ["api-x", "api-y", "web"],
        )
        out = capsys.readouterr().out
        assert "api-x: projects 'api-x' and 'api-y' would share" in out

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

    def test_an_unreadable_map_still_checks_the_pinned_projects(
        self, rig, tmp_path, monkeypatch
    ):
        # The map only places ``auto`` projects; a pinned pair collides with or
        # without it, so a broken map must not switch the check off.
        projs = _twin_apis(tmp_path, rig)
        _no_node_contact(monkeypatch)

        def broken(*_a: object, **_k: object) -> dict[str, NodeMapEntry]:
            raise ValueError("node-map.json: not valid JSON")

        monkeypatch.setattr(nodes, "read_node_map", broken)
        outcomes = _batch(_config(*projs))
        assert [o.ok for o in outcomes] == [False, False, False]
        assert all(
            "would share the node folder name 'api'" in (o.error or "")
            for o in outcomes
        )


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

    def test_node_session_ids_follow_the_group_filter(self, rig, tmp_path):
        a = ProjectConfig(path=str(tmp_path / "a"), node="second", group="work")
        b = ProjectConfig(path=str(tmp_path / "b"), node="second")
        assert launch.node_session_ids(_config(a, b), group="WORK") == ["a"]
