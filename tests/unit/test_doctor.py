"""Tests for `magent doctor` (cli/doctor.py) — every check function in
isolation with fakes, plus the CLI exit-code and --json contracts."""

from __future__ import annotations

import json
import os
import subprocess
import types
from pathlib import Path

import pytest
from click.testing import CliRunner

from magent import cli, wt_keys
from magent import env as env_module
from magent import psmux as psmux_mod
from magent.cli import doctor
from magent.cli.doctor import (
    FAIL,
    OK,
    WARN,
    WEDGE_REPAIR_HINT,
    _check_agent_tools,
    _check_claude_token,
    _check_config,
    _check_hotkey,
    _check_idle_reap,
    _check_monitors,
    _check_nodes,
    _check_psmux_wedge,
    _check_sentry,
    _check_tailscale,
    _check_upload_port,
    _monitor_topology,
)
from magent.config import SCHEMA_VERSION, load_config
from magent.grid import MonitorRect
from magent.remote_mux import ScriptLine
from tests.conftest import FakePlatform

# A settings file nested past the JSON parser's depth: json.loads raises
# RecursionError on it, which is not a ValueError.
_NESTED = '{"actions": ' + "[" * 200_000 + "]" * 200_000 + "}"


class TestCheckConfig:
    def test_missing_config_fails_with_init_hint(self, tmp_path):
        (result, cfg) = _check_config(tmp_path / "nope.json")
        assert result[0] == FAIL
        assert "--init" in result[1]
        assert cfg is None

    def test_stale_version_warns_with_migrate_hint(self, tmp_config):
        path = tmp_config({"version": 1, "projects": [{"path": "api"}]})
        (result, cfg) = _check_config(Path(path))
        assert result[0] == WARN
        assert "migrate" in result[1]
        assert cfg is not None

    def test_current_config_ok(self, tmp_config):
        path = tmp_config({"version": SCHEMA_VERSION, "projects": [{"path": "api"}]})
        (result, cfg) = _check_config(Path(path))
        assert result[0] == OK
        assert cfg is not None

    def test_text_with_no_utf8_form_fails_in_our_words(self, tmp_config):
        # F-SUR-1: a colorless title with a lone surrogate used to escape this
        # check as the tab-color hash's UnicodeEncodeError (not a ConfigError),
        # taking doctor down with a traceback instead of reporting the config.
        path = tmp_config(
            {
                "version": SCHEMA_VERSION,
                "projects": [{"path": "api", "title": "api\ud83d"}],
            }
        )
        (result, cfg) = _check_config(Path(path))
        assert result == (
            FAIL,
            (
                "config invalid: projects[0].title has text with no UTF-8 form"
                " (UnicodeEncodeError): 'api\\ud83d'"
            ),
        )
        assert cfg is None


class TestCheckEnv:
    def test_invalid_field_fails_naming_the_full_var(self, monkeypatch):
        monkeypatch.setenv("MAGENT_LOG_LEVEL", "BOGUS")
        status, detail = doctor._check_env()
        assert status == FAIL
        assert "MAGENT_LOG_LEVEL" in detail

    def test_clean_env_is_ok(self, monkeypatch):
        for key in list(os.environ):
            if key.upper().startswith("MAGENT_"):
                monkeypatch.delenv(key, raising=False)
        assert doctor._check_env()[0] == OK

    def test_an_env_file_that_is_not_utf8_fails_naming_it(self):
        # Exact: the item has no field name, so nothing may precede the path.
        env_module.ENV_FILE.write_bytes(b"MAGENT_LOG_LEVEL=\xff\xfe\n")
        status, detail = doctor._check_env()
        assert status == FAIL
        assert detail == (
            f"invalid environment variable(s): {env_module.ENV_FILE} is not "
            "valid UTF-8 (UnicodeDecodeError); re-save it as UTF-8 (see .env.example)"
        )

    def test_an_env_file_that_is_not_utf8_leaves_every_other_check_running(
        self, monkeypatch, tmp_config
    ):
        # Driven below the CLI: the group callback refuses a bad env before
        # any subcommand runs. The sentry check reads get_env() again, and
        # _run_checks has no per-check guard, so it has to survive the file
        # too or the checklist dies at the first check after env.
        fp = FakePlatform()
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
        monkeypatch.setattr("magent.cli.background._probe_port", lambda _p: False)
        monkeypatch.setattr("magent.cli.background._running_upload_port", lambda: None)
        env_module.ENV_FILE.write_bytes(b"MAGENT_LOG_LEVEL=\xff\xfe\n")
        config_file = Path(
            tmp_config({"version": SCHEMA_VERSION, "projects": [{"path": "api"}]})
        )

        checks = {c["name"]: c for c in doctor._run_checks(config_file)}

        assert checks["env"]["status"] == FAIL
        assert "is not valid UTF-8" in checks["env"]["detail"]
        assert checks["sentry"]["detail"].startswith("skipped")
        assert "upload port" in checks


class TestCheckAgentTools:
    def test_missing_used_tool_warns_by_name(self, monkeypatch, tmp_config):
        path = tmp_config(
            {
                "version": SCHEMA_VERSION,
                "settings": {"tools": {"claude": "claude-definitely-missing --x"}},
                "projects": [{"path": "api", "tool": "claude"}],
            }
        )
        cfg = load_config(path)
        monkeypatch.setattr(doctor.shutil, "which", lambda _cmd: None)

        status, detail = _check_agent_tools(cfg)

        assert status == WARN
        assert "claude" in detail

    def test_unused_tools_do_not_warn(self, monkeypatch, tmp_config):
        path = tmp_config(
            {
                "version": SCHEMA_VERSION,
                "settings": {"tools": {"claude": "claude", "codex": "codex"}},
                "projects": [{"path": "api", "tool": "claude"}],
            }
        )
        cfg = load_config(path)
        monkeypatch.setattr(
            doctor.shutil, "which", lambda cmd: "/x/claude" if cmd == "claude" else None
        )

        status, _detail = _check_agent_tools(cfg)

        assert status == OK


class TestCheckMonitors:
    def test_no_monitors_fails(self, monkeypatch):
        fp = FakePlatform(monitors=[])
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
        status, detail = _check_monitors()
        assert status == FAIL
        assert "tiling" in detail

    def test_monitors_ok(self, monkeypatch):
        fp = FakePlatform()
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
        assert _check_monitors()[0] == OK


