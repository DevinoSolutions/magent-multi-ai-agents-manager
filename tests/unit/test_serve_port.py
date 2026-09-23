"""`magent serve` resolves its port from the config, not from a literal.

The defect this pins: `serve`'s `--port` defaulted to 8033 while `status`,
`up`, `down` and `doctor` all watch `settings.uploadPort`. On a config that
sets anything else, a bare `magent serve` bound a port nothing else was
looking at, and every other surface reported the upload server dead.

Both entry points are covered here because they take different routes to the
port: `--ensure` hands it to `_maybe_start_upload_server` (which bakes it into
the detached child's argv), the blocking path hands it to `run_server`.
"""

import json
import sys

import pytest

from magent import cli
from magent.cli.mobile import _FALLBACK_UPLOAD_PORT, _configured_upload_port
from tests.conftest import FakePlatform


def _write_config(path, port=None, extra_settings=None):
    settings = {"uploadServer": True}
    if port is not None:
        settings["uploadPort"] = port
    if extra_settings:
        settings.update(extra_settings)
    path.write_text(
        json.dumps({"version": 3, "projects": [], "settings": settings}),
        encoding="utf-8",
    )
    return str(path)


@pytest.fixture
def ensured(monkeypatch):
    """Record the (port, config_path) every `serve --ensure` would start."""
    calls = []
    monkeypatch.setattr(
        "magent.cli.mobile._maybe_start_upload_server",
        lambda port, config_path: calls.append((port, config_path)),
    )
    return calls


@pytest.fixture
def served(monkeypatch):
    """Record the kwargs the blocking `serve` path hands to run_server."""
    calls = []
    monkeypatch.setattr(
        "magent.upload_server.run_server",
        lambda **kwargs: calls.append(kwargs),
    )
    # The banner shells out to `tailscale`; the port is the subject here.
    monkeypatch.setattr("magent.tailnet.ip4", lambda: None)
    return calls


class TestServeEnsureUsesConfiguredPort:
    def test_ensure_uses_the_configs_upload_port(self, runner, tmp_path, ensured):
        cfg = _write_config(tmp_path / "magent.config.json", port=8034)

        result = runner.invoke(cli.main, ["--config", cfg, "serve", "--ensure"])

        assert result.exit_code == 0, result.output
        assert ensured == [(8034, cfg)]
        assert "upload server ensured on port 8034" in result.stdout

    def test_explicit_port_still_wins(self, runner, tmp_path, ensured):
        cfg = _write_config(tmp_path / "magent.config.json", port=8034)

        result = runner.invoke(
            cli.main, ["--config", cfg, "serve", "--ensure", "-p", "9099"]
        )

        assert result.exit_code == 0, result.output
        assert ensured == [(9099, cfg)]
        assert "upload server ensured on port 9099" in result.stdout

    def test_missing_config_falls_back_to_8033(self, runner, tmp_path, ensured):
        missing = str(tmp_path / "nope.json")

        result = runner.invoke(cli.main, ["--config", missing, "serve", "--ensure"])

        # serve has always started without a config; it must not begin failing.
        assert result.exit_code == 0, result.output
        assert ensured == [(_FALLBACK_UPLOAD_PORT, missing)]

    def test_invalid_config_falls_back_to_8033(self, runner, tmp_path, ensured):
        bad = tmp_path / "magent.config.json"
        bad.write_text("{ this is not json", encoding="utf-8")

        result = runner.invoke(cli.main, ["--config", str(bad), "serve", "--ensure"])

        assert result.exit_code == 0, result.output
        assert ensured == [(_FALLBACK_UPLOAD_PORT, str(bad))]

    def test_config_without_upload_port_falls_back_to_8033(
        self, runner, tmp_path, ensured
    ):
        cfg = _write_config(tmp_path / "magent.config.json")

        result = runner.invoke(cli.main, ["--config", cfg, "serve", "--ensure"])

        assert result.exit_code == 0, result.output
        assert ensured == [(_FALLBACK_UPLOAD_PORT, cfg)]


class TestServeBlockingPathUsesConfiguredPort:
    """The `--ensure` path and the blocking path must resolve identically --
    the defect was one default shared by both, so one fix has to reach both."""

    def test_run_server_gets_the_configs_upload_port(self, runner, tmp_path, served):
        cfg = _write_config(tmp_path / "magent.config.json", port=8034)

        result = runner.invoke(cli.main, ["--config", cfg, "serve"])

        assert result.exit_code == 0, result.output
        assert served == [{"port": 8034, "config_path": cfg, "host": None}]

    def test_explicit_port_still_wins(self, runner, tmp_path, served):
        cfg = _write_config(tmp_path / "magent.config.json", port=8034)

        result = runner.invoke(cli.main, ["--config", cfg, "serve", "-p", "9099"])

        assert result.exit_code == 0, result.output
        assert served == [{"port": 9099, "config_path": cfg, "host": None}]

    def test_missing_config_falls_back_to_8033(self, runner, tmp_path, served):
        missing = str(tmp_path / "nope.json")

        result = runner.invoke(cli.main, ["--config", missing, "serve"])

        assert result.exit_code == 0, result.output
        assert served == [
            {"port": _FALLBACK_UPLOAD_PORT, "config_path": missing, "host": None}
        ]


