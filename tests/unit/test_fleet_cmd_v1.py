"""`magent choose` / `interrupt`, and the `--json`/`--v1` shells' refusals."""

from __future__ import annotations

import json
import time

import pytest

from magent import cli, control, psmux
from tests.unit._fake_psmux import make_fake_psmux

MID = "·"
CARET = chr(0x276F)
IDLE = f"done.\n{CARET} \nFable 5.1 {MID} high"
DIALOG = f"Do you want to proceed?\n{CARET} 1. Yes\n  2. No\nFable 5.1 {MID} high"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)


@pytest.fixture(autouse=True)
def _patient_capture(monkeypatch):
    monkeypatch.setattr(psmux, "CAPTURE_PANE_TIMEOUT_S", 60.0)


@pytest.fixture
def fleet(tmp_config, tmp_path, monkeypatch):
    fake = make_fake_psmux(tmp_path, pane=IDLE, live=["caramel"])
    monkeypatch.setattr("magent.psmux.find_psmux", lambda: fake.path)
    cfg = tmp_config(
        {"projects": [{"path": str(tmp_path / "caramel"), "title": "caramel"}]}
    )
    return fake, cfg


class TestChoose:
    def test_answers_a_dialog(self, runner, fleet):
        fake, cfg = fleet
        fake.set_pane(DIALOG)
        fake.set_pane_after_send(IDLE)
        result = runner.invoke(cli.main, ["--config", cfg, "choose", "cara", "2"])
        assert result.exit_code == 0
        assert "chose 2 in" in result.output
        [call] = fake.send_key_calls()
        assert (call[-1], "-l" in call) == ("2", True)

    def test_a_dialog_still_up_exits_4(self, runner, fleet):
        fake, cfg = fleet
        fake.set_pane(DIALOG)
        result = runner.invoke(cli.main, ["--config", cfg, "choose", "caramel", "1"])
        assert result.exit_code == 4
        assert "still shows a dialog" in result.output

    def test_no_dialog_exits_2_and_types_nothing(self, runner, fleet):
        fake, cfg = fleet
        result = runner.invoke(cli.main, ["--config", cfg, "choose", "caramel", "1"])
        assert result.exit_code == 2
        assert "not showing a dialog" in result.output
        assert fake.send_key_calls() == []

    @pytest.mark.parametrize("option", ["0", "10"])
    def test_an_option_outside_1_to_9_is_a_usage_error(self, runner, fleet, option):
        _fake, cfg = fleet
        result = runner.invoke(cli.main, ["--config", cfg, "choose", "caramel", option])
        assert result.exit_code == 2


class TestInterrupt:
    def test_presses_escape(self, runner, fleet):
        fake, cfg = fleet
        result = runner.invoke(cli.main, ["--config", cfg, "interrupt", "caramel"])
        assert result.exit_code == 0
        assert "interrupted" in result.output
        [call] = fake.send_key_calls()
        assert (call[-1], "-l" in call) == ("Escape", False)

    def test_json(self, runner, fleet):
        _fake, cfg = fleet
        result = runner.invoke(
            cli.main, ["--config", cfg, "interrupt", "caramel", "--json"]
        )
        assert json.loads(result.stdout) == {
            "ok": True,
            "data": {"session": "caramel", "key": "Escape", "pane_state_after": "idle"},
        }