class TestMonitorTopology:
    """The additive `monitors` key: exact `grid.MonitorRect` fields, and a
    never-crash contract when the platform probe fails or finds nothing."""

    def _two_monitors(self) -> list[MonitorRect]:
        return [
            MonitorRect(x=0, y=0, w=1920, h=1200, is_primary=True, scale_factor=1.5),
            MonitorRect(
                x=-2560, y=0, w=2560, h=1440, is_primary=False, scale_factor=1.0
            ),
        ]

    def test_topology_dicts_carry_every_monitorrect_field(self, monkeypatch):
        fp = FakePlatform(monitors=self._two_monitors())
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
        topo = _monitor_topology()
        assert topo == [
            {
                "x": 0,
                "y": 0,
                "w": 1920,
                "h": 1200,
                "is_primary": True,
                "scale_factor": 1.5,
            },
            {
                "x": -2560,
                "y": 0,
                "w": 2560,
                "h": 1440,
                "is_primary": False,
                "scale_factor": 1.0,
            },
        ]

    def test_empty_when_no_monitors(self, monkeypatch):
        fp = FakePlatform(monitors=[])
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
        assert _monitor_topology() == []

    def test_never_crashes_on_probe_failure(self, monkeypatch):
        class _Boom:
            def list_monitors(self):
                raise OSError("no display")

        monkeypatch.setattr("magent.platform.get_platform", _Boom)
        assert _monitor_topology() == []

    def test_json_envelope_is_additive_and_includes_monitors(
        self, runner, monkeypatch, tmp_config
    ):
        fp = FakePlatform(monitors=self._two_monitors())
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
        monkeypatch.setattr("magent.cli.background._probe_port", lambda _p: False)
        monkeypatch.setattr("magent.cli.background._running_upload_port", lambda: None)
        config_path = tmp_config(
            {"version": SCHEMA_VERSION, "projects": [{"path": "api"}]}
        )

        result = runner.invoke(cli.main, ["--config", config_path, "doctor", "--json"])

        payload = json.loads(result.stdout)
        # existing keys unchanged (purely additive)
        assert set(payload) == {"ok", "checks", "failures", "monitors"}
        assert payload["ok"] is True
        assert len(payload["monitors"]) == 2
        assert payload["monitors"][0]["scale_factor"] == 1.5

    def test_human_output_lists_each_monitor(self, runner, monkeypatch, tmp_config):
        fp = FakePlatform(monitors=self._two_monitors())
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
        monkeypatch.setattr("magent.cli.background._probe_port", lambda _p: False)
        monkeypatch.setattr("magent.cli.background._running_upload_port", lambda: None)
        config_path = tmp_config(
            {"version": SCHEMA_VERSION, "projects": [{"path": "api"}]}
        )

        result = runner.invoke(cli.main, ["--config", config_path, "doctor"])

        assert "1920x1200 @ (0,0) 150% *primary" in result.output
        assert "2560x1440 @ (-2560,0) 100%" in result.output


def _tailscale_cp(returncode: int, stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["tailscale", "ip", "-4"], returncode=returncode, stdout=stdout, stderr=""
    )


class TestCheckTailscale:
    """Characterization pins: the four WARN/OK wordings are user-facing and
    must survive the tailnet-leaf dedup (P1-01) byte-for-byte. Mocks sit at
    the shutil.which / subprocess.run boundary so the pins hold whether the
    probe lives in doctor.py or in a shared leaf."""

    def test_missing_binary_warns_loopback_only(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda _cmd: None)
        status, detail = _check_tailscale()
        assert status == WARN
        assert "loopback" in detail

    def test_present_but_not_responding_warns(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda _cmd: "/usr/bin/tailscale")

        def _hang(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd="tailscale", timeout=5)

        monkeypatch.setattr(subprocess, "run", _hang)
        status, detail = _check_tailscale()
        assert (status, detail) == (WARN, "tailscale present but not responding")

    def test_up_reports_first_ipv4(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda _cmd: "/usr/bin/tailscale")
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _tailscale_cp(0, "100.64.1.2\nfd7a::2\n")
        )
        status, detail = _check_tailscale()
        assert (status, detail) == (OK, "tailscale up (100.64.1.2)")

    def test_no_ipv4_warns_logged_out_or_down(self, monkeypatch):
        monkeypatch.setattr(doctor.shutil, "which", lambda _cmd: "/usr/bin/tailscale")
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _tailscale_cp(1, ""))
        status, detail = _check_tailscale()
        assert (status, detail) == (
            WARN,
            "tailscale installed but no IPv4 (logged out or down?)",
        )


class TestCheckUploadPort:
    def test_free_port_is_ok(self, monkeypatch):
        monkeypatch.setattr("magent.cli.background._probe_port", lambda _p: False)
        monkeypatch.setattr("magent.cli.background._running_upload_port", lambda: None)
        status, _ = _check_upload_port(None)
        assert status == OK

    def test_foreign_occupant_warns(self, monkeypatch):
        monkeypatch.setattr("magent.cli.background._probe_port", lambda _p: True)
        monkeypatch.setattr("magent.cli.background._running_upload_port", lambda: None)
        status, detail = _check_upload_port(None)
        assert status == WARN
        assert "occupied" in detail


class TestCheckHotkey:
    """The hotkey check used to answer "does this OS support Alt+V" -- true on
    every Windows box whether or not a listener had run since the last reboot,
    so a machine where Alt+V had been dead for days passed it. It now reports
    the real listener liveness, through the same state machine `status` renders
    so the two surfaces can never disagree."""

    def _platform(self, monkeypatch, *, supports_hotkey):
        fp = FakePlatform(supports_hotkey=supports_hotkey)
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)

    def _listener(self, monkeypatch, state):
        monkeypatch.setattr("magent.cli.status._upload_state", lambda port: "on")
        monkeypatch.setattr("magent.cli.status._listener_state", lambda upload: state)

    def test_platform_without_hotkey_support_is_ok(self, monkeypatch):
        self._platform(monkeypatch, supports_hotkey=False)
        status, detail = _check_hotkey(None)
        assert status == OK
        assert "Windows-only" in detail

    def test_running_listener_is_ok(self, monkeypatch):
        self._platform(monkeypatch, supports_hotkey=True)
        self._listener(monkeypatch, "on")
        status, detail = _check_hotkey(None)
        assert status == OK
        assert "heartbeat fresh" in detail

    def test_dead_listener_fails_with_the_shared_repair_hint(self, monkeypatch):
        from magent.cli.status import LISTENER_REPAIR_HINT

        self._platform(monkeypatch, supports_hotkey=True)
        self._listener(monkeypatch, "dead")
        status, detail = _check_hotkey(None)
        assert status == FAIL
        assert "no Alt+V listener" in detail
        assert LISTENER_REPAIR_HINT in detail
        assert "down" not in detail.lower()  # never a fleet teardown

    def test_wedged_listener_fails_and_says_so(self, monkeypatch):
        self._platform(monkeypatch, supports_hotkey=True)
        self._listener(monkeypatch, "stale")
        status, detail = _check_hotkey(None)
        assert status == FAIL
        assert "heartbeat expired" in detail
        assert "down" not in detail.lower()  # never a fleet teardown

    def test_listener_off_by_design_is_ok_and_names_its_owner(self, monkeypatch):
        self._platform(monkeypatch, supports_hotkey=True)
        monkeypatch.setattr("magent.cli.status._upload_state", lambda port: "off")
        monkeypatch.setattr("magent.cli.status._listener_state", lambda upload: "off")
        status, detail = _check_hotkey(None)
        assert status == OK
        assert "starts with the upload server" in detail


