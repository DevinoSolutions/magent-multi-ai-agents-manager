"""Unit tests for `magent status` -- real liveness (HTTP /health + hook
heartbeat) instead of presence-only port/pid probes (F-IC-005), plus the new
degraded exit code and --json (R2's status half).

End-to-end through status_cmd via click.testing.CliRunner. psmux_status is
monkeypatched to avoid touching real psmux; an explicit --config <path> (like
test_up_json / test_main_dry_run_dispatch in test_cli_smoke.py) sidesteps
config *discovery* entirely, so no test ever searches the real filesystem.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from magent import agent_state, cli
from magent.cli import status as status_mod
from magent.config import SCHEMA_VERSION


def _no_psmux(monkeypatch):
    monkeypatch.setattr(
        "magent.launch.psmux_status", lambda cfg, group=None: ([], [], [])
    )
    # The Session-0 scan walks the machine's REAL process list, so leaving it
    # live would make every status assertion depend on whether this developer
    # (or this runner) happens to have ssh'd in lately.
    monkeypatch.setattr("magent.cli.status.session0_server_pids", list)


def _both_off(monkeypatch):
    """Baseline: upload server unreachable/absent, listener off BY DESIGN.

    The listener half is stubbed at ``_listener_state`` on every platform (it
    takes the upload state as an argument since the serve daemon supervises it).
    Tests that care about the listener's own state machine drive
    ``_listener_state`` directly -- see TestListenerStateMachine -- rather than
    reaching through this baseline.
    """
    monkeypatch.setattr("magent.cli.status._health_check", lambda port: False)
    monkeypatch.setattr("magent.cli.status._probe_port", lambda port: False)
    monkeypatch.setattr("magent.upload_server.server_pid", lambda port: None)
    monkeypatch.setattr("magent.cli.status.pid_alive", lambda pid: False)
    monkeypatch.setattr("magent.cli.status._listener_state", lambda upload: "off")


def _listener(monkeypatch, state):
    """Force the rendered listener state, whatever the real machine is doing."""
    monkeypatch.setattr("magent.cli.status._listener_state", lambda upload: state)


def _fake_psmux(monkeypatch, up, projects=None, apps=None, down=()):
    """Pretend psmux reports `up` live sessions whose panes run `apps`.

    Never touches a real psmux server: `psmux_status` (the liveness fan-out)
    and `pane_current_commands` (the foreground-app fan-out) are both faked, so
    this machine's ~40 real sessions are never probed.
    """
    monkeypatch.setattr(
        "magent.launch.psmux_status",
        lambda cfg, group=None: (up, list(down), projects if projects else up),
    )
    monkeypatch.setattr("magent.psmux.find_psmux", lambda: "psmux")
    monkeypatch.setattr(
        "magent.psmux.pane_current_commands",
        lambda names, psmux=None: {n: (apps or {}).get(n, "") for n in names},
    )


class TestNoConfig:
    """Pin: preserved from before the liveness-probe change."""

    def test_exit_1_when_no_config(self, runner, tmp_path):
        result = runner.invoke(
            cli.main, ["--config", str(tmp_path / "nope.json"), "status"]
        )
        assert result.exit_code == 1

    def test_json_exit_1_when_no_config(self, runner, tmp_path):
        result = runner.invoke(
            cli.main, ["--config", str(tmp_path / "nope.json"), "status", "--json"]
        )
        assert result.exit_code == 1
        # P3-04: one error envelope shape across every CLI JSON surface.
        assert json.loads(result.stdout) == {"ok": False, "error": "No config found."}


class TestJsonInvalidConfig:
    """NF-S3-005: when the config EXISTS but is invalid, status --json must
    still emit a parseable JSON error envelope on stdout (not a plain-text
    stderr line) -- mirroring the already-JSON missing-config path."""

    def test_json_invalid_config_emits_json_error_exit_1(self, runner, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{ not valid json")
        result = runner.invoke(cli.main, ["--config", str(bad), "status", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["error"]


class TestStatusLines:
    def test_prints_upload_server_and_listener_lines(
        self, runner, tmp_config, monkeypatch
    ):
        # Pin: the report's two daemon lines are the status contract,
        # independent of the liveness probes' actual state.
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert "Upload server" in result.output
        assert "Alt+V listener" in result.output

    def test_both_off_is_healthy_exit_0(self, runner, tmp_config, monkeypatch):
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert result.exit_code == 0
        assert "off" in result.output


class TestUploadServerLiveness:
    def test_health_check_true_means_on_exit_0(self, runner, tmp_config, monkeypatch):
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        monkeypatch.setattr("magent.cli.status._health_check", lambda port: True)
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert result.exit_code == 0
        assert "ON" in result.output

    def test_health_false_but_port_open_means_dead_exit_3(
        self, runner, tmp_config, monkeypatch
    ):
        # The exact "reports ON while dead" bug (F-IC-005), now surfaced.
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        monkeypatch.setattr("magent.cli.status._probe_port", lambda port: True)
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert result.exit_code == 3
        assert "DEAD" in result.output

    def test_health_false_but_pid_alive_means_dead_exit_3(
        self, runner, tmp_config, monkeypatch
    ):
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        monkeypatch.setattr("magent.upload_server.server_pid", lambda port: 4321)
        monkeypatch.setattr("magent.cli.status.pid_alive", lambda pid: pid == 4321)
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert result.exit_code == 3
        assert "DEAD" in result.output


class TestDeadUploadServerWithNoWatchdog:
    """A red line the user cannot act on is half an answer. The attention
    daemon is the upload server's supervisor, so a DEAD server with the daemon
    off is a fault that nothing will repair on its own -- and the report says
    so, once, next to the line it explains."""

    def _dead_server(self, monkeypatch, *, attention: str):
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        monkeypatch.setattr("magent.cli.status._probe_port", lambda port: True)
        monkeypatch.setattr("magent.cli.status._attention_state", lambda: attention)
        # The suite-wide isolation fixture pins the supervisor OFF (so no unit
        # test can spawn a real server); the hint is only offered when it WOULD
        # supervise, so this tier asks for the shipped default back. `status`
        # only reads the flag -- it never starts anything.
        monkeypatch.setenv("MAGENT_UPLOAD_SUPERVISOR", "1")
        monkeypatch.setattr("magent.env._cached_env", None)

    def test_the_repair_names_the_daemon(self, runner, tmp_config, monkeypatch):
        self._dead_server(monkeypatch, attention="off")
        cfgpath = tmp_config({"projects": [], "settings": {"uploadServer": True}})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert status_mod.UPLOAD_WATCHDOG_HINT in result.output

    def test_a_running_daemon_needs_no_hint(self, runner, tmp_config, monkeypatch):
        # It is already watching; the next tick revives the server.
        self._dead_server(monkeypatch, attention="on")
        cfgpath = tmp_config({"projects": [], "settings": {"uploadServer": True}})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert status_mod.UPLOAD_WATCHDOG_HINT not in result.output

    def test_no_hint_when_the_config_has_no_upload_server(
        self, runner, tmp_config, monkeypatch
    ):
        self._dead_server(monkeypatch, attention="off")
        cfgpath = tmp_config({"projects": [], "settings": {"uploadServer": False}})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert status_mod.UPLOAD_WATCHDOG_HINT not in result.output

    def test_no_hint_when_the_user_owns_the_servers_lifetime(
        self, runner, tmp_config, monkeypatch
    ):
        # Advice that would do nothing: with MAGENT_UPLOAD_SUPERVISOR=0 the
        # daemon deliberately supervises nothing.
        self._dead_server(monkeypatch, attention="off")
        monkeypatch.setenv("MAGENT_UPLOAD_SUPERVISOR", "0")
        monkeypatch.setattr("magent.env._cached_env", None)
        cfgpath = tmp_config({"projects": [], "settings": {"uploadServer": True}})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert status_mod.UPLOAD_WATCHDOG_HINT not in result.output


@pytest.mark.skipif(sys.platform != "win32", reason="hotkey is Windows-only")
class TestListenerStateMachine:
    """The listener's four states, driven directly.

    The headline is ``dead``: no listener, on a hotkey-capable platform, while
    the upload server is SERVING. Before the serve daemon supervised the
    listener, that exact situation -- observed live, a listener last started 8
    days and one reboot earlier -- rendered as `off  (starts with `magent
    attach`)` and exit 0: a benign default hiding a broken Alt+V.
    """

    def _machine(self, monkeypatch, *, pid, fresh=True, supervised=True):
        monkeypatch.setattr("magent.hotkey.listener_pid", lambda: pid)
        monkeypatch.setattr("magent.cli.status.heartbeat_fresh", lambda name: fresh)
        monkeypatch.setattr(
            "magent.upload_server.supervision_enabled", lambda: supervised
        )

    def test_live_pid_and_fresh_heartbeat_is_on(self, monkeypatch):
        self._machine(monkeypatch, pid=4242, fresh=True)
        assert status_mod._listener_state("on") == "on"

    def test_live_pid_with_expired_heartbeat_is_stale(self, monkeypatch):
        self._machine(monkeypatch, pid=4242, fresh=False)
        assert status_mod._listener_state("on") == "stale"

    def test_no_listener_while_the_server_serves_is_dead(self, monkeypatch):
        self._machine(monkeypatch, pid=None)
        assert status_mod._listener_state("on") == "dead"

    def test_no_listener_and_no_server_is_off_not_dead(self, monkeypatch):
        # Nobody promised a listener: serve is what supervises one.
        self._machine(monkeypatch, pid=None)
        assert status_mod._listener_state("off") == "off"

    def test_a_dead_server_does_not_also_report_a_dead_listener(self, monkeypatch):
        # The upload line already says DEAD; a second red line for the
        # downstream symptom would be noise, not information.
        self._machine(monkeypatch, pid=None)
        assert status_mod._listener_state("dead") == "off"

    def test_no_listener_is_not_dead_when_supervision_is_opted_out(self, monkeypatch):
        # MAGENT_HOTKEY_SUPERVISOR=0 means the user owns the listener's
        # lifetime. Reporting DEAD would invent a promise nobody made.
        self._machine(monkeypatch, pid=None, supervised=False)
        assert status_mod._listener_state("on") == "off"

    def test_a_wedged_listener_is_still_stale_with_supervision_off(self, monkeypatch):
        # Who STARTS it has no bearing on whether a running one is healthy.
        self._machine(monkeypatch, pid=4242, fresh=False, supervised=False)
        assert status_mod._listener_state("on") == "stale"


def test_listener_state_is_off_where_the_platform_has_no_hotkey(
    monkeypatch, fake_platform
):
    # Cross-platform: a machine that cannot run the listener is never "dead"
    # for not running it, however healthy the upload server is.
    monkeypatch.setattr("magent.platform.get_platform", lambda: fake_platform)
    assert status_mod._listener_state("on") == "off"


class TestListenerRendering:
    """What the three states actually put on screen, and what they exit with."""

    def _render(self, runner, tmp_config, monkeypatch, state, *, serving=True):
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        monkeypatch.setattr("magent.cli.status._health_check", lambda port: serving)
        _listener(monkeypatch, state)
        cfgpath = tmp_config({"projects": []})
        return runner.invoke(cli.main, ["--config", cfgpath, "status"])

    def test_dead_is_red_actionable_and_exit_3(self, runner, tmp_config, monkeypatch):
        result = self._render(runner, tmp_config, monkeypatch, "dead")

        assert result.exit_code == 3
        assert "upload server is up but no listener" in result.output
        assert "Alt+V does nothing" in result.output
        assert status_mod.LISTENER_REPAIR_HINT in result.output

    def test_stale_is_red_actionable_and_exit_3(self, runner, tmp_config, monkeypatch):
        result = self._render(runner, tmp_config, monkeypatch, "stale")

        assert result.exit_code == 3
        assert "STALE" in result.output
        assert status_mod.LISTENER_REPAIR_HINT in result.output

    def test_off_names_the_new_owner_and_stays_exit_0(
        self, runner, tmp_config, monkeypatch
    ):
        monkeypatch.setattr("magent.upload_server.supervision_enabled", lambda: True)
        result = self._render(runner, tmp_config, monkeypatch, "off", serving=False)

        assert result.exit_code == 0
        # The old hint said "starts with `magent attach`", which stopped being
        # the whole truth when serve took ownership.
        assert "starts with the upload server" in result.output
        assert "`magent attach`" not in result.output
        assert status_mod.LISTENER_REPAIR_HINT not in result.output

    def test_off_says_so_differently_when_supervision_is_opted_out(
        self, runner, tmp_config, monkeypatch
    ):
        monkeypatch.setattr("magent.upload_server.supervision_enabled", lambda: False)
        result = self._render(runner, tmp_config, monkeypatch, "off", serving=False)

        assert result.exit_code == 0
        # "starts with the upload server" would be a lie here.
        assert "MAGENT_HOTKEY_SUPERVISOR" in result.output
        assert "starts with the upload server" not in result.output

    def test_on_prints_no_repair_hint(self, runner, tmp_config, monkeypatch):
        result = self._render(runner, tmp_config, monkeypatch, "on")

        assert result.exit_code == 0
        assert status_mod.LISTENER_REPAIR_HINT not in result.output

    def test_dead_is_published_in_json(self, runner, tmp_config, monkeypatch):
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        monkeypatch.setattr("magent.cli.status._health_check", lambda port: True)
        _listener(monkeypatch, "dead")
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status", "--json"])

        assert result.exit_code == 3
        assert json.loads(result.stdout)["listener"] == "dead"


class TestJson:
    def test_healthy_emits_parseable_status_and_exit_0(
        self, runner, tmp_config, monkeypatch
    ):
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        monkeypatch.setattr("magent.cli.status._health_check", lambda port: True)
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status", "--json"])

        assert result.exit_code == 0
        # P3-04: `ok: true` on success; snake_case state keys (P3-03).
        assert json.loads(result.stdout) == {
            "ok": True,
            "upload_server": "on",
            "listener": "off",
            "attention": "off",
            "node_sync": "off",
            "agents": [],
            "psmux_sessions": [],
            "psmux_session0": 0,
            "node_sessions": [],
        }

    def test_degraded_emits_parseable_status_and_exit_3(
        self, runner, tmp_config, monkeypatch
    ):
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        monkeypatch.setattr(
            "magent.cli.status._probe_port", lambda port: True
        )  # -> dead
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status", "--json"])

        assert result.exit_code == 3
        # `ok: true` even when degraded -- degraded is exit 3 + the state fields,
        # not the error discriminator (only config errors carry ok: false).
        assert json.loads(result.stdout) == {
            "ok": True,
            "upload_server": "dead",
            "listener": "off",
            "attention": "off",
            "node_sync": "off",
            "agents": [],
            "psmux_sessions": [],
            "psmux_session0": 0,
            "node_sessions": [],
        }


class TestAttentionLiveness:
    """P6-01: a crashed attention daemon -- a heartbeat file left behind with no
    live pid -- must read 'crashed' and degrade the exit code, distinct from a
    clean 'off' (never started / cleanly stopped, which removes the heartbeat)."""

    def _attention(self, monkeypatch, *, pid, fresh, age):
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)  # upload + listener both healthy-off
        monkeypatch.setattr("magent.cli.attention_cmd.daemon_pid", lambda: pid)
        monkeypatch.setattr("magent.cli.status.heartbeat_fresh", lambda name: fresh)
        monkeypatch.setattr("magent.cli.status.heartbeat_age", lambda name: age)

    def test_pid_and_fresh_heartbeat_is_on_exit_0(
        self, runner, tmp_config, monkeypatch
    ):
        self._attention(monkeypatch, pid=4242, fresh=True, age=1.0)
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert result.exit_code == 0
        assert "Attention" in result.output
        assert "CRASHED" not in result.output

    def test_pid_but_stale_heartbeat_is_stale_exit_3(
        self, runner, tmp_config, monkeypatch
    ):
        self._attention(monkeypatch, pid=4242, fresh=False, age=999.0)
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert result.exit_code == 3
        assert "STALE" in result.output

    def test_no_pid_with_lingering_heartbeat_is_crashed_exit_3(
        self, runner, tmp_config, monkeypatch
    ):
        self._attention(monkeypatch, pid=None, fresh=False, age=12.0)
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert result.exit_code == 3
        assert "CRASHED" in result.output

    def test_no_pid_no_heartbeat_is_off_exit_0(self, runner, tmp_config, monkeypatch):
        self._attention(monkeypatch, pid=None, fresh=False, age=None)
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert result.exit_code == 0
        assert "CRASHED" not in result.output
        assert "STALE" not in result.output

    def test_json_crashed_degrades_exit_3(self, runner, tmp_config, monkeypatch):
        self._attention(monkeypatch, pid=None, fresh=False, age=8.0)
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status", "--json"])

        assert result.exit_code == 3
        assert json.loads(result.stdout)["attention"] == "crashed"


class TestMenuDownServerReport:
    """NF-S3-001: the menu's 'x = all + server' path branches on
    stop_server()'s return value like down_cmd, instead of always claiming
    'Stopped upload server.' regardless of the truthful boolean."""

    def _drive(self, monkeypatch, tmp_config, stop_ok):
        from magent.cli import status as status_mod

        monkeypatch.setattr(
            "magent.launch.psmux_status",
            lambda cfg, group=None: ([{"name": "api"}], [], []),
        )
        monkeypatch.setattr(
            "magent.launch.stop_psmux", lambda targets: (list(targets), [])
        )
        monkeypatch.setattr("magent.cli.status._probe_port", lambda port: True)
        monkeypatch.setattr("magent.upload_server.stop_server", lambda port: stop_ok)
        monkeypatch.setattr(status_mod.click, "prompt", lambda *a, **k: "x")
        monkeypatch.setattr(status_mod.click, "pause", lambda *a, **k: None)
        cfgpath = tmp_config({"version": 2, "projects": [{"path": "api"}]})
        status_mod._menu_down(Path(cfgpath))

    def test_reports_stopped_when_stop_server_true(
        self, monkeypatch, tmp_config, capsys
    ):
        self._drive(monkeypatch, tmp_config, stop_ok=True)
        out = capsys.readouterr().out
        assert "Stopped upload server on port" in out

    def test_reports_failure_when_stop_server_false(
        self, monkeypatch, tmp_config, capsys
    ):
        self._drive(monkeypatch, tmp_config, stop_ok=False)
        out = capsys.readouterr().out
        assert "could not be stopped" in out
        assert "Stopped upload server on port" not in out


class TestMenuUpReportsCasualties:
    """The menu's `u` tells the truth about what came up.

    Live repro: one project's session was refused by psmux, `launch_verified`
    logged "session never came up after respawn; left down: ...", and the menu
    printed "+ Brought up 2 session(s) headlessly" anyway -- `bring_up`
    discarded the verify's answer and returned every attempted name.
    """

    def _drive(self, monkeypatch, tmp_config, *, created, failed):
        from magent.cli import status as status_mod

        monkeypatch.setattr(
            "magent.launch.psmux_status",
            lambda cfg, group=None: ([], [{"name": "api", "session": "api"}], [{}]),
        )
        monkeypatch.setattr(
            "magent.launch.bring_up_psmux",
            lambda cfg, only=None, group=None: (list(created), list(failed)),
        )
        monkeypatch.setattr(status_mod.click, "prompt", lambda *a, **k: "a")
        monkeypatch.setattr(status_mod.click, "pause", lambda *a, **k: None)
        cfgpath = tmp_config({"version": 2, "projects": [{"path": "api"}]})
        status_mod._menu_up(Path(cfgpath))

    def test_failed_sessions_are_named_in_red(self, monkeypatch, tmp_config, capsys):
        self._drive(monkeypatch, tmp_config, created=["web"], failed=["api"])
        out = capsys.readouterr().out
        assert "Brought up 1 session(s)" in out
        assert "1 session(s) failed to come up" in out
        assert "api" in out

    def test_a_clean_wave_says_nothing_about_failures(
        self, monkeypatch, tmp_config, capsys
    ):
        self._drive(monkeypatch, tmp_config, created=["api", "web"], failed=[])
        out = capsys.readouterr().out
        assert "Brought up 2 session(s)" in out
        assert "failed to come up" not in out


class TestPsmuxSessionSection:
    """The agents live inside the psmux sessions, so `status` reports each live
    one's foreground app and agent state -- not just the daemons around them."""

    def _live(self, monkeypatch, tmp_path, apps, state=None, down=()):
        _both_off(monkeypatch)
        api = tmp_path / "api"
        api.mkdir()
        if state:
            agent_state.write_state(str(api), state)
        up = [{"name": "api", "session": "api", "group": "core"}]
        projects = [{"name": "api", "session": "api", "resolved": str(api)}]
        _fake_psmux(monkeypatch, up, projects, apps, down=down)

    def test_lists_live_session_with_app_and_state(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        self._live(
            monkeypatch, tmp_path, {"api": "claude"}, state=agent_state.NEEDS_INPUT
        )
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert result.exit_code == 0
        assert "api" in result.output
        assert "claude" in result.output
        assert "needs input" in result.output

    def test_bare_shell_reads_as_idle(self, runner, tmp_config, tmp_path, monkeypatch):
        self._live(monkeypatch, tmp_path, {"api": "pwsh"})
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert result.exit_code == 0
        assert "idle" in result.output
        # The shell name itself is the diagnosis, not the display.
        assert "pwsh" not in result.output

    def test_unreadable_pane_never_claims_idle(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        self._live(monkeypatch, tmp_path, {"api": ""})
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert result.exit_code == 0
        assert "idle" not in result.output

    def test_not_live_sessions_are_summarized_not_listed(
        self, runner, tmp_config, monkeypatch
    ):
        _both_off(monkeypatch)
        down = [{"name": f"p{i}", "session": f"p{i}"} for i in range(9)]
        _fake_psmux(monkeypatch, [], [], {}, down=down)
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert result.exit_code == 0
        assert "9 not running" in result.output
        assert "p8" not in result.output  # summarized: only a short preview

    def test_no_sessions_never_probes_psmux(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        # Fast path unchanged: nothing configured -> not one psmux round-trip.
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        monkeypatch.setattr(
            "magent.psmux.pane_current_commands",
            lambda names, psmux=None: pytest.fail("probed psmux with no sessions"),
        )
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert result.exit_code == 0


class TestPsmuxSessionsJson:
    def test_additive_key_carries_name_app_idle_state(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        _both_off(monkeypatch)
        api = tmp_path / "api"
        api.mkdir()
        agent_state.write_state(str(api), agent_state.WORKING)
        web = tmp_path / "web"
        web.mkdir()
        up = [
            {"name": "api", "session": "api", "group": None},
            {"name": "web", "session": "web", "group": None},
        ]
        projects = [
            {"name": "api", "session": "api", "resolved": str(api)},
            {"name": "web", "session": "web", "resolved": str(web)},
        ]
        _fake_psmux(monkeypatch, up, projects, {"api": "claude", "web": "pwsh"})
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status", "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["psmux_sessions"] == [
            {
                "name": "api",
                "app": "claude",
                "idle": False,
                "state": agent_state.WORKING,
            },
            {"name": "web", "app": "pwsh", "idle": True, "state": ""},
        ]

    def test_the_state_column_honors_the_configured_staleness(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        """The wider half of the picker's staleness bug: these rows come from
        session_picker's `_session_states`, so before the fix `status` aged its
        `agents` array with settings.attention (via engine_from_config) and its
        psmux-session table with the module defaults -- the two halves of one
        report disagreeing about the same record."""
        _both_off(monkeypatch)
        api = tmp_path / "api"
        api.mkdir()
        agent_state.write_state(str(api), agent_state.WORKING)
        # 120s old: inside the 1800s module default, well past a 10s config
        # window -- so which map is in force decides what this column says.
        rec_path = agent_state._path_for(str(api))
        rec = json.loads(rec_path.read_text(encoding="utf-8"))
        rec["ts"] = rec["ts"] - 120.0
        rec_path.write_text(json.dumps(rec), encoding="utf-8")
        _fake_psmux(
            monkeypatch,
            [{"name": "api", "session": "api"}],
            [{"name": "api", "session": "api", "resolved": str(api)}],
            {"api": "claude"},
        )
        cfgpath = tmp_config(
            {"projects": [], "settings": {"attention": {"stalenessWorkingS": 10}}}
        )

        result = runner.invoke(cli.main, ["--config", cfgpath, "status", "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["psmux_sessions"] == [
            {"name": "api", "app": "claude", "idle": False, "state": ""}
        ]

    def test_the_default_config_still_reports_that_same_working_record(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        """Control for the pin above, and the half that makes it a pin at all:
        the identical 120s-old record under an untouched config keeps its
        `working` state. Without this, a `_psmux_sessions` that blanked the
        column for any reason -- a broken lookup, a hardcoded window, a
        re-imported `attention.STALENESS_S` that happened to be small -- would
        pass the configured-window test by accident. Paired, the two say the
        column is aged by whatever `staleness_from_config` answers and nothing
        else."""
        _both_off(monkeypatch)
        api = tmp_path / "api"
        api.mkdir()
        agent_state.write_state(str(api), agent_state.WORKING)
        rec_path = agent_state._path_for(str(api))
        rec = json.loads(rec_path.read_text(encoding="utf-8"))
        rec["ts"] = rec["ts"] - 120.0
        rec_path.write_text(json.dumps(rec), encoding="utf-8")
        _fake_psmux(
            monkeypatch,
            [{"name": "api", "session": "api"}],
            [{"name": "api", "session": "api", "resolved": str(api)}],
            {"api": "claude"},
        )
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status", "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["psmux_sessions"] == [
            {
                "name": "api",
                "app": "claude",
                "idle": False,
                "state": agent_state.WORKING,
            }
        ]

    def test_existing_envelope_and_exit_codes_are_undisturbed(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        # Additive only: a live psmux session is not a degraded daemon.
        _both_off(monkeypatch)
        api = tmp_path / "api"
        api.mkdir()
        _fake_psmux(
            monkeypatch,
            [{"name": "api", "session": "api"}],
            [{"name": "api", "session": "api", "resolved": str(api)}],
            {"api": "pwsh"},
        )
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status", "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["upload_server"] == "off"
        assert payload["listener"] == "off"
        assert payload["attention"] == "off"
        assert payload["agents"] == []


class TestSessionActions:
    """The interactive status flow can act on what it just listed: attach to a
    session, or revive one whose agent fell back to a bare shell."""

    def _rows(self):
        return [
            {"name": "api", "app": "claude", "idle": False, "state": ""},
            {"name": "web", "app": "pwsh", "idle": True, "state": ""},
        ]

    def _drive(self, monkeypatch, tmp_config, choice):
        from magent.cli import status as status_mod

        monkeypatch.setattr(status_mod.click, "prompt", lambda *a, **k: choice)
        monkeypatch.setattr(status_mod.click, "pause", lambda *a, **k: None)
        opened: list[str] = []
        revived: list[str] = []
        monkeypatch.setattr(status_mod, "_open_session", opened.append)
        monkeypatch.setattr(
            status_mod, "_revive_session", lambda cfg_file, sid: revived.append(sid)
        )
        status_mod._session_actions(
            Path(tmp_config({"version": 3, "projects": []})), self._rows()
        )
        return opened, revived

    def test_digit_opens_that_session(self, monkeypatch, tmp_config):
        assert self._drive(monkeypatch, tmp_config, "1") == (["api"], [])

    def test_r_digit_revives_that_session(self, monkeypatch, tmp_config):
        assert self._drive(monkeypatch, tmp_config, "r2") == ([], ["web"])

    def test_quit_does_nothing(self, monkeypatch, tmp_config):
        assert self._drive(monkeypatch, tmp_config, "q") == ([], [])

    def test_out_of_range_does_nothing(self, monkeypatch, tmp_config, capsys):
        assert self._drive(monkeypatch, tmp_config, "9") == ([], [])
        assert "Invalid choice" in capsys.readouterr().out

    def test_bare_r_does_nothing(self, monkeypatch, tmp_config):
        assert self._drive(monkeypatch, tmp_config, "r") == ([], [])

    def test_open_reuses_the_pickers_attach_path(self, monkeypatch):
        from magent.cli import status as status_mod

        monkeypatch.setattr("magent.psmux.find_psmux", lambda: "psmux")
        seen: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "magent.cli.session_picker._attach_session",
            lambda binary, target, reset: seen.append((binary, target)),
        )
        status_mod._open_session("api")
        assert seen == [("psmux", "api")]

    def test_open_without_psmux_is_reported_not_crashed(self, monkeypatch, capsys):
        from magent.cli import status as status_mod

        monkeypatch.setattr("magent.psmux.find_psmux", lambda: None)
        status_mod._open_session("api")
        assert "psmux not found" in capsys.readouterr().out

    def test_revive_targets_only_that_session(self, monkeypatch, tmp_config, capsys):
        from magent.cli import status as status_mod

        calls: list[list[str] | None] = []

        def _fake_revive(cfg, only=None, group=None):
            calls.append(only)
            return only or []

        monkeypatch.setattr("magent.psmux.revive_sessions", _fake_revive)
        status_mod._revive_session(
            Path(tmp_config({"version": 3, "projects": []})), "web"
        )
        assert calls == [["web"]]
        assert "Revived" in capsys.readouterr().out

    def test_revive_reports_a_no_op_truthfully(self, monkeypatch, tmp_config, capsys):
        from magent.cli import status as status_mod

        monkeypatch.setattr(
            "magent.psmux.revive_sessions", lambda cfg, only=None, group=None: []
        )
        status_mod._revive_session(
            Path(tmp_config({"version": 3, "projects": []})), "web"
        )
        out = capsys.readouterr().out
        assert "Nothing to revive" in out
        assert "Revived" not in out

    def test_menu_status_prompts_only_when_sessions_are_listed(
        self, monkeypatch, tmp_config, tmp_path
    ):
        from magent.cli import status as status_mod

        _both_off(monkeypatch)
        api = tmp_path / "api"
        api.mkdir()
        _fake_psmux(
            monkeypatch,
            [{"name": "api", "session": "api"}],
            [{"name": "api", "session": "api", "resolved": str(api)}],
            {"api": "claude"},
        )
        acted: list[list[dict[str, object]]] = []
        monkeypatch.setattr(
            status_mod, "_session_actions", lambda cf, sessions: acted.append(sessions)
        )
        monkeypatch.setattr(status_mod.click, "pause", lambda *a, **k: None)
        status_mod._menu_status(Path(tmp_config({"version": 3, "projects": []})))
        assert [s["name"] for s in acted[0]] == ["api"]

    def test_menu_status_falls_back_to_a_pause_with_no_sessions(
        self, monkeypatch, tmp_config
    ):
        from magent.cli import status as status_mod

        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        monkeypatch.setattr(
            status_mod,
            "_session_actions",
            lambda cf, sessions: pytest.fail("prompted with nothing to act on"),
        )
        paused: list[bool] = []
        monkeypatch.setattr(
            status_mod.click, "pause", lambda *a, **k: paused.append(True)
        )
        status_mod._menu_status(Path(tmp_config({"version": 3, "projects": []})))
        assert paused == [True]


class TestAgentsRollup:
    """WIN (P6): the human status report summarizes how many agents are waiting
    on you when any session is needs-input/error; silent otherwise."""

    def test_rollup_counts_waiting_agents(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        api = tmp_path / "api"
        api.mkdir()
        agent_state.write_state(str(api), agent_state.NEEDS_INPUT)
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert result.exit_code == 0
        assert "1 agent(s) need you" in result.output
        assert "api" in result.output

    def test_error_state_also_counts(self, runner, tmp_config, tmp_path, monkeypatch):
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        for name in ("api", "web"):
            d = tmp_path / name
            d.mkdir()
            agent_state.write_state(str(d), agent_state.ERROR)
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert result.exit_code == 0
        assert "2 agent(s) need you" in result.output

    def test_no_rollup_when_nothing_waiting(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        api = tmp_path / "api"
        api.mkdir()
        agent_state.write_state(str(api), agent_state.WORKING)  # not waiting
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert result.exit_code == 0
        assert "need you" not in result.output


class TestDownStopsWhatItPromisesAndReportsWhatItProved:
    """The reported bug, from both ends.

    Field report 1 (30 running / 15 stopped, then `down --all` naming only the
    config-order TAIL): `down` iterated the liveness snapshot, so every session
    that snapshot missed was neither stopped nor mentioned -- "these stay
    always".

    Field report 2 (`down --all` printed "Stopped 46" while 11 were still alive
    and attachable): the report was the length of the list the command had
    TRIED, and nothing re-probed.
    """

    def _run(self, runner, tmp_config, monkeypatch, argv, *, up, configured, result):
        cfgpath = tmp_config({"projects": [{"path": "myapp"}]})
        monkeypatch.setattr(
            "magent.launch.psmux_status",
            lambda cfg, group=None: (
                [{"name": n, "session": n} for n in up],
                [],
                [{"name": n, "session": n} for n in configured],
            ),
        )
        killed: list[list[str]] = []
        monkeypatch.setattr(
            "magent.launch.stop_psmux",
            lambda targets: (killed.append(list(targets)), result)[1],
        )
        monkeypatch.setattr("magent.cli.attach._read_last_host", lambda: None)
        monkeypatch.setattr("magent.upload_server.stop_server", lambda port: False)
        monkeypatch.setattr("magent.cli.attention_cmd.stop_daemon", lambda: False)
        if sys.platform == "win32":
            monkeypatch.setattr("magent.hotkey.stop_listener", lambda: False)
        out = runner.invoke(cli.main, ["--config", cfgpath, "down", *argv])
        return out, killed

    def test_all_kills_sessions_the_liveness_probe_missed(
        self, runner, tmp_config, monkeypatch
    ):
        # `--all` says "stop EVERY psmux session". The probe saw only the head
        # of the config; the tail must still be killed, because kill-server
        # against a dead socket is a harmless no-op and skipping it is the bug.
        _out, killed = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--all"],
            up=("api",),
            configured=("api", "web", "db"),
            result=(["api", "web", "db"], []),
        )
        assert killed == [["api", "web", "db"]]

    def test_a_named_selection_matches_config_not_only_the_live_snapshot(
        self, runner, tmp_config, monkeypatch
    ):
        _out, killed = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["web"],
            up=("api",),
            configured=("api", "web"),
            result=(["web"], []),
        )
        assert killed == [["web"]]

    def test_the_report_names_only_verified_stops(
        self, runner, tmp_config, monkeypatch
    ):
        out, _killed = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--all"],
            up=("api", "web"),
            configured=("api", "web"),
            result=(["api"], ["web"]),
        )
        assert "Stopped 1 session(s): api" in out.output
        assert "Stopped 2 session(s)" not in out.output

    def test_survivors_are_named_loudly(self, runner, tmp_config, monkeypatch):
        out, _killed = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--all"],
            up=("api", "web"),
            configured=("api", "web"),
            result=(["api"], ["web"]),
        )
        assert "1 session(s) would NOT stop: web" in out.output

    def test_nothing_running_says_so_instead_of_claiming_a_shutdown(
        self, runner, tmp_config, monkeypatch
    ):
        out, killed = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--all"],
            up=(),
            configured=("api",),
            result=([], []),
        )
        # Still ATTEMPTED (the probe may be the thing that is wrong)...
        assert killed == [["api"]]
        # ...but nothing is claimed.
        assert "No running sessions to stop." in out.output
        assert "Stopped" not in out.output.split("Upload server")[0]


class TestDownStopsANodeProjectsOrphanedLocalSession:
    """A project ran here, then gained ``"node": "second"``. From then on every
    local psmux path skips it (it runs on the node), so its old LOCAL session
    vanished from status, the picker and `down` -- alive and unstoppable.

    `down` acting locally therefore also targets the in-scope node projects'
    session ids. Killing a socket with no server is a no-op, and the report
    names only what the re-probe PROVED stopped, so a node project with no
    local session costs nothing and claims nothing.
    """

    def _run(self, runner, tmp_config, monkeypatch, argv, *, projects, live_local):
        cfgpath = tmp_config(
            {
                "version": SCHEMA_VERSION,
                "settings": {"nodes": {"second": {"host": "devino-second"}}},
                "projects": projects,
            }
        )
        # The REAL psmux_status -- it is the thing that hides the node project.
        # Only the psmux binary is substituted: its lookup and its liveness probe.
        monkeypatch.setattr("magent.psmux.find_psmux", lambda *a, **k: "psmux")
        monkeypatch.setattr(
            "magent.psmux.live_sessions",
            lambda names, *a, **k: [n for n in names if n in live_local],
        )
        killed: list[list[str]] = []

        def fake_stop(targets):
            killed.append(list(targets))
            return [t for t in targets if t in live_local], []

        monkeypatch.setattr("magent.launch.stop_psmux", fake_stop)
        # PR-D: `down` now also asks each node for its own session. These tests
        # pin the LOCAL half, so the node half answers "not running there";
        # TestDownStopsNodeSessionsWhereTheyRun owns the node half.
        self.node_calls: list[list[str]] = []

        def no_node_session(cfg, sids):
            self.node_calls.append(list(sids))
            return [], []

        monkeypatch.setattr("magent.launch.stop_node_sessions", no_node_session)
        monkeypatch.setattr("magent.cli.attach._read_last_host", lambda: None)
        monkeypatch.setattr("magent.upload_server.stop_server", lambda port: False)
        monkeypatch.setattr("magent.cli.attention_cmd.stop_daemon", lambda: False)
        if sys.platform == "win32":
            monkeypatch.setattr("magent.hotkey.stop_listener", lambda: False)
        out = runner.invoke(cli.main, ["--config", cfgpath, "down", *argv])
        return out, killed

    def test_down_all_stops_the_orphaned_local_session(
        self, runner, tmp_config, monkeypatch, tmp_path
    ):
        out, killed = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--all"],
            projects=[{"path": str(tmp_path / "api"), "node": "second"}],
            live_local={"api"},
        )
        assert out.exit_code == 0, out.output
        assert killed == [["api"]]
        assert "Stopped 1 session(s): api" in out.output

    def test_a_node_project_with_no_local_session_is_tried_but_never_claimed(
        self, runner, tmp_config, monkeypatch, tmp_path
    ):
        out, killed = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--all"],
            projects=[{"path": str(tmp_path / "api"), "node": "second"}],
            live_local=set(),
        )
        assert out.exit_code == 0, out.output
        assert killed == [["api"]]
        assert "No running sessions to stop." in out.output
        assert "Stopped" not in out.output.split("Upload server")[0]
        assert "would NOT stop" not in out.output

    def test_local_and_node_targets_go_to_one_stop_call(
        self, runner, tmp_config, monkeypatch, tmp_path
    ):
        (tmp_path / "web").mkdir()
        out, killed = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--all"],
            projects=[
                {"path": str(tmp_path / "web")},
                {"path": str(tmp_path / "api"), "node": "second"},
            ],
            live_local={"web", "api"},
        )
        assert out.exit_code == 0, out.output
        assert killed == [["web", "api"]]
        assert self.node_calls == [["api"]]  # a local id is never dialed
        assert "Stopped 2 session(s): web, api" in out.output

    @pytest.mark.parametrize(("name", "expected"), [("api", "api"), ("web", "web")])
    def test_a_named_down_selects_node_sids_like_any_other(
        self, runner, tmp_config, monkeypatch, tmp_path, name, expected
    ):
        (tmp_path / "web").mkdir()
        _out, killed = self._run(
            runner,
            tmp_config,
            monkeypatch,
            [name],
            projects=[
                {"path": str(tmp_path / "web")},
                {"path": str(tmp_path / "api"), "node": "second"},
            ],
            live_local={"web", "api"},
        )
        assert killed == [[expected]]

    def test_the_group_scope_applies_to_node_projects(
        self, runner, tmp_config, monkeypatch, tmp_path
    ):
        _out, killed = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--group", "a"],
            projects=[
                {"path": str(tmp_path / "api"), "node": "second", "group": "a"},
                {"path": str(tmp_path / "db"), "node": "second", "group": "b"},
            ],
            live_local={"api", "db"},
        )
        assert killed == [["api"]]

    def test_a_cloud_project_is_already_a_local_target_and_not_doubled(
        self, runner, tmp_config, monkeypatch, tmp_path
    ):
        (tmp_path / "sky").mkdir()
        _out, killed = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--all"],
            projects=[{"path": str(tmp_path / "sky"), "node": "cloud"}],
            live_local={"sky"},
        )
        assert killed == [["sky"]]

    def test_forwarding_to_a_host_kills_nothing_here(
        self, runner, tmp_config, monkeypatch, tmp_path
    ):
        from magent.cli import attach as attach_mod

        monkeypatch.setattr(
            attach_mod,
            "_ssh_capture",
            lambda target, remote_cmd, timeout=30, stdin_text=None: (0, "", ""),
        )
        monkeypatch.setattr(attach_mod, "_close_attach_windows", lambda names: 0)
        _out, killed = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--host", "user@box", "--all"],
            projects=[{"path": str(tmp_path / "api"), "node": "second"}],
            live_local={"api"},
        )
        assert killed == []
        assert self.node_calls == []


