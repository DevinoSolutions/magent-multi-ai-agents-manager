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
from pathlib import Path

import pytest

from magent import agent_state, cli
from magent.cli import status as status_mod


def _no_psmux(monkeypatch):
    monkeypatch.setattr(
        "magent.launch.psmux_status", lambda cfg, group=None: ([], [], [])
    )


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
            "agents": [],
            "psmux_sessions": [],
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
            "agents": [],
            "psmux_sessions": [],
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