class TestCheckAttention:
    """The attention daemon, through the same state machine `status` renders
    (``cli.status._attention_state``) so the two surfaces cannot disagree.
    WARN at worst: a machine without attention signals is quieter, not broken,
    and serve revives a daemon that died."""

    def _state(self, monkeypatch, state):
        monkeypatch.setattr("magent.cli.status._attention_state", lambda: state)

    def test_running_is_ok(self, monkeypatch):
        self._state(monkeypatch, "on")
        status, _detail = doctor._check_attention()
        assert status == OK

    def test_a_restart_is_not_a_problem_and_is_named(self, monkeypatch):
        self._state(monkeypatch, "off-since-restart")
        status, detail = doctor._check_attention()
        assert status == OK
        assert "not running since the last restart" in detail

    def test_a_crash_warns_and_points_at_the_log(self, monkeypatch):
        self._state(monkeypatch, "crashed")
        status, detail = doctor._check_attention()
        assert status == WARN
        assert "attention.log" in detail

    def test_a_wedged_daemon_warns(self, monkeypatch):
        self._state(monkeypatch, "stale")
        status, detail = doctor._check_attention()
        assert status == WARN
        assert "heartbeat expired" in detail

    def test_never_started_is_ok(self, monkeypatch):
        self._state(monkeypatch, "off")
        status, detail = doctor._check_attention()
        assert status == OK
        assert "magent attention -d" in detail


class TestCheckWtKeys:
    """The Ctrl+Backspace / Shift+Enter bindings that survive psmux. Never a
    FAIL by design: a missing binding costs ergonomics, not a working fleet,
    and doctor's exit code is read by CI and by `magent status`."""

    def _platform(self, monkeypatch, *, supported):
        fp = FakePlatform(supports_wt_keybindings=supported)
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)

    def _settings(self, monkeypatch, tmp_path, doc):
        path = tmp_path / "settings.json"
        path.write_text(doc if isinstance(doc, str) else json.dumps(doc), "utf-8")
        monkeypatch.setattr("magent.wt_keys.find_settings", lambda: path)
        return path

    def test_other_os_is_ok_and_never_touches_a_settings_file(self, monkeypatch):
        self._platform(monkeypatch, supported=False)
        monkeypatch.setattr(
            "magent.wt_keys.find_settings",
            lambda: pytest.fail("probed for settings.json off Windows"),
        )
        status, detail = doctor._check_wt_keys()
        assert status == OK
        assert "Windows-only" in detail

    def test_no_windows_terminal_is_ok_not_a_finding(self, monkeypatch):
        self._platform(monkeypatch, supported=True)
        monkeypatch.setattr("magent.wt_keys.find_settings", lambda: None)
        status, detail = doctor._check_wt_keys()
        assert status == OK
        assert "not found" in detail

    def test_missing_bindings_warn_with_the_repair_hint(self, monkeypatch, tmp_path):
        self._platform(monkeypatch, supported=True)
        self._settings(monkeypatch, tmp_path, {})
        status, detail = doctor._check_wt_keys()
        assert status == WARN
        assert "ctrl+backspace" in detail and "shift+enter" in detail
        assert "magent terminal install" in detail

    def test_installed_bindings_are_ok(self, monkeypatch, tmp_path):
        self._platform(monkeypatch, supported=True)
        doc: dict[str, object] = {}
        for binding in wt_keys.BINDINGS:
            wt_keys._add_binding(doc, binding, wt_keys.SCHEMA_SPLIT)
        self._settings(monkeypatch, tmp_path, doc)
        status, detail = doctor._check_wt_keys()
        assert status == OK
        assert "survive psmux" in detail

    def test_a_foreign_binding_is_reported_as_a_conflict(self, monkeypatch, tmp_path):
        self._platform(monkeypatch, supported=True)
        doc: dict[str, object] = {
            "actions": [{"command": {"action": "copy"}, "keys": "ctrl+backspace"}]
        }
        wt_keys._add_binding(doc, wt_keys.BINDINGS[1], wt_keys.SCHEMA_ACTIONS_INLINE)
        self._settings(monkeypatch, tmp_path, doc)
        status, detail = doctor._check_wt_keys()
        assert status == WARN
        assert "bound to something else: ctrl+backspace" in detail

    def test_jsonc_settings_warn_rather_than_fail(self, monkeypatch, tmp_path):
        self._platform(monkeypatch, supported=True)
        self._settings(monkeypatch, tmp_path, "{ // comment\n }")
        status, detail = doctor._check_wt_keys()
        assert status == WARN
        assert "magent terminal install" in detail

    def test_settings_nested_past_the_parsers_depth_warn_naming_it(
        self, monkeypatch, tmp_path
    ):
        # json.loads raises RecursionError there, not ValueError.
        self._platform(monkeypatch, supported=True)
        self._settings(monkeypatch, tmp_path, _NESTED)
        status, detail = doctor._check_wt_keys()
        assert status == WARN
        assert "nested too deeply" in detail
        assert "magent terminal install" in detail

    def test_settings_nested_past_the_parsers_depth_leave_the_rest_running(
        self, runner, monkeypatch, tmp_path, tmp_config
    ):
        # _run_checks has no per-check guard: an exception out of this one
        # check would take every other check down with it.
        self._platform(monkeypatch, supported=True)
        self._settings(monkeypatch, tmp_path, _NESTED)
        monkeypatch.setattr("magent.cli.background._probe_port", lambda _p: False)
        monkeypatch.setattr("magent.cli.background._running_upload_port", lambda: None)
        config_path = tmp_config(
            {"version": SCHEMA_VERSION, "projects": [{"path": "api"}]}
        )

        result = runner.invoke(cli.main, ["--config", config_path, "doctor", "--json"])

        checks = {c["name"]: c for c in json.loads(result.stdout)["checks"]}
        assert checks["wt-keys"]["status"] == WARN
        assert "nested too deeply" in checks["wt-keys"]["detail"]
        assert {"logs dir", "state dir", "sentry", "tailscale", "upload port"} <= set(
            checks
        )


