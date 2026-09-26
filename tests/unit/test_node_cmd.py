"""magent node doctor: exit codes, rows, and what reaches ssh."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from magent import cli, env, log, node_sync, nodes, remote_mux
from magent.cli import node_cmd
from magent.config import SCHEMA_VERSION, load_config
from magent.remote_mux import ProvisionReport, ScriptLine
from magent.style import style
from tests.unit._fake_ssh import gh_auth_status

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
                    "nodes": {"second": {"host": "devino-second", "user": "amin"}},
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
        fake_ssh.set_reply("bash -s", stdout="Welcome to devino-second\n")
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
        # rc None is a timeout or an over-cap reply. Its wording waits on D10's
        # RemoteError.timed_out (the D-MERGE note at _unreachable); what must
        # hold today is that it is a failure, never a crash or a pass.
        def doctor(node, *, timeout_s):
            raise remote_mux.RemoteError(None, "", ("ssh", node.target))

        monkeypatch.setattr(remote_mux, "doctor", doctor)
        result = _doctor(runner, _pool_file(tmp_config))
        assert result.exit_code == 1
        assert "cannot reach amin@devino-second: rc=None" in result.stdout

    def test_the_crash_row_s_pointer_to_nodes_log_is_true(
        self, runner, tmp_config, monkeypatch
    ):
        # The row says "see ~/.magent/logs/nodes.log": the traceback must be
        # there, under the nodes logger, with the exception attached (cq-F16).
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
            raise KeyError("boom")

        monkeypatch.setattr(log, "get_logger", get_logger)
        monkeypatch.setattr(remote_mux, "doctor", doctor)
        result = _doctor(runner, _pool_file(tmp_config), "--json")
        assert result.exit_code == 1, result.output
        assert json.loads(result.stdout)["nodes"]["second"][0]["item"] == "doctor"
        crashed = [
            r
            for asked, r in records
            if asked == "nodes" and r.exc_info and r.exc_info[0] is KeyError
        ]
        assert crashed, [(a, r.getMessage()) for a, r in records]
        assert "second" in crashed[0].getMessage()


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
            "settings": {"nodes": {"second": {"host": "devino-second"}}},
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
        assert "  second  devino-second" in out
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
                "ssh: connect to host devino-second port 22: Connection refused\n"
            ),
            rc=255,
        )
        result = _doctor(runner, _pool_file(tmp_config))
        assert "cannot reach amin@devino-second: ssh: connect to host" in (
            result.stdout
        )
        assert "Warning: banner" not in result.stdout

    def test_a_blank_stderr_falls_back_to_the_rc(self, runner, tmp_config, fake_ssh):
        fake_ssh.set_reply("bash -s", stderr="\n\n", rc=255)
        result = _doctor(runner, _pool_file(tmp_config))
        assert "cannot reach amin@devino-second: rc=255" in result.stdout


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

    def test_the_future_row_names_how_far_ahead(self, tmp_config):
        now = time.time()
        _snapshot("second", now + 120)
        cfg = load_config(_pool_file(tmp_config))
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
NOT_LOGGED_IN = (
    "fail\tclaude-login\tClaude Code is not logged in here -- "
    "run once: ssh amin@devino-second claude\n"
)


def _pc_key(name: str = "id_ed25519.pub", text: str = PC_KEY + "\n") -> Path:
    path = Path.home() / ".ssh" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _node_answers(
    fake_ssh, *, users=("amin",), setup_extra="", doctor=NOT_LOGGED_IN
) -> None:
    """The fake node: root's setup call reports a key per user; provision
    (`--force`) installs the hook; doctor (`--target`) answers ``doctor``."""
    keys = "".join(
        f"key\t{u}\tssh-ed25519 AAAA{u.upper()} magent@devino-second\n" for u in users
    )
    fake_ssh.set_reply(
        "root@devino-second", stdout=f"did\tpackages\tinstalled\n{setup_extra}{keys}"
    )
    fake_ssh.set_reply(
        "--force", stdout="did\tstate_hook\t~/.magent/bin/state-hook.sh\n"
    )
    fake_ssh.set_reply("--target", stdout="ok\ttmux\ttmux 3.4\n" + doctor)


def _gh_can_add_keys(fake_gh) -> None:
    fake_gh.set_reply(
        "auth status", stdout=gh_auth_status("amin", "admin:public_key, repo")
    )


def _setup(runner, cfg: str, *args: str):
    return runner.invoke(cli.main, ["--config", cfg, "node", "setup", *args])


def _targets(fake_ssh) -> list[str]:
    return [next(a for a in c.argv if "@devino-" in a) for c in fake_ssh.calls()]


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
            "root@devino-second",
            "amin@devino-second",
            "amin@devino-second",
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
        assert add.argv[add.argv.index("--title") + 1] == "magent amin@devino-second"
        assert add.stdin == b"ssh-ed25519 AAAAAMIN magent@devino-second\n"

    def test_the_nodes_key_row_is_not_printed(
        self, runner, tmp_config, fake_ssh, fake_gh
    ):
        _pc_key()
        _node_answers(fake_ssh)
        _gh_can_add_keys(fake_gh)
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert "AAAAAMIN" not in result.output

    def test_a_missing_claude_login_is_the_one_fail_that_does_not_fail_setup(
        self, runner, tmp_config, fake_ssh, fake_gh
    ):
        _pc_key()
        _node_answers(fake_ssh)
        _gh_can_add_keys(fake_gh)
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert result.exit_code == 0, result.output
        assert (
            "last step, by hand, once: ssh amin@devino-second claude" in result.stdout
        )

    def test_a_logged_in_node_gets_no_reminder(
        self, runner, tmp_config, fake_ssh, fake_gh
    ):
        _pc_key()
        _node_answers(fake_ssh, doctor="ok\tclaude-login\tlogged in\n")
        _gh_can_add_keys(fake_gh)
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert result.exit_code == 0, result.output
        assert "last step" not in result.stdout
        assert "Ready." in result.stdout

    def test_any_other_failed_step_exits_one(
        self, runner, tmp_config, fake_ssh, fake_gh
    ):
        _pc_key()
        _node_answers(
            fake_ssh, setup_extra="fail\tdocker:amin\tusermod -aG docker amin failed\n"
        )
        _gh_can_add_keys(fake_gh)
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert result.exit_code == 1
        assert "1 step(s) failed." in result.stdout
        assert "Ready." not in result.stdout

    def test_no_gh_login_fails_the_github_row_and_setup(
        self, runner, tmp_config, fake_ssh
    ):
        _pc_key()
        _node_answers(fake_ssh)
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert result.exit_code == 1
        assert "gh is not logged in on this PC: gh auth login" in result.stdout

    def test_each_user_is_created_and_then_provisioned_as_itself(
        self, runner, tmp_config, fake_ssh, fake_gh
    ):
        _pc_key()
        _node_answers(fake_ssh, users=("amin", "bob"))
        _gh_can_add_keys(fake_gh)
        _setup(
            runner, _pool_file(tmp_config), "second", "--user", "amin", "--user", "bob"
        )
        assert fake_ssh.calls()[0].argv[-1] == (
            f"bash -c 'bash -s -- {remote_mux.SOCKET} amin bob'"
        )
        assert _targets(fake_ssh)[1:] == [
            "amin@devino-second",
            "amin@devino-second",
            "bob@devino-second",
            "bob@devino-second",
        ]

    def test_a_user_whose_setup_produced_no_key_goes_no_further(
        self, runner, tmp_config, fake_ssh, fake_gh
    ):
        _pc_key()
        _node_answers(
            fake_ssh, users=("amin",), setup_extra="fail\tuser:bob\tuseradd: nope\n"
        )
        _gh_can_add_keys(fake_gh)
        _setup(
            runner, _pool_file(tmp_config), "second", "--user", "amin", "--user", "bob"
        )
        assert "bob@devino-second" not in _targets(fake_ssh)

    def test_the_default_user_is_the_nodes_user(self, runner, tmp_config, fake_ssh):
        _pc_key()
        _setup(runner, _pool_file(tmp_config), "second")
        assert fake_ssh.calls()[0].argv[-1] == (
            f"bash -c 'bash -s -- {remote_mux.SOCKET} amin'"
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
            runner, _pool_file(tmp_config), "second", "--user", "amin", "--user", "bob"
        )
        assert budgets == [remote_mux.SETUP_TIMEOUT_S + 2 * remote_mux.SETUP_PER_USER_S]


class TestNodeSetupRefusesBeforeAnySsh:
    def test_no_public_key_names_the_key_option(self, runner, tmp_config, fake_ssh):
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert result.exit_code == 2
        assert "--key" in result.output
        assert fake_ssh.calls() == []

    def test_a_private_key_is_refused_and_never_printed(
        self, runner, tmp_config, fake_ssh
    ):
        secret = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ"
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
            "root@devino-second",
            stderr="root@devino-second: Permission denied (publickey).\n",
            rc=255,
        )
        result = _setup(runner, _pool_file(tmp_config), "second")
        assert result.exit_code == 1
        assert "Permission denied (publickey)." in result.stdout
        assert "ssh root@devino-second true" in result.stdout
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
        assert calls == ["amin@devino-second"]
        assert "may still be running" in result.stdout
        assert "ssh root@devino-second true" not in result.stdout
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
        assert seen == ["amin@devino-second"]
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
        assert "cannot reach amin@devino-second" in result.stdout
        assert "last step" not in result.stdout


class TestNodeSetupNeverReadsSilenceAsSuccess:
    def test_a_root_hop_that_printed_nothing_fails_each_user(
        self, runner, tmp_config, fake_ssh, fake_gh
    ):
        # Exit 0 and not one row (a ForceCommand, a MOTD-only login): setup.sh
        # never ran, and "Ready." would be a lie.
        _pc_key()
        _gh_can_add_keys(fake_gh)
        result = _setup(
            runner, _pool_file(tmp_config), "second", "--user", "amin", "--user", "bob"
        )
        assert result.exit_code == 1
        assert "no node key came back for amin" in result.stdout
        assert "no node key came back for bob" in result.stdout
        assert "Ready." not in result.stdout
        assert len(fake_ssh.calls()) == 1

    def test_a_user_whose_failure_has_its_own_row_gets_no_second_one(
        self, runner, tmp_config, fake_ssh, fake_gh
    ):
        _pc_key()
        _node_answers(
            fake_ssh, users=("amin",), setup_extra="fail\tuser:bob\tuseradd: nope\n"
        )
        _gh_can_add_keys(fake_gh)
        result = _setup(
            runner, _pool_file(tmp_config), "second", "--user", "amin", "--user", "bob"
        )
        assert "no node key came back" not in result.stdout
        assert "1 step(s) failed." in result.stdout

    def test_a_doctor_that_printed_nothing_is_a_fail(
        self, runner, tmp_config, fake_ssh, fake_gh
    ):
        _pc_key()
        fake_ssh.set_reply(
            "root@devino-second",
            stdout="key\tamin\tssh-ed25519 AAAAAMIN magent@devino-second\n",
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
        _node_answers(fake_ssh, users=("amin", "bob"))
        _gh_can_add_keys(fake_gh)
        _setup(
            runner, _pool_file(tmp_config), "second", "--user", "amin", "--user", "bob"
        )
        assert seen == [("amin@devino-second", True), ("bob@devino-second", True)]
