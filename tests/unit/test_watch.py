"""Tests for `magent watch` (cli/watch.py) and the status agents snapshot."""

from __future__ import annotations

import json
import time

from magent import agent_state, cli
from magent.cli.watch import _age_label, _focus_by_name
from tests.conftest import FakePlatform


def _write_aged_working(path, seconds: float = 120.0) -> None:
    """Write a WORKING record for `path`, then age its ts `seconds` into the
    past — older than the tests' 10s config staleness but far younger than the
    1800s module default, so the effective state reveals which window won."""
    agent_state.write_state(str(path), agent_state.WORKING)
    files = list(agent_state.STATE_DIR.glob("*.json"))
    rec = json.loads(files[0].read_text(encoding="utf-8"))
    rec["ts"] = time.time() - seconds
    files[0].write_text(json.dumps(rec), encoding="utf-8")


class TestAgeLabel:
    def test_seconds(self):
        assert _age_label(42.7) == "42s"

    def test_minutes(self):
        assert _age_label(125) == "2m05s"

    def test_hours(self):
        assert _age_label(3725) == "1h02m"


class TestFocusByName:
    def test_md_window_focused(self):
        fp = FakePlatform(windows={"magent:[!] api": 7}, supports_attention=True)

        assert _focus_by_name(fp, "api") is True
        assert fp.focused == [7]

    def test_contains_fallback_for_ide_windows(self):
        fp = FakePlatform(windows={"api — Visual Studio Code": 9})
        fp.find_window = lambda title, mode="exact": 9 if mode == "contains" else None

        assert _focus_by_name(fp, "api") is True
        assert fp.focused == [9]

    def test_flash_fallback_when_focus_fails(self):
        class _NoFocus(FakePlatform):
            def focus_window(self, handle) -> bool:
                return False

        fp = _NoFocus(windows={"magent:api": 7}, supports_attention=True)

        assert _focus_by_name(fp, "api") is True
        assert fp.flashed == [7]

    def test_no_window_returns_false(self):
        fp = FakePlatform(windows={})

        assert _focus_by_name(fp, "ghost") is False


class TestWatchOnce:
    def test_renders_sessions_most_urgent_first(
        self, runner, monkeypatch, tmp_path, tmp_config
    ):
        api = tmp_path / "api"
        web = tmp_path / "web"
        api.mkdir()
        web.mkdir()
        agent_state.write_state(str(api), agent_state.NEEDS_INPUT)
        agent_state.write_state(str(web), agent_state.WORKING)

        fp = FakePlatform()
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
        config_path = tmp_config(
            {
                "version": 2,
                "projects": [{"path": str(api)}, {"path": str(web)}],
            }
        )

        result = runner.invoke(cli.main, ["--config", config_path, "watch", "--once"])

        assert result.exit_code == 0, result.output
        assert "needs-input" in result.output
        assert result.output.index("api") < result.output.index("web")

    def test_empty_store_renders_hint(self, runner, monkeypatch, tmp_config):
        fp = FakePlatform()
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
        config_path = tmp_config({"version": 2, "projects": [{"path": "api"}]})

        result = runner.invoke(cli.main, ["--config", config_path, "watch", "--once"])

        assert result.exit_code == 0
        assert "No agent sessions" in result.output


class TestStatusJsonAgents:
    def test_agents_listed_with_states(self, runner, monkeypatch, tmp_path, tmp_config):
        api = tmp_path / "api"
        api.mkdir()
        agent_state.write_state(str(api), agent_state.DONE)
        config_path = tmp_config({"version": 2, "projects": [{"path": str(api)}]})

        result = runner.invoke(cli.main, ["--config", config_path, "status", "--json"])

        payload = json.loads(result.stdout)
        assert payload["agents"] == [
            {"name": "api", "state": "done", "age_s": payload["agents"][0]["age_s"]}
        ]
        assert payload["agents"][0]["age_s"] >= 0


class TestConfigStaleness:
    """status and watch honor settings.attention.stalenessWorkingS — both built
    the engine with module defaults before, ignoring config (P3-10 follow-up)."""

    def test_status_json_ages_working_per_config(
        self, runner, monkeypatch, tmp_path, tmp_config
    ):
        api = tmp_path / "api"
        api.mkdir()
        _write_aged_working(api)
        config_path = tmp_config(
            {
                "version": 3,
                "projects": [{"path": str(api)}],
                "settings": {"attention": {"stalenessWorkingS": 10}},
            }
        )

        result = runner.invoke(cli.main, ["--config", config_path, "status", "--json"])

        payload = json.loads(result.stdout)
        # honoring the 10s config value ages the 120s-old 'working' to 'idle';
        # the module default (1800s) would have kept it 'working'.
        assert payload["agents"][0]["state"] == "idle"

    def test_watch_ages_working_per_config(
        self, runner, monkeypatch, tmp_path, tmp_config
    ):
        api = tmp_path / "api"
        api.mkdir()
        _write_aged_working(api)
        fp = FakePlatform()
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)
        config_path = tmp_config(
            {
                "version": 3,
                "projects": [{"path": str(api)}],
                "settings": {"attention": {"stalenessWorkingS": 10}},
            }
        )

        result = runner.invoke(cli.main, ["--config", config_path, "watch", "--once"])

        assert result.exit_code == 0, result.output
        # 'idle' (aged) rather than 'working' proves watch's engine used config.
        assert "idle" in result.output
        assert "working" not in result.output