class TestCheckPsmuxSessionZero:
    """psmux servers stranded in logon Session 0 -- the residue of the incident
    the desktop hand-off prevents. They are worse than dead: the ``~/.psmux``
    registry is shared across sessions, so such a server answers
    ``has-session`` and HOLDS its name while being invisible and unattachable
    from the desktop, which is why every bring-up there logged "session never
    came up after respawn"."""

    def _platform(self, monkeypatch, *, supports_psmux=True):
        fp = FakePlatform(supports_psmux=supports_psmux)
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)

    def _stranded(self, monkeypatch, pids):
        monkeypatch.setattr(doctor.psmux, "session0_server_pids", lambda: list(pids))

    def test_a_platform_without_psmux_never_looks(self, monkeypatch):
        self._platform(monkeypatch, supports_psmux=False)
        monkeypatch.setattr(
            doctor.psmux,
            "session0_server_pids",
            lambda: pytest.fail("scanned on a platform without psmux"),
        )

        status, detail = doctor._check_psmux_session0()

        assert status == OK
        assert "Windows-only" in detail

    def test_a_clean_machine_is_quiet(self, monkeypatch):
        self._platform(monkeypatch)
        self._stranded(monkeypatch, [])

        status, detail = doctor._check_psmux_session0()

        assert status == OK
        assert "Session 0" in detail

    def test_stranded_servers_are_counted_and_named(self, monkeypatch):
        self._platform(monkeypatch)
        self._stranded(monkeypatch, [11, 22, 33])

        status, detail = doctor._check_psmux_session0()

        # WARN, never FAIL: magent did not start these and cannot stop them, so
        # this must not start failing doctor on a machine whose only problem is
        # that somebody once ssh'd in.
        assert status == WARN
        assert "3 psmux server(s)" in detail
        assert "elevated shell" in detail


class TestCheckDaemonsSession0:
    """magent's OWN daemons stranded in Session 0 -- a serve there holds the
    loopback port this desktop's Alt+V needs. Same posture as psmux-session0:
    WARN at worst, and the one wording `status` prints too."""

    def _stranded(self, monkeypatch, found):
        monkeypatch.setattr("magent.cli.status.session0_daemons", lambda: list(found))

    def test_a_clean_machine_is_quiet(self, monkeypatch):
        self._stranded(monkeypatch, [])

        status, detail = doctor._check_daemons_session0()

        assert status == OK
        assert "Session 0" in detail

    def test_stranded_daemons_warn_with_the_shared_wording(self, monkeypatch):
        from magent.cli.status import session0_daemons_message

        found = [("upload server :8034", 70)]
        self._stranded(monkeypatch, found)

        status, detail = doctor._check_daemons_session0()

        assert status == WARN
        assert detail == session0_daemons_message(found)


