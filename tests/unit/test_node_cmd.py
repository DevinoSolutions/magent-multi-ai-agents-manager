"""magent node: G's surfaces -- the table, plan, push and recall.

F16 creates this module too (node doctor). Every class below is G's and self-
contained, so merging the two files is a union of imports plus both sets of
classes.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from magent import cli, log, node_sync, nodes, remote_mux
from tests.unit._node_fixtures import (
    D_ATTRS,
    before_d,
    config_json,
    entry,
    git,
    needs_d,
    seed_history,
)

# D-MERGE: tests that wait on sub-plan D are gated through _node_fixtures'
# one D_ATTRS list (needs_d); D's merge switches them on by itself, and they
# fail until the deferred code named in node_cmd.py's D-MERGE index lands.


class TestTheOneDGate:
    def test_a_name_outside_the_one_list_is_refused_not_silently_skipped(self):
        # A typo (or a D rename) would otherwise keep a test skipped forever.
        with pytest.raises(KeyError, match="D_ATTRS"):
            needs_d("kill_sessions", plan=":1")
        with pytest.raises(KeyError, match="D_ATTRS"):
            before_d("kill_sessions")

    def test_every_gate_says_d_merge_so_the_exit_criterion_sees_it(self):
        for name in D_ATTRS:
            for mark in (needs_d(name, plan=":1"), before_d(name)):
                assert mark.kwargs["reason"].startswith("D-MERGE: ")


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


def _write_load(nick: str, rows: list[tuple[float, int, int, int]]) -> None:
    """load.jsonl rows ``(ts, mem_total_mb, mem_avail_mb, my_sessions)``, in
    the given (file) order."""
    path = nodes.load_path(nick)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(
                {
                    "ts": ts,
                    "nproc": 4,
                    "load1": 1.0,
                    "load5": 1.0,
                    "load15": 1.0,
                    "mem_total_mb": total,
                    "mem_avail_mb": avail,
                    "my_sessions": mine,
                }
            )
            + "\n"
            for ts, total, avail, mine in rows
        ),
        encoding="utf-8",
    )


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

    def test_memory_and_sessions_come_from_the_newest_sample(self, runner, tmp_config):
        # Levels, not rates: the row shows the NEWEST reading even when it is
        # neither first nor last in the file -- and memory as FREE (4000 of
        # 16000 is 25% free, 75% used).
        now = time.time()
        _write_load(
            "second",
            [
                (now - 600, 16000, 12000, 5),
                (now - 60, 16000, 4000, 2),
                (now - 1200, 16000, 14000, 7),
            ],
        )
        cfg = tmp_config(config_json(("second",), []))

        result = runner.invoke(cli.main, ["--config", cfg, "node"])

        cells = _row(result.stdout, "second").split()
        assert cells[5:8] == ["25%", "free", "2"]

    def test_a_sample_without_a_memory_total_shows_no_memory(self, runner, tmp_config):
        _write_load("second", [(time.time() - 60, 0, 0, 1)])
        cfg = tmp_config(config_json(("second",), []))

        result = runner.invoke(cli.main, ["--config", cfg, "node"])

        assert result.exit_code == 0
        assert _row(result.stdout, "second").split()[5] == "-"

    def test_the_columns_line_up_under_their_headers(self, runner, tmp_config):
        seed_history("second", "quiet", now=time.time() + 30)
        body = config_json(("second", "t"), [])
        body["settings"]["nodes"]["t"]["host"] = "a-much-longer-host.example.com"
        cfg = tmp_config(body)

        result = runner.invoke(cli.main, ["--config", cfg, "node"])

        header = _row(result.stdout, "nick")
        rows = [_row(result.stdout, "second"), _row(result.stdout, "t ")]
        for column in ("host", "user", "load p75 30m", "mem", "my sessions", "daemon"):
            start = header.index(column)
            for line in [header, *rows]:
                assert line[start - 2 : start] == "  ", (column, line)
                assert line[start] != " ", (column, line)

    def test_a_node_without_a_user_shows_the_login_sessions_run_as(
        self, runner, tmp_config, monkeypatch
    ):
        # The D4 rule's one home is nodes.node_for_nick: the LOWERCASED local
        # login, never the raw environment value.
        monkeypatch.setenv("USERNAME", "Amin")
        monkeypatch.setenv("USER", "Amin")
        body = config_json(("second",), [])
        del body["settings"]["nodes"]["second"]["user"]
        cfg = tmp_config(body)

        result = runner.invoke(cli.main, ["--config", cfg, "node"])

        cfg_typed = cli.config_io._load_config_or_exit(Path(cfg))
        expected = nodes.node_for_nick(cfg_typed, "second", local_user="Amin").user
        assert expected == "amin"
        assert _row(result.stdout, "second").split()[2] == expected

    @pytest.mark.parametrize(
        "local",
        [
            "Amin Dhouib",  # not a node login once lowercased
            "root",  # D4: running as root never silently means root on a node
        ],
        ids=["unusable-local-login", "local-root"],
    )
    def test_a_login_the_rule_refuses_is_marked_not_guessed(
        self, runner, tmp_config, monkeypatch, local
    ):
        monkeypatch.setenv("USERNAME", local)
        monkeypatch.setenv("USER", local)
        body = config_json(("second",), [])
        del body["settings"]["nodes"]["second"]["user"]
        cfg = tmp_config(body)

        result = runner.invoke(cli.main, ["--config", cfg, "node"])

        assert result.exit_code == 0
        row = _row(result.stdout, "second")
        assert "? (set user)" in row
        # A substring, not a token: "amin dhouib" holds a space, so no token
        # of row.split() could ever equal it (cq-G11 r2).
        assert local.lower() not in row

    def test_an_explicit_empty_user_never_reaches_the_table(
        self, runner, tmp_config, monkeypatch
    ):
        # The config refuses it at load (exit 1), so the table can never show
        # the local login for a node whose sessions could not run at all.
        monkeypatch.setenv("USERNAME", "amin")
        monkeypatch.setenv("USER", "amin")
        body = config_json(("second",), [])
        body["settings"]["nodes"]["second"]["user"] = ""
        cfg = tmp_config(body)

        result = runner.invoke(cli.main, ["--config", cfg, "node"])

        assert result.exit_code == 1
        assert "settings.nodes.second.user must not be empty" in result.stderr
        assert "second" not in result.stdout

    def test_a_node_without_samples_says_no_data(self, runner, tmp_config):
        cfg = tmp_config(config_json(("second",), []))

        result = runner.invoke(cli.main, ["--config", cfg, "node"])

        assert "no data" in result.stdout

    def test_an_unreadable_history_says_unreadable_not_no_data(
        self, runner, tmp_config
    ):
        # Unknown, not "never sampled": the file is there and cannot be read.
        path = nodes.load_path("second")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfe not utf-8 \x80\x81\n")
        cfg = tmp_config(config_json(("second",), []))

        result = runner.invoke(cli.main, ["--config", cfg, "node"])

        assert result.exit_code == 0, result.output
        assert "unreadable (UnicodeDecodeError)" in _row(result.stdout, "second")
        assert "no data" not in result.stdout

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
        assert "load p75" not in result.stdout

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
            "nodes",
            "magent.remote_mux",
            "magent.node_sync",
            "magent.nodes",
        }

    def test_importing_the_cli_does_not_load_nodes(self):
        """magent.nodes pulls in the sessions registry (~11 ms); `magent --help`
        must not pay for it. A fresh interpreter, since this one has it."""
        import subprocess
        import sys

        probe = (
            "import sys, magent.cli; "
            "print(sorted(m for m in ('magent.nodes', 'magent.remote_mux',"
            " 'magent.node_sync') if m in sys.modules))"
        )
        out = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout
        assert out.strip() == "[]"


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
        assert "(a dry run -- nothing is changed)" in result.stdout
        assert "auto -> @second" in result.stdout
        # Rows follow settings.nodes order (place() keeps it), not the score.
        assert result.stdout.index("  second ") < result.stdout.index("  third ")
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
        # A kept project samples nothing, so there is no score table at all.
        assert "score" not in result.stdout

    def test_plan_marks_no_floor_when_every_node_is_under_it(
        self, runner, tmp_config, api_dir, no_states
    ):
        seed_history("second", "starved", now=time.time() + 30)
        seed_history("third", "starved", now=time.time() + 30)
        cfg = tmp_config(config_json(("second", "third"), [_project(api_dir, "auto")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "plan", "api"])

        rows = _score_rows(result.stdout, "second", "third")
        assert rows["second"][-1] == "*"
        # place() fell back to every node: third lost the tie-break, not the
        # floor, so it carries no chosen/floor cell at all.
        assert len(rows["third"]) == 7
        assert "floor =" not in result.stdout

    def test_plan_says_nowhere_and_prints_the_notes_when_nothing_can_be_scored(
        self, runner, tmp_config, api_dir, no_states, monkeypatch
    ):
        monkeypatch.setattr(remote_mux, "sample", lambda node: None)
        nodes.update_node_map("api", entry("third"))
        cfg = tmp_config(config_json(("second",), [_project(api_dir, "auto")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "plan", "api"])

        assert result.exit_code == 0
        assert (
            f"api  auto -> nowhere ({nodes.PLACE_REASONS['no-data']})" in result.stdout
        )
        assert "'third' is no longer in settings.nodes" in result.stdout

    @pytest.mark.parametrize("unreadable", ["torn", "busy"])
    def test_plan_says_node_unknown_when_the_map_cannot_be_read(
        self, runner, tmp_config, api_dir, no_states, monkeypatch, unreadable
    ):
        # api runs on third; the map that says so cannot be read, so plan must
        # neither guess a node nor claim there is no load data (D17).
        monkeypatch.setattr(remote_mux, "sample", lambda node: None)
        seed_history("second", "quiet", now=time.time() + 30)
        nodes.update_node_map("api", entry("third"))
        if unreadable == "torn":
            nodes.NODE_MAP_PATH.write_text("{ torn", encoding="utf-8")
        else:

            def busy() -> dict[str, nodes.NodeMapEntry]:
                raise PermissionError(13, "The process cannot access the file")

            monkeypatch.setattr(nodes, "load_node_map_strict", busy)
        cfg = tmp_config(config_json(("second", "third"), [_project(api_dir, "auto")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "plan", "api"])

        assert result.exit_code == 0
        assert "api  auto -> (node unknown)" in result.stdout
        assert "@second" not in result.stdout
        # The refusal a launch would print, as the same failure line.
        assert "x api: the node map is unreadable" in result.stdout

    def test_plan_for_an_unknown_project_exits_2(self, runner, tmp_config, api_dir):
        cfg = tmp_config(config_json(("second",), [_project(api_dir, "auto")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "plan", "nope"])

        assert result.exit_code == 2
        assert "no configured project matches 'nope'" in result.stderr
        assert "magent node plan" not in result.stdout

    def test_plan_refuses_a_project_and_all_together(self, runner, tmp_config, api_dir):
        cfg = tmp_config(config_json(("second",), [_project(api_dir, "auto")]))

        result = runner.invoke(
            cli.main, ["--config", cfg, "node", "plan", "api", "--all"]
        )

        assert result.exit_code == 2
        assert "name one project, or pass --all" in result.stderr

    def test_plan_all_skips_a_disabled_project(
        self, runner, tmp_config, tmp_path, no_states
    ):
        seed_history("second", "quiet", now=time.time() + 30)
        for name in ("api", "web"):
            (tmp_path / name).mkdir()
        web = {**_project(tmp_path / "web", "auto", "web"), "enabled": False}
        cfg = tmp_config(
            config_json(("second",), [_project(tmp_path / "api", "auto", "api"), web])
        )

        result = runner.invoke(cli.main, ["--config", cfg, "node", "plan", "--all"])

        assert "api  auto -> @second" in result.stdout
        assert "web" not in result.stdout

    def test_plan_all_with_no_node_project_says_so(self, runner, tmp_config, api_dir):
        cfg = tmp_config(
            config_json(("second",), [{"path": str(api_dir), "title": "api"}])
        )

        result = runner.invoke(cli.main, ["--config", cfg, "node", "plan", "--all"])

        assert result.exit_code == 0
        assert 'no enabled project has "node" set' in result.stdout

    # D-MERGE: plan G :3000-3012 -- the push set needs D's node_git_states.
    @needs_d("node_git_states", plan=":3192-3210, :3257")
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
        assert "->" not in result.stdout
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


# D-MERGE: plan G Task 13 (:3298-3380) -- written now, switched on by D's merge.
@needs_d("node_recipe", "node_git_states", "push_files", plan=":3390-3449")
@pytest.mark.usefixtures("_node_user")
class TestNodePush:
    @pytest.fixture
    def shipped(self, monkeypatch):
        calls: list[tuple[str, str]] = []

        def _push_files(node, recipe):
            calls.append((node.nick, recipe.project))
            return [".env", "apps/web/.env.local"]

        monkeypatch.setattr(remote_mux, "push_files", _push_files)
        return calls

    def test_push_ships_the_push_set_to_the_placed_node(
        self, runner, tmp_config, api_dir, no_states, shipped
    ):
        nodes.update_node_map("api", entry("third"))
        cfg = tmp_config(config_json(("second", "third"), [_project(api_dir, "auto")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "push", "api"])

        assert result.exit_code == 0
        assert shipped == [("third", "api")]
        assert "shipped 2 file(s) to @third: .env, apps/web/.env.local" in result.stdout

    def test_push_for_a_pinned_project_goes_to_its_pin(
        self, runner, tmp_config, api_dir, no_states, shipped
    ):
        cfg = tmp_config(config_json(("second",), [_project(api_dir, "second")]))

        runner.invoke(cli.main, ["--config", cfg, "node", "push", "api"])

        assert shipped == [("second", "api")]

    def test_push_for_an_unplaced_auto_project_exits_2(
        self, runner, tmp_config, api_dir, no_states, shipped
    ):
        cfg = tmp_config(config_json(("second",), [_project(api_dir, "auto")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "push", "api"])

        assert result.exit_code == 2
        assert shipped == []
        assert "magent up api" in result.stderr

    def test_push_to_a_node_that_does_not_answer_exits_3_with_its_reason(
        self, runner, tmp_config, api_dir, no_states, monkeypatch
    ):
        def _refuse(node, recipe):
            raise remote_mux.RemoteError(
                255,
                "ssh: connect to host devino-second: Connection refused",
                ("ssh", "devino-second"),
            )

        monkeypatch.setattr(remote_mux, "push_files", _refuse)
        cfg = tmp_config(config_json(("second",), [_project(api_dir, "second")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "push", "api"])

        assert result.exit_code == 3
        assert "Connection refused" in result.stderr

    def test_push_with_nothing_to_ship_says_so(
        self, runner, tmp_config, api_dir, no_states, monkeypatch
    ):
        monkeypatch.setattr(remote_mux, "push_files", lambda node, recipe: [])
        cfg = tmp_config(config_json(("second",), [_project(api_dir, "second")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "push", "api"])

        assert result.exit_code == 0
        assert "nothing to ship" in result.stdout

    def test_push_for_a_cloud_project_is_refused_before_any_node_is_touched(
        self, runner, tmp_config, api_dir, no_states, shipped
    ):
        cfg = tmp_config(config_json(("second",), [_project(api_dir, "cloud")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "push", "api"])

        assert result.exit_code == 2
        assert "runs in the cloud" in result.stderr
        assert shipped == []
