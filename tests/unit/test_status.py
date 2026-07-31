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


def _no_psmux(monkeypatch):
    monkeypatch.setattr(
        "magent.launch.psmux_status", lambda cfg, group=None: ([], [], [])
    )


def _both_off(monkeypatch):
    """Baseline: upload server unreachable/absent, listener not running."""
    monkeypatch.setattr("magent.cli.status._health_check", lambda port: False)
    monkeypatch.setattr("magent.cli.status._probe_port", lambda port: False)
    monkeypatch.setattr("magent.upload_server.server_pid", lambda port: None)
    monkeypatch.setattr("magent.cli.status.pid_alive", lambda pid: False)
    if sys.platform == "win32":
        monkeypatch.setattr("magent.hotkey.listener_pid", lambda: None)
    else:
        monkeypatch.setattr("magent.cli.status._listener_state", lambda: "off")


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


class TestListenerLiveness:
    def test_heartbeat_not_fresh_with_live_pid_means_stale_exit_3(
        self, runner, tmp_config, monkeypatch
    ):
        _no_psmux(monkeypatch)
        _both_off(monkeypatch)
        monkeypatch.setattr(
            "magent.cli.status._health_check", lambda port: True
        )  # upload healthy
        if sys.platform == "win32":
            monkeypatch.setattr("magent.hotkey.listener_pid", lambda: 9999)
            monkeypatch.setattr("magent.cli.status.heartbeat_fresh", lambda name: False)
        else:
            monkeypatch.setattr("magent.cli.status._listener_state", lambda: "stale")
        cfgpath = tmp_config({"projects": []})

        result = runner.invoke(cli.main, ["--config", cfgpath, "status"])

        assert result.exit_code == 3
        assert "STALE" in result.output


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
        monkeypatch.setattr("magent.launch.kill_psmux", lambda targets: None)
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
