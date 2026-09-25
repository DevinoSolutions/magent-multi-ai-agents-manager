"""magent node: G's surfaces -- the table, plan, push and recall.

F16 creates this module too (node doctor). Every class below is G's and self-
contained, so merging the two files is a union of imports plus both sets of
classes.
"""

from __future__ import annotations

import time

from magent import cli, log, node_sync, nodes
from tests.unit._node_fixtures import config_json, seed_history


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
