"""magent node doctor: exit codes, rows, and what reaches ssh."""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from magent import cli, log, node_sync, nodes, remote_mux
from magent.cli import node_cmd
from magent.config import SCHEMA_VERSION, load_config
from magent.remote_mux import ProvisionReport, ScriptLine

HEALTHY = "ok\ttmux\ttmux 3.4\nok\tclaude-login\tlogged in\n"
NODE_PROJECT = {"path": "api", "node": "second"}


def _pool_file(tmp_config, nicks=("second",), projects=()) -> str:
    return tmp_config(
        {
            "version": SCHEMA_VERSION,
            "settings": {
                "nodes": {n: {"host": f"devino-{n}", "user": "amin"} for n in nicks}
            },
            "projects": list(projects),
        }
    )


def _snapshot(nick: str, ts: object) -> None:
    # The shape E's nodes.read_sessions accepts: a numeric ts and a list.
    nodes.write_json_atomic(nodes.sessions_path(nick), {"ts": ts, "sessions": []})


def _heartbeat(age_s: float = 0.0) -> None:
    # The name comes from E's node_sync, the one _daemon_state() reads
    # (DECISION-17); a literal here would pass even if the two drifted.
    log.write_heartbeat(node_sync.HEARTBEAT_NAME)
    if age_s:
        old = time.time() - age_s
        path = log.HEARTBEAT_DIR / f"{node_sync.HEARTBEAT_NAME}.heartbeat"
        os.utime(path, (old, old))


def _sync(tmp_config, projects=()) -> list[ScriptLine]:
    cfg = load_config(_pool_file(tmp_config, projects=projects))
    return node_cmd.sync_lines(cfg, "second", now=time.time())


def _doctor(runner, cfg: str, *args: str):
    return runner.invoke(cli.main, ["--config", cfg, "node", "doctor", *args])


class TestThisPcsSyncRows:
    def test_a_running_daemon_is_ok(self, tmp_config):
        _heartbeat()
        assert _sync(tmp_config)[0] == ScriptLine("ok", "sync-daemon", "running")

    def test_a_stale_heartbeat_warns(self, tmp_config):
        _heartbeat(age_s=log.HEARTBEAT_MAX_AGE + 60)
        assert _sync(tmp_config)[0].status == "warn"

    def test_a_stopped_daemon_warns_while_a_project_runs_on_a_node(self, tmp_config):
        assert _sync(tmp_config, projects=[NODE_PROJECT])[0].status == "warn"

    def test_a_disabled_node_project_does_not_want_the_daemon(self, tmp_config):
        # The daemon's own predicate (node_sync.wanted): serve would not start
        # it for a disabled project, so its absence is not a warning.
        projects = [{**NODE_PROJECT, "enabled": False}]
        assert _sync(tmp_config, projects=projects)[0].status == "skip"

    def test_a_stopped_daemon_is_a_skip_when_no_project_uses_a_node(self, tmp_config):
        assert _sync(tmp_config)[0] == ScriptLine(
            "skip", "sync-daemon", "no project runs on a node"
        )

    def test_a_fresh_snapshot_is_ok(self, tmp_config):
        _snapshot("second", time.time() - 5)
        assert _sync(tmp_config)[1] == ScriptLine("ok", "snapshot", "pulled 5s ago")

    def test_a_snapshot_older_than_two_pulls_warns_and_names_the_limit(
        self, tmp_config
    ):
        _snapshot("second", time.time() - 61)
        row = _sync(tmp_config)[1]
        assert row.status == "warn"
        assert "(60s)" in row.detail

    def test_a_snapshot_from_the_future_warns_and_names_the_clock(self, tmp_config):
        # sessions_stale reads a ts past now + 2 pulls as stale too; the row
        # must not call that "pulled 0s ago, older than ...".
        _snapshot("second", time.time() + 120)
        row = _sync(tmp_config)[1]
        assert row.status == "warn"
        assert "ago" not in row.detail
        assert "in the future" in row.detail
        assert "(60s)" in row.detail

    def test_no_snapshot_yet_is_a_skip(self, tmp_config):
        assert _sync(tmp_config)[1].status == "skip"

    @pytest.mark.parametrize("ts", ["yesterday", True, None])
    def test_a_snapshot_without_a_numeric_ts_is_a_skip(self, tmp_config, ts):
        _snapshot("second", ts)
        assert _sync(tmp_config)[1].status == "skip"