class TestCheckPsmuxWedge:
    """The machine-wide psmux control-plane wedge (2026-08-18/19): every psmux
    command hangs forever from any console while ConPTY itself is healthy, and
    the sessions behind it are FROZEN, not dead. It cost hours to diagnose and
    the tempting reaction -- mass-restart, or reboot -- would have destroyed 40
    live agent sessions. The check exists to say all of that in one line."""

    def _platform(self, monkeypatch, *, supports_psmux):
        fp = FakePlatform(supports_psmux=supports_psmux)
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)

    def _probe(self, monkeypatch, probe, *, binary="/x/psmux"):
        monkeypatch.setattr(doctor.psmux, "find_psmux", lambda: binary)
        monkeypatch.setattr(doctor.psmux, "probe_control_plane", lambda: probe)

    def _no_zombies(self, monkeypatch):
        monkeypatch.setattr("magent.procs.count_processes", lambda _name: None)

    def test_platform_without_psmux_never_probes(self, monkeypatch):
        """The capability gate is the FIRST thing, not a fallback: on a
        platform that cannot run psmux the check must not spawn anything."""

        def _boom() -> object:
            raise AssertionError("the probe ran on a platform without psmux")

        self._platform(monkeypatch, supports_psmux=False)
        monkeypatch.setattr(doctor.psmux, "probe_control_plane", _boom)

        status, detail = _check_psmux_wedge()

        assert status == OK
        assert "Windows-only" in detail

    def test_missing_binary_is_skipped_not_failed(self, monkeypatch):
        self._platform(monkeypatch, supports_psmux=True)
        monkeypatch.setattr(doctor.psmux, "find_psmux", lambda: None)
        monkeypatch.setattr(
            doctor.psmux,
            "probe_control_plane",
            lambda: pytest.fail("probed without a binary"),
        )

        status, detail = _check_psmux_wedge()

        assert status == OK
        assert "not installed" in detail

    def test_a_responsive_control_plane_passes_quietly(self, monkeypatch):
        self._platform(monkeypatch, supports_psmux=True)
        self._probe(
            monkeypatch,
            psmux_mod.ControlProbe(responsive=True, timed_out=False, elapsed_s=0.89),
        )

        status, detail = _check_psmux_wedge()

        assert status == OK
        assert "responded in 0.89s" in detail

    def test_a_timed_out_probe_fails_with_the_three_facts(self, monkeypatch):
        self._platform(monkeypatch, supports_psmux=True)
        self._probe(
            monkeypatch,
            psmux_mod.ControlProbe(responsive=False, timed_out=True, elapsed_s=5.0),
        )
        self._no_zombies(monkeypatch)

        status, detail = _check_psmux_wedge()

        assert status == FAIL
        # (a) it is a global control-plane wedge and the sessions are alive
        assert "WEDGED machine-wide" in detail
        assert "FROZEN, not dead" in detail
        assert "do NOT restart them, do NOT reboot" in detail
        # (b) the recovery, precisely enough to act on
        assert "conhost.exe" in detail
        assert "kill ONLY those" in detail
        # (c) what to expect afterwards
        assert "every session returns intact" in detail

    def test_the_hint_is_ascii_and_stays_short(self):
        # It is read on a broken machine and pasted into bug reports; the
        # status-line/ASCII rule applies (a ambiguous-width glyph once
        # corrupted the psmux bar).
        assert WEDGE_REPAIR_HINT.isascii()
        assert 3 <= len(WEDGE_REPAIR_HINT.splitlines()) <= 5

    def test_resident_zombies_enrich_the_finding(self, monkeypatch):
        self._platform(monkeypatch, supports_psmux=True)
        self._probe(
            monkeypatch,
            psmux_mod.ControlProbe(responsive=False, timed_out=True, elapsed_s=5.0),
        )
        monkeypatch.setattr("magent.procs.count_processes", lambda _name: 14)

        status, detail = _check_psmux_wedge()

        assert status == FAIL
        assert "(14 psmux.exe resident)" in detail

    def test_an_unknown_count_is_never_rendered_as_zero(self, monkeypatch):
        self._platform(monkeypatch, supports_psmux=True)
        self._probe(
            monkeypatch,
            psmux_mod.ControlProbe(responsive=False, timed_out=True, elapsed_s=5.0),
        )
        self._no_zombies(monkeypatch)

        _status, detail = _check_psmux_wedge()

        assert "psmux.exe resident" not in detail

    def test_a_binary_that_will_not_run_warns_rather_than_crying_wedge(
        self, monkeypatch
    ):
        self._platform(monkeypatch, supports_psmux=True)
        self._probe(
            monkeypatch,
            psmux_mod.ControlProbe(responsive=False, timed_out=False, elapsed_s=0.0),
        )

        status, detail = _check_psmux_wedge()

        assert status == WARN
        assert "would not run" in detail

    def test_the_multi_line_detail_reaches_json_whole(
        self, runner, monkeypatch, tmp_config
    ):
        """The JSON shape is unchanged (name/status/detail) and the runbook
        survives as one string -- a bug report carries the repair, not a
        truncated first sentence."""
        monkeypatch.setattr(
            doctor,
            "_check_psmux_wedge",
            lambda: (FAIL, f"wedged.\n{WEDGE_REPAIR_HINT}"),
        )
        monkeypatch.setattr("magent.platform.get_platform", FakePlatform)
        monkeypatch.setattr("magent.cli.background._probe_port", lambda _p: False)
        monkeypatch.setattr("magent.cli.background._running_upload_port", lambda: None)
        config_path = tmp_config(
            {"version": SCHEMA_VERSION, "projects": [{"path": "api"}]}
        )

        result = runner.invoke(cli.main, ["--config", config_path, "doctor", "--json"])

        payload = json.loads(result.stdout)
        wedge = next(c for c in payload["checks"] if c["name"] == "psmux wedge")
        assert set(wedge) == {"name", "status", "detail"}
        assert wedge["status"] == FAIL
        assert WEDGE_REPAIR_HINT in wedge["detail"]
        assert result.exit_code == 1

    def test_the_human_report_indents_the_runbook(
        self, runner, monkeypatch, tmp_config
    ):
        monkeypatch.setattr(
            doctor,
            "_check_psmux_wedge",
            lambda: (FAIL, "wedged.\nline two of the runbook"),
        )
        monkeypatch.setattr("magent.platform.get_platform", FakePlatform)
        monkeypatch.setattr("magent.cli.background._probe_port", lambda _p: False)
        monkeypatch.setattr("magent.cli.background._running_upload_port", lambda: None)
        config_path = tmp_config(
            {"version": SCHEMA_VERSION, "projects": [{"path": "api"}]}
        )

        result = runner.invoke(cli.main, ["--config", config_path, "doctor"])

        first = "  x psmux wedge  wedged."
        assert first in result.output
        # continuation lines start under the detail column, not at column 0
        column = first.index("wedged.")
        assert f"\n{' ' * column}line two of the runbook" in result.output


class TestCheckSentry:
    """The DSN-set-but-SDK-missing state surfaces HERE (as a broken-install
    warning with a repair hint — sentry-sdk is a base dependency), never as a
    per-command stderr nag — see
    test_sentry.py::TestMissingSdkIsQuietButLogged for the init side."""

    def _fake_env(self, monkeypatch, dsn):
        monkeypatch.setattr(
            "magent.env.get_env", lambda: types.SimpleNamespace(sentry_dsn=dsn)
        )

    def test_no_dsn_is_ok_and_reports_off(self, monkeypatch):
        self._fake_env(monkeypatch, None)
        status, detail = _check_sentry()
        assert status == OK
        assert "off" in detail

    def test_dsn_with_sdk_installed_is_ok(self, monkeypatch):
        self._fake_env(monkeypatch, "https://example@o0.ingest.sentry.io/0")
        monkeypatch.setattr("magent.sentry.sdk_installed", lambda: True)
        status, detail = _check_sentry()
        assert status == OK
        assert "active" in detail

    def test_dsn_without_sdk_warns_as_broken_install_with_repair_hint(
        self, monkeypatch
    ):
        self._fake_env(monkeypatch, "https://example@o0.ingest.sentry.io/0")
        monkeypatch.setattr("magent.sentry.sdk_installed", lambda: False)
        status, detail = _check_sentry()
        assert status == WARN
        assert "sentry-sdk is missing" in detail
        # sentry-sdk is bundled, so its absence means the install is damaged:
        # the hint must be a repair, not an optional-extra install.
        assert "install looks broken" in detail
        assert "pip install --force-reinstall magent-multi-ai-agents-manager" in detail
        assert "[sentry]" not in detail