class TestDownActsOnTheAttachHost:
    """`magent down` on an attach CLIENT used to be a near no-op: there are no
    local psmux sessions on a laptop, so it stopped the local Alt+V listener and
    nothing else -- while `attach`'s own goodbye line advertises that exact
    command for stopping the sessions it just opened.

    Now: an explicit --host always wins, and with nothing matching locally the
    shutdown auto-targets the remembered attach host. On the HOST itself local
    sessions match, so the auto path never fires there.
    """

    def _run(
        self,
        runner,
        tmp_config,
        monkeypatch,
        argv,
        *,
        up=(),
        configured=None,
        last_host=None,
        rc=0,
        out="",
        err="",
    ):
        from magent.cli import attach as attach_mod

        cfgpath = tmp_config({"projects": [{"path": "myapp"}]})
        rows = [{"name": n, "session": n} for n in up]
        # The third element is what `down` KILLS (every configured eligible
        # session); `up` is only what decides local-vs-remote.
        projects = [
            {"name": n, "session": n}
            for n in (up if configured is None else configured)
        ]
        monkeypatch.setattr(
            "magent.launch.psmux_status", lambda cfg, group=None: (rows, [], projects)
        )
        killed: list[list[str]] = []
        monkeypatch.setattr(
            "magent.launch.stop_psmux",
            lambda targets: (killed.append(list(targets)), (list(targets), []))[1],
        )
        monkeypatch.setattr(attach_mod, "_read_last_host", lambda: last_host)
        closed: list[list[str]] = []
        monkeypatch.setattr(
            attach_mod,
            "_close_attach_windows",
            lambda names: (closed.append(list(names)), len(list(names)))[1],
        )
        sent: list[tuple[str, str]] = []

        def fake_ssh(target, remote_cmd, timeout=30, stdin_text=None):
            sent.append((target, remote_cmd))
            return rc, out, err

        monkeypatch.setattr(attach_mod, "_ssh_capture", fake_ssh)
        # The local daemon half is unchanged by this feature; keep it inert so
        # no test touches a real server/listener/daemon.
        monkeypatch.setattr("magent.upload_server.stop_server", lambda port: False)
        monkeypatch.setattr("magent.cli.attention_cmd.stop_daemon", lambda: False)
        if sys.platform == "win32":
            monkeypatch.setattr("magent.hotkey.stop_listener", lambda: False)

        result = runner.invoke(cli.main, ["--config", cfgpath, "down", *argv])
        return result, sent, killed, closed

    def test_explicit_host_wins_even_with_live_local_sessions(
        self, runner, tmp_config, monkeypatch
    ):
        result, sent, killed, _ = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--host", "user@box", "--all"],
            up=("api",),
        )
        assert result.exit_code == 0
        assert sent == [("user@box", "magent down --all")]
        assert killed == []  # nothing local was stopped

    def test_auto_targets_the_remembered_host_when_nothing_matches_locally(
        self, runner, tmp_config, monkeypatch
    ):
        result, sent, killed, _ = self._run(
            runner, tmp_config, monkeypatch, ["--all"], up=(), last_host="me@host"
        )
        assert result.exit_code == 0
        assert sent == [("me@host", "magent down --all")]
        assert killed == []

    def test_live_local_sessions_keep_the_shutdown_local(
        self, runner, tmp_config, monkeypatch
    ):
        # This is the HOST case: sessions match here, so a remembered attach
        # host must never hijack the shutdown.
        result, sent, killed, _ = self._run(
            runner, tmp_config, monkeypatch, [], up=("api",), last_host="me@host"
        )
        assert result.exit_code == 0
        assert sent == []
        assert killed == [["api"]]

    def test_no_local_sessions_and_no_remembered_host_stays_local(
        self, runner, tmp_config, monkeypatch
    ):
        result, sent, killed, _ = self._run(runner, tmp_config, monkeypatch, [])
        assert result.exit_code == 0
        assert sent == []
        assert killed == []
        assert "No matching sessions in config." in result.output

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            ([], "magent down"),
            (["--all"], "magent down --all"),
            (["--server"], "magent down --server"),
            (["-g", "core"], 'magent down -g "core"'),
            (["api", "web"], 'magent down "api" "web"'),
            (["api", "--server"], 'magent down "api" --server'),
        ],
        ids=["bare", "all", "server", "group", "names", "name+server"],
    )
    def test_selection_is_forwarded_verbatim(
        self, runner, tmp_config, monkeypatch, argv, expected
    ):
        result, sent, _killed, _ = self._run(
            runner, tmp_config, monkeypatch, ["--host", "u@h", *argv]
        )
        assert result.exit_code == 0
        assert sent == [("u@h", expected)]

    def test_named_selection_closes_only_those_local_windows(
        self, runner, tmp_config, monkeypatch
    ):
        _result, _sent, _killed, closed = self._run(
            runner, tmp_config, monkeypatch, ["--host", "u@h", "api"]
        )
        assert closed == [["api"]]

    def test_all_closes_every_local_attach_window(
        self, runner, tmp_config, monkeypatch
    ):
        # An empty selection means "every magent: window": killing the remote
        # psmux servers makes every attached ssh client exit at once.
        _result, _sent, _killed, closed = self._run(
            runner, tmp_config, monkeypatch, ["--host", "u@h", "--all"]
        )
        assert closed == [[]]

    def test_ssh_failure_exits_non_zero(self, runner, tmp_config, monkeypatch):
        result, sent, _killed, _ = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--host", "u@h", "--all"],
            rc=255,
            err="ssh: connect to host u@h port 22: Connection refused",
        )
        assert result.exit_code == 255
        assert len(sent) == 1
        assert "did not run the shutdown" in result.output

    def test_remote_output_is_surfaced(self, runner, tmp_config, monkeypatch):
        result, _sent, _killed, _ = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--host", "u@h", "--all"],
            out="  + Stopped 4 session(s): api, web, db, ops\n",
        )
        assert "Stopped 4 session(s): api, web, db, ops" in result.output

    def test_local_daemon_stops_still_run_on_a_remote_all(
        self, runner, tmp_config, monkeypatch
    ):
        # Unchanged behavior: --all still stops THIS machine's upload server,
        # Alt+V listener and attention daemon (the remote `--all` handles the
        # host's own copies).
        result, _sent, _killed, _ = self._run(
            runner, tmp_config, monkeypatch, ["--host", "u@h", "--all"]
        )
        assert "Upload server not running" in result.output
        assert "Attention daemon was not running." in result.output