class TestMobileFallsBackToConfiguredPort:
    def test_no_running_server_uses_the_configs_upload_port(
        self, runner, tmp_path, monkeypatch
    ):
        cfg = _write_config(tmp_path / "magent.config.json", port=8034)
        monkeypatch.setattr("magent.cli.mobile._running_upload_port", lambda: None)
        monkeypatch.setattr("magent.cli.mobile._tailnet_host", lambda: "host.example")

        result = runner.invoke(cli.main, ["--config", cfg, "mobile"])

        assert result.exit_code == 0, result.output
        assert "http://host.example:8034/" in result.stdout

    def test_a_running_server_still_wins_over_the_config(
        self, runner, tmp_path, monkeypatch
    ):
        cfg = _write_config(tmp_path / "magent.config.json", port=8034)
        monkeypatch.setattr("magent.cli.mobile._running_upload_port", lambda: 9099)
        monkeypatch.setattr("magent.cli.mobile._tailnet_host", lambda: "host.example")

        result = runner.invoke(cli.main, ["--config", cfg, "mobile"])

        assert result.exit_code == 0, result.output
        assert "http://host.example:9099/" in result.stdout


class TestConfiguredUploadPortResolver:
    def test_reads_upload_port_from_the_named_config(self, tmp_path):
        cfg = _write_config(tmp_path / "magent.config.json", port=8034)
        assert _configured_upload_port(cfg) == 8034

    def test_absent_config_is_the_fallback_not_an_exit(self, tmp_path):
        assert (
            _configured_upload_port(str(tmp_path / "nope.json"))
            == _FALLBACK_UPLOAD_PORT
        )


class TestEnsureHandsOffFromSessionZero:
    """A Session-0 upload server is not merely useless, it is HARMFUL: it takes
    the loopback port the desktop's own Alt+V needs, so every press then talks
    to a server that can see none of the desktop's sessions. `magent attach`
    fires this command on the host over ssh, which on Windows is Session 0."""

    def _plat(self, monkeypatch, **kwargs):
        plat = FakePlatform(interactive_session=False, **kwargs)
        monkeypatch.setattr("magent.platform.get_platform", lambda: plat)
        return plat

    def _policy(self, monkeypatch, value):
        monkeypatch.setenv("MAGENT_SESSION0_POLICY", value)
        monkeypatch.setattr("magent.env._cached_env", None)

    def test_it_reruns_ensure_on_the_desktop(
        self, runner, tmp_path, monkeypatch, ensured
    ):
        plat = self._plat(monkeypatch, supports_handoff=True)
        self._policy(monkeypatch, "handoff")
        cfg = _write_config(tmp_path / "magent.config.json", port=8034)

        result = runner.invoke(cli.main, ["--config", cfg, "serve", "--ensure"])

        from magent.launch import SESSION0_SERVE_TIMEOUT_S

        assert result.exit_code == 0
        # Nothing was started HERE -- that is the whole point.
        assert ensured == []
        argv, timeout = plat.handoffs[0]
        assert argv == [
            sys.executable,
            "-m",
            "magent",
            "--config",
            cfg,
            "serve",
            "-p",
            "8034",
            "--ensure",
        ]
        # A short budget: `--ensure` returns the moment a detached server is up,
        # unlike a bring-up storm.
        assert timeout == SESSION0_SERVE_TIMEOUT_S

    def test_refuse_names_the_port_contention(
        self, runner, tmp_path, monkeypatch, ensured
    ):
        self._plat(monkeypatch, supports_handoff=True)
        self._policy(monkeypatch, "refuse")
        cfg = _write_config(tmp_path / "magent.config.json", port=8034)

        result = runner.invoke(cli.main, ["--config", cfg, "serve", "--ensure"])

        assert result.exit_code == 1
        assert ensured == []
        assert "refusing to start the upload server" in result.output

    def test_allow_ensures_it_right_here(self, runner, tmp_path, monkeypatch, ensured):
        plat = self._plat(monkeypatch, supports_handoff=True)
        self._policy(monkeypatch, "allow")
        cfg = _write_config(tmp_path / "magent.config.json", port=8034)

        result = runner.invoke(cli.main, ["--config", cfg, "serve", "--ensure"])

        assert result.exit_code == 0
        assert ensured == [(8034, cfg)]
        assert plat.handoffs == []

    def test_a_foreground_serve_is_left_alone(self, runner, tmp_path, monkeypatch):
        # A plain `magent serve` is a command somebody is watching, wherever
        # they ran it. It is `--ensure` that plants a survivor nobody sees.
        plat = self._plat(monkeypatch, supports_handoff=True)
        self._policy(monkeypatch, "handoff")
        served: list[object] = []
        monkeypatch.setattr(
            "magent.upload_server.run_server",
            lambda **kwargs: served.append(kwargs),
        )
        monkeypatch.setattr("magent.tailnet.ip4", lambda: None)
        cfg = _write_config(tmp_path / "magent.config.json", port=8034)

        result = runner.invoke(cli.main, ["--config", cfg, "serve"])

        assert result.exit_code == 0
        assert plat.handoffs == []
        assert served and served[0]["port"] == 8034
