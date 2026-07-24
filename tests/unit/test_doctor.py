"""Tests for `magent doctor` (cli/doctor.py) — every check function in
isolation with fakes, plus the CLI exit-code and --json contracts."""

from __future__ import annotations

import json
import os
import subprocess
import types
from pathlib import Path

from magent import cli
from magent.cli import doctor
from magent.cli.doctor import (
    FAIL,
    OK,
    WARN,
    _check_agent_tools,
    _check_config,
    _check_monitors,
    _check_sentry,
    _check_tailscale,
    _check_upload_port,
    _monitor_topology,
)
from magent.config import SCHEMA_VERSION, load_config
from magent.grid import MonitorRect
from tests.conftest import FakePlatform


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
            "monitors",
            "hotkey",
            "logs dir",
            "state dir",
            "sentry",
            "tailscale",
            "upload port",
        } == names