class TestSessionZeroServers:
    """`status` is the surface a user is already looking at when a session
    refuses to come up, and a psmux server stranded in logon Session 0 is the
    reason: it holds the name (the ~/.psmux registry is shared) while being
    invisible to this desktop."""

    def _world(self, monkeypatch, pids):
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        monkeypatch.setattr(
            "magent.cli.status.session0_server_pids", lambda: list(pids)
        )

    def test_a_clean_machine_says_nothing(self, runner, tmp_config, monkeypatch):
        self._world(monkeypatch, [])
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert "Session 0" not in result.output

    def test_stranded_servers_are_reported_with_the_repair(
        self, runner, tmp_config, monkeypatch
    ):
        self._world(monkeypatch, [11, 22])
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert "2 psmux server(s) run in logon Session 0" in result.output
        assert "elevated shell" in result.output

    def test_it_does_not_change_the_exit_contract(
        self, runner, tmp_config, monkeypatch
    ):
        # These servers are nothing this magent started or can stop, so they
        # are not a DEGRADED daemon -- the 0/1/3 contract every script reads
        # must not move because somebody once ssh'd into this box.
        self._world(monkeypatch, [11])
        monkeypatch.setattr("magent.cli.status._health_check", lambda port: True)
        monkeypatch.setattr("magent.cli.status._listener_state", lambda up: "off")
        monkeypatch.setattr("magent.cli.status._attention_state", lambda: "off")
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert result.exit_code == 0

    def test_the_json_count_is_additive(self, runner, tmp_config, monkeypatch):
        self._world(monkeypatch, [11, 22, 33])
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status", "--json"])

        assert json.loads(result.stdout)["psmux_session0"] == 3