class TestCheckIdleReap:
    """WARN at worst: a reaper that is off is a choice. The one WARN that is
    not about the config: reaping ON with the state hook unwired, which parks
    nothing ever (R7 vetoes every session). Real ``reap.off_reason`` over a
    FakePlatform; the hook settings file lives in conftest's tmp home."""

    _REAP_EVENTS = ("UserPromptSubmit", "Stop", "Notification", "SessionStart")

    @pytest.fixture(autouse=True)
    def _platform(self, monkeypatch):
        self.plat = FakePlatform(supports_psmux=True)
        monkeypatch.setattr("magent.platform.get_platform", lambda: self.plat)

    @pytest.fixture
    def reaping_on(self, monkeypatch):
        monkeypatch.setenv("MAGENT_IDLE_REAP", "1")
        monkeypatch.setattr("magent.env._cached_env", None)

    @staticmethod
    def _cfg(*, enabled=True, minutes=120):
        from magent.config import MagentConfig

        cfg = MagentConfig(projects=[])
        cfg.settings.idle_reap.enabled = enabled
        cfg.settings.idle_reap.after_minutes = minutes
        return cfg

    @staticmethod
    def _hooks(events, command="magent-state-hook --source claude"):
        path = Path.home() / ".claude" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        hook = {"hooks": [{"type": "command", "command": command}]}
        path.write_text(
            json.dumps({"hooks": {e: [hook] for e in events}}), encoding="utf-8"
        )
        return path

    def test_no_config_says_the_reaper_waits_for_one(self):
        assert _check_idle_reap(None) == (
            WARN,
            "config invalid or missing; the reaper stays off until it loads",
        )

    def test_the_env_kill_switch_is_ok_and_named(self):
        # conftest pins MAGENT_IDLE_REAP=0 for every test.
        assert _check_idle_reap(self._cfg()) == (OK, "off (MAGENT_IDLE_REAP=0)")

    def test_an_invalid_environment_is_not_called_the_kill_switch(self, monkeypatch):
        monkeypatch.setenv("MAGENT_IDLE_REAP", "not-a-bool")
        monkeypatch.setattr("magent.env._cached_env", None)
        assert _check_idle_reap(self._cfg()) == (
            OK,
            "off (the MAGENT_* environment did not validate)",
        )

    def test_the_env_check_is_the_one_that_flags_an_invalid_environment(
        self, monkeypatch
    ):
        # The idle-reap line above stays OK because doctor's env check fails on
        # the same environment: one flag, not two.
        monkeypatch.setenv("MAGENT_IDLE_REAP", "not-a-bool")
        monkeypatch.setattr("magent.env._cached_env", None)
        status, detail = doctor._check_env()
        assert status == FAIL
        assert "MAGENT_IDLE_REAP" in detail

    def test_off_in_settings_is_ok_and_named(self, reaping_on):
        assert _check_idle_reap(self._cfg(enabled=False)) == (
            OK,
            "off in settings.idleReap",
        )

    @pytest.mark.parametrize(
        ("plat", "why"),
        [
            (FakePlatform(supports_psmux=False), "unsupported platform (no psmux)"),
            (
                FakePlatform(supports_psmux=True, interactive_session=False),
                "non-interactive logon session",
            ),
        ],
        ids=["no-psmux", "non-interactive"],
    )
    def test_a_platform_gate_is_ok_and_named(self, reaping_on, plat, why):
        self.plat = plat
        assert _check_idle_reap(self._cfg()) == (OK, f"off: {why}")

    def test_on_and_wired_shows_the_threshold(self, reaping_on):
        self._hooks(self._REAP_EVENTS)
        assert _check_idle_reap(self._cfg()) == (OK, "on, parks after 120 min idle")

    def test_the_module_form_counts_as_wired(self, reaping_on):
        self._hooks(
            self._REAP_EVENTS, command="py -3 -m magent.state_hook --source claude"
        )
        assert _check_idle_reap(self._cfg(minutes=45))[0] == OK

    @pytest.mark.parametrize("minutes", [29, 5, 0, -1])
    def test_a_value_under_the_floor_says_it_was_raised(self, reaping_on, minutes):
        self._hooks(self._REAP_EVENTS)
        assert _check_idle_reap(self._cfg(minutes=minutes)) == (
            OK,
            (
                f"on, parks after 30 min idle (afterMinutes={minutes} raised to the "
                "30-min floor)"
            ),
        )

    def test_the_floor_itself_is_not_called_raised(self, reaping_on):
        self._hooks(self._REAP_EVENTS)
        assert _check_idle_reap(self._cfg(minutes=30)) == (
            OK,
            "on, parks after 30 min idle",
        )

    def test_on_with_no_hooks_warns_that_nothing_will_be_parked(self, reaping_on):
        assert _check_idle_reap(self._cfg()) == (
            WARN,
            (
                "on, parks after 120 min idle, but the state hook is not wired for "
                "UserPromptSubmit, Stop, Notification, SessionStart; nothing will ever "
                "be parked; run magent hooks install"
            ),
        )

    @pytest.mark.parametrize("missing", _REAP_EVENTS)
    def test_each_of_the_four_events_is_required(self, reaping_on, missing):
        self._hooks(
            [e for e in self._REAP_EVENTS if e != missing]
            + ["PostToolUse", "SessionEnd"]
        )
        status, detail = _check_idle_reap(self._cfg())
        assert status == WARN
        assert f"not wired for {missing};" in detail

    def test_someone_elses_hook_on_an_event_is_not_ours(self, reaping_on):
        self._hooks(self._REAP_EVENTS)
        self._hooks(["Stop"], command="notify-send done")
        status, detail = _check_idle_reap(self._cfg())
        assert status == WARN
        assert (
            "not wired for UserPromptSubmit, Stop, Notification, SessionStart;"
            in detail
        )

    def test_a_hooks_value_that_is_not_a_map_is_unknown(self, reaping_on):
        # hooks_cmd._load_settings refuses a file whose "hooks" is not an
        # object, so doctor says it cannot tell, as `magent hooks status` does.
        path = self._hooks(self._REAP_EVENTS)
        path.write_text(json.dumps({"hooks": ["Stop"]}), encoding="utf-8")
        status, detail = _check_idle_reap(self._cfg())
        assert status == WARN
        assert f'but {path} is unreadable ("hooks" is not a JSON object)' in detail
        assert detail.endswith("; cannot tell whether the state hook is wired")

    @pytest.mark.parametrize(
        "text",
        [
            "{not json",
            "[1, 2]",
            # Nested past the parser's depth: json.loads raises RecursionError,
            # which no per-check guard in _run_checks would catch -- the whole
            # `magent doctor` would crash instead of warning here.
            '{"hooks": ' + "[" * 200_000 + "]" * 200_000 + "}",
        ],
        ids=["json", "not-a-map", "nested"],
    )
    def test_an_unreadable_hook_file_warns_and_names_it(self, reaping_on, text):
        path = self._hooks(self._REAP_EVENTS)
        path.write_text(text, encoding="utf-8")
        status, detail = _check_idle_reap(self._cfg(minutes=5))
        assert status == WARN
        assert detail.startswith(
            "on, parks after 30 min idle (afterMinutes=5 raised to the 30-min floor), "
            f"but {path} is unreadable ("
        )
        assert detail.endswith("); cannot tell whether the state hook is wired")

    def test_a_hook_file_the_os_will_not_read_warns_and_names_it(self, reaping_on):
        # A refused read (here the path is a directory) is the same unknown as
        # a file that does not parse, and `magent hooks status` agrees.
        path = Path.home() / ".claude" / "settings.json"
        path.mkdir(parents=True)
        status, detail = _check_idle_reap(self._cfg())
        assert status == WARN
        assert f"but {path} is unreadable (" in detail
        assert detail.endswith("); cannot tell whether the state hook is wired")


