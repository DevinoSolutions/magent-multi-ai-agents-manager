"""magent node: doctor and setup (exit codes, rows, and what reaches ssh),
then G's surfaces -- the table, plan and push."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from magent import cli, env, launch, log, node_sync, nodes, remote_mux
from magent.cli import node_cmd
from magent.config import SCHEMA_VERSION, load_config
from magent.lockfile import LockHeld
from magent.nodes import LocalGitState
from magent.remote_mux import ProvisionReport, ScriptLine
from magent.style import style
from tests.unit._fake_ssh import gh_auth_status
from tests.unit._node_fixtures import (
    config_json,
    entry,
    git,
    seed_history,
)

HEALTHY = (
    "ok\ttmux\ttmux 3.4\nok\tclaude-auth\tsubscription token accepted by Anthropic\n"
)
NODE_PROJECT = {"path": "api", "node": "second"}


def _pool_file(tmp_config, nicks=("second",), projects=()) -> str:
    return tmp_config(
        {
            "version": SCHEMA_VERSION,
            "settings": {
                "nodes": {n: {"host": f"box-{n}", "user": "demo"} for n in nicks}
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


@pytest.fixture
def placed_sync(monkeypatch):
    """Node sync is expected: serve may spawn it (conftest pins the switch to
    0) and a session is placed on a node."""
    monkeypatch.setenv("MAGENT_NODE_SYNC", "1")
    nodes.write_node_map({"api": entry("second", "api")})


class TestThisPcsSyncRows:
    def test_a_running_daemon_is_ok(self, tmp_config):
        _heartbeat()
        assert _sync(tmp_config)[0] == ScriptLine("ok", "sync-daemon", "running")

    def test_a_stale_heartbeat_warns(self, tmp_config, monkeypatch):
        monkeypatch.setenv("MAGENT_NODE_SYNC", "1")
        # Sync is expected only while a session is placed on a node; with none
        # the same leftover heartbeat is idle, not stale (TestAnIdleSyncIsNotStale).
        nodes.write_node_map({"api": entry("second", "api")})
        _heartbeat(age_s=log.HEARTBEAT_MAX_AGE + 60)
        assert _sync(tmp_config, projects=[NODE_PROJECT])[0].status == "warn"

    def test_a_stopped_daemon_warns_while_a_session_runs_on_a_node(self, tmp_config):
        nodes.write_node_map({"api": entry("second", "api")})
        assert _sync(tmp_config, projects=[NODE_PROJECT])[0].status == "warn"

    def test_a_stopped_daemon_is_a_skip_while_nothing_is_placed(self, tmp_config):
        # It wound down on its own (node_sync.IDLE_EXIT_S), and serve starts
        # it again with the next placement: not a warning.
        assert _sync(tmp_config, projects=[NODE_PROJECT])[0] == ScriptLine(
            "skip", "sync-daemon", "no session is placed on a node"
        )

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
        self, tmp_config, placed_sync
    ):
        _snapshot("second", time.time() - 61)
        row = _sync(tmp_config, projects=[NODE_PROJECT])[1]
        assert row.status == "warn"
        assert "(60s)" in row.detail

    def test_a_snapshot_from_the_future_warns_and_names_the_clock(
        self, tmp_config, placed_sync
    ):
        # sessions_stale reads a ts past now + 2 pulls as stale too; the row
        # must not call that "pulled 0s ago, older than ...".
        _snapshot("second", time.time() + 120)
        row = _sync(tmp_config, projects=[NODE_PROJECT])[1]
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


class TestAnIdleSyncIsNotStale:
    """Sync is expected only while a session runs on a node (status's
    ``node_sync: off`` is the same verdict). With none, the daemon idle-exits
    by design and its leftover heartbeat / old snapshot are not trouble."""

    @pytest.fixture(autouse=True)
    def _sync_switch_on(self, monkeypatch):
        # conftest pins MAGENT_NODE_SYNC=0; status's verdict reads it too.
        monkeypatch.setenv("MAGENT_NODE_SYNC", "1")

    @staticmethod
    def _place() -> None:
        nodes.write_node_map({"api": entry("second", "api")})

    def test_a_switched_off_sync_says_so_instead_of_blaming_missing_sessions(
        self, tmp_config, monkeypatch
    ):
        monkeypatch.setenv("MAGENT_NODE_SYNC", "0")
        _heartbeat(age_s=log.HEARTBEAT_MAX_AGE + 60)
        row = _sync(tmp_config, projects=[NODE_PROJECT])[0]
        assert row.status == "skip"
        assert "MAGENT_NODE_SYNC=0" in row.detail

    def test_a_leftover_stale_heartbeat_is_not_a_warning_when_nothing_is_placed(
        self, tmp_config
    ):
        _heartbeat(age_s=log.HEARTBEAT_MAX_AGE + 60)
        row = _sync(tmp_config, projects=[NODE_PROJECT])[0]
        assert row == ScriptLine(
            "skip", "sync-daemon", "not running -- no node sessions to sync"
        )
        assert "magent status" not in row.detail

    def test_an_old_snapshot_is_not_a_warning_when_nothing_is_placed(self, tmp_config):
        _snapshot("second", time.time() - 67948)
        row = _sync(tmp_config, projects=[NODE_PROJECT])[1]
        assert row.status == "skip"
        assert "67948s ago" in row.detail
        assert "stale" not in row.detail

    def test_a_future_snapshot_is_not_a_warning_when_nothing_is_placed(
        self, tmp_config
    ):
        _snapshot("second", time.time() + 120)
        assert _sync(tmp_config, projects=[NODE_PROJECT])[1].status == "skip"

    def test_a_stale_heartbeat_still_warns_while_a_session_is_placed(self, tmp_config):
        self._place()
        _heartbeat(age_s=log.HEARTBEAT_MAX_AGE + 60)
        row = _sync(tmp_config, projects=[NODE_PROJECT])[0]
        assert row.status == "warn"
        assert "heartbeat is stale" in row.detail

    def test_an_old_snapshot_still_warns_while_a_session_is_placed(self, tmp_config):
        self._place()
        _snapshot("second", time.time() - 61)
        row = _sync(tmp_config, projects=[NODE_PROJECT])[1]
        assert row.status == "warn"
        assert "(60s)" in row.detail

    def test_a_future_snapshot_still_warns_while_a_session_is_placed(self, tmp_config):
        self._place()
        _snapshot("second", time.time() + 120)
        assert _sync(tmp_config, projects=[NODE_PROJECT])[1].status == "warn"

    def test_the_top_level_doctor_does_not_warn_on_an_idle_sync(
        self, tmp_config, fake_ssh
    ):
        from magent.cli.doctor import _check_nodes

        fake_ssh.set_reply("bash -s", stdout="ok\ttmux\ttmux 3.4\n")
        _heartbeat(age_s=log.HEARTBEAT_MAX_AGE + 60)
        _snapshot("second", time.time() - 67948)
        cfg = load_config(_pool_file(tmp_config, projects=[NODE_PROJECT]))
        assert _check_nodes(cfg) == ("ok", "1 node(s) healthy")

    def test_the_top_level_doctor_still_warns_on_a_stale_sync_in_use(
        self, tmp_config, fake_ssh
    ):
        from magent.cli.doctor import _check_nodes

        fake_ssh.set_reply("bash -s", stdout="ok\ttmux\ttmux 3.4\n")
        self._place()
        _heartbeat(age_s=log.HEARTBEAT_MAX_AGE + 60)
        _snapshot("second", time.time() - 67948)
        cfg = load_config(_pool_file(tmp_config, projects=[NODE_PROJECT]))
        status, detail = _check_nodes(cfg)
        assert status == "warn"
        assert "second: sync-daemon, snapshot" in detail


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
                "fail\tclaude-auth\tAnthropic rejected this node's Claude token -- "
                "on the PC run: magent node auth refresh\n"
            ),
        )
        result = _doctor(runner, _pool_file(tmp_config))
        assert result.exit_code == 1
        assert "on the PC run: magent node auth refresh" in result.stdout

    def test_warnings_alone_exit_zero(self, runner, tmp_config, fake_ssh):
        fake_ssh.set_reply("bash -s", stdout="warn\tlocale\tcharmap is POSIX\n")
        assert _doctor(runner, _pool_file(tmp_config)).exit_code == 0

    def test_an_unreachable_node_is_a_failed_row_naming_its_target(
        self, runner, tmp_config, fake_ssh
    ):
        fake_ssh.set_reply(
            "bash -s",
            stderr="ssh: connect to host box-second port 22: Connection refused\n",
            rc=255,
        )
        result = _doctor(runner, _pool_file(tmp_config))
        assert result.exit_code == 1
        assert (
            "cannot reach demo@box-second: ssh: connect to host box-second "
            "port 22: Connection refused"
        ) in result.stdout

    def test_a_nick_checks_only_that_node(self, runner, tmp_config, fake_ssh):
        _doctor(runner, _pool_file(tmp_config, ("second", "fifth")), "fifth")
        (call,) = fake_ssh.calls()
        assert "demo@box-fifth" in call.argv

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
            "claude-auth",
            "sync-daemon",
            "snapshot",
        ]

    def test_json_counts_failures_and_exits_one(self, runner, tmp_config, fake_ssh):
        fake_ssh.set_reply("bash -s", stdout="fail\tgit\tgit is not on PATH\n")
        result = _doctor(runner, _pool_file(tmp_config), "--json")
        assert result.exit_code == 1
        assert json.loads(result.stdout)["failures"] == 1


class TestANodeWithoutATokenNamesTheFixThatWorks:
    """doctor.sh runs on the node and cannot see this PC, so its row for a
    missing token says "run: magent node setup" -- right only while this PC
    holds a token setup would ship. Without one, setup has just run and
    cannot help (the PC rows already say `node auth refresh`): the node row
    must say the same thing instead of contradicting them."""

    NODE_SAYS = (
        "fail\tclaude-auth\t"
        "no Claude subscription token on this node -- run: magent node setup\n"
    )
    TOKEN = "sk-ant-oat01-DECOY-" + "ab12_-" * 16

    def _detail(self, runner, tmp_config, fake_ssh) -> str:
        fake_ssh.set_reply("bash -s", stdout=self.NODE_SAYS)
        result = _doctor(runner, _pool_file(tmp_config), "--json")
        rows = json.loads(result.stdout)["nodes"]["second"]
        return next(r["detail"] for r in rows if r["item"] == "claude-auth")

    def test_with_no_token_on_this_pc_it_names_the_refresh_here(
        self, runner, tmp_config, fake_ssh
    ):
        detail = self._detail(runner, tmp_config, fake_ssh)
        assert detail == (
            "no Claude subscription token on this node, and this PC has none "
            "to give it -- on the PC, in a terminal: magent node auth refresh"
        )
        assert "node setup" not in detail

    def test_an_expired_pc_token_is_no_token_to_give(
        self, runner, tmp_config, fake_ssh
    ):
        from magent import node_auth

        node_auth.write_token(self.TOKEN, now=time.time() - 2 * 365 * 86400)
        assert "magent node auth refresh" in self._detail(runner, tmp_config, fake_ssh)

    def test_with_a_token_here_setup_is_the_fix_and_the_row_is_kept(
        self, runner, tmp_config, fake_ssh
    ):
        from magent import node_auth

        node_auth.write_token(self.TOKEN)
        detail = self._detail(runner, tmp_config, fake_ssh)
        assert detail == self.NODE_SAYS.split("\t")[2].rstrip("\n")

    def test_the_words_it_recognizes_are_doctor_sh_s_own(self):
        script = (
            Path(node_cmd.__file__).parent.parent / "node_scripts" / "doctor.sh"
        ).read_text(encoding="utf-8")
        assert f'claude-auth "{node_cmd.NODE_NO_TOKEN} -- run:' in script


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

    def test_the_budget_reaches_the_ssh_child_itself(
        self, runner, tmp_config, monkeypatch, fake_ssh
    ):
        # Through the REAL remote_mux.doctor -> run_script -> run: only the
        # spawn is replaced (fake_ssh is the client lookup), so a layer that
        # swapped in its own timeout fails.
        seen: list[float] = []

        def spawn(argv, *, timeout_s, **kwargs):
            seen.append(timeout_s)
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        monkeypatch.setattr(remote_mux, "_spawn", spawn)
        monkeypatch.setattr(remote_mux, "DOCTOR_TIMEOUT_S", 7.5)
        _doctor(runner, _pool_file(tmp_config, ("second", "fifth")), "--json")
        assert seen == [7.5, 7.5]


def _rows(result) -> dict[str, dict[str, str]]:
    return {r["item"]: r for r in json.loads(result.stdout)["nodes"]["second"]}


class TestThisPcsRowsAreReadBeforeTheSsh:
    def test_a_snapshot_the_daemon_pulls_during_a_slow_doctor_is_not_clock_skew(
        self, runner, tmp_config, monkeypatch
    ):
        # doctor.sh can take a minute; the daemon pulls meanwhile, so a
        # sessions.json read AFTER it is stamped later than the doctor's `now`.
        _snapshot("second", time.time() - 1)

        def doctor(node, *, timeout_s):
            _snapshot("second", time.time() + 30)
            return ProvisionReport((ScriptLine("ok", "tmux", "tmux 3.4"),))

        monkeypatch.setattr(remote_mux, "doctor", doctor)
        cfg = tmp_config(
            {
                "version": SCHEMA_VERSION,
                "settings": {
                    "nodes": {"second": {"host": "box-second", "user": "demo"}},
                    "nodeSync": {"pullIntervalS": 5},
                },
                "projects": [],
            }
        )
        snap = _rows(_doctor(runner, cfg, "--json"))["snapshot"]
        assert snap["status"] == "ok", snap


class TestTheDoctorNeverReadsSilenceOrACrashAsHealth:
    def test_a_node_that_printed_no_rows_is_not_healthy(
        self, runner, tmp_config, fake_ssh
    ):
        # rc 0 and not one row: a ForceCommand login, a MOTD-only shell -- the
        # node ran nothing the doctor can vouch for.
        fake_ssh.set_reply("bash -s", stdout="Welcome to box-second\n")
        result = _doctor(runner, _pool_file(tmp_config), "--json")
        assert result.exit_code == 1, result.output
        first = json.loads(result.stdout)["nodes"]["second"][0]
        assert (first["status"], first["item"]) == ("fail", "doctor")

    def test_one_node_that_raises_does_not_sink_the_others(
        self, runner, tmp_config, monkeypatch
    ):
        def doctor(node, *, timeout_s):
            if node.nick == "third":
                raise KeyError("boom")
            return ProvisionReport((ScriptLine("ok", "tmux", node.nick),))

        monkeypatch.setattr(remote_mux, "doctor", doctor)
        cfg = _pool_file(tmp_config, ("second", "third", "fifth"))
        result = _doctor(runner, cfg, "--json")
        assert result.exit_code == 1, result.output
        body = json.loads(result.stdout)["nodes"]
        assert body["second"][0]["detail"] == "second"
        assert body["fifth"][0]["detail"] == "fifth"
        assert body["third"] == [
            {
                "status": "fail",
                "item": "doctor",
                "detail": (
                    "the check itself crashed (KeyError) -- see ~/.magent/logs/nodes.log"
                ),
            }
        ]

    def test_a_node_that_never_answered_is_a_fail_row(
        self, runner, tmp_config, monkeypatch
    ):
        # A node that went silent: RemoteError.timed_out (D10) words it, not
        # rc None -- rc None is also a spawn failure or an over-cap reply.
        def doctor(node, *, timeout_s):
            raise _timed_out(timeout_s)

        monkeypatch.setattr(remote_mux, "doctor", doctor)
        result = _doctor(runner, _pool_file(tmp_config))
        assert result.exit_code == 1
        assert (
            "no answer from demo@box-second: timed out after "
            f"{remote_mux.DOCTOR_TIMEOUT_S:g}s"
        ) in result.stdout
        assert "cannot reach" not in result.stdout

    def test_a_call_that_never_started_is_still_cannot_reach(
        self, runner, tmp_config, monkeypatch
    ):
        # rc None with no flag set: the call never ran, so nothing went silent.
        def doctor(node, *, timeout_s):
            raise remote_mux.RemoteError(None, "", ("ssh", node.target))

        monkeypatch.setattr(remote_mux, "doctor", doctor)
        result = _doctor(runner, _pool_file(tmp_config))
        assert result.exit_code == 1
        assert "cannot reach demo@box-second: rc=None" in result.stdout

    @pytest.mark.parametrize(
        ("raised", "named"),
        [
            (KeyError("boom"), "KeyError"),
            (
                PermissionError(13, "Permission denied", "/srv/boom"),
                "PermissionError [Errno 13]",
            ),
        ],
        ids=["bug", "oserror"],
    )
    def test_the_crash_row_s_pointer_to_nodes_log_is_true(
        self, runner, tmp_config, monkeypatch, raised, named
    ):
        # The row says "see ~/.magent/logs/nodes.log": the traceback must be
        # there, under the nodes logger at WARNING, with the exception attached
        # (cq-F16). The one ERROR line is a Sentry event, so it names the
        # class and errno only: an exception's words can quote a host or a path.
        records: list[tuple[str, logging.LogRecord]] = []

        class _Keep(logging.Handler):
            def __init__(self, asked: str) -> None:
                super().__init__()
                self.asked = asked

            def emit(self, record: logging.LogRecord) -> None:
                records.append((self.asked, record))

        def get_logger(name: str) -> logging.Logger:
            logger = logging.getLogger(f"test_node_cmd.{name}")
            logger.handlers = [_Keep(name)]
            logger.propagate = False
            logger.setLevel(logging.DEBUG)
            return logger

        def doctor(node, *, timeout_s):
            raise raised

        monkeypatch.setattr(log, "get_logger", get_logger)
        monkeypatch.setattr(remote_mux, "doctor", doctor)
        result = _doctor(runner, _pool_file(tmp_config), "--json")
        assert result.exit_code == 1, result.output
        assert json.loads(result.stdout)["nodes"]["second"][0]["item"] == "doctor"
        crashed = [
            r
            for asked, r in records
            if asked == "nodes"
            and r.levelno == logging.WARNING
            and r.exc_info
            and r.exc_info[1] is raised
        ]
        assert crashed, [(a, r.levelname, r.getMessage()) for a, r in records]
        assert "second" in crashed[0].getMessage()
        assert "boom" in crashed[0].getMessage()
        errors = [
            (asked, r.getMessage(), r.exc_info)
            for asked, r in records
            if r.levelno >= logging.ERROR
        ]
        assert errors == [("nodes", f"node doctor: a check crashed: {named}", None)]


class TestALegacyCodePageStdout:
    def test_a_node_s_undecodable_byte_does_not_crash_the_doctor(
        self, tmp_config, monkeypatch
    ):
        # _report_of decodes with errors="replace": one invalid byte from the
        # node is U+FFFD, which cp1252 (a redirected Windows stdout) lacks.
        def doctor(node, *, timeout_s):
            return ProvisionReport(
                (ScriptLine("ok", "git", "git 2.43 \N{REPLACEMENT CHARACTER}"),)
            )

        monkeypatch.setattr(remote_mux, "doctor", doctor)
        result = CliRunner(charset="cp1252").invoke(
            cli.main, ["--config", _pool_file(tmp_config), "node", "doctor"]
        )
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            repr(result.exception)
        )
        assert "git 2.43 ?" in result.stdout
        assert "No failures." in result.stdout

    def test_a_node_s_undecodable_byte_in_an_item_does_not_crash_the_doctor(
        self, tmp_config, monkeypatch
    ):
        # The item column is the node's words too: doctor.sh prints it, and
        # _report_of's errors="replace" can put U+FFFD there as well (cq-F16).
        def doctor(node, *, timeout_s):
            return ProvisionReport((ScriptLine("ok", "git�", "git 2.43"),))

        monkeypatch.setattr(remote_mux, "doctor", doctor)
        result = CliRunner(charset="cp1252").invoke(
            cli.main, ["--config", _pool_file(tmp_config), "node", "doctor"]
        )
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            repr(result.exception)
        )
        assert "git?" in result.stdout
        assert "No failures." in result.stdout


def _userless_pool_file(tmp_config) -> str:
    return tmp_config(
        {
            "version": SCHEMA_VERSION,
            "settings": {"nodes": {"second": {"host": "box-second"}}},
            "projects": [],
        }
    )


class TestANodeWhoseUserCannotResolve:
    def test_without_a_nick_it_is_a_failed_config_row(
        self, runner, tmp_config, fake_ssh, monkeypatch
    ):
        monkeypatch.setattr(env, "local_username", lambda: "")
        result = _doctor(runner, _userless_pool_file(tmp_config), "--json")
        assert result.exit_code == 1
        row = json.loads(result.stdout)["nodes"]["second"][0]
        assert (row["status"], row["item"]) == ("fail", "config")
        assert fake_ssh.calls() == []

    def test_naming_it_is_the_same_failed_row_not_a_refusal(
        self, runner, tmp_config, fake_ssh, monkeypatch
    ):
        # The nick IS in the pool: the node is broken, the request is not.
        monkeypatch.setattr(env, "local_username", lambda: "")
        result = _doctor(runner, _userless_pool_file(tmp_config), "second", "--json")
        assert result.exit_code == 1
        row = json.loads(result.stdout)["nodes"]["second"][0]
        assert (row["status"], row["item"]) == ("fail", "config")
        assert fake_ssh.calls() == []


class TestTheTextRowsAreTheContract:
    def test_each_status_has_its_mark_and_the_items_align(
        self, runner, tmp_config, fake_ssh
    ):
        fake_ssh.set_reply(
            "bash -s",
            stdout=(
                "ok\ttmux\ttmux 3.4\n"
                "warn\tlocale\tcharmap is POSIX\n"
                "fail\tgit\tgit is not on PATH\n"
            ),
        )
        out = _doctor(runner, _pool_file(tmp_config)).stdout.splitlines()
        w = len("sync-daemon")
        assert "  second  box-second" in out
        assert f"    + {'tmux'.ljust(w)}  tmux 3.4" in out
        assert f"    ! {'locale'.ljust(w)}  charmap is POSIX" in out
        assert f"    x {'git'.ljust(w)}  git is not on PATH" in out
        assert (
            f"    - {'snapshot'.ljust(w)}  no sessions snapshot from this node yet"
        ) in out

    def test_only_the_rows_that_need_nothing_are_dimmed(
        self, runner, tmp_config, fake_ssh
    ):
        # ok and skip rows recede; a warn or fail detail keeps full weight.
        fake_ssh.set_reply(
            "bash -s",
            stdout="ok\ttmux\ttmux 3.4\nwarn\tlocale\tcharmap is POSIX\n",
        )
        result = runner.invoke(
            cli.main,
            ["--config", _pool_file(tmp_config), "node", "doctor"],
            color=True,
        )
        dim = style("tmux 3.4", dim=True)
        assert dim in result.stdout
        assert style("charmap is POSIX", dim=False) in result.stdout
        assert style("charmap is POSIX", dim=True) not in result.stdout

    def test_an_unknown_nick_is_on_stderr_not_stdout(
        self, runner, tmp_config, fake_ssh
    ):
        result = _doctor(runner, _pool_file(tmp_config), "nope")
        assert "not in settings.nodes" in result.stderr
        assert "not in settings.nodes" not in result.stdout


class TestTheUnreachableRow:
    def test_it_quotes_ssh_s_last_stderr_line(self, runner, tmp_config, fake_ssh):
        fake_ssh.set_reply(
            "bash -s",
            stderr=(
                "Warning: banner\n"
                "ssh: connect to host box-second port 22: Connection refused\n"
            ),
            rc=255,
        )
        result = _doctor(runner, _pool_file(tmp_config))
        assert "cannot reach demo@box-second: ssh: connect to host" in (result.stdout)
        assert "Warning: banner" not in result.stdout

    def test_a_blank_stderr_falls_back_to_the_rc(self, runner, tmp_config, fake_ssh):
        fake_ssh.set_reply("bash -s", stderr="\n\n", rc=255)
        result = _doctor(runner, _pool_file(tmp_config))
        assert "cannot reach demo@box-second: rc=255" in result.stdout


def _over_cap(said: str) -> remote_mux.RemoteError:
    # What remote_mux raises when a reply passes the stdout cap: its own first
    # line, then the child's last words before the flood.
    reason = f"reply exceeded {remote_mux.MAX_REPLY_BYTES} bytes"
    return remote_mux.RemoteError(
        None, f"{reason}\n{said}" if said else reason, ("ssh",), over_cap=True
    )


class TestAnOverCapReplyIsNotUnreachable:
    """The node answered -- too much. Not "cannot reach" (the node was
    reached), not "no answer from" (it answered): our words, from the flag.
    The child's words after the cap line are the node's, so the log has them
    and the screen never does."""

    SAID = "doctor.sh: line 12: the node's own words"
    ROW = ScriptLine(
        "fail",
        "reach",
        "demo@box-second answered, but its reply ran past the size cap",
    )

    @pytest.fixture
    def over_cap(self, monkeypatch):
        def doctor(node, *, timeout_s):
            raise _over_cap(self.SAID)

        monkeypatch.setattr(remote_mux, "doctor", doctor)

    @pytest.fixture
    def nodes_log(self, caplog):
        log.get_logger("nodes")  # sets the level; caplog must come after
        caplog.set_level("WARNING", logger="magent.nodes")
        return lambda: [
            r.getMessage()
            for r in caplog.records
            if r.name == "magent.nodes" and r.levelno == logging.WARNING
        ]

    def test_node_checks_words_it_from_the_flag(self, tmp_config, over_cap, nodes_log):
        cfg = load_config(_pool_file(tmp_config))
        rows = node_cmd.node_checks(cfg, "second", now=time.time())
        assert rows[0] == self.ROW
        shown = " ".join(f"{row.item} {row.detail}" for row in rows)
        assert self.SAID not in shown
        assert "exceeded" not in shown
        assert "cannot reach" not in shown
        assert "no answer from" not in shown
        # ... and nodes.log has what the screen does not.
        assert any(self.SAID in message for message in nodes_log())

    def test_the_screen_never_carries_the_nodes_words(
        self, runner, tmp_config, over_cap, nodes_log
    ):
        result = _doctor(runner, _pool_file(tmp_config))
        assert result.exit_code == 1
        assert self.ROW.detail in result.stdout
        assert self.SAID not in result.output
        assert "cannot reach" not in result.output


class TestTheSnapshotRow:
    def test_each_node_reads_its_own_snapshot(self, tmp_config):
        _snapshot("fifth", time.time())
        cfg = load_config(_pool_file(tmp_config, ("second", "fifth")))
        assert node_cmd.sync_lines(cfg, "second", now=time.time())[1].status == "skip"
        assert node_cmd.sync_lines(cfg, "fifth", now=time.time())[1].status == "ok"

    def test_a_slightly_future_snapshot_reads_zero_seconds_not_negative(
        self, tmp_config
    ):
        now = time.time()
        _snapshot("second", now + 5)
        cfg = load_config(_pool_file(tmp_config))
        assert node_cmd.sync_lines(cfg, "second", now=now)[1].detail == (
            "pulled 0s ago"
        )

    def test_the_future_row_names_how_far_ahead(self, tmp_config, placed_sync):
        now = time.time()
        _snapshot("second", now + 120)
        cfg = load_config(_pool_file(tmp_config, projects=[NODE_PROJECT]))
        assert "stamped 120s in the future" in (
            node_cmd.sync_lines(cfg, "second", now=now)[1].detail
        )

    def test_the_doctor_reads_a_fresh_snapshot_as_ok(
        self, runner, tmp_config, fake_ssh
    ):
        fake_ssh.set_reply("bash -s", stdout="ok\ttmux\ttmux 3.4\n")
        _snapshot("second", time.time())
        snap = _rows(_doctor(runner, _pool_file(tmp_config), "--json"))["snapshot"]
        assert snap["status"] == "ok", snap


PC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKEPCKEY me@pc"
TOKEN_ACCEPTED = "ok\tclaude-auth\tsubscription token accepted by Anthropic\n"
TOKEN_REJECTED = (
    "fail\tclaude-auth\tAnthropic rejected this node's Claude token -- "
    "on the PC run: magent node auth refresh\n"
)
# A decoy subscription token (gitleaks allowlists the word under tests/).
CLAUDE_TOKEN = "sk-ant-oat01-DECOY-" + "ab12_-" * 16


def _setup_token_ui(token: str = CLAUDE_TOKEN) -> str:
    """What `claude setup-token` prints on success, reduced to its words."""
    return (
        "Opening browser to sign in...\n"
        "Long-lived authentication token created successfully!\n\n"
        f"Your OAuth token (valid for 1 year):\n\n{token}\n\n"
        "Store this token securely. You won't be able to see it again.\n"
    )


@pytest.fixture
def at_a_terminal(monkeypatch):
    """A human at this terminal: setup may mint (setup-token's stdin is
    DEVNULL, never the test runner's)."""
    monkeypatch.setattr(node_cmd, "_can_approve", lambda: True)
    monkeypatch.setattr(node_cmd, "_MINT_STDIN", subprocess.DEVNULL)


def _pc_key(name: str = "id_ed25519.pub", text: str = PC_KEY + "\n") -> Path:
    path = Path.home() / ".ssh" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _node_answers(
    fake_ssh, *, users=("demo",), setup_extra="", doctor=TOKEN_ACCEPTED
) -> None:
    """The fake node: root's setup call reports a key per user; provision
    (`--force`) installs the hook; doctor (`--target`) answers ``doctor``."""
    keys = "".join(
        f"key\t{u}\tssh-ed25519 AAAA{u.upper()} magent@box-second\n" for u in users
    )
    fake_ssh.set_reply(
        "root@box-second", stdout=f"did\tpackages\tinstalled\n{setup_extra}{keys}"
    )
    fake_ssh.set_reply(
        "--force", stdout="did\tstate_hook\t~/.magent/bin/state-hook.sh\n"
    )
    fake_ssh.set_reply("--target", stdout="ok\ttmux\ttmux 3.4\n" + doctor)


def _gh_can_add_keys(fake_gh) -> None:
    fake_gh.set_reply(
        "auth status", stdout=gh_auth_status("demo", "admin:public_key, repo")
    )


def _setup(runner, cfg: str, *args: str):
    return runner.invoke(cli.main, ["--config", cfg, "node", "setup", *args])


def _targets(fake_ssh) -> list[str]:
    return [next(a for a in c.argv if "@box-" in a) for c in fake_ssh.calls()]


def _timed_out(seconds: float) -> remote_mux.RemoteError:
    # What remote_mux raises when it kills a call at its bound.
    return remote_mux.RemoteError(
        None, f"timed out after {seconds:g}s", ("ssh",), timed_out=True
    )


class TestNodeSetup:
    def test_it_sets_up_as_root_then_provisions_and_checks_as_the_user(
        self, runner, tmp_config, fake_ssh, fake_gh
    ):
        _pc_key()
        _node_answers(fake_ssh)
        _gh_can_add_keys(fake_gh)
        _setup(runner, _pool_file(tmp_config), "second")
        calls = fake_ssh.calls()
        assert _targets(fake_ssh) == [
            "root@box-second",
            "demo@box-second",
            "demo@box-second",
        ]
        assert "--force" in calls[1].argv[-1]
        assert "--target" in calls[2].argv[-1]

    def test_this_pcs_public_key_is_the_setup_payload(
        self, runner, tmp_config, fake_ssh
    ):
        _pc_key()
        _node_answers(fake_ssh)
        _setup(runner, _pool_file(tmp_config), "second")
        assert fake_ssh.calls()[0].stdin.endswith((PC_KEY + "\n").encode("ascii"))

    def test_the_nodes_key_is_registered_under_a_title_naming_it(
        self, runner, tmp_config, fake_ssh, fake_gh
    ):
        _pc_key()
        _node_answers(fake_ssh)
        _gh_can_add_keys(fake_gh)
        _setup(runner, _pool_file(tmp_config), "second")
        (add,) = [c for c in fake_gh.calls() if c.argv[:2] == ["ssh-key", "add"]]
        assert add.argv[add.argv.index("--title") + 1] == "magent demo@box-second"
        assert add.stdin == b"ssh-ed25519 AAAADEMO magent@box-second\n"

    def test_the_nodes_key_row_is_not_printed(
        self, runner, tmp_config, fake_ssh, fake_gh
    ):
        _pc_key()
        _node_answers(fake_ssh)
        _gh_can_add_keys(fake_gh)
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert "AAAADEMO" not in result.output

    def test_a_node_whose_token_is_rejected_fails_setup_naming_the_refresh(
        self, runner, tmp_config, fake_ssh, fake_gh
    ):
        _pc_key()
        _node_answers(fake_ssh, doctor=TOKEN_REJECTED)
        _gh_can_add_keys(fake_gh)
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert result.exit_code == 1
        assert "magent node auth refresh" in result.stdout
        # No step for the user to take by hand any more.
        assert "by hand" not in result.stdout
        assert "ssh demo@box-second claude" not in result.stdout

    def test_a_healthy_node_is_ready(self, runner, tmp_config, fake_ssh, fake_gh):
        _pc_key()
        _node_answers(fake_ssh)
        _gh_can_add_keys(fake_gh)
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert result.exit_code == 0, result.output
        assert "Ready." in result.stdout

    def test_any_other_failed_step_exits_one(
        self, runner, tmp_config, fake_ssh, fake_gh
    ):
        _pc_key()
        _node_answers(
            fake_ssh, setup_extra="fail\tdocker:demo\tusermod -aG docker demo failed\n"
        )
        _gh_can_add_keys(fake_gh)
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert result.exit_code == 1
        assert "1 step(s) failed." in result.stdout
        assert "Ready." not in result.stdout

    def test_no_gh_login_skips_the_optional_key_and_setup_still_succeeds(
        self, runner, tmp_config, fake_ssh
    ):
        # git reaches GitHub over https with the gh login, so the node key
        # is optional: its absence is a skip, never a failed setup.
        _pc_key()
        _node_answers(fake_ssh)
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert result.exit_code == 0, result.output
        assert "not needed: git uses the gh login" in result.stdout

    def test_a_login_without_the_key_scope_never_asks_for_a_refresh(
        self, runner, tmp_config, fake_ssh, fake_gh
    ):
        _pc_key()
        _node_answers(fake_ssh)
        fake_gh.set_reply("auth status", stdout=gh_auth_status("demo", "repo"))
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert result.exit_code == 0, result.output
        assert "not needed: git uses the gh login" in result.stdout
        assert "gh auth refresh" not in result.output
        assert not [c for c in fake_gh.calls() if c.argv[:2] == ["ssh-key", "add"]]

    def test_each_user_is_created_and_then_provisioned_as_itself(
        self, runner, tmp_config, fake_ssh, fake_gh
    ):
        _pc_key()
        _node_answers(fake_ssh, users=("demo", "bob"))
        _gh_can_add_keys(fake_gh)
        _setup(
            runner, _pool_file(tmp_config), "second", "--user", "demo", "--user", "bob"
        )
        assert fake_ssh.calls()[0].argv[-1] == (
            f"bash -c 'bash -s -- {remote_mux.SOCKET} demo bob'"
        )
        assert _targets(fake_ssh)[1:] == [
            "demo@box-second",
            "demo@box-second",
            "bob@box-second",
            "bob@box-second",
        ]

    def test_a_user_whose_setup_produced_no_key_goes_no_further(
        self, runner, tmp_config, fake_ssh, fake_gh
    ):
        _pc_key()
        _node_answers(
            fake_ssh, users=("demo",), setup_extra="fail\tuser:bob\tuseradd: nope\n"
        )
        _gh_can_add_keys(fake_gh)
        _setup(
            runner, _pool_file(tmp_config), "second", "--user", "demo", "--user", "bob"
        )
        assert "bob@box-second" not in _targets(fake_ssh)

    def test_the_default_user_is_the_nodes_user(self, runner, tmp_config, fake_ssh):
        _pc_key()
        _setup(runner, _pool_file(tmp_config), "second")
        assert fake_ssh.calls()[0].argv[-1] == (
            f"bash -c 'bash -s -- {remote_mux.SOCKET} demo'"
        )

    def test_key_names_the_public_key_to_authorize(self, runner, tmp_config, fake_ssh):
        other = _pc_key("work.pub", "ssh-ed25519 AAAAWORKKEY me@work\n")
        _setup(runner, _pool_file(tmp_config), "second", "--key", str(other))
        assert fake_ssh.calls()[0].stdin.endswith(b"ssh-ed25519 AAAAWORKKEY me@work\n")

    def test_without_key_the_first_default_public_key_is_used(
        self, runner, tmp_config, fake_ssh
    ):
        _pc_key("id_rsa.pub", "ssh-rsa AAAARSAKEY me@pc\n")
        _pc_key("id_ecdsa.pub", "ecdsa-sha2-nistp256 AAAAECDSAKEY me@pc\n")
        _setup(runner, _pool_file(tmp_config), "second")
        assert fake_ssh.calls()[0].stdin.endswith(
            b"ecdsa-sha2-nistp256 AAAAECDSAKEY me@pc\n"
        )

    def test_a_key_file_saved_with_a_bom_is_read_without_it(
        self, runner, tmp_config, fake_ssh
    ):
        # Windows PowerShell 5.1's `Set-Content -Encoding UTF8` writes one.
        _pc_key(text="﻿" + PC_KEY + "\n")
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert "not one ssh public key line" not in result.output
        payload = fake_ssh.calls()[0].stdin
        assert payload.endswith((PC_KEY + "\n").encode("ascii"))
        assert "﻿".encode() not in payload

    def test_a_repeated_user_is_set_up_once_in_first_seen_order(
        self, runner, tmp_config, fake_ssh, fake_gh
    ):
        _pc_key()
        _node_answers(fake_ssh, users=("bob", "demo"))
        _gh_can_add_keys(fake_gh)
        _setup(
            runner,
            _pool_file(tmp_config),
            "second",
            *("--user", "bob", "--user", "demo", "--user", "bob"),
        )
        assert fake_ssh.calls()[0].argv[-1] == (
            f"bash -c 'bash -s -- {remote_mux.SOCKET} bob demo'"
        )
        assert _targets(fake_ssh)[1:] == [
            "bob@box-second",
            "bob@box-second",
            "demo@box-second",
            "demo@box-second",
        ]
        adds = [c for c in fake_gh.calls() if c.argv[:2] == ["ssh-key", "add"]]
        assert len(adds) == 2

    def test_the_root_hop_gets_the_budget_that_grows_with_the_users(
        self, runner, tmp_config, fake_ssh, monkeypatch
    ):
        # setup_node's own default (SETUP_TIMEOUT_S + SETUP_PER_USER_S per
        # user): a budget passed in would cut it for two users or more.
        budgets: list[float] = []
        real = remote_mux.run_script

        def spy(node, script, args, **kwargs):
            if script == "setup":
                budgets.append(kwargs["timeout_s"])
            return real(node, script, args, **kwargs)

        monkeypatch.setattr(remote_mux, "run_script", spy)
        _pc_key()
        _setup(
            runner, _pool_file(tmp_config), "second", "--user", "demo", "--user", "bob"
        )
        assert budgets == [remote_mux.SETUP_TIMEOUT_S + 2 * remote_mux.SETUP_PER_USER_S]


class TestNodeSetupMintsTheClaudeTokenOnce:
    def test_the_first_setup_mints_once_and_later_ones_reuse_it(
        self, runner, tmp_config, fake_ssh, fake_claude, at_a_terminal
    ):
        _pc_key()
        _node_answers(fake_ssh)
        fake_claude.set_reply("setup-token", stdout=_setup_token_ui())
        cfg = _pool_file(tmp_config)
        first = _setup(runner, cfg, "second")
        second = _setup(runner, cfg, "second")
        assert [c.argv for c in fake_claude.calls()] == [["setup-token"]]
        assert "subscription token minted" in first.stdout
        assert "subscription token on this PC" in second.stdout

    def test_the_one_prompt_is_shown_only_when_it_mints(
        self, runner, tmp_config, fake_ssh, fake_claude, at_a_terminal
    ):
        _pc_key()
        _node_answers(fake_ssh)
        fake_claude.set_reply("setup-token", stdout=_setup_token_ui())
        cfg = _pool_file(tmp_config)
        first = _setup(runner, cfg, "second")
        second = _setup(runner, cfg, "second")
        prompt = "approve in the browser that just opened"
        assert first.stdout.count(prompt) == 1
        assert prompt not in second.stdout

    def test_the_token_never_reaches_the_screen_an_argv_or_a_log(
        self, runner, tmp_config, fake_ssh, fake_claude, at_a_terminal
    ):
        _pc_key()
        _node_answers(fake_ssh)
        fake_claude.set_reply("setup-token", stdout=_setup_token_ui())
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert "Your OAuth token" in result.stdout  # setup-token's UI, up to it
        assert CLAUDE_TOKEN[:12] not in result.output
        for call in [*fake_ssh.calls(), *fake_claude.calls()]:
            assert not any(CLAUDE_TOKEN[:12] in a for a in call.argv)
        logs = log.LOG_DIR if log.LOG_DIR.exists() else None
        if logs is not None:
            for f in logs.rglob("*.log*"):
                assert CLAUDE_TOKEN[:12] not in f.read_text("utf-8", "replace")

    def test_the_token_minted_is_the_one_the_provision_ships(
        self, runner, tmp_config, fake_ssh, fake_claude, at_a_terminal, monkeypatch
    ):
        shipped: list[str | None] = []
        real = remote_mux.build_payload

        def spy(scope, **kwargs):
            shipped.append(kwargs.get("claude_token"))
            return real(scope, **kwargs)

        monkeypatch.setattr(remote_mux, "build_payload", spy)
        _pc_key()
        _node_answers(fake_ssh)
        fake_claude.set_reply("setup-token", stdout=_setup_token_ui())
        _setup(runner, _pool_file(tmp_config), "second")
        assert shipped == [CLAUDE_TOKEN]

    def test_no_terminal_means_no_mint_and_a_warning_naming_the_refresh(
        self, runner, tmp_config, fake_ssh, fake_claude
    ):
        _pc_key()
        _node_answers(fake_ssh)
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert fake_claude.calls() == []
        (row,) = [ln for ln in result.stdout.splitlines() if "claude-auth" in ln][:1]
        assert "magent node auth refresh" in row

    def test_a_setup_that_never_reached_a_user_never_mints(
        self, runner, tmp_config, fake_ssh, fake_claude, at_a_terminal
    ):
        # The root hop made no user: no provision will ship a token, so no
        # browser opens for one.
        _pc_key()
        fake_ssh.set_reply("root@box-second", stdout="fail\tuser:demo\tnope\n")
        _setup(runner, _pool_file(tmp_config), "second")
        assert fake_claude.calls() == []


def _auth(runner, *args: str):
    return runner.invoke(cli.main, ["node", "auth", *args])


class TestNodeAuth:
    def test_refresh_mints_even_over_a_token_inside_its_year(
        self, runner, fake_claude, at_a_terminal
    ):
        from magent import node_auth

        node_auth.write_token("sk-ant-oat01-DECOY-" + "cd34-_" * 16)
        fake_claude.set_reply("setup-token", stdout=_setup_token_ui())
        result = _auth(runner, "refresh")
        assert result.exit_code == 0, result.output
        stored = node_auth.read_token()
        assert stored is not None
        assert stored.token == CLAUDE_TOKEN
        assert "approve in the browser that just opened" in result.stdout
        assert "magent node setup <nick>" in result.stdout
        assert CLAUDE_TOKEN[:12] not in result.output

    def test_a_refresh_that_fails_exits_one(self, runner, fake_claude, at_a_terminal):
        fake_claude.set_reply("setup-token", stdout="nope\n", rc=1)
        result = _auth(runner, "refresh")
        assert result.exit_code == 1
        assert "exited 1" in result.stdout

    def test_refresh_without_a_terminal_refuses_before_any_mint(
        self, runner, fake_claude
    ):
        result = _auth(runner, "refresh")
        assert result.exit_code == 2
        assert fake_claude.calls() == []

    def test_status_names_no_token_yet(self, runner):
        result = _auth(runner, "status")
        assert result.exit_code == 1
        assert "no subscription token on this PC yet" in result.stdout

    def test_status_shows_until_when_and_never_the_token(self, runner):
        from magent import node_auth

        node_auth.write_token(CLAUDE_TOKEN)
        result = _auth(runner, "status")
        assert result.exit_code == 0
        assert "valid until" in result.stdout
        assert CLAUDE_TOKEN[:12] not in result.output

    def test_status_says_an_expired_token_is_expired(self, runner):
        from magent import node_auth

        node_auth.write_token(CLAUDE_TOKEN, now=0.0)
        result = _auth(runner, "status")
        assert result.exit_code == 1
        assert "expired" in result.stdout
        assert node_auth.REFRESH_COMMAND in result.stdout


class TestNodeSetupRefusesBeforeAnySsh:
    def test_no_public_key_names_the_key_option(self, runner, tmp_config, fake_ssh):
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert result.exit_code == 2
        assert "--key" in result.output
        assert fake_ssh.calls() == []

    def test_a_private_key_is_refused_and_never_printed(
        self, runner, tmp_config, fake_ssh
    ):
        secret = "b3BlbnNzDECOYXktdjEAAAAABG5vbmUAAAAEbm9uZQ"
        path = _pc_key(
            "id_ed25519",
            (
                "-----BEGIN OPENSSH PRIVATE KEY-----\n"
                f"{secret}\n"
                "-----END OPENSSH PRIVATE KEY-----\n"
            ),
        )
        result = _setup(runner, _pool_file(tmp_config), "second", "--key", str(path))
        assert result.exit_code == 2
        assert "PRIVATE key" in result.output
        assert secret not in result.output
        assert fake_ssh.calls() == []

    def test_a_line_that_is_not_an_ssh_key_is_refused_and_never_printed(
        self, runner, tmp_config, fake_ssh
    ):
        path = _pc_key("id_ed25519.pub", "hunter2 AAAAWOULDBESECRET\n")
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert result.exit_code == 2
        assert f"{path} is not one ssh public key line" in result.output
        assert "hunter2" not in result.output
        assert "WOULDBESECRET" not in result.output
        assert fake_ssh.calls() == []

    @pytest.mark.parametrize(
        "line",
        [
            "ssh-dss AAAAB3NzaC1kc3MAAACBAFAKE me@pc",  # a type setup.sh refuses
            "ssh-ed25519\tAAAAC3NzaC1lZDI1NTE5AAAAIFAKE me@pc",  # a tab, not a space
            "ssh-ed25519 AAAA!notbase64 me@pc",
        ],
    )
    def test_a_line_setup_sh_would_refuse_is_refused_here_first(
        self, runner, tmp_config, fake_ssh, line
    ):
        # setup.sh's KEY_RE would refuse it after the root hop (exit 1);
        # refused here, nothing is sent (exit 2).
        _pc_key(text=line + "\n")
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert result.exit_code == 2
        assert "is not one ssh public key line" in result.output
        assert fake_ssh.calls() == []

    def test_the_key_rule_is_setup_sh_s_key_re(self):
        # One rule in two languages: bash's [[:cntrl:]] class is spelled out
        # for Python, and bash anchors with ^...$ where Python fullmatches.
        text = (
            Path(node_cmd.__file__).parent.parent / "node_scripts" / "setup.sh"
        ).read_text(encoding="utf-8")
        (bash,) = [
            line.split("=", 1)[1].strip("'")
            for line in text.splitlines()
            if line.startswith("KEY_RE=")
        ]
        assert bash == (
            "^"
            + node_cmd._KEY_RE.pattern.replace(r"[^\x00-\x1f\x7f]", "[^[:cntrl:]]")
            + "$"
        )

    def test_two_key_lines_are_refused(self, runner, tmp_config, fake_ssh):
        _pc_key(text=f"{PC_KEY}\n{PC_KEY}\n")
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert result.exit_code == 2
        assert "is not one ssh public key line" in result.output
        assert fake_ssh.calls() == []

    def test_an_unreadable_key_file_names_the_reason(
        self, runner, tmp_config, fake_ssh
    ):
        _pc_key()
        missing = Path.home() / ".ssh" / "gone.pub"
        result = _setup(runner, _pool_file(tmp_config), "second", "--key", str(missing))
        assert result.exit_code == 2
        assert f"cannot read {missing}: FileNotFoundError" in result.output
        assert fake_ssh.calls() == []

    def test_a_bad_user_name_is_refused(self, runner, tmp_config, fake_ssh):
        _pc_key()
        result = _setup(runner, _pool_file(tmp_config), "second", "--user", "Bob")
        assert result.exit_code == 2
        assert "not a valid Unix user name: Bob" in result.output
        assert fake_ssh.calls() == []

    @pytest.mark.parametrize("name", ["bob$(id)", "bob\n"])
    def test_a_valid_prefix_is_not_a_valid_name(
        self, runner, tmp_config, fake_ssh, name
    ):
        # The whole name must match: a valid head does not carry the rest.
        _pc_key()
        result = _setup(runner, _pool_file(tmp_config), "second", "--user", name)
        assert result.exit_code == 2
        assert "not a valid Unix user name" in result.output
        assert fake_ssh.calls() == []

    def test_root_is_refused_as_a_node_user(self, runner, tmp_config, fake_ssh):
        # setup.sh refuses it too, but only after the root hop.
        _pc_key()
        result = _setup(runner, _pool_file(tmp_config), "second", "--user", "root")
        assert result.exit_code == 2
        assert "root is not a node user" in result.output
        assert fake_ssh.calls() == []

    def test_an_unknown_nick_names_the_pool(self, runner, tmp_config, fake_ssh):
        _pc_key()
        result = _setup(runner, _pool_file(tmp_config), "nope")
        assert result.exit_code == 2
        # node_for_nick owns the wording.
        assert "node 'nope' is not in settings.nodes; known nodes: second" in (
            result.output
        )
        assert fake_ssh.calls() == []


class TestNodeSetupUnreachable:
    def test_an_unreachable_root_login_exits_one_and_names_the_check(
        self, runner, tmp_config, fake_ssh, fake_gh
    ):
        _pc_key()
        fake_ssh.set_reply(
            "root@box-second",
            stderr="root@box-second: Permission denied (publickey).\n",
            rc=255,
        )
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert result.exit_code == 1
        assert "Permission denied (publickey)." in result.stdout
        assert "ssh root@box-second true" in result.stdout
        assert len(fake_ssh.calls()) == 1
        assert fake_gh.calls() == []

    def test_a_timed_out_root_hop_may_still_be_running_and_is_not_retried(
        self, runner, tmp_config, fake_ssh, fake_gh, monkeypatch
    ):
        # The outcome is unknown: the root hop may still be running on the
        # node, so it is neither "unreachable" nor sent again.
        calls: list[str] = []

        def timed_out(node, users, pubkey, **kwargs):
            calls.append(node.target)
            raise _timed_out(900)

        monkeypatch.setattr(remote_mux, "setup_node", timed_out)
        _pc_key()
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert result.exit_code == 1
        assert calls == ["demo@box-second"]
        assert "may still be running" in result.stdout
        assert "ssh root@box-second true" not in result.stdout
        assert "cannot reach" not in result.stdout
        assert fake_ssh.calls() == []
        assert fake_gh.calls() == []

    def test_a_timed_out_provision_may_still_be_running_and_is_not_checked(
        self, runner, tmp_config, fake_ssh, fake_gh, monkeypatch
    ):
        seen: list[str] = []

        def timed_out(node, config, *, home, timeout_s, force=False):
            seen.append(node.target)
            raise _timed_out(timeout_s)

        monkeypatch.setattr(remote_mux, "provision_node", timed_out)
        _pc_key()
        _node_answers(fake_ssh)
        _gh_can_add_keys(fake_gh)
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert result.exit_code == 1
        assert seen == ["demo@box-second"]
        assert "may still be running" in result.stdout
        assert "--target" not in " ".join(c.argv[-1] for c in fake_ssh.calls())

    def test_an_unreachable_user_login_gets_no_login_reminder(
        self, runner, tmp_config, fake_ssh, fake_gh
    ):
        # The doctor never ran: the reach row says why, and a reminder to log
        # in would name a step nobody has seen is missing.
        _pc_key()
        # Registered first: the first matching reply wins.
        fake_ssh.set_reply("--target", stderr="ssh: Connection reset\n", rc=255)
        _node_answers(fake_ssh)
        _gh_can_add_keys(fake_gh)
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert result.exit_code == 1
        assert "cannot reach demo@box-second" in result.stdout
        assert "last step" not in result.stdout

    def test_a_timed_out_root_hop_names_root_the_bound_and_the_step(
        self, runner, tmp_config, fake_ssh, fake_gh, monkeypatch
    ):
        def timed_out(node, users, pubkey, **kwargs):
            raise _timed_out(900)

        monkeypatch.setattr(remote_mux, "setup_node", timed_out)
        _pc_key()
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert (
            "no answer from root@box-second (timed out after 900s) -- setup "
            "may still be running there"
        ) in result.stdout

    def test_a_timed_out_provision_names_the_user_the_bound_and_the_step(
        self, runner, tmp_config, fake_ssh, fake_gh, monkeypatch
    ):
        def timed_out(node, config, *, home, timeout_s, force=False):
            raise _timed_out(timeout_s)

        monkeypatch.setattr(remote_mux, "provision_node", timed_out)
        _pc_key()
        _node_answers(fake_ssh)
        _gh_can_add_keys(fake_gh)
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert (
            "no answer from demo@box-second (timed out after 300s) -- the "
            "provision may still be running there"
        ) in result.stdout

    def test_an_unreachable_root_hop_names_root_not_the_user(
        self, runner, tmp_config, fake_ssh, fake_gh
    ):
        _pc_key()
        fake_ssh.set_reply(
            "root@box-second",
            stderr="root@box-second: Permission denied (publickey).\n",
            rc=255,
        )
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert "cannot reach root@box-second: " in result.stdout

    def test_an_over_cap_root_hop_may_still_have_run_and_gets_no_login_hint(
        self, runner, tmp_config, fake_ssh, fake_gh, monkeypatch, caplog
    ):
        # A reply over the cap is killed mid-run: like a timeout, setup.sh may
        # have run to the end, so neither the row nor a hint blames the root
        # login -- the two can never contradict each other. But the node
        # ANSWERED (too much): not "no answer from", and the child's words
        # after the cap line are the node's -- nodes.log's, never the screen's.
        said = "setup.sh: line 40: the node's own words"

        def over_cap(node, users, pubkey, **kwargs):
            raise _over_cap(said)

        monkeypatch.setattr(remote_mux, "setup_node", over_cap)
        log.get_logger("nodes")  # sets the level; caplog must come after
        caplog.set_level("WARNING", logger="magent.nodes")
        _pc_key()
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert result.exit_code == 1
        assert (
            "root@box-second answered, but its reply ran past the size cap -- "
            "setup may still be running there: rerun magent node setup once it "
            "has finished (every step is idempotent)"
        ) in result.stdout
        assert said not in result.output
        assert "exceeded" not in result.output
        assert "no answer from" not in result.stdout
        assert "cannot reach" not in result.stdout
        assert "ssh root@box-second true" not in result.stdout
        assert fake_gh.calls() == []
        assert any(
            said in r.getMessage()
            for r in caplog.records
            if r.name == "magent.nodes" and r.levelno == logging.WARNING
        )

    def test_the_real_over_cap_error_reads_as_outcome_unknown(self, fake_ssh):
        # The real raise, end to end: remote_mux flags it, and the predicate
        # reads that flag (D17's outcome_unknown).
        fake_ssh.set_mode("flood")
        node = nodes.Node(nick="second", host="box-second", user="root", root="~")
        with pytest.raises(remote_mux.RemoteError) as caught:
            remote_mux.run(node, ["flood"], timeout_s=60, max_stdout_bytes=64 * 1024)
        assert not caught.value.timed_out
        assert caught.value.over_cap
        assert node_cmd._outcome_unknown(caught.value)

    def test_the_predicate_reads_the_flags_never_the_nodes_words(self):
        # stderr_tail is the NODE's words: a node that prints "reply exceeded"
        # did not overflow anything, and an over-cap reply is known by its
        # flag whatever its first line says.
        said = remote_mux.RemoteError(1, "reply exceeded 5 bytes", ("ssh",))
        flagged = remote_mux.RemoteError(None, "", ("ssh",), over_cap=True)
        assert not node_cmd._outcome_unknown(said)
        assert node_cmd._outcome_unknown(flagged)

    def test_a_spawn_failure_is_not_outcome_unknown(
        self, runner, tmp_config, fake_ssh, fake_gh, monkeypatch
    ):
        # rc None too, but the command never ran: a plain failure, hint kept.
        def no_start(node, users, pubkey, **kwargs):
            raise remote_mux.RemoteError(None, "Permission denied", ("ssh",))

        monkeypatch.setattr(remote_mux, "setup_node", no_start)
        _pc_key()
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert result.exit_code == 1
        assert "cannot reach root@box-second: Permission denied" in result.stdout
        assert "may still be running" not in result.stdout
        assert "ssh root@box-second true" in result.stdout


class TestNodeSetupNeverReadsSilenceAsSuccess:
    def test_a_root_hop_that_printed_nothing_fails_each_user(
        self, runner, tmp_config, fake_ssh, fake_gh
    ):
        # Exit 0 and not one row (a ForceCommand, a MOTD-only login): setup.sh
        # never ran, and "Ready." would be a lie.
        _pc_key()
        _gh_can_add_keys(fake_gh)
        result = _setup(
            runner, _pool_file(tmp_config), "second", "--user", "demo", "--user", "bob"
        )
        assert result.exit_code == 1
        assert "no node key came back for demo" in result.stdout
        assert "no node key came back for bob" in result.stdout
        assert "Ready." not in result.stdout
        assert len(fake_ssh.calls()) == 1

    def test_a_user_whose_failure_has_its_own_row_gets_no_second_one(
        self, runner, tmp_config, fake_ssh, fake_gh
    ):
        _pc_key()
        _node_answers(
            fake_ssh, users=("demo",), setup_extra="fail\tuser:bob\tuseradd: nope\n"
        )
        _gh_can_add_keys(fake_gh)
        result = _setup(
            runner, _pool_file(tmp_config), "second", "--user", "demo", "--user", "bob"
        )
        assert "no node key came back" not in result.stdout
        assert "1 step(s) failed." in result.stdout

    def test_a_doctor_that_printed_nothing_is_a_fail(
        self, runner, tmp_config, fake_ssh, fake_gh
    ):
        _pc_key()
        fake_ssh.set_reply(
            "root@box-second",
            stdout="key\tdemo\tssh-ed25519 AAAADEMO magent@box-second\n",
        )
        fake_ssh.set_reply("--target", stdout="")
        _gh_can_add_keys(fake_gh)
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert result.exit_code == 1
        assert "the node printed no doctor rows" in result.stdout
        assert "Ready." not in result.stdout


class TestNodeSetupProvisionsThroughTheOneBody:
    def test_each_user_is_provisioned_by_provision_node_forced(
        self, runner, tmp_config, fake_ssh, fake_gh, monkeypatch
    ):
        # DECISION-24: setup and the bring-up share remote_mux.provision_node.
        seen: list[tuple[str, bool]] = []

        def fake(node, config, *, home, timeout_s, force=False):
            seen.append((node.target, force))
            return remote_mux.ProvisionReport(())

        monkeypatch.setattr(remote_mux, "provision_node", fake)
        _pc_key()
        _node_answers(fake_ssh, users=("demo", "bob"))
        _gh_can_add_keys(fake_gh)
        _setup(
            runner, _pool_file(tmp_config), "second", "--user", "demo", "--user", "bob"
        )
        assert seen == [("demo@box-second", True), ("bob@box-second", True)]

    def test_provision_gets_its_own_budget_and_this_pcs_home(
        self, runner, tmp_config, fake_ssh, fake_gh, monkeypatch
    ):
        seen: list[tuple[float, Path]] = []

        def fake(node, config, *, home, timeout_s, force=False):
            seen.append((timeout_s, home))
            return remote_mux.ProvisionReport(())

        monkeypatch.setattr(remote_mux, "provision_node", fake)
        _pc_key()
        _node_answers(fake_ssh)
        _gh_can_add_keys(fake_gh)
        _setup(runner, _pool_file(tmp_config), "second")
        assert seen == [(remote_mux.PROVISION_TIMEOUT_S, Path.home())]


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
        assert rows[1][:5] == ["second", "box-second", "demo", "0.40", "(31)"]
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
        monkeypatch.setenv("USERNAME", "Demo")
        monkeypatch.setenv("USER", "Demo")
        body = config_json(("second",), [])
        del body["settings"]["nodes"]["second"]["user"]
        cfg = tmp_config(body)

        result = runner.invoke(cli.main, ["--config", cfg, "node"])

        cfg_typed = cli.config_io._load_config_or_exit(Path(cfg))
        expected = nodes.node_for_nick(cfg_typed, "second", local_user="Demo").user
        assert expected == "demo"
        assert _row(result.stdout, "second").split()[2] == expected

    @pytest.mark.parametrize(
        "local",
        [
            "Alice Smith",  # not a node login once lowercased
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
        # A substring, not a token: "alice smith" holds a space, so no token
        # of row.split() could ever equal it (cq-G11 r2).
        assert local.lower() not in row

    def test_an_explicit_empty_user_never_reaches_the_table(
        self, runner, tmp_config, monkeypatch
    ):
        # The config refuses it at load (exit 1), so the table can never show
        # the local login for a node whose sessions could not run at all.
        monkeypatch.setenv("USERNAME", "demo")
        monkeypatch.setenv("USER", "demo")
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
        assert "magent node add <host>" in result.stdout
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
    monkeypatch.setattr("magent.env.local_username", lambda: "demo")


@pytest.fixture
def no_states(monkeypatch):
    monkeypatch.setattr("magent.launch.node_git_states", lambda config, proj: [])


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

    @staticmethod
    def _seed_token(nick: str, fixture: str, *, ready: bool) -> None:
        """``fixture``'s history with every row saying whether the node user
        has its Claude subscription token, as lib.sh's magent_sample does."""
        path = seed_history(nick, fixture, now=time.time() + 30)
        rows = [
            json.dumps(json.loads(line) | {"claude_auth": ready})
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    def test_plan_marks_a_node_skipped_for_its_missing_claude_token(
        self, runner, tmp_config, api_dir, no_states
    ):
        self._seed_token("second", "quiet", ready=False)
        self._seed_token("third", "bursty", ready=True)
        cfg = tmp_config(config_json(("second", "third"), [_project(api_dir, "auto")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "plan", "api"])

        rows = _score_rows(result.stdout, "second", "third")
        assert result.exit_code == 0
        assert "auto -> @third" in result.stdout
        # The quieter node: its score still shows, and why it was passed over.
        assert rows["second"][-3:] == ["0.45", "no", "token"]
        assert rows["third"][-1] == "*"
        assert (
            "no token = no Claude subscription token on the node"
            " (run: magent node setup <nick>)"
        ) in result.stdout
        assert "floor =" not in result.stdout

    def test_plan_names_the_fix_when_no_node_has_a_claude_token(
        self, runner, tmp_config, api_dir, no_states
    ):
        self._seed_token("second", "quiet", ready=False)
        self._seed_token("third", "bursty", ready=False)
        cfg = tmp_config(config_json(("second", "third"), [_project(api_dir, "auto")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "plan", "api"])

        rows = _score_rows(result.stdout, "second", "third")
        assert "auto -> @second" in result.stdout
        assert "run: magent node setup second" in result.stdout
        # Nothing was skipped: every node lacks it, so the score decided.
        assert len(rows["third"]) == 7
        assert "no token =" not in result.stdout

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
        # The words themselves, as printed: a reason reworded into the
        # no-data text would read an unknown map as no load data.
        assert (
            "api  auto -> (node unknown)  (the node map could not be read,"
            " so where it runs is unknown)"
        ) in result.stdout
        assert "no node has load samples" not in result.stdout
        assert "@second" not in result.stdout
        # The refusal a launch would print, as the same failure line.
        assert "x api: the node map could not be read" in result.stdout

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

    def test_plan_lists_the_push_set_relative_to_the_project(
        self, runner, tmp_config, api_dir, no_states, monkeypatch
    ):
        monkeypatch.setattr(
            nodes,
            "push_set",
            lambda project_dir, states, *, home, extras=(): (
                api_dir / ".env",
                api_dir / ".claude" / "settings.local.json",
            ),
        )
        cfg = tmp_config(config_json(("second",), [_project(api_dir, "second")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "plan", "api"])

        assert "ships  .env" in result.stdout
        assert ".claude/settings.local.json" in result.stdout

    def test_a_push_entry_is_listed_as_a_bring_up_would_ship_it(
        self, runner, tmp_config, api_dir, no_states
    ):
        # The recipe builder ships a project's `push` entries too; a list
        # without them would promise less than the bring-up delivers.
        (api_dir / "notes.txt").write_text("n\n", encoding="utf-8")
        project = {**_project(api_dir, "second"), "push": ["notes.txt"]}
        cfg = tmp_config(config_json(("second",), [project]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "plan", "api"])

        assert result.exit_code == 0
        assert "ships  notes.txt" in result.stdout

    def test_a_tree_the_plan_cannot_read_is_unknown_by_its_class(
        self, runner, tmp_config, api_dir, monkeypatch, caplog
    ):
        def _unreadable(config, proj):
            raise PermissionError(13, "Access is denied", str(api_dir))

        monkeypatch.setattr("magent.launch.node_git_states", _unreadable)
        log.get_logger("nodes")  # sets the level; caplog must come after
        caplog.set_level("WARNING", logger="magent.nodes")
        cfg = tmp_config(config_json(("second",), [_project(api_dir, "second")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "plan", "api"])

        assert result.exit_code == 0
        assert "(unknown: PermissionError; see nodes.log)" in result.stdout
        assert "nothing beyond git" not in result.stdout
        assert "Access is denied" not in result.stdout
        assert any(
            "Access is denied" in r.getMessage()
            for r in caplog.records
            if r.name == "magent.nodes"
        )

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


@pytest.fixture
def one_repo(monkeypatch, api_dir):
    """api_dir as the one repo D's git read finds: its recipe builder refuses
    a project with none (a node clones the project from its origin)."""
    state = LocalGitState(
        path=api_dir,
        url="git@github.com:demo/api.git",
        branch="main",
        dirty=False,
        unpushed=False,
        detached=False,
    )
    monkeypatch.setattr("magent.launch.node_git_states", lambda config, proj: [state])


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
        self, runner, tmp_config, api_dir, one_repo, shipped
    ):
        nodes.update_node_map("api", entry("third"))
        cfg = tmp_config(config_json(("second", "third"), [_project(api_dir, "auto")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "push", "api"])

        assert result.exit_code == 0
        assert shipped == [("third", "api")]
        assert "shipped 2 file(s) to @third: .env, apps/web/.env.local" in result.stdout

    def test_push_for_a_pinned_project_goes_to_its_pin(
        self, runner, tmp_config, api_dir, one_repo, shipped
    ):
        cfg = tmp_config(config_json(("second",), [_project(api_dir, "second")]))

        runner.invoke(cli.main, ["--config", cfg, "node", "push", "api"])

        assert shipped == [("second", "api")]

    def test_push_for_an_unplaced_auto_project_exits_2(
        self, runner, tmp_config, api_dir, one_repo, shipped
    ):
        cfg = tmp_config(config_json(("second",), [_project(api_dir, "auto")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "push", "api"])

        assert result.exit_code == 2
        assert shipped == []
        assert "magent up api" in result.stderr

    def test_push_to_a_node_that_does_not_answer_exits_3_with_its_reason(
        self, runner, tmp_config, api_dir, one_repo, monkeypatch
    ):
        def _refuse(node, recipe):
            raise remote_mux.RemoteError(
                255,
                "ssh: connect to host box-second: Connection refused",
                ("ssh", "box-second"),
            )

        monkeypatch.setattr(remote_mux, "push_files", _refuse)
        cfg = tmp_config(config_json(("second",), [_project(api_dir, "second")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "push", "api"])

        assert result.exit_code == 3
        assert "Connection refused" in result.stderr

    def test_push_with_nothing_to_ship_says_so(
        self, runner, tmp_config, api_dir, one_repo, monkeypatch
    ):
        monkeypatch.setattr(remote_mux, "push_files", lambda node, recipe: [])
        cfg = tmp_config(config_json(("second",), [_project(api_dir, "second")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "push", "api"])

        assert result.exit_code == 0
        assert "nothing to ship" in result.stdout

    def test_push_with_the_node_map_unreadable_is_unknown_not_unplaced(
        self, runner, tmp_config, api_dir, one_repo, shipped, caplog
    ):
        nodes.NODE_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        nodes.NODE_MAP_PATH.write_text("{ torn", encoding="utf-8")
        log.get_logger("nodes")  # sets the level; caplog must come after
        caplog.set_level("WARNING", logger="magent.nodes")
        cfg = tmp_config(config_json(("second",), [_project(api_dir, "auto")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "push", "api"])

        assert result.exit_code == 1
        assert shipped == []
        assert "the node map could not be read (ValueError)" in result.stderr
        assert "so where api runs is unknown" in result.stderr
        assert "not placed yet" not in result.stderr
        # The parser's own words go to the log, never the screen.
        assert "Expecting" not in result.stderr
        assert any(
            "Expecting" in r.getMessage()
            for r in caplog.records
            if r.name == "magent.nodes"
        )

    def test_push_with_a_malformed_map_entry_is_unknown_not_unplaced(
        self, runner, tmp_config, api_dir, one_repo, shipped, caplog
    ):
        # Round-2 ruling: an entry the map cannot read makes the map
        # unreadable. Dropped, api read as "not placed yet".
        nodes.NODE_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        nodes.NODE_MAP_PATH.write_text(
            json.dumps(
                {
                    "api": {
                        "nick": "second",
                        "sid": "api",
                        "remote_root": "~/magent/api",
                        "placed_ts": 1.0,
                        "attached_existing": "yes",
                    }
                }
            ),
            encoding="utf-8",
        )
        log.get_logger("nodes")  # sets the level; caplog must come after
        caplog.set_level("WARNING", logger="magent.nodes")
        cfg = tmp_config(config_json(("second",), [_project(api_dir, "auto")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "push", "api"])

        assert result.exit_code == 1
        assert shipped == []
        assert "the node map could not be read (ValueError)" in result.stderr
        assert "so where api runs is unknown" in result.stderr
        assert "not placed yet" not in result.stderr
        assert "malformed" not in result.output
        assert any(
            "'api'" in r.getMessage() and "attached_existing" in r.getMessage()
            for r in caplog.records
            if r.name == "magent.nodes"
        )

    def test_push_of_a_project_with_no_repo_names_the_recipe_refusal(
        self, runner, tmp_config, api_dir, no_states, shipped
    ):
        # D's recipe builder: a node clones the project, so a project with no
        # repo has no recipe -- and nothing is dialed.
        cfg = tmp_config(config_json(("second",), [_project(api_dir, "second")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "push", "api"])

        assert result.exit_code == 2
        assert "cannot build api's recipe" in result.stderr
        assert "has no git repo" in result.stderr
        assert shipped == []

    def test_a_file_refused_on_this_pc_is_named_by_its_class(
        self, runner, tmp_config, api_dir, one_repo, monkeypatch
    ):
        def _refuse(node, recipe):
            raise PermissionError(13, "Access is denied", str(api_dir / ".env"))

        monkeypatch.setattr(remote_mux, "push_files", _refuse)
        cfg = tmp_config(config_json(("second",), [_project(api_dir, "second")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "push", "api"])

        assert result.exit_code == 1
        assert "could not ship api's files" in result.stderr
        assert "PermissionError" in result.stderr
        assert "Access is denied" not in result.stderr
        assert "Traceback" not in result.output

    def test_the_nodes_list_of_shipped_files_reaches_the_screen_printable(
        self, runner, tmp_config, api_dir, one_repo, monkeypatch
    ):
        monkeypatch.setattr(
            remote_mux, "push_files", lambda node, recipe: ["\x1b[31m.env", "é.env"]
        )
        cfg = tmp_config(config_json(("second",), [_project(api_dir, "second")]))

        result = runner.invoke(cli.main, ["--config", cfg, "node", "push", "api"])

        assert result.exit_code == 0
        assert "shipped 2 file(s) to @second: ?[31m.env, ?.env" in result.stdout
        assert "\x1b" not in result.stdout


# --- magent node push, for a cloud project (plan J, J11m) -----------------------

# A low-entropy marker no output may ever carry. It sits in a plain value and in
# a multi-line quoted one, under names that are not credential-shaped, and a
# line INSIDE the quoted value looks like a variable (INSIDE_THE_VALUE).
SENTINEL = "SENTINEL_VALUE_DO_NOT_PRINT"
CLOUD_ENV_TEXT = (
    f"PLAIN_SETTING={SENTINEL}\n"
    f'MULTI_LINE="first {SENTINEL}\nINSIDE_THE_VALUE={SENTINEL}\nlast"\n'
)


@pytest.fixture
def cloud_env(tmp_path, monkeypatch) -> Path:
    """NODES_DIR (the digest key and the records) and the temp dir (the hand-off
    file) in tmp. Returns the temp dir, so a test can count what is left in it."""
    monkeypatch.setattr(nodes, "NODES_DIR", tmp_path / "nodes")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    return scratch


def _handoffs(scratch: Path) -> list[Path]:
    return list(scratch.glob("magent-cloud-env-*"))


def _clean_state(repo: Path, **overrides: object) -> LocalGitState:
    base = LocalGitState(
        path=repo,
        url="https://github.com/me/api.git",
        branch="main",
        dirty=False,
        unpushed=False,
        detached=False,
        ignored=(".env",),
    )
    return dataclasses.replace(base, **overrides)


@pytest.fixture
def cloud_project(tmp_path, tmp_config, cloud_env, monkeypatch) -> str:
    """A clean, pushed GitHub checkout whose gitignored .env holds the sentinel."""
    repo = tmp_path / "api"
    repo.mkdir()
    (repo / ".env").write_text(CLOUD_ENV_TEXT, encoding="utf-8")
    monkeypatch.setattr(
        "magent.launch.node_git_states", lambda config, proj: [_clean_state(repo)]
    )
    return tmp_config(
        {
            "version": SCHEMA_VERSION,
            "projects": [{"path": str(repo), "node": "cloud", "cloudTask": "t"}],
        }
    )


def _cloud_push(runner, cfg, *extra, stdin=None):
    return runner.invoke(
        cli.main, ["--config", cfg, "node", "push", "api", *extra], input=stdin
    )


def _cloud_gate(cfg):
    return launch.cloud_refusal(load_config(cfg), "api")


def _raises(exc: BaseException):
    def _raise(*args, **kwargs):
        raise exc

    return _raise


def _logged_text() -> str:
    """Every rotating log magent wrote under the redirected home."""
    if not log.LOG_DIR.exists():
        return ""
    return "".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in log.LOG_DIR.glob("*.log*")
    )


class TestPushHandsACloudPushSetOffByHand:
    def test_it_shows_names_and_lengths_never_a_value_and_opens_the_gate(
        self, runner, cloud_project
    ):
        assert _cloud_gate(cloud_project) is not None
        result = _cloud_push(runner, cloud_project, stdin="y\n")
        assert result.exit_code == 0
        assert f"PLAIN_SETTING  ******** ({len(SENTINEL)} chars)" in result.stdout
        assert "MULTI_LINE  ********" in result.stdout
        assert SENTINEL not in result.stdout + result.stderr
        assert _cloud_gate(cloud_project) is None

    def test_a_line_inside_a_multi_line_value_is_never_shown_as_a_name(
        self, runner, cloud_project
    ):
        result = _cloud_push(runner, cloud_project, stdin="y\n")
        assert "INSIDE_THE_VALUE" not in result.stdout + result.stderr

    def test_the_file_holds_the_values_while_the_prompt_waits_and_is_gone_after(
        self, runner, cloud_project, cloud_env, monkeypatch
    ):
        seen: list[str] = []

        def _confirm(text, **kwargs):
            [handoff] = _handoffs(cloud_env)
            seen.append(handoff.read_text(encoding="utf-8"))
            return True

        monkeypatch.setattr(click, "confirm", _confirm)
        result = _cloud_push(runner, cloud_project)
        assert result.exit_code == 0
        assert len(seen) == 1
        # Verbatim, the continuation lines of a quoted value included.
        assert f"INSIDE_THE_VALUE={SENTINEL}" in seen[0]
        assert re.search(r"magent-cloud-env-\S+\.env", result.stdout)
        assert _handoffs(cloud_env) == []

    def test_not_confirming_records_nothing_and_leaves_no_file(
        self, runner, cloud_project, cloud_env
    ):
        result = _cloud_push(runner, cloud_project, stdin="n\n")
        assert result.exit_code == 2
        assert "nothing recorded" in result.stderr
        assert _cloud_gate(cloud_project) is not None
        assert _handoffs(cloud_env) == []

    @pytest.mark.parametrize("how", ["stdin-closed", "ctrl-c"])
    def test_an_abandoned_prompt_leaves_no_file_and_records_nothing(
        self, runner, cloud_project, cloud_env, monkeypatch, how
    ):
        if how == "ctrl-c":
            monkeypatch.setattr(click, "confirm", _raises(KeyboardInterrupt()))
        result = _cloud_push(runner, cloud_project, stdin="")
        assert result.exit_code == 1
        assert _handoffs(cloud_env) == []
        assert _cloud_gate(cloud_project) is not None

    def test_yes_records_at_once_and_keeps_the_file_for_the_user(
        self, runner, cloud_project, cloud_env
    ):
        result = _cloud_push(runner, cloud_project, "--yes")
        assert result.exit_code == 0
        [handoff] = _handoffs(cloud_env)
        assert SENTINEL in handoff.read_text(encoding="utf-8")
        assert str(handoff) in result.stdout
        assert f"delete {handoff} once it is pasted" in result.stdout
        assert _cloud_gate(cloud_project) is None

    def test_what_is_recorded_is_what_was_handed_off_not_what_the_file_became(
        self, runner, cloud_project, tmp_path, monkeypatch
    ):
        # The prompt can wait minutes. An edit made while it waits was never
        # pasted, so the record must not claim it was: the gate stays shut.
        def _edit_then_confirm(text, **kwargs):
            with (tmp_path / "api" / ".env").open("a", encoding="utf-8") as fh:
                fh.write("ADDED_DURING_THE_PROMPT=1\n")
            return True

        monkeypatch.setattr(click, "confirm", _edit_then_confirm)
        assert _cloud_push(runner, cloud_project).exit_code == 0
        assert _cloud_gate(cloud_project) is not None

    def test_a_dirty_checkout_is_reported_alongside(
        self, runner, cloud_project, monkeypatch, tmp_path
    ):
        dirty = _clean_state(tmp_path / "api", dirty=True)
        monkeypatch.setattr(
            "magent.launch.node_git_states", lambda config, proj: [dirty]
        )
        result = _cloud_push(runner, cloud_project, stdin="y\n")
        assert result.exit_code == 0
        assert "the cloud create is also blocked" in result.stdout
        assert "uncommitted" in result.stdout  # _note writes to stdout

    def test_a_cloud_push_reaches_no_node(self, runner, cloud_project, monkeypatch):
        # The other half of the retired refusal pin: a cloud project has no node.
        monkeypatch.setattr(
            remote_mux,
            "push_files",
            lambda node, recipe: pytest.fail("a cloud push reached a node"),
        )
        assert _cloud_push(runner, cloud_project, "--yes").exit_code == 0


class TestAColdPushStopsBeforeAnythingIsWritten:
    def _assert_untouched(self, result, cloud_env):
        assert result.exit_code != 0
        assert "Traceback" not in result.output
        assert _handoffs(cloud_env) == []
        assert nodes.read_cloud_record("api") is None

    def test_a_folder_that_is_not_a_repository_is_refused(
        self, runner, cloud_project, cloud_env, monkeypatch
    ):
        monkeypatch.setattr("magent.launch.node_git_states", lambda config, proj: [])
        result = _cloud_push(runner, cloud_project, "--yes")
        assert result.exit_code == 2
        assert "not a git repository" in result.stderr
        self._assert_untouched(result, cloud_env)

    def test_a_workspace_of_several_repositories_is_refused(
        self, runner, cloud_project, cloud_env, monkeypatch, tmp_path
    ):
        two = [_clean_state(tmp_path / "a"), _clean_state(tmp_path / "b")]
        monkeypatch.setattr("magent.launch.node_git_states", lambda config, proj: two)
        result = _cloud_push(runner, cloud_project, "--yes")
        assert result.exit_code == 2
        assert "ONE git repository" in result.stderr
        self._assert_untouched(result, cloud_env)

    def test_a_missing_folder_is_refused(self, runner, tmp_config, tmp_path, cloud_env):
        cfg = tmp_config(
            {
                "version": SCHEMA_VERSION,
                "projects": [
                    {
                        "path": str(tmp_path / "gone"),
                        "title": "api",
                        "node": "cloud",
                        "cloudTask": "t",
                    }
                ],
            }
        )
        result = _cloud_push(runner, cfg, "--yes")
        assert result.exit_code == 2
        assert "does not exist on this machine" in result.stderr
        self._assert_untouched(result, cloud_env)

    @pytest.mark.parametrize(
        ("error", "said"),
        [
            (
                remote_mux.RemoteError(
                    128, "fatal: detected dubious ownership", ("git", "status")
                ),
                "dubious ownership",
            ),
            (
                remote_mux.RemoteError(None, "", ("git", "status"), timed_out=True),
                "timed out",
            ),
        ],
        ids=["git-said-no", "git-never-answered"],
    )
    def test_git_that_fails_is_a_named_exit_not_a_traceback(
        self, runner, cloud_project, cloud_env, monkeypatch, error, said
    ):
        monkeypatch.setattr("magent.launch.node_git_states", _raises(error))
        result = _cloud_push(runner, cloud_project, "--yes")
        assert result.exit_code == 1
        assert "git could not read" in result.stderr
        assert said in result.stderr
        self._assert_untouched(result, cloud_env)


class TestWhenThereIsNothingToPaste:
    def _only(self, tmp_path, monkeypatch, *ignored: str):
        """The repo's ignored files are exactly ``ignored``: the fixture's .env
        goes, and a file named there is made."""
        (tmp_path / "api" / ".env").unlink()
        for name in ignored:
            (tmp_path / "api" / name).write_text("notes\n", encoding="utf-8")
        state = _clean_state(tmp_path / "api", ignored=ignored)
        monkeypatch.setattr(
            "magent.launch.node_git_states", lambda config, proj: [state]
        )

    def test_an_empty_push_set_says_so_and_writes_no_hand_off_file(
        self, runner, cloud_project, cloud_env, tmp_path, monkeypatch
    ):
        self._only(tmp_path, monkeypatch)
        result = _cloud_push(runner, cloud_project)
        assert result.exit_code == 0
        assert "nothing inside the project to hand off" in result.stdout
        assert _handoffs(cloud_env) == []
        assert _cloud_gate(cloud_project) is None

    def test_a_file_that_cannot_travel_is_named_and_needs_a_yes_not_a_blank_file(
        self, runner, cloud_project, cloud_env, tmp_path, monkeypatch
    ):
        self._only(tmp_path, monkeypatch, "CLAUDE.local.md")
        result = _cloud_push(runner, cloud_project, stdin="y\n")
        assert result.exit_code == 0
        assert "CLAUDE.local.md cannot travel by hand" in result.stdout
        assert "Paste the contents" not in result.stdout
        assert _handoffs(cloud_env) == []
        assert _cloud_gate(cloud_project) is None

    def test_declining_that_records_nothing(
        self, runner, cloud_project, cloud_env, tmp_path, monkeypatch
    ):
        self._only(tmp_path, monkeypatch, "CLAUDE.local.md")
        result = _cloud_push(runner, cloud_project, stdin="n\n")
        assert result.exit_code == 2
        assert nodes.read_cloud_record("api") is None

    def test_a_path_outside_the_project_is_noted_printable_and_still_recorded(
        self, runner, cloud_project, tmp_path, monkeypatch
    ):
        outside = tmp_path / "elsewhere\x1b[31m" / ".env"
        monkeypatch.setattr(
            nodes,
            "cloud_push_set",
            lambda project_dir, states, **kw: nodes.CloudPushSet(
                project_dir=project_dir, files=(), outside=(outside,), names=()
            ),
        )
        result = _cloud_push(runner, cloud_project)
        assert result.exit_code == 0
        assert "is outside the project" in result.stdout
        assert "\x1b" not in result.stdout
        assert nodes.read_cloud_record("api") is not None


class TestAFailureIsAWordNeverATraceback:
    RETRY = "another magent is updating cloud hand-off state; try again"

    @pytest.mark.parametrize(("extra", "text"), [(("--yes",), None), ((), "y\n")])
    def test_a_held_records_lock_is_a_retry_message_and_the_file_is_gone(
        self, runner, cloud_project, cloud_env, monkeypatch, extra, text
    ):
        monkeypatch.setattr(nodes, "write_cloud_record", _raises(LockHeld("held")))
        result = _cloud_push(runner, cloud_project, *extra, stdin=text)
        assert result.exit_code == 1
        assert self.RETRY in result.stderr
        assert isinstance(result.exception, SystemExit)
        assert _handoffs(cloud_env) == []

    def test_a_held_key_lock_stops_before_anything_is_shown_or_written(
        self, runner, cloud_project, cloud_env, monkeypatch
    ):
        monkeypatch.setattr(nodes, "push_set_digest", _raises(LockHeld("held")))
        result = _cloud_push(runner, cloud_project, "--yes")
        assert result.exit_code == 1
        assert self.RETRY in result.stderr
        assert "PLAIN_SETTING" not in result.stdout
        assert _handoffs(cloud_env) == []

    def test_an_unreadable_file_is_a_refusal_naming_it_and_the_error_class(
        self, runner, cloud_project, cloud_env, tmp_path, monkeypatch
    ):
        real = nodes.cloud_push_set

        def _then_it_vanishes(project_dir, states, **kw):
            ps = real(project_dir, states, **kw)
            (tmp_path / "api" / ".env").unlink()
            return ps

        monkeypatch.setattr(nodes, "cloud_push_set", _then_it_vanishes)
        result = _cloud_push(runner, cloud_project, "--yes")
        assert result.exit_code == 1
        assert ".env cannot be read (FileNotFoundError)" in result.stderr
        assert "magent node push api" in result.stderr
        assert "Traceback" not in result.output
        assert _handoffs(cloud_env) == []
        assert nodes.read_cloud_record("api") is None

    def test_a_file_that_goes_unreadable_before_the_hand_off_is_the_same_refusal(
        self, runner, cloud_project, cloud_env, monkeypatch
    ):
        monkeypatch.setattr(
            nodes,
            "write_manual_handoff",
            _raises(nodes.PushSetUnreadable(".env", "PermissionError")),
        )
        result = _cloud_push(runner, cloud_project, "--yes")
        assert result.exit_code == 1
        assert ".env cannot be read (PermissionError)" in result.stderr
        assert _handoffs(cloud_env) == []
        assert nodes.read_cloud_record("api") is None

    def test_any_other_os_error_is_its_class_only(
        self, runner, cloud_project, cloud_env, monkeypatch
    ):
        monkeypatch.setattr(
            nodes,
            "write_manual_handoff",
            _raises(PermissionError(13, "denied", "C:/private/place")),
        )
        result = _cloud_push(runner, cloud_project, "--yes")
        assert result.exit_code == 1
        assert "PermissionError" in result.stderr
        assert "denied" not in result.stderr and "private" not in result.stderr
        assert "Traceback" not in result.output

    @pytest.mark.parametrize(("extra", "text"), [(("--yes",), None), ((), "y\n")])
    def test_an_unexpected_error_after_the_file_exists_still_deletes_it(
        self, runner, cloud_project, cloud_env, monkeypatch, extra, text
    ):
        monkeypatch.setattr(nodes, "write_cloud_record", _raises(RuntimeError("boom")))
        result = _cloud_push(runner, cloud_project, *extra, stdin=text)
        assert isinstance(result.exception, RuntimeError)
        assert _handoffs(cloud_env) == []

    def test_a_file_that_will_not_delete_is_named_not_hidden(
        self, runner, cloud_project, cloud_env, monkeypatch
    ):
        real_unlink = Path.unlink

        def _stuck(self, missing_ok=False):
            if self.name.startswith("magent-cloud-env-"):
                raise PermissionError(13, "in use")
            real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", _stuck)
        result = _cloud_push(runner, cloud_project, stdin="y\n")
        assert result.exit_code == 0
        assert "could not delete" in result.stdout
        assert "PermissionError" in result.stdout
        assert SENTINEL not in result.stdout + result.stderr


def _leftover(scratch: Path, name: str, *, age_s: float = 7200.0) -> Path:
    """A hand-off file an earlier push left, ``age_s`` old, holding the sentinel."""
    path = scratch / name
    path.write_text(f"PLAIN_SETTING={SENTINEL}\n", encoding="utf-8")
    then = time.time() - age_s
    os.utime(path, (then, then))
    return path


class TestAPushClearsWhatAnEarlierPushLeft:
    """A terminal closed at the prompt skips the ``finally`` that deletes the
    hand-off file, and ``--yes`` keeps it by design. Each ``node push`` starts
    by sweeping our own old ones and saying how many, never which or what."""

    def test_old_files_are_cleared_and_counted_in_one_line(
        self, runner, cloud_project, cloud_env
    ):
        _leftover(cloud_env, "magent-cloud-env-aaaa.env")
        _leftover(cloud_env, "magent-cloud-env-bbbb.env")
        result = _cloud_push(runner, cloud_project, stdin="y\n")
        assert result.exit_code == 0
        assert "cleared 2 hand-off file(s) an earlier push left behind" in result.stdout
        assert "aaaa" not in result.stdout + result.stderr
        assert SENTINEL not in result.stdout + result.stderr
        assert _handoffs(cloud_env) == []

    def test_a_push_with_nothing_left_over_says_nothing_about_it(
        self, runner, cloud_project
    ):
        result = _cloud_push(runner, cloud_project, stdin="y\n")
        assert result.exit_code == 0
        assert "earlier push" not in result.stdout

    def test_a_recent_file_is_kept_and_not_mentioned(
        self, runner, cloud_project, cloud_env
    ):
        # Another terminal's push may be waiting at its prompt.
        fresh = _leftover(cloud_env, "magent-cloud-env-aaaa.env", age_s=60.0)
        result = _cloud_push(runner, cloud_project, stdin="y\n")
        assert result.exit_code == 0
        assert fresh.exists()
        assert "earlier push" not in result.stdout

    def test_the_sweep_comes_first_so_a_refused_push_still_clears(
        self, runner, cloud_project, cloud_env, monkeypatch
    ):
        monkeypatch.setattr("magent.launch.node_git_states", lambda config, proj: [])
        _leftover(cloud_env, "magent-cloud-env-aaaa.env")
        result = _cloud_push(runner, cloud_project, "--yes")
        assert result.exit_code == 2
        assert "not a git repository" in result.stderr
        assert "cleared 1 hand-off file(s)" in result.stdout
        assert _handoffs(cloud_env) == []

    def test_a_file_that_would_not_go_is_counted_and_the_push_goes_on(
        self, runner, cloud_project, monkeypatch
    ):
        monkeypatch.setattr(nodes, "sweep_handoff_leftovers", lambda: (1, 2))
        result = _cloud_push(runner, cloud_project, stdin="y\n")
        assert result.exit_code == 0
        assert "cleared 1 hand-off file(s) an earlier push left behind" in result.stdout
        assert "2 more could not be deleted: delete them yourself" in result.stdout
        assert nodes.read_cloud_record("api") is not None

    def test_only_a_stuck_file_is_still_one_line(
        self, runner, cloud_project, monkeypatch
    ):
        monkeypatch.setattr(nodes, "sweep_handoff_leftovers", lambda: (0, 1))
        result = _cloud_push(runner, cloud_project, stdin="y\n")
        assert "cleared" not in result.stdout
        assert "1 more could not be deleted" in result.stdout

    @pytest.mark.usefixtures("_node_user")
    def test_a_node_project_push_does_not_sweep(
        self, runner, tmp_config, api_dir, one_repo, monkeypatch
    ):
        # The hand-off belongs to the cloud flow: a node push has none.
        monkeypatch.setattr(remote_mux, "push_files", lambda node, recipe: [".env"])
        monkeypatch.setattr(
            nodes,
            "sweep_handoff_leftovers",
            lambda: pytest.fail("a node push swept the temp dir"),
        )
        cfg = tmp_config(config_json(("second",), [_project(api_dir, "second")]))
        result = runner.invoke(cli.main, ["--config", cfg, "node", "push", "api"])
        assert result.exit_code == 0


class TestAHandOffFileThatCannotBePrivateIsNeverWritten:
    """The refusal comes BEFORE the values are written and before any record:
    the gate stays shut, and nothing holding a value exists."""

    @pytest.fixture
    def not_private(self, monkeypatch):
        def refuse(prefix, suffix):
            raise nodes.HandoffNotPrivate("not-private")

        monkeypatch.setattr(nodes, "_open_private", refuse)

    @pytest.mark.parametrize(("extra", "text"), [(("--yes",), None), ((), "y\n")])
    def test_it_is_a_named_refusal_with_nothing_written_or_recorded(
        self, runner, cloud_project, cloud_env, not_private, extra, text
    ):
        result = _cloud_push(runner, cloud_project, *extra, stdin=text)
        assert result.exit_code == 1
        assert (
            "could not make a private file for api's values (not-private)"
            in result.stderr
        )
        assert "nothing was written and nothing was recorded" in result.stderr
        assert "magent node push api" in result.stderr
        assert "Traceback" not in result.output
        assert isinstance(result.exception, SystemExit)
        assert _handoffs(cloud_env) == []
        assert nodes.read_cloud_record("api") is None
        assert _cloud_gate(cloud_project) is not None

    def test_the_prompt_is_never_reached(
        self, runner, cloud_project, not_private, monkeypatch
    ):
        monkeypatch.setattr(
            click, "confirm", lambda *a, **k: pytest.fail("asked to confirm a paste")
        )
        assert _cloud_push(runner, cloud_project).exit_code == 1

    def test_the_log_names_the_reason_and_no_value(
        self, runner, cloud_project, not_private, caplog
    ):
        caplog.set_level(logging.DEBUG)
        _cloud_push(runner, cloud_project, "--yes")
        assert "not-private" in caplog.text + _logged_text()
        assert SENTINEL not in caplog.text + _logged_text()


class TestNoValueEverReachesAScreenOrALog:
    """Every way the command can end, with the sentinel in the .env: it is in
    the hand-off file the user asked for and nowhere else."""

    @pytest.mark.parametrize(
        ("extra", "text", "fault"),
        [
            ((), "y\n", None),
            ((), "n\n", None),
            ((), "", None),
            (("--yes",), None, None),
            (("--yes",), None, "records-lock"),
            ((), "y\n", "records-lock"),
            (("--yes",), None, "unreadable"),
            ((), "y\n", "os-error"),
            ((), "y\n", "not-private"),
        ],
        ids=[
            "confirmed",
            "declined",
            "stdin-closed",
            "yes",
            "yes-lock-held",
            "confirmed-lock-held",
            "unreadable",
            "os-error",
            "not-private",
        ],
    )
    def test_the_sentinel_is_in_no_stdout_no_stderr_and_no_log(
        self, runner, cloud_project, monkeypatch, caplog, extra, text, fault
    ):
        caplog.set_level(logging.DEBUG)
        if fault == "records-lock":
            monkeypatch.setattr(nodes, "write_cloud_record", _raises(LockHeld("held")))
        elif fault == "unreadable":
            monkeypatch.setattr(
                nodes,
                "write_manual_handoff",
                _raises(nodes.PushSetUnreadable(".env", "PermissionError")),
            )
        elif fault == "os-error":
            monkeypatch.setattr(
                nodes, "write_manual_handoff", _raises(OSError(5, "disk on fire"))
            )
        elif fault == "not-private":
            monkeypatch.setattr(
                nodes, "_open_private", _raises(nodes.HandoffNotPrivate("not-private"))
            )
        result = _cloud_push(runner, cloud_project, *extra, stdin=text)
        everything = (
            result.stdout + result.stderr + result.output + caplog.text + _logged_text()
        )
        assert SENTINEL not in everything
        assert "INSIDE_THE_VALUE" not in everything
