"""magent node: G's surfaces -- the table, plan, push and recall.

F16 creates this module too (node doctor). Every class below is G's and self-
contained, so merging the two files is a union of imports plus both sets of
classes.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from magent import cli, launch, log, node_sync, nodes, remote_mux
from tests.unit._node_fixtures import config_json, entry, git, seed_history

# D-MERGE: D's launch.node_git_states (plan D :2919) is not on this branch yet.
# Every test that needs it is written and skipped on this flag, so D's merge
# switches them on by itself -- and they fail until the deferred code lands.
_NO_D_GIT_STATES = not hasattr(launch, "node_git_states")
_D_GIT_STATES_REASON = (
    "D-MERGE: needs D's launch.node_git_states (plan G :3192-3210, :3257)"
)


def _nodes_tree() -> dict[str, bytes]:
    root = nodes.NODES_DIR
    if not root.exists():
        return {}
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _row(stdout: str, nick: str) -> str:
    return next(line for line in stdout.splitlines() if line.strip().startswith(nick))


class TestTheNodeTable:
    def test_each_node_gets_a_row_in_config_order_with_its_30_minute_load(
        self, runner, tmp_config
    ):
        seed_history("second", "quiet", now=time.time() + 30)
        cfg = tmp_config(config_json(("third", "second"), []))

        result = runner.invoke(cli.main, ["--config", cfg, "node"])

        rows = [
            line.split()
            for line in result.stdout.splitlines()
            if line.strip().startswith(("second", "third"))
        ]
        assert result.exit_code == 0
        assert [r[0] for r in rows] == ["third", "second"]
        assert rows[1][:5] == ["second", "devino-second", "amin", "0.40", "(31)"]
        assert "50%" in rows[1]

    def test_a_node_without_samples_says_no_data(self, runner, tmp_config):
        cfg = tmp_config(config_json(("second",), []))

        result = runner.invoke(cli.main, ["--config", cfg, "node"])

        assert "no data" in result.stdout

    def test_the_daemon_column_reads_the_sync_heartbeat(self, runner, tmp_config):
        log.write_heartbeat(node_sync.HEARTBEAT_NAME)
        cfg = tmp_config(config_json(("second",), []))

        result = runner.invoke(cli.main, ["--config", cfg, "node"])

        assert _row(result.stdout, "second").rstrip().endswith("ok")

    def test_a_stopped_daemon_is_said_so(self, runner, tmp_config):
        cfg = tmp_config(config_json(("second",), []))

        result = runner.invoke(cli.main, ["--config", cfg, "node"])

        assert _row(result.stdout, "second").rstrip().endswith("stopped")

    def test_no_nodes_configured_says_how_to_add_one(self, runner, tmp_config):
        cfg = tmp_config(config_json((), []))

        result = runner.invoke(cli.main, ["--config", cfg, "node"])

        assert result.exit_code == 0
        assert "settings.nodes" in result.stdout

    def test_the_table_writes_nothing(self, runner, tmp_config):
        seed_history("second", "quiet", now=time.time() + 30)
        before = _nodes_tree()
        cfg = tmp_config(config_json(("second",), []))

        runner.invoke(cli.main, ["--config", cfg, "node"])

        assert _nodes_tree() == before


class TestNodeCmdKeepsTheHelpPathLight:
    def test_node_cmd_keeps_ssh_and_tar_off_the_help_path(self):
        """DECISION-26 v: the registration hub imports node_cmd for every
        ``magent --help``, so remote_mux and node_sync are imported in-body."""
        import ast
        import inspect

        from magent.cli import node_cmd

        tree = ast.parse(inspect.getsource(node_cmd))
        top: set[str] = set()
        for stmt in tree.body:
            if isinstance(stmt, ast.ImportFrom):
                top |= {stmt.module or ""} | {alias.name for alias in stmt.names}
            elif isinstance(stmt, ast.Import):
                top |= {alias.name for alias in stmt.names}
        assert not top & {
            "remote_mux",
            "node_sync",
            "magent.remote_mux",
            "magent.node_sync",
        }


@pytest.fixture
def api_dir(tmp_path) -> Path:
    repo = tmp_path / "api"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    return repo


@pytest.fixture
def _node_user(monkeypatch):
    # Not autouse: F16's classes share this module and must not inherit it.
    monkeypatch.setattr("magent.env.local_username", lambda: "amin")


@pytest.fixture
def no_states(monkeypatch):
    # D-MERGE: drop raising=False once D's launch.node_git_states is merged.
    monkeypatch.setattr(
        "magent.launch.node_git_states", lambda config, proj: [], raising=False
    )


def _project(path: Path, node: str, title: str = "api") -> dict[str, object]:
    return {"path": str(path), "title": title, "node": node}


def _score_rows(stdout: str, *nicks: str) -> dict[str, list[str]]:
    return {
        line.split()[0]: line.split()
        for line in stdout.splitlines()
        if line.strip().startswith(nicks)
    }


@pytest.mark.usefixtures("_node_user")
class TestNodePlan:
    def test_plan_prints_every_nodes_score_and_marks_the_chosen_one(
        self, runner, tmp_config, api_dir, no_states
    ):
        seed_history("second", "quiet", now=time.time() + 30)
        seed_history("third", "bursty", now=time.time() + 30)
        cfg = tmp_config(config_json(("second", "third"), [_project(api_dir, "auto")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "plan", "api"])

        rows = _score_rows(result.stdout, "second", "third")
        assert result.exit_code == 0
        assert "auto -> @second" in result.stdout
        assert rows["second"] == [
            "second",
            "31",
            "0.40",
            "0.00",
            "0.00",
            "1",
            "0.45",
            "*",
        ]
        assert rows["third"] == ["third", "31", "0.25", "0.81", "0.00", "0", "1.06"]
        assert "floor =" not in result.stdout

    def test_plan_marks_a_node_the_memory_floor_skipped(
        self, runner, tmp_config, api_dir, no_states
    ):
        seed_history("second", "starved", now=time.time() + 30)
        seed_history("third", "bursty", now=time.time() + 30)
        cfg = tmp_config(config_json(("second", "third"), [_project(api_dir, "auto")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "plan", "api"])

        rows = _score_rows(result.stdout, "second", "third")
        assert "auto -> @third" in result.stdout
        assert rows["second"] == [
            "second",
            "31",
            "0.40",
            "0.00",
            "0.07",
            "1",
            "0.52",
            "floor",
        ]
        assert rows["third"] == [
            "third",
            "31",
            "0.25",
            "0.81",
            "0.00",
            "0",
            "1.06",
            "*",
        ]
        assert "floor = under 10% free memory" in result.stdout

    def test_plan_writes_nothing_not_even_the_node_map(
        self, runner, tmp_config, api_dir, no_states
    ):
        seed_history("second", "quiet", now=time.time() + 30)
        nodes.update_node_map("web", entry("second", "web"))
        cfg = tmp_config(config_json(("second",), [_project(api_dir, "auto")]))
        before, config_before = _nodes_tree(), Path(cfg).read_bytes()

        runner.invoke(cli.main, ["--config", cfg, "node", "plan", "api"])

        assert _nodes_tree() == before
        assert Path(cfg).read_bytes() == config_before

    def test_plan_names_a_pinned_project_without_scoring_it(
        self, runner, tmp_config, api_dir, no_states
    ):
        cfg = tmp_config(config_json(("second", "third"), [_project(api_dir, "third")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "plan", "api"])

        assert "pinned -> @third" in result.stdout
        assert "score" not in result.stdout

    def test_plan_shows_the_node_a_placed_project_is_kept_on(
        self, runner, tmp_config, api_dir, no_states
    ):
        seed_history("second", "quiet", now=time.time() + 30)
        seed_history("third", "bursty", now=time.time() + 30)
        nodes.update_node_map("api", entry("third"))
        cfg = tmp_config(config_json(("second", "third"), [_project(api_dir, "auto")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "plan", "api"])

        assert "auto -> @third" in result.stdout
        assert nodes.PLACE_REASONS["kept"] in result.stdout

    # D-MERGE: plan G :3000-3012 -- the push set needs D's node_git_states.
    @pytest.mark.skipif(_NO_D_GIT_STATES, reason=_D_GIT_STATES_REASON)
    def test_plan_lists_the_push_set_relative_to_the_project(
        self, runner, tmp_config, api_dir, no_states, monkeypatch
    ):
        monkeypatch.setattr(
            nodes,
            "push_set",
            lambda project_dir, states, *, home: (
                api_dir / ".env",
                api_dir / ".claude" / "settings.local.json",
            ),
        )
        cfg = tmp_config(config_json(("second",), [_project(api_dir, "second")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "plan", "api"])

        assert "ships  .env" in result.stdout
        assert ".claude/settings.local.json" in result.stdout

    def test_plan_for_a_project_without_a_node_exits_2(
        self, runner, tmp_config, api_dir
    ):
        cfg = tmp_config(
            config_json(("second",), [{"path": str(api_dir), "title": "api"}])
        )

        result = runner.invoke(cli.main, ["--config", cfg, "node", "plan", "api"])

        assert result.exit_code == 2
        assert 'has no "node" set' in result.stderr

    def test_plan_all_spreads_like_a_launch(
        self, runner, tmp_config, tmp_path, no_states
    ):
        seed_history("second", "quiet", now=time.time() + 30)
        seed_history("third", "quiet", now=time.time() + 30)
        for name in ("api", "web"):
            (tmp_path / name).mkdir()
        cfg = tmp_config(
            config_json(
                ("second", "third"),
                [
                    _project(tmp_path / "api", "auto", "api"),
                    _project(tmp_path / "web", "auto", "web"),
                ],
            )
        )

        result = runner.invoke(cli.main, ["--config", cfg, "node", "plan", "--all"])

        assert "api  auto -> @second" in result.stdout
        assert "web  auto -> @third" in result.stdout

    def test_a_sparse_node_is_sampled_live_exactly_once_by_plan(
        self, runner, tmp_config, api_dir, no_states, monkeypatch
    ):
        calls: list[str] = []

        def _sample(node):
            calls.append(node.nick)
            return nodes.LoadSample(time.time(), 4, 3.6, 3.6, 3.6, 16000, 8000, 0)

        monkeypatch.setattr(remote_mux, "sample", _sample)
        seed_history("second", "quiet", now=time.time() + 30)
        seed_history("third", "sparse", now=time.time() + 30)
        cfg = tmp_config(config_json(("second", "third"), [_project(api_dir, "auto")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "plan", "api"])

        assert calls == ["third"]
        assert "1 live" in result.stdout

    def test_plan_for_a_cloud_project_names_it_and_places_nothing(
        self, runner, tmp_config, api_dir, no_states, monkeypatch
    ):
        sampled: list[str] = []
        monkeypatch.setattr(
            remote_mux, "sample", lambda node: sampled.append(node.nick)
        )
        cfg = tmp_config(config_json(("second",), [_project(api_dir, "cloud")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "plan", "api"])

        assert result.exit_code == 0
        assert "api  cloud -- pinned" in result.stdout
        assert sampled == []

    def test_plan_all_leaves_cloud_projects_to_plan_j(
        self, runner, tmp_config, tmp_path, no_states
    ):
        seed_history("second", "quiet", now=time.time() + 30)
        for name in ("api", "web"):
            (tmp_path / name).mkdir()
        cfg = tmp_config(
            config_json(
                ("second",),
                [
                    _project(tmp_path / "api", "auto", "api"),
                    _project(tmp_path / "web", "cloud", "web"),
                ],
            )
        )

        result = runner.invoke(cli.main, ["--config", cfg, "node", "plan", "--all"])

        assert "api  auto -> @second" in result.stdout
        assert "web" not in result.stdout

    def test_plan_needs_a_project_or_all(self, runner, tmp_config):
        cfg = tmp_config(config_json(("second",), []))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "plan"])

        assert result.exit_code == 2