class TestDoctorCli:
    def _all_ok(self, monkeypatch):
        monkeypatch.setattr(
            doctor,
            "_run_checks",
            lambda _f: [{"name": "config", "status": OK, "detail": "fine"}],
        )

    def _one_fail(self, monkeypatch):
        monkeypatch.setattr(
            doctor,
            "_run_checks",
            lambda _f: [
                {"name": "config", "status": OK, "detail": "fine"},
                {"name": "monitors", "status": FAIL, "detail": "none"},
            ],
        )

    def test_exit_0_when_no_failures(self, runner, monkeypatch, tmp_config):
        self._all_ok(monkeypatch)
        config_path = tmp_config({"version": SCHEMA_VERSION, "projects": []})

        result = runner.invoke(cli.main, ["--config", config_path, "doctor"])

        assert result.exit_code == 0
        assert "No failures" in result.output

    def test_exit_1_when_any_failure(self, runner, monkeypatch, tmp_config):
        self._one_fail(monkeypatch)
        config_path = tmp_config({"version": SCHEMA_VERSION, "projects": []})

        result = runner.invoke(cli.main, ["--config", config_path, "doctor"])

        assert result.exit_code == 1
        assert "1 check(s) failed" in result.output

    def test_json_schema_and_exit_code(self, runner, monkeypatch, tmp_config):
        self._one_fail(monkeypatch)
        config_path = tmp_config({"version": SCHEMA_VERSION, "projects": []})

        result = runner.invoke(cli.main, ["--config", config_path, "doctor", "--json"])

        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        # P3-04: doctor always emits ok: true (it produced a valid report); the
        # per-check verdict lives in `failures` + the exit code.
        assert payload["ok"] is True
        assert payload["failures"] == 1
        assert {c["name"] for c in payload["checks"]} == {"config", "monitors"}
        assert all({"name", "status", "detail"} <= set(c) for c in payload["checks"])

    def test_real_checks_run_end_to_end(self, runner, monkeypatch, tmp_config):
        """No stubbing of _run_checks: the real checks execute against fakes
        and a valid config — proves the composition, not just the runner."""
        fp = FakePlatform()
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
        monkeypatch.setattr("magent.cli.background._probe_port", lambda _p: False)
        monkeypatch.setattr("magent.cli.background._running_upload_port", lambda: None)
        config_path = tmp_config(
            {"version": SCHEMA_VERSION, "projects": [{"path": "api"}]}
        )

        result = runner.invoke(cli.main, ["--config", config_path, "doctor", "--json"])

        payload = json.loads(result.stdout)
        names = {c["name"] for c in payload["checks"]}
        assert {
            "config",
            "env",
            "agent tools",
            "terminal",
            "psmux wedge",
            "psmux-session0",
            "daemons-session0",
            "monitors",
            "hotkey",
            "attention",
            "wt-keys",
            "wt-icons",
            "logs dir",
            "state dir",
            "sentry",
            "tailscale",
            "upload port",
            "nodes",
            "idle-reap",
            "claude-token",
        } == names


def _nodes_cfg(tmp_config, nicks=("second",)):
    return load_config(
        tmp_config(
            {
                "version": SCHEMA_VERSION,
                "settings": {
                    "nodes": {n: {"host": f"box-{n}", "user": "demo"} for n in nicks}
                },
                "projects": [],
            }
        )
    )