class TestJsonRefusals:
    def test_no_match_is_a_not_found_envelope(self, runner, fleet):
        _fake, cfg = fleet
        result = runner.invoke(
            cli.main, ["--config", cfg, "send", "ghost", "hi", "--json"]
        )
        assert result.exit_code == 2
        body = json.loads(result.stdout)
        assert body["error"]["code"] == "not_found"
        assert body["error"]["details"] == {"live": ["caramel"]}

    def test_no_psmux_is_an_unavailable_envelope(self, runner, fleet, monkeypatch):
        _fake, cfg = fleet
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: None)
        result = runner.invoke(
            cli.main, ["--config", cfg, "interrupt", "caramel", "--json"]
        )
        assert result.exit_code == 3
        assert json.loads(result.stdout)["error"]["code"] == "unavailable"

    def test_model_json_takes_one_session(self, runner, fleet):
        _fake, cfg = fleet
        result = runner.invoke(
            cli.main, ["--config", cfg, "model", "--all", "opus", "--json"]
        )
        assert result.exit_code == 2

    def test_model_json_reports_the_last_refusal(self, runner, fleet, monkeypatch):
        _fake, cfg = fleet
        calls = []

        def _refuse(*args, **kwargs):
            calls.append(args)
            raise control.ControlError(
                "unavailable", "psmux send failed", {"session_state": "live"}
            )

        monkeypatch.setattr(control, "set_model", _refuse)
        result = runner.invoke(
            cli.main, ["--config", cfg, "model", "caramel", "opus", "--json"]
        )
        assert result.exit_code == 3
        assert len(calls) == 3  # retried, then given up
        assert json.loads(result.stdout)["error"] == {
            "code": "unavailable",
            "message": "psmux send failed",
            "details": {"session_state": "live"},
        }

    def test_model_json_reports_a_refused_name_at_once(
        self, runner, fleet, monkeypatch
    ):
        _fake, cfg = fleet
        calls = []

        def _refuse(*args, **kwargs):
            calls.append(args)
            raise control.ControlError("invalid_request", "model must be a name")

        monkeypatch.setattr(control, "set_model", _refuse)
        result = runner.invoke(
            cli.main, ["--config", cfg, "model", "caramel", "op;us", "--json"]
        )
        assert result.exit_code == 2
        assert len(calls) == 1
        assert json.loads(result.stdout)["error"]["code"] == "invalid_request"

    def test_model_json_always_busy_is_an_idle_timeout(
        self, runner, fleet, monkeypatch
    ):
        _fake, cfg = fleet
        calls = []

        def _busy(*args, **kwargs):
            calls.append(args)
            raise control.ControlError(
                "conflict",
                "caramel is not idle (pane is busy)",
                {"reason": "busy", "pane_state": "busy"},
            )

        monkeypatch.setattr(control, "set_model", _busy)
        argv = ["--config", cfg, "model", "caramel", "opus", "--json"]
        result = runner.invoke(cli.main, [*argv, "--max-minutes", "0.001"])
        assert result.exit_code == 4
        assert len(calls) >= 1  # swept until the deadline, never given up early
        error = json.loads(result.stdout)["error"]
        assert (error["code"], error["details"]["reason"]) == ("timeout", "not_idle")

    def test_model_json_reports_the_switch_after_busy_sweeps(
        self, runner, fleet, monkeypatch
    ):
        _fake, cfg = fleet
        calls = []

        def _switch(*args, **kwargs):
            calls.append(args)
            if len(calls) < 3:
                raise control.ControlError(
                    "conflict", "busy", {"reason": "busy", "pane_state": "busy"}
                )
            return control.ModelResult("caramel", "Opus 5", "max", True)

        monkeypatch.setattr(control, "set_model", _switch)
        result = runner.invoke(
            cli.main, ["--config", cfg, "model", "caramel", "opus", "--json"]
        )
        assert result.exit_code == 0
        assert len(calls) == 3  # busy, busy, switched
        assert json.loads(result.stdout) == {
            "ok": True,
            "data": {
                "session": "caramel",
                "model": "Opus 5",
                "effort": "max",
                "verified": True,
            },
        }

    def test_status_v1_without_a_config(self, runner, tmp_path):
        result = runner.invoke(
            cli.main, ["--config", str(tmp_path / "absent.json"), "status", "--v1"]
        )
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"]["code"] == "unavailable"

    def test_sessions_v1_with_a_broken_config(self, runner, tmp_path):
        bad = tmp_path / "magent.config.json"
        bad.write_text('{"projects": "nope"}', encoding="utf-8")
        result = runner.invoke(cli.main, ["--config", str(bad), "sessions", "--v1"])
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"]["code"] == "unavailable"