class TestStatusShowsNodeSessions:
    """A node session's row comes from the sync daemon's last pull, never
    from a live ssh call -- and a stale one is a row state, not a degraded
    daemon, so the 0/1/3 exit contract is untouched."""

    def _config(self, tmp_config, tmp_path):
        return tmp_config(
            {
                "projects": [{"path": str(tmp_path), "title": "api", "node": "second"}],
                "settings": {
                    "nodes": {"second": {"host": "devino-second", "user": "amin"}}
                },
            }
        )

    def _snapshot(self, monkeypatch, tmp_path, ts):
        from magent import nodes

        monkeypatch.setattr(nodes, "NODES_DIR", tmp_path / "nodes")
        monkeypatch.setattr(nodes, "NODE_MAP_PATH", tmp_path / "node-map.json")
        nodes.write_json_atomic(
            nodes.sessions_path("second"), {"ts": ts, "sessions": ["api"]}
        )

    def test_json_carries_the_node_and_its_state(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        monkeypatch.setattr("magent.cli.status._health_check", lambda port: True)
        self._snapshot(monkeypatch, tmp_path, ts=time.time())
        result = runner.invoke(
            cli.main,
            ["--config", self._config(tmp_config, tmp_path), "status", "--json"],
        )
        assert json.loads(result.stdout)["node_sessions"] == [
            {"name": "api", "session": "api", "node": "second", "state": "live"}
        ]

    def test_the_node_key_sits_right_after_psmux_session0(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        self._snapshot(monkeypatch, tmp_path, ts=time.time())
        result = runner.invoke(
            cli.main,
            ["--config", self._config(tmp_config, tmp_path), "status", "--json"],
        )
        keys = list(json.loads(result.stdout))
        assert keys[keys.index("psmux_session0") + 1] == "node_sessions"

    def test_an_unreachable_node_reads_stale_and_is_not_degraded(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        monkeypatch.setattr("magent.cli.status._health_check", lambda port: True)
        self._snapshot(monkeypatch, tmp_path, ts=0.0)
        result = runner.invoke(
            cli.main,
            ["--config", self._config(tmp_config, tmp_path), "status", "--json"],
        )
        assert result.exit_code == 0
        assert json.loads(result.stdout)["node_sessions"][0]["state"] == "stale"

    def test_the_report_lists_them_with_their_node(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        self._snapshot(monkeypatch, tmp_path, ts=time.time())
        result = runner.invoke(
            cli.main, ["--config", self._config(tmp_config, tmp_path), "status"]
        )
        assert "Nodes" in result.stdout
        assert "api" in result.stdout
        assert "@second" in result.stdout
        assert "live" in result.stdout

    def test_a_stale_report_row_does_not_degrade_the_exit(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        monkeypatch.setattr("magent.cli.status._health_check", lambda port: True)
        self._snapshot(monkeypatch, tmp_path, ts=0.0)
        result = runner.invoke(
            cli.main, ["--config", self._config(tmp_config, tmp_path), "status"]
        )
        assert result.exit_code == 0
        assert "stale" in result.stdout

    def test_an_unplaced_auto_project_reads_not_placed(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        self._snapshot(monkeypatch, tmp_path, ts=time.time())
        cfg = tmp_config(
            {
                "projects": [{"path": str(tmp_path), "title": "new", "node": "auto"}],
                "settings": {
                    "nodes": {"second": {"host": "devino-second", "user": "amin"}}
                },
            }
        )
        result = runner.invoke(cli.main, ["--config", cfg, "status"])
        assert "(not placed)" in result.stdout
        assert "dead" in result.stdout

    def test_the_report_prints_the_session_id_not_the_title(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        # The map's sid is what runs on the node; the title only names the row.
        from magent import nodes

        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        monkeypatch.setattr(nodes, "NODES_DIR", tmp_path / "nodes")
        monkeypatch.setattr(nodes, "NODE_MAP_PATH", tmp_path / "node-map.json")
        nodes.write_json_atomic(
            nodes.sessions_path("second"),
            {"ts": time.time(), "sessions": ["api-old"]},
        )
        nodes.update_node_map(
            "api",
            nodes.NodeMapEntry(
                nick="second",
                sid="api-old",
                placed_ts=1.0,
                attached_existing=False,
                remote_root="/home/amin/magent/api",
            ),
        )
        result = runner.invoke(
            cli.main, ["--config", self._config(tmp_config, tmp_path), "status"]
        )
        assert "api-old" in result.stdout
        assert "live" in result.stdout

    def test_an_unreadable_node_map_reads_stale_never_dead(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        # The cq-D17 probe, end to end: a placed auto project and a pinned one
        # under a mapped sid, both live, then the map torn in half. Neither
        # row may read dead or unplaced, status must not raise, and a stale
        # row still does not degrade the exit.
        from magent import nodes

        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        monkeypatch.setattr("magent.cli.status._health_check", lambda port: True)
        monkeypatch.setattr(nodes, "NODES_DIR", tmp_path / "nodes")
        node_map = tmp_path / "node-map.json"
        monkeypatch.setattr(nodes, "NODE_MAP_PATH", node_map)
        nodes.write_json_atomic(
            nodes.sessions_path("second"),
            {"ts": time.time(), "sessions": ["api", "web-old"]},
        )
        for name, sid in (("api", "api"), ("web", "web-old")):
            nodes.update_node_map(
                name,
                nodes.NodeMapEntry(
                    nick="second",
                    sid=sid,
                    placed_ts=1.0,
                    attached_existing=False,
                    remote_root=f"/home/amin/magent/{name}",
                ),
            )
        for sub in ("api", "web"):
            (tmp_path / sub).mkdir()
        cfgpath = tmp_config(
            {
                "projects": [
                    {"path": str(tmp_path / "api"), "title": "api", "node": "auto"},
                    {"path": str(tmp_path / "web"), "title": "web", "node": "second"},
                ],
                "settings": {
                    "nodes": {"second": {"host": "devino-second", "user": "amin"}}
                },
            }
        )
        text = node_map.read_text(encoding="utf-8")
        node_map.write_text(text[: len(text) // 2], encoding="utf-8")

        result = runner.invoke(cli.main, ["--config", cfgpath, "status", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout)["node_sessions"] == [
            {"name": "api", "session": "api", "node": None, "state": "stale"},
            {"name": "web", "session": "web", "node": "second", "state": "stale"},
        ]
        human = runner.invoke(cli.main, ["--config", cfgpath, "status"])
        assert human.exit_code == 0
        nodes_block = human.stdout.split("Nodes", 1)[1]
        assert "stale" in nodes_block
        assert "dead" not in nodes_block
        assert "(not placed)" not in nodes_block
        assert "(node unknown)" in nodes_block

    def test_a_config_without_node_projects_prints_no_nodes_section(
        self, runner, tmp_config, monkeypatch
    ):
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        result = runner.invoke(
            cli.main, ["--config", tmp_config({"projects": []}), "status"]
        )
        assert "Nodes" not in result.stdout


class TestAStaleNodeSyncDaemonDegradesStatus:
    """Exit 3 when a project runs on a node and the sync daemon's heartbeat has
    gone stale: every node row would then be frozen at a pull nobody refreshes.
    A STOPPED daemon is not degraded (serve starts one; with serve off the
    upload-server line already says so), and without a node project -- or
    with the MAGENT_NODE_SYNC switch off -- nobody expects a daemon at all.
    Driven through the real heartbeat file; the switch is set back on here
    (conftest turns it off for every tier)."""

    def _config(self, tmp_config, tmp_path, *, on_node=True, tool=None):
        project = {"path": str(tmp_path), "title": "api"}
        if on_node:
            project["node"] = "second"
        if tool:
            project["tool"] = tool
        return tmp_config(
            {
                "projects": [project],
                "settings": {
                    "nodes": {"second": {"host": "devino-second", "user": "amin"}}
                },
            }
        )

    def _beat(self, age_s):
        """The daemon's heartbeat, last touched ``age_s`` seconds ago."""
        import os

        from magent import log, node_sync

        log.write_heartbeat(node_sync.HEARTBEAT_NAME)
        path = log.HEARTBEAT_DIR / f"{node_sync.HEARTBEAT_NAME}.heartbeat"
        then = time.time() - age_s
        os.utime(path, (then, then))

    def _status(self, runner, cfgpath, *extra):
        return runner.invoke(cli.main, ["--config", cfgpath, "status", *extra])

    @staticmethod
    def _switch(monkeypatch, value):
        """MAGENT_NODE_SYNC, re-read: the env singleton is cached."""
        monkeypatch.setenv("MAGENT_NODE_SYNC", value)
        monkeypatch.setattr("magent.env._cached_env", None)

    @pytest.fixture(autouse=True)
    def _healthy_otherwise(self, monkeypatch, tmp_path):
        from magent import nodes

        self._switch(monkeypatch, "1")
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        monkeypatch.setattr("magent.cli.status._health_check", lambda port: True)
        monkeypatch.setattr(nodes, "NODES_DIR", tmp_path / "nodes")
        monkeypatch.setattr(nodes, "NODE_MAP_PATH", tmp_path / "node-map.json")

    def test_stale_with_a_node_project_exits_3_and_says_so(
        self, runner, tmp_config, tmp_path
    ):
        from magent import log

        self._beat(log.HEARTBEAT_MAX_AGE + 30)
        result = self._status(runner, self._config(tmp_config, tmp_path), "--json")
        assert result.exit_code == 3
        assert json.loads(result.stdout)["node_sync"] == "stale"

    def test_the_report_names_the_stale_daemon_and_its_repair(
        self, runner, tmp_config, tmp_path
    ):
        from magent import log

        self._beat(log.HEARTBEAT_MAX_AGE + 30)
        result = self._status(runner, self._config(tmp_config, tmp_path))
        assert result.exit_code == 3
        assert "node sync daemon stale" in result.stdout
        assert status_mod.NODE_SYNC_REPAIR_HINT in result.stdout
        assert "magent node sync --stop" in status_mod.NODE_SYNC_REPAIR_HINT

    def test_stale_without_a_node_project_changes_nothing(
        self, runner, tmp_config, tmp_path
    ):
        from magent import log

        self._beat(log.HEARTBEAT_MAX_AGE + 30)
        cfgpath = self._config(tmp_config, tmp_path, on_node=False)
        result = self._status(runner, cfgpath, "--json")
        assert result.exit_code == 0
        assert json.loads(result.stdout)["node_sync"] == "off"
        human = self._status(runner, cfgpath)
        assert human.exit_code == 0
        assert "node sync daemon stale" not in human.stdout

    def test_status_expects_a_daemon_exactly_when_serve_spawns_one(
        self, runner, tmp_config, tmp_path
    ):
        # A node-pinned IDE project has no node SESSION (it stays on this PC),
        # but serve still spawns the daemon for it (node_sync.wanted) -- so a
        # stale one is degraded, and is named even with no Nodes rows to show.
        from magent import log

        self._beat(log.HEARTBEAT_MAX_AGE + 30)
        cfgpath = self._config(tmp_config, tmp_path, tool="code")
        result = self._status(runner, cfgpath, "--json")
        assert result.exit_code == 3
        payload = json.loads(result.stdout)
        assert (payload["node_sync"], payload["node_sessions"]) == ("stale", [])
        human = self._status(runner, cfgpath)
        assert human.exit_code == 3
        assert "Nodes" in human.stdout
        assert "node sync daemon stale" in human.stdout
        assert status_mod.NODE_SYNC_REPAIR_HINT in human.stdout

    def test_a_fresh_heartbeat_is_ok_and_exits_0(self, runner, tmp_config, tmp_path):
        self._beat(1)
        cfgpath = self._config(tmp_config, tmp_path)
        result = self._status(runner, cfgpath, "--json")
        assert result.exit_code == 0
        assert json.loads(result.stdout)["node_sync"] == "ok"
        human = self._status(runner, cfgpath)
        assert human.exit_code == 0
        assert "node sync daemon stale" not in human.stdout

    def test_a_stopped_daemon_is_not_degraded(self, runner, tmp_config, tmp_path):
        cfgpath = self._config(tmp_config, tmp_path)
        result = self._status(runner, cfgpath, "--json")
        assert result.exit_code == 0
        assert json.loads(result.stdout)["node_sync"] == "stopped"
        human = self._status(runner, cfgpath)
        assert human.exit_code == 0
        # Named, as the JSON names it -- but never as the degraded "stale".
        assert status_mod.NODE_SYNC_STOPPED_LINE in human.stdout
        assert "node sync daemon stale" not in human.stdout
        assert status_mod.NODE_SYNC_REPAIR_HINT not in human.stdout

    def test_the_switch_off_means_no_daemon_is_expected(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        # serve's own gate is the switch AND wanted: a heartbeat left by a
        # daemon that died before MAGENT_NODE_SYNC=0 was set is nobody's, and
        # "serve starts a fresh one" would be a promise serve never keeps.
        from magent import log

        self._beat(log.HEARTBEAT_MAX_AGE + 30)
        self._switch(monkeypatch, "0")
        cfgpath = self._config(tmp_config, tmp_path)
        result = self._status(runner, cfgpath, "--json")
        assert result.exit_code == 0
        assert json.loads(result.stdout)["node_sync"] == "off"
        human = self._status(runner, cfgpath)
        assert human.exit_code == 0
        assert "node sync daemon" not in human.stdout
        assert "serve starts a fresh one" not in human.stdout

    def test_the_verdict_counts_node_sync_alongside_the_other_daemons(self):
        healthy = {
            "upload_server": "on",
            "listener": "on",
            "attention": "on",
        }
        for state, degraded in (
            ("stale", True),
            ("ok", False),
            ("stopped", False),
            ("off", False),
        ):
            assert (
                status_mod._is_degraded({**healthy, "node_sync": state}) is degraded
            ), state


class TestDownStopsNodeSessionsWhereTheyRun:
    """PR-D: a node project's id names TWO sessions -- the one on its node,
    and the local one it may have left here before it gained a ``node`` (D9).
    `down` kills each exactly once, on its own path (``stop_psmux`` here,
    ``remote_mux.kill_session`` there), and reports the id once: never "No
    running sessions to stop." above "Stopped 1 session(s): api"."""

    @pytest.fixture(autouse=True)
    def _map(self, monkeypatch, tmp_path):
        from magent import nodes

        monkeypatch.setattr(nodes, "NODE_MAP_PATH", tmp_path / "node-map.json")

    def _run(
        self,
        runner,
        tmp_config,
        monkeypatch,
        argv,
        *,
        projects,
        live_local=frozenset(),
        answers=None,
        last_host=None,
        real_stop=False,
        pull=None,
        sync_daemon=False,
    ):
        from magent import node_sync
        from magent.cli import attach as attach_mod

        cfgpath = tmp_config(
            {
                "version": SCHEMA_VERSION,
                "settings": {
                    "nodes": {
                        "second": {"host": "devino-second", "user": "amin"},
                        "third": {"host": "devino-third", "user": "amin"},
                    }
                },
                "projects": projects,
            }
        )
        killed: list[list[str]] = []
        if real_stop:
            # The REAL stop_psmux on a machine with no psmux at all: it answers
            # ([], []) -- the half that used to print "No running sessions to
            # stop." above the node half's "Stopped".
            monkeypatch.setattr("magent.psmux.find_psmux", lambda *a, **k: None)
        else:
            monkeypatch.setattr("magent.psmux.find_psmux", lambda *a, **k: "psmux")
            monkeypatch.setattr(
                "magent.psmux.live_sessions",
                lambda names, *a, **k: [n for n in names if n in live_local],
            )

            def fake_stop(targets):
                killed.append(list(targets))
                return [t for t in targets if t in live_local], []

            monkeypatch.setattr("magent.launch.stop_psmux", fake_stop)
        dialed: list[tuple[str, str]] = []

        def kill(node, sid):
            dialed.append((node.nick, sid))
            return (answers or {}).get(sid, True)

        monkeypatch.setattr("magent.remote_mux.kill_session", kill)
        monkeypatch.setattr(attach_mod, "_read_last_host", lambda: last_host)
        sent: list[tuple[str, str]] = []

        def fake_ssh(target, remote_cmd, timeout=30, stdin_text=None):
            sent.append((target, remote_cmd))
            return 0, "", ""

        monkeypatch.setattr(attach_mod, "_ssh_capture", fake_ssh)
        monkeypatch.setattr(attach_mod, "_close_attach_windows", lambda names: 0)
        monkeypatch.setattr("magent.upload_server.stop_server", lambda port: False)
        monkeypatch.setattr("magent.cli.attention_cmd.stop_daemon", lambda: False)
        if sys.platform == "win32":
            monkeypatch.setattr("magent.hotkey.stop_listener", lambda: False)
        # The last turn comes home before each kill (Task 16): a pull that
        # never dials unless the test says how it goes.
        monkeypatch.setattr(
            node_sync, "final_pull", pull or (lambda config, name, **_k: None)
        )
        monkeypatch.setattr(node_sync, "stop_daemon", lambda: sync_daemon)
        out = runner.invoke(cli.main, ["--config", cfgpath, "down", *argv])
        return out, killed, dialed, sent

    @staticmethod
    def _hold(name, nick="second", sid=None):
        from magent import nodes
        from magent.nodes import NodeMapEntry

        nodes.update_node_map(
            name,
            NodeMapEntry(
                nick=nick,
                sid=sid or name,
                placed_ts=1.0,
                attached_existing=False,
                remote_root=f"~/magent/{name}",
            ),
        )

    @staticmethod
    def _session_lines(out):
        return out.output.split("Upload server")[0]

    def test_the_node_half_is_the_only_report_when_nothing_runs_here(
        self, runner, tmp_config, monkeypatch, tmp_path
    ):
        # THE regression: the local half found nothing, the node half killed
        # api. One truthful line -- not "No running sessions", then "Stopped".
        from magent import nodes

        self._hold("api")
        out, _killed, dialed, sent = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["api"],
            projects=[{"path": str(tmp_path / "api"), "node": "second"}],
            real_stop=True,
        )
        assert out.exit_code == 0, out.output
        assert dialed == [("second", "api")]
        assert sent == []
        lines = self._session_lines(out)
        assert "No running sessions to stop." not in lines
        assert lines.count("Stopped") == 1
        assert "Stopped 1 session(s): api" in lines
        assert nodes.read_node_map() == {}

    def test_an_orphan_here_and_a_session_there_are_two_kills_and_one_name(
        self, runner, tmp_config, monkeypatch, tmp_path
    ):
        self._hold("api")
        out, killed, dialed, _sent = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--all"],
            projects=[{"path": str(tmp_path / "api"), "node": "second"}],
            live_local={"api"},
        )
        assert out.exit_code == 0, out.output
        # Each session exactly once, each on its own path.
        assert killed == [["api"]]
        assert dialed == [("second", "api")]
        lines = self._session_lines(out)
        assert lines.count("Stopped") == 1
        assert "Stopped 1 session(s): api" in lines

    def test_local_and_node_sessions_share_one_stopped_line(
        self, runner, tmp_config, monkeypatch, tmp_path
    ):
        (tmp_path / "web").mkdir()
        self._hold("api")
        out, killed, dialed, _sent = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--all"],
            projects=[
                {"path": str(tmp_path / "web")},
                {"path": str(tmp_path / "api"), "node": "second"},
            ],
            live_local={"web"},
        )
        assert killed == [["web", "api"]]
        assert dialed == [("second", "api")]
        assert "Stopped 2 session(s): web, api" in self._session_lines(out)

    def test_a_node_that_cannot_be_asked_is_named_and_never_claimed(
        self, runner, tmp_config, monkeypatch, tmp_path
    ):
        # The local orphan died, but the node session may still be running:
        # the name is a survivor -- not a stop, and not "nothing to stop".
        from magent import nodes

        self._hold("api")
        out, _killed, _dialed, _sent = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["api"],
            projects=[{"path": str(tmp_path / "api"), "node": "second"}],
            live_local={"api"},
            answers={"api": None},
        )
        assert out.exit_code == 0, out.output
        lines = self._session_lines(out)
        assert "1 session(s) would NOT stop: api" in lines
        assert "nodes.log" in lines
        assert "Stopped" not in lines
        assert "No running sessions to stop." not in lines
        assert "api" in nodes.read_node_map()

    def test_nothing_anywhere_still_says_so(
        self, runner, tmp_config, monkeypatch, tmp_path
    ):
        out, killed, dialed, _sent = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--all"],
            projects=[{"path": str(tmp_path / "api"), "node": "second"}],
            answers={"api": False},
        )
        assert killed == [["api"]]
        assert dialed == [("second", "api")]
        lines = self._session_lines(out)
        assert "No running sessions to stop." in lines
        assert "Stopped" not in lines

    def test_a_session_placed_from_here_keeps_down_off_the_attach_host(
        self, runner, tmp_config, monkeypatch, tmp_path
    ):
        # Nothing psmux-live here, but this PC's node map placed api: only a
        # LOCAL down reaches it, so the remembered host must not take over --
        # and the user is told how to reach the host's sessions anyway.
        self._hold("api")
        out, _killed, dialed, sent = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--all"],
            projects=[{"path": str(tmp_path / "api"), "node": "second"}],
            last_host="me@host",
        )
        assert out.exit_code == 0, out.output
        assert sent == []
        assert dialed == [("second", "api")]
        hints = [ln for ln in out.output.splitlines() if "--host" in ln]
        assert len(hints) == 1, out.output
        assert "magent down --host me@host" in hints[0]

    @pytest.mark.parametrize(
        ("answer", "report"),
        [
            (None, "1 session(s) would NOT stop: api"),
            (False, "No running sessions to stop."),
        ],
        ids=["unreachable", "already-gone"],
    )
    def test_the_hint_never_claims_a_stop_the_report_did_not(
        self, runner, tmp_config, monkeypatch, tmp_path, answer, report
    ):
        # The hint follows the report whatever it said: after a survivor or
        # "nothing to stop", a hint reading "Stopped" would be the very
        # contradiction the folded report exists to remove.
        self._hold("api")
        out, _killed, _dialed, sent = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--all"],
            projects=[{"path": str(tmp_path / "api"), "node": "second"}],
            answers={"api": answer},
            last_host="me@host",
        )
        assert out.exit_code == 0, out.output
        assert sent == []
        lines = self._session_lines(out)
        assert report in lines
        assert "magent down --host me@host" in lines
        assert "Stopped" not in lines

    def test_a_placement_with_no_remembered_host_prints_no_hint(
        self, runner, tmp_config, monkeypatch, tmp_path
    ):
        self._hold("api")
        out, _killed, dialed, sent = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--all"],
            projects=[{"path": str(tmp_path / "api"), "node": "second"}],
        )
        assert out.exit_code == 0, out.output
        assert (sent, dialed) == ([], [("second", "api")])
        assert "--host" not in out.output

    def test_a_down_that_live_sessions_keep_local_prints_no_hint(
        self, runner, tmp_config, monkeypatch, tmp_path
    ):
        # Local work was running: `down` was local before PR-D too, so the
        # placement is not the reason and there is nothing new to say.
        (tmp_path / "web").mkdir()
        self._hold("api")
        out, _killed, _dialed, sent = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--all"],
            projects=[
                {"path": str(tmp_path / "web")},
                {"path": str(tmp_path / "api"), "node": "second"},
            ],
            live_local={"web"},
            last_host="me@host",
        )
        assert out.exit_code == 0, out.output
        assert sent == []
        assert "--host" not in out.output

    def test_a_node_project_only_in_config_still_forwards_to_the_attach_host(
        self, runner, tmp_config, monkeypatch, tmp_path
    ):
        # An attach client sharing the host's config holds no placement: its
        # `down --all` is still the host's, exactly as before PR-D.
        out, killed, dialed, sent = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--all"],
            projects=[{"path": str(tmp_path / "api"), "node": "second"}],
            last_host="me@host",
        )
        assert out.exit_code == 0, out.output
        assert sent == [("me@host", "magent down --all")]
        assert killed == []
        assert dialed == []

    def test_an_explicit_host_dials_no_node(
        self, runner, tmp_config, monkeypatch, tmp_path
    ):
        self._hold("api")
        out, killed, dialed, sent = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--host", "u@h", "--all"],
            projects=[{"path": str(tmp_path / "api"), "node": "second"}],
            live_local={"api"},
        )
        assert sent == [("u@h", "magent down --all")]
        assert killed == []
        assert dialed == []
        assert "magent down --host" not in out.output

    def test_where_down_acts_and_what_it_kills_read_one_placement(
        self, runner, tmp_config, monkeypatch, tmp_path
    ):
        # The map records api under a session id other than the derived one:
        # the node half kills that one, so `down` must also count it as
        # placed here and stay off the attach host.
        self._hold("api", sid="api-2")
        out, _killed, dialed, sent = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--all"],
            projects=[{"path": str(tmp_path / "api"), "node": "second"}],
            last_host="me@host",
        )
        assert out.exit_code == 0, out.output
        assert sent == []
        assert dialed == [("second", "api-2")]

    def test_a_retitled_placed_project_keeps_down_here(
        self, runner, tmp_config, monkeypatch, tmp_path
    ):
        # Placed as "my.web", retitled "my web" since: the name misses, the
        # recorded sid still names its running session. Found by sid, so it
        # counts as placed here and never forwards to the attach host.
        self._hold("my.web", "third", sid="my-web")
        out, _killed, dialed, sent = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--all"],
            projects=[
                {"path": str(tmp_path / "web"), "title": "my web", "node": "auto"}
            ],
            last_host="me@host",
        )
        assert out.exit_code == 0, out.output
        assert sent == []
        assert dialed == [("third", "my-web")]

    @pytest.mark.parametrize("state", ["torn", "busy"])
    def test_an_unreadable_map_is_a_survivor_line_not_nothing_to_stop(
        self, runner, tmp_config, monkeypatch, tmp_path, state
    ):
        from magent import nodes

        if state == "torn":
            nodes.NODE_MAP_PATH.write_text("{ torn", encoding="utf-8")
        else:
            # Intact on disk, but another process has it open (Windows).
            self._hold("web", "third")

            def busy() -> dict[str, nodes.NodeMapEntry]:
                raise PermissionError(13, "The process cannot access the file")

            monkeypatch.setattr(nodes, "load_node_map_strict", busy)
        out, _killed, dialed, _sent = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--all"],
            projects=[{"path": str(tmp_path / "web"), "node": "auto"}],
        )
        assert out.exit_code == 0, out.output
        assert dialed == []
        lines = self._session_lines(out)
        assert "1 session(s) would NOT stop: web" in lines
        assert "No running sessions to stop." not in lines

    def test_down_all_stops_the_node_sync_daemon(
        self, runner, tmp_config, monkeypatch, tmp_path
    ):
        out, *_ = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--all"],
            projects=[{"path": str(tmp_path / "api"), "node": "second"}],
            real_stop=True,
            sync_daemon=True,
        )
        assert out.exit_code == 0, out.output
        assert "Stopped the node sync daemon." in out.stdout

    def test_down_all_with_node_projects_says_the_daemon_was_not_running(
        self, runner, tmp_config, monkeypatch, tmp_path
    ):
        out, *_ = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["--all"],
            projects=[{"path": str(tmp_path / "api"), "node": "second"}],
            real_stop=True,
        )
        assert out.exit_code == 0, out.output
        assert "Node sync daemon was not running." in out.stdout

    def test_down_of_one_name_leaves_the_node_sync_daemon_alone(
        self, runner, tmp_config, monkeypatch, tmp_path
    ):
        # stop_daemon would answer True: only --all may ask it.
        out, *_ = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["api"],
            projects=[{"path": str(tmp_path / "api"), "node": "second"}],
            real_stop=True,
            sync_daemon=True,
        )
        assert out.exit_code == 0, out.output
        assert "node sync daemon" not in out.stdout.lower()

    def test_a_config_without_nodes_says_nothing_about_it(
        self, runner, tmp_config, monkeypatch
    ):
        from magent import node_sync

        monkeypatch.setattr("magent.psmux.find_psmux", lambda *a, **k: None)
        monkeypatch.setattr(node_sync, "stop_daemon", lambda: False)
        monkeypatch.setattr("magent.cli.attention_cmd.stop_daemon", lambda: False)
        monkeypatch.setattr("magent.upload_server.stop_server", lambda port: False)
        if sys.platform == "win32":
            monkeypatch.setattr("magent.hotkey.stop_listener", lambda: False)
        result = runner.invoke(
            cli.main, ["--config", tmp_config({"projects": []}), "down", "--all"]
        )
        assert result.exit_code == 0, result.output
        assert "node sync" not in result.stdout.lower()

    def test_a_last_turn_that_did_not_come_home_is_said_and_the_kill_still_counts(
        self, runner, tmp_config, monkeypatch, tmp_path
    ):
        from magent import nodes
        from magent.remote_mux import RemoteError

        self._hold("api")

        def pull(config: object, name: str, **_k: object) -> None:
            raise RemoteError(0, "magent: could not store every file", ("pull.sh",))

        out, _killed, dialed, _sent = self._run(
            runner,
            tmp_config,
            monkeypatch,
            ["api"],
            projects=[{"path": str(tmp_path / "api"), "node": "second"}],
            real_stop=True,
            pull=pull,
        )
        assert out.exit_code == 0, out.output
        assert dialed == [("second", "api")]
        lines = self._session_lines(out)
        assert "api: last turn not pulled (could not store every file)" in lines
        assert "magent node sync --once" in lines
        assert "Stopped 1 session(s): api" in lines
        assert "api" in nodes.read_node_map()


class TestTheShutdownReportFoldsBothHalves:
    """``_report_shutdown`` directly: one name, two sessions, no line that
    contradicts another."""

    def _report(self, capsys, *halves):
        status_mod._report_shutdown(*halves)
        return capsys.readouterr().out

    def test_a_local_survivor_is_never_claimed_by_a_node_stop(self, capsys):
        out = self._report(capsys, [], ["api"], ["api"], [])
        assert "Stopped" not in out
        assert out.count("would NOT stop: api") == 1
        assert "launch.log" in out

    def test_survivors_on_both_halves_each_get_their_reason(self, capsys):
        out = self._report(capsys, [], ["api"], [], ["api"])
        assert out.count("would NOT stop: api") == 2
        assert "launch.log" in out
        assert "nodes.log" in out
        assert "Stopped" not in out
        assert "No running sessions" not in out

    def test_no_node_half_prints_what_it_always_did(self, capsys):
        assert self._report(capsys, ["web"], []) == ("  + Stopped 1 session(s): web\n")