class TestTheNodesRow:
    def test_no_nodes_is_ok(self, tmp_config):
        cfg = load_config(tmp_config({"version": SCHEMA_VERSION, "projects": []}))
        assert _check_nodes(cfg) == ("ok", "no nodes configured")

    def test_no_loadable_config_is_skipped_not_called_node_free(self):
        # A missing or broken config may well configure nodes: say the row was
        # skipped (the config row already fails), never "no nodes configured".
        assert _check_nodes(None) == (
            "ok",
            "skipped -- config missing or invalid (see the config check)",
        )

    def test_healthy_nodes_are_ok(self, tmp_config, monkeypatch):
        monkeypatch.setattr(
            "magent.cli.node_cmd.doctor_report",
            lambda cfg, nicks: {
                n: [ScriptLine("ok", "tmux", "tmux 3.4")] for n in nicks
            },
        )
        assert _check_nodes(_nodes_cfg(tmp_config, ("second", "fifth"))) == (
            "ok",
            "2 node(s) healthy",
        )

    @pytest.mark.parametrize("status", ["fail", "warn"])
    def test_a_troubled_node_is_only_a_warning_naming_its_items(
        self, tmp_config, monkeypatch, status
    ):
        monkeypatch.setattr(
            "magent.cli.node_cmd.doctor_report",
            lambda cfg, nicks: {
                "second": [
                    ScriptLine("ok", "tmux", ""),
                    ScriptLine(status, "claude-login", "not logged in"),
                ],
                "fifth": [ScriptLine("ok", "tmux", "")],
            },
        )
        assert _check_nodes(_nodes_cfg(tmp_config, ("second", "fifth"))) == (
            "warn",
            "second: claude-login -- details: magent node doctor",
        )

    def test_troubled_nodes_join_by_semicolon_their_items_by_comma(
        self, tmp_config, monkeypatch
    ):
        monkeypatch.setattr(
            "magent.cli.node_cmd.doctor_report",
            lambda cfg, nicks: {
                "second": [
                    ScriptLine("warn", "github-key", "not registered"),
                    ScriptLine("ok", "tmux", ""),
                    ScriptLine("fail", "claude-login", "not logged in"),
                ],
                "third": [
                    ScriptLine("ok", "tmux", ""),
                    ScriptLine("skip", "snapshot", ""),
                ],
                "fifth": [ScriptLine("fail", "reach", "cannot reach demo@box-fifth")],
            },
        )
        cfg = _nodes_cfg(tmp_config, ("second", "third", "fifth"))
        assert _check_nodes(cfg) == (
            "warn",
            "second: github-key, claude-login; fifth: reach -- details: magent node doctor",
        )

    def test_nodes_keep_the_config_order(self, tmp_config, monkeypatch):
        monkeypatch.setattr(
            "magent.cli.node_cmd.doctor_report",
            lambda cfg, nicks: {n: [ScriptLine("fail", "reach", "")] for n in nicks},
        )
        assert _check_nodes(_nodes_cfg(tmp_config, ("second", "fifth"))) == (
            "warn",
            "second: reach; fifth: reach -- details: magent node doctor",
        )

    def test_a_row_neither_fail_nor_warn_is_healthy(self, tmp_config, monkeypatch):
        monkeypatch.setattr(
            "magent.cli.node_cmd.doctor_report",
            lambda cfg, nicks: {
                "second": [ScriptLine("did", "x", ""), ScriptLine("key", "y", "")]
            },
        )
        assert _check_nodes(_nodes_cfg(tmp_config)) == ("ok", "1 node(s) healthy")

    def test_doctor_hands_the_loaded_config_to_the_nodes_row(
        self, runner, monkeypatch, tmp_config
    ):
        monkeypatch.setattr("magent.platform.get_platform", FakePlatform)
        monkeypatch.setattr("magent.cli.background._probe_port", lambda _p: False)
        monkeypatch.setattr("magent.cli.background._running_upload_port", lambda: None)
        monkeypatch.setattr(
            "magent.cli.node_cmd.doctor_report",
            lambda cfg, nicks: {n: [ScriptLine("fail", "reach", "")] for n in nicks},
        )
        config_path = tmp_config(
            {
                "version": SCHEMA_VERSION,
                "settings": {
                    "nodes": {"second": {"host": "box-second", "user": "demo"}}
                },
                "projects": [],
            }
        )

        result = runner.invoke(cli.main, ["--config", config_path, "doctor", "--json"])

        rows = {c["name"]: c for c in json.loads(result.stdout)["checks"]}
        assert rows["nodes"] == {
            "name": "nodes",
            "status": "warn",
            "detail": "second: reach -- details: magent node doctor",
        }

    def test_the_row_comes_right_after_upload_port(
        self, runner, monkeypatch, tmp_config
    ):
        # ORDER, not just membership: the audit's row order is upload port,
        # nodes, then mcp-relay (K12 pins its own row directly after this one).
        monkeypatch.setattr("magent.platform.get_platform", FakePlatform)
        monkeypatch.setattr("magent.cli.background._probe_port", lambda _p: False)
        monkeypatch.setattr("magent.cli.background._running_upload_port", lambda: None)
        config_path = tmp_config({"version": SCHEMA_VERSION, "projects": []})

        result = runner.invoke(cli.main, ["--config", config_path, "doctor", "--json"])

        names = [c["name"] for c in json.loads(result.stdout)["checks"]]
        assert names.index("nodes") == names.index("upload port") + 1, names

    def test_skip_rows_are_healthy_through_the_real_path(self, tmp_config, fake_ssh):
        # No sync daemon and no snapshot: this PC's two rows are `skip`, and a
        # node that is merely not synced yet is not a troubled one.
        fake_ssh.set_reply("bash -s", stdout="ok\ttmux\ttmux 3.4\n")
        assert _check_nodes(_nodes_cfg(tmp_config)) == ("ok", "1 node(s) healthy")

    def test_an_unreachable_node_is_a_warning_through_the_real_path(
        self, tmp_config, fake_ssh
    ):
        fake_ssh.set_reply(
            "bash -s", stderr="ssh: connect to host box-second: No route\n", rc=255
        )
        assert _check_nodes(_nodes_cfg(tmp_config)) == (
            "warn",
            "second: reach -- details: magent node doctor",
        )

    def test_a_node_s_unencodable_item_renders_on_a_legacy_code_page(
        self, tmp_config, monkeypatch
    ):
        # The item names are the node's words (doctor.sh prints them, and
        # _report_of's errors="replace" can put U+FFFD there), which cp1252 -- a
        # redirected Windows stdout -- lacks: the row degrades a glyph, never
        # the command.
        monkeypatch.setattr(
            "magent.cli.node_cmd.doctor_report",
            lambda cfg, nicks: {
                "second": [
                    ScriptLine("fail", "claude-login\N{REPLACEMENT CHARACTER}", "")
                ]
            },
        )
        monkeypatch.setattr("magent.platform.get_platform", FakePlatform)
        cfg = _nodes_cfg(tmp_config)

        def only_the_nodes_row(_f):
            # Computed inside invoke, while the runner's cp1252 stdout is installed.
            status, detail = _check_nodes(cfg)
            return [{"name": "nodes", "status": status, "detail": detail}]

        monkeypatch.setattr(doctor, "_run_checks", only_the_nodes_row)
        result = CliRunner(charset="cp1252").invoke(
            cli.main, ["--config", tmp_config({"version": SCHEMA_VERSION}), "doctor"]
        )
        assert result.exception is None, repr(result.exception)
        assert "second: claude-login? -- details: magent node doctor" in result.stdout


class TestTheClaudeTokenRow:
    """The PC's node token, read by node_auth.token_health -- WARN at worst,
    like the nodes row: an ageing token is advice, not a broken machine. Never
    mints (doctor is not a person's command)."""

    TOKEN = "sk-ant-oat01-DECOY-" + "ab12_-" * 16
    DAY = 86400.0

    def _write(self, *, age_s):
        import time

        from magent import node_auth

        node_auth.write_token(self.TOKEN, now=time.time() - age_s)

    def test_no_nodes_is_ok_whatever_the_file_says(self, tmp_config):
        cfg = load_config(tmp_config({"version": SCHEMA_VERSION, "projects": []}))
        assert _check_claude_token(cfg) == ("ok", "no nodes configured")

    def test_no_loadable_config_is_skipped(self):
        assert _check_claude_token(None) == (
            "ok",
            "skipped -- config missing or invalid (see the config check)",
        )

    def test_a_fresh_token_is_ok_and_names_its_end(self, tmp_config):
        self._write(age_s=self.DAY)
        status, detail = _check_claude_token(_nodes_cfg(tmp_config))
        assert status == "ok"
        assert detail.startswith("valid until ")

    def test_no_token_yet_is_a_warning_naming_the_command(self, tmp_config):
        from magent import node_auth

        status, detail = _check_claude_token(_nodes_cfg(tmp_config))
        assert status == "warn"
        assert node_auth.REFRESH_COMMAND in detail

    @pytest.mark.parametrize("days_left", [12, -3])
    def test_an_ageing_or_expired_token_is_a_warning(self, tmp_config, days_left):
        from magent import node_auth

        self._write(age_s=node_auth.TOKEN_LIFETIME_S - days_left * self.DAY)
        status, detail = _check_claude_token(_nodes_cfg(tmp_config))
        assert status == "warn"
        assert node_auth.REFRESH_COMMAND in detail
        assert self.TOKEN not in detail

    def test_it_never_mints(self, tmp_config, monkeypatch):
        from magent import node_auth

        def boom(*_a, **_k):
            raise AssertionError("doctor minted")

        monkeypatch.setattr(node_auth, "mint_token", boom)
        monkeypatch.setattr(node_auth, "ensure_token", boom)
        assert _check_claude_token(_nodes_cfg(tmp_config))[0] == "warn"