class TestNodeDoctor:
    def test_a_healthy_node_exits_zero_and_prints_its_rows(
        self, runner, tmp_config, fake_ssh
    ):
        fake_ssh.set_reply("bash -s", stdout=HEALTHY)
        _heartbeat()
        _snapshot("second", time.time())
        result = _doctor(runner, _pool_file(tmp_config))
        assert result.exit_code == 0, result.output
        assert "tmux 3.4" in result.stdout
        assert "No failures." in result.stdout

    def test_a_failed_row_exits_one(self, runner, tmp_config, fake_ssh):
        fake_ssh.set_reply(
            "bash -s",
            stdout=(
                "fail\tclaude-login\tClaude Code is not logged in here -- "
                "run once: ssh amin@devino-second claude\n"
            ),
        )
        result = _doctor(runner, _pool_file(tmp_config))
        assert result.exit_code == 1
        assert "run once: ssh amin@devino-second claude" in result.stdout

    def test_warnings_alone_exit_zero(self, runner, tmp_config, fake_ssh):
        fake_ssh.set_reply("bash -s", stdout="warn\tlocale\tcharmap is POSIX\n")
        assert _doctor(runner, _pool_file(tmp_config)).exit_code == 0

    def test_an_unreachable_node_is_a_failed_row_naming_its_target(
        self, runner, tmp_config, fake_ssh
    ):
        fake_ssh.set_reply(
            "bash -s",
            stderr="ssh: connect to host devino-second port 22: Connection refused\n",
            rc=255,
        )
        result = _doctor(runner, _pool_file(tmp_config))
        assert result.exit_code == 1
        assert (
            "cannot reach amin@devino-second: ssh: connect to host devino-second "
            "port 22: Connection refused"
        ) in result.stdout

    def test_a_nick_checks_only_that_node(self, runner, tmp_config, fake_ssh):
        _doctor(runner, _pool_file(tmp_config, ("second", "fifth")), "fifth")
        (call,) = fake_ssh.calls()
        assert "amin@devino-fifth" in call.argv

    def test_no_nick_checks_every_node_in_config_order(
        self, runner, tmp_config, fake_ssh
    ):
        result = _doctor(runner, _pool_file(tmp_config, ("second", "fifth")), "--json")
        assert list(json.loads(result.stdout)["nodes"]) == ["second", "fifth"]
        assert len(fake_ssh.calls()) == 2

    def test_an_unknown_nick_exits_two_and_names_the_pool(
        self, runner, tmp_config, fake_ssh
    ):
        result = _doctor(runner, _pool_file(tmp_config), "nope")
        assert result.exit_code == 2
        # node_for_nick's own message, word for word.
        assert "node 'nope' is not in settings.nodes; known nodes: second" in (
            result.output
        )
        assert fake_ssh.calls() == []

    def test_an_unknown_nick_under_json_is_an_error_envelope(
        self, runner, tmp_config, fake_ssh
    ):
        result = _doctor(runner, _pool_file(tmp_config), "nope", "--json")
        assert result.exit_code == 2
        assert json.loads(result.stdout) == {
            "ok": False,
            "error": "node 'nope' is not in settings.nodes; known nodes: second",
        }
        assert fake_ssh.calls() == []

    def test_a_config_that_cannot_load_exits_one(self, runner, tmp_path, fake_ssh):
        bad = tmp_path / "broken.json"
        bad.write_text("{not json", encoding="utf-8")
        result = _doctor(runner, str(bad), "--json")
        assert result.exit_code == 1
        assert json.loads(result.stdout)["ok"] is False
        assert fake_ssh.calls() == []

    def test_no_nodes_configured_says_how_to_add_one(
        self, runner, tmp_config, fake_ssh
    ):
        result = _doctor(runner, _pool_file(tmp_config, ()))
        assert result.exit_code == 0
        assert "no nodes configured" in result.stdout
        assert fake_ssh.calls() == []

    def test_json_carries_every_row_in_order(self, runner, tmp_config, fake_ssh):
        fake_ssh.set_reply("bash -s", stdout=HEALTHY)
        result = _doctor(runner, _pool_file(tmp_config), "--json")
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert (payload["ok"], payload["failures"]) == (True, 0)
        rows = payload["nodes"]["second"]
        assert rows[0] == {"status": "ok", "item": "tmux", "detail": "tmux 3.4"}
        assert [r["item"] for r in rows] == [
            "tmux",
            "claude-login",
            "sync-daemon",
            "snapshot",
        ]

    def test_json_counts_failures_and_exits_one(self, runner, tmp_config, fake_ssh):
        fake_ssh.set_reply("bash -s", stdout="fail\tgit\tgit is not on PATH\n")
        result = _doctor(runner, _pool_file(tmp_config), "--json")
        assert result.exit_code == 1
        assert json.loads(result.stdout)["failures"] == 1


class TestTheNodesAreCheckedConcurrently:
    def test_every_node_is_in_flight_at_once(self, runner, tmp_config, monkeypatch):
        # A barrier only one caller at a time can reach breaks after its
        # timeout, and BrokenBarrierError is not a RemoteError: a serial doctor
        # crashes here instead of passing slowly.
        barrier = threading.Barrier(3, timeout=30)

        def doctor(node, *, timeout_s):
            barrier.wait()
            return ProvisionReport((ScriptLine("ok", "tmux", node.nick),))

        monkeypatch.setattr(remote_mux, "doctor", doctor)
        cfg = _pool_file(tmp_config, ("second", "third", "fifth"))
        result = _doctor(runner, cfg, "--json")
        assert result.exit_code == 0, result.output
        body = json.loads(result.stdout)["nodes"]
        assert [rows[0]["detail"] for rows in body.values()] == [
            "second",
            "third",
            "fifth",
        ]

    def test_each_node_gets_the_one_doctor_budget(
        self, runner, tmp_config, monkeypatch
    ):
        # N nodes cost one DOCTOR_TIMEOUT_S, read from remote_mux, never a
        # copy of its value.
        seen: list[float] = []

        def doctor(node, *, timeout_s):
            seen.append(timeout_s)
            return ProvisionReport(())

        monkeypatch.setattr(remote_mux, "doctor", doctor)
        monkeypatch.setattr(remote_mux, "DOCTOR_TIMEOUT_S", 7.5)
        _doctor(runner, _pool_file(tmp_config, ("second", "fifth")), "--json")
        assert seen == [7.5, 7.5]
