"""control: the fleet verbs as data, against the real on-disk fake psmux."""

from __future__ import annotations

import threading
import time

import pytest

from magent import control, fleetview, launch, psmux
from tests.unit._fake_psmux import make_fake_psmux

MID = "·"
CARET = chr(0x276F)
IDLE = f"done.\n{CARET} \nFable 5.1 {MID} high"
DIALOG = f"Do you want to proceed?\n{CARET} 1. Yes\n  2. No\nFable 5.1 {MID} high"
BUSY = f"* Working (3s {MID} esc to interrupt)\nFable 5.1 {MID} high"


def _node_row(session: str, nick: str, state: str) -> fleetview.SessionRow:
    """A pool-node row as ``fleetview.rows`` reports one from the sync
    daemon's last pull (no ssh is dialled to build it)."""
    return fleetview.SessionRow(
        session=session,
        name=session,
        path="",
        cwd=None,
        group=None,
        tool=None,
        enabled=True,
        node=nick,
        live=state == "live",
        hook_state=None,
        hook_state_ts=None,
        hook_state_age_s=None,
        hook_state_stale=False,
        pane_state=None,
        pane_state_ts=None,
        node_state=fleetview.NODE_STATES[state],
        model=None,
        effort=None,
        session_id=None,
    )


def _slow_pane(fake, monkeypatch) -> None:
    """Make every capture outrun the clock (the recorder sleeps for real: the
    in-process ``time.sleep`` stub does not reach the child)."""
    fake.set_capture_delay(1.5)
    monkeypatch.setattr(psmux, "CAPTURE_PANE_TIMEOUT_S", 0.3)


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
        {
            "version": 4,
            "projects": [
                {"path": str(tmp_path / "caramel"), "title": "caramel"},
                {"path": str(tmp_path / "upup"), "title": "upup"},
                {"path": str(tmp_path / "sky"), "title": "sky", "node": "cloud"},
            ],
        }
    )
    return fake, cfg


class TestTargets:
    def test_an_unknown_session_is_not_found(self, fleet):
        _fake, cfg = fleet
        with pytest.raises(control.ControlError) as err:
            control.interrupt(cfg, "ghost")
        assert err.value.code == "not_found"

    def test_fuzzy_names_are_not_resolved_here(self, fleet):
        _fake, cfg = fleet
        with pytest.raises(control.ControlError) as err:
            control.interrupt(cfg, "cara")
        assert err.value.code == "not_found"

    def test_a_configured_session_that_is_down_is_not_found(self, fleet):
        _fake, cfg = fleet
        with pytest.raises(control.ControlError) as err:
            control.interrupt(cfg, "upup")
        assert (err.value.code, err.value.details) == (
            "not_found",
            {"reason": "not_live"},
        )

    def test_a_cloud_session_is_a_conflict(self, fleet):
        fake, cfg = fleet
        fake.set_live(["caramel", "sky"])
        with pytest.raises(control.ControlError) as err:
            control.send(cfg, "sky", "hello there friend")
        assert (err.value.code, err.value.details) == ("conflict", {"reason": "cloud"})
        assert fake.send_key_calls() == []

    def test_no_psmux_is_unavailable(self, fleet, monkeypatch):
        _fake, cfg = fleet
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: None)
        with pytest.raises(control.ControlError) as err:
            control.interrupt(cfg, "caramel")
        assert err.value.code == "unavailable"

    def test_a_pool_node_session_is_a_conflict(self, fleet, monkeypatch):
        fake, cfg = fleet
        monkeypatch.setattr(
            fleetview, "rows", lambda *_a, **_k: [_node_row("remote1", "box", "live")]
        )
        with pytest.raises(control.ControlError) as err:
            control.send(cfg, "remote1", "hello there friend")
        assert (err.value.code, err.value.details) == ("conflict", {"reason": "node"})
        assert "node box" in err.value.message
        assert fake.send_key_calls() == []


class TestSend:
    def test_pastes_literally_then_presses_enter(self, fleet):
        fake, cfg = fleet
        result = control.send(cfg, "caramel", "Refactor the parser thoroughly")
        assert result == control.SendResult("caramel", True, "idle")
        sends = fake.send_key_calls()
        assert sends[0][-1] == "Refactor the parser thoroughly"
        assert "-l" in sends[0]
        assert sends[1][-1] == "Enter"
        assert "-l" not in sends[1]

    def test_a_prompt_still_on_the_input_line_is_not_confirmed(self, fleet):
        fake, cfg = fleet
        fake.set_pane(
            f"x\n{CARET} Refactor the parser thoroughly\nFable 5.1 {MID} high"
        )
        result = control.send(cfg, "caramel", "Refactor the parser thoroughly")
        assert result.confirmed is False

    def test_empty_text_is_invalid(self, fleet):
        _fake, cfg = fleet
        with pytest.raises(control.ControlError) as err:
            control.send(cfg, "caramel", "   ")
        assert err.value.code == "invalid_request"

    def test_wait_idle_that_runs_out_is_a_timeout_and_sends_nothing(self, fleet):
        fake, cfg = fleet
        fake.set_pane(BUSY)
        with pytest.raises(control.ControlError) as err:
            control.send(cfg, "caramel", "hello there", wait_idle=True, timeout_s=0)
        assert (err.value.code, err.value.details) == (
            "timeout",
            {"reason": "not_idle"},
        )
        assert fake.send_key_calls() == []

    def test_empty_text_is_refused_before_any_wait(self, fleet):
        fake, cfg = fleet
        fake.set_pane(BUSY)  # a wait here would run out as ``timeout``
        with pytest.raises(control.ControlError) as err:
            control.send(cfg, "caramel", " \n ", wait_idle=True, timeout_s=0)
        assert err.value.code == "invalid_request"
        assert fake.calls() == []

    @pytest.mark.parametrize(
        "timeout_s", [float("inf"), float("nan"), -1, control.SEND_TIMEOUT_MAX_S + 1]
    )
    def test_a_timeout_outside_the_bound_is_invalid(self, fleet, timeout_s):
        fake, cfg = fleet
        with pytest.raises(control.ControlError) as err:
            control.send(cfg, "caramel", "hello there friend", timeout_s=timeout_s)
        assert err.value.code == "invalid_request"
        assert "timeout_s" in err.value.message
        assert fake.calls() == []

    def test_the_bound_itself_is_accepted(self, fleet):
        _fake, cfg = fleet
        result = control.send(
            cfg, "caramel", "hello there friend", timeout_s=control.SEND_TIMEOUT_MAX_S
        )
        assert result.confirmed is True

    def test_a_refused_paste_on_a_live_session_is_unavailable(self, fleet):
        fake, cfg = fleet
        fake.set_send_failure()
        with pytest.raises(control.ControlError) as err:
            control.send(cfg, "caramel", "hello there friend")
        assert err.value.code == "unavailable"
        assert "psmux send failed" in err.value.message
        # Exactly one paste was attempted, then the liveness re-probe.
        assert len(fake.send_key_calls()) == 1
        assert [c for c in fake.calls() if "has-session" in c][-1][-1] == "caramel"

    def test_a_refused_paste_on_a_session_that_died_is_not_found(self, fleet):
        fake, cfg = fleet
        fake.set_send_failure(gone=True)  # dies between liveness read and paste
        with pytest.raises(control.ControlError) as err:
            control.send(cfg, "caramel", "hello there friend")
        assert (err.value.code, err.value.details) == (
            "not_found",
            {"reason": "not_live"},
        )
        assert len(fake.send_key_calls()) == 1

    def test_a_refused_paste_with_a_probe_that_runs_out_is_unavailable(
        self, fleet, monkeypatch
    ):
        """A frozen-but-live agent answers neither the send nor the re-probe
        in time: that is ``unavailable`` with the liveness unknown, never
        ``not_found`` (which would send a client to ``start``)."""
        fake, cfg = fleet
        fake.set_send_failure()
        fake.set_has_session_delay(1.0)
        monkeypatch.setattr(psmux, "CAPTURE_PANE_TIMEOUT_S", 0.3)
        with pytest.raises(control.ControlError) as err:
            control.send(cfg, "caramel", "hello there friend")
        assert (err.value.code, err.value.details) == (
            "unavailable",
            {"session_state": "unknown"},
        )

    def test_compact_then_prompt_is_two_pastes_each_with_enter(self, fleet):
        fake, cfg = fleet
        result = control.send(
            cfg, "caramel", "Refactor the parser thoroughly", compact=True
        )
        assert result == control.SendResult("caramel", True, "idle")
        sends = fake.send_key_calls()
        assert [s[-1] for s in sends] == [
            "/compact",
            "Enter",
            "Refactor the parser thoroughly",
            "Enter",
        ]
        assert "-l" in sends[0] and "-l" in sends[2]

    def test_compact_only_reports_the_pane_state_it_read(self, fleet):
        fake, cfg = fleet
        result = control.send(cfg, "caramel", "", compact=True, timeout_s=0)
        assert result == control.SendResult("caramel", True, "idle")
        assert [s[-1] for s in fake.send_key_calls()] == ["/compact", "Enter"]

    def test_compact_that_never_settles_sends_no_prompt(self, fleet):
        fake, cfg = fleet
        fake.set_pane(BUSY)
        with pytest.raises(control.ControlError) as err:
            control.send(
                cfg, "caramel", "hello there friend", compact=True, timeout_s=0
            )
        assert (err.value.code, err.value.details) == (
            "timeout",
            {"reason": "not_idle"},
        )
        assert [s[-1] for s in fake.send_key_calls()] == ["/compact", "Enter"]


class TestChoose:
    def test_presses_the_digit_alone_in_a_dialog(self, fleet):
        fake, cfg = fleet
        fake.set_pane(DIALOG)
        fake.set_pane_after_send(IDLE)
        result = control.choose(cfg, "caramel", 2)
        assert result == control.ChooseResult("caramel", 2, True, "idle")
        [call] = fake.send_key_calls()
        assert call[-1] == "2"
        assert "-l" in call
        assert "Enter" not in call

    def test_refuses_outside_a_dialog_and_types_nothing(self, fleet):
        fake, cfg = fleet
        with pytest.raises(control.ControlError) as err:
            control.choose(cfg, "caramel", 1)
        assert err.value.code == "conflict"
        assert err.value.details == {"reason": "not_in_dialog", "pane_state": "idle"}
        assert fake.send_key_calls() == []

    def test_a_dialog_still_up_is_not_confirmed(self, fleet):
        fake, cfg = fleet
        fake.set_pane(DIALOG)
        assert control.choose(cfg, "caramel", 1).confirmed is False

    @pytest.mark.parametrize("option", [0, 10, -1])
    def test_option_out_of_range_is_invalid(self, fleet, option):
        _fake, cfg = fleet
        with pytest.raises(control.ControlError) as err:
            control.choose(cfg, "caramel", option)
        assert err.value.code == "invalid_request"

    def test_a_pane_that_does_not_answer_is_a_timeout_and_types_nothing(
        self, fleet, monkeypatch
    ):
        fake, cfg = fleet
        fake.set_pane(DIALOG)
        _slow_pane(fake, monkeypatch)
        with pytest.raises(control.ControlError) as err:
            control.choose(cfg, "caramel", 2)
        assert (err.value.code, err.value.details) == (
            "timeout",
            {"reason": "pane_timeout", "delivered": False},
        )
        assert fake.send_key_calls() == []

    def test_a_refused_digit_on_a_live_session_is_unavailable(self, fleet):
        fake, cfg = fleet
        fake.set_pane(DIALOG)
        fake.set_send_failure()
        with pytest.raises(control.ControlError) as err:
            control.choose(cfg, "caramel", 2)
        assert err.value.code == "unavailable"
        assert len(fake.send_key_calls()) == 1

    def test_a_refused_digit_on_a_session_that_died_is_not_found(self, fleet):
        fake, cfg = fleet
        fake.set_pane(DIALOG)
        fake.set_send_failure(gone=True)
        with pytest.raises(control.ControlError) as err:
            control.choose(cfg, "caramel", 2)
        assert (err.value.code, err.value.details) == (
            "not_found",
            {"reason": "not_live"},
        )


class TestInterrupt:
    def test_presses_escape_as_a_key(self, fleet):
        fake, cfg = fleet
        fake.set_pane(BUSY)
        fake.set_pane_after_send(IDLE)
        result = control.interrupt(cfg, "caramel")
        assert result == control.InterruptResult("caramel", "Escape", "idle")
        [call] = fake.send_key_calls()
        assert call[-1] == "Escape"
        assert "-l" not in call
        assert call[call.index("-t") + 1] == "caramel"

    def test_a_pane_that_does_not_answer_after_the_key_says_it_was_delivered(
        self, fleet, monkeypatch
    ):
        fake, cfg = fleet
        fake.set_pane(BUSY)
        _slow_pane(fake, monkeypatch)
        with pytest.raises(control.ControlError) as err:
            control.interrupt(cfg, "caramel")
        assert (err.value.code, err.value.details) == (
            "timeout",
            {"reason": "pane_timeout", "delivered": True},
        )
        assert len(fake.send_key_calls()) == 1

    def test_a_refused_key_on_a_live_session_is_unavailable(self, fleet):
        fake, cfg = fleet
        fake.set_pane(BUSY)
        fake.set_send_failure()
        with pytest.raises(control.ControlError) as err:
            control.interrupt(cfg, "caramel")
        assert err.value.code == "unavailable"
        [call] = fake.send_key_calls()
        assert call[-1] == "Escape"

    def test_a_refused_key_on_a_session_that_died_is_not_found(self, fleet):
        fake, cfg = fleet
        fake.set_pane(BUSY)
        fake.set_send_failure(gone=True)
        with pytest.raises(control.ControlError) as err:
            control.interrupt(cfg, "caramel")
        assert (err.value.code, err.value.details) == (
            "not_found",
            {"reason": "not_live"},
        )

    def test_a_refused_key_with_a_probe_that_runs_out_is_unavailable(
        self, fleet, monkeypatch
    ):
        fake, cfg = fleet
        fake.set_pane(BUSY)
        fake.set_send_failure()
        fake.set_has_session_delay(1.0)
        monkeypatch.setattr(psmux, "CAPTURE_PANE_TIMEOUT_S", 0.3)
        with pytest.raises(control.ControlError) as err:
            control.interrupt(cfg, "caramel")
        assert (err.value.code, err.value.details) == (
            "unavailable",
            {"session_state": "unknown"},
        )
        [call] = fake.send_key_calls()
        assert call[-1] == "Escape"


class TestSetModel:
    def test_switches_an_idle_session_and_verifies_the_footer(self, fleet):
        fake, cfg = fleet
        fake.set_pane_after_send(f"x\n{CARET} \nOpus 5 {MID} high")
        result = control.set_model(cfg, "caramel", "opus")
        assert result == control.ModelResult("caramel", "Opus 5", "high", True)
        assert fake.send_key_calls()[0][-1] == "/model opus"

    def test_model_and_effort_are_two_pastes_verified_together(self, fleet):
        fake, cfg = fleet
        fake.set_pane_after_send(f"x\n{CARET} \nOpus 5 {MID} max")
        result = control.set_model(cfg, "caramel", "opus", "max")
        assert result == control.ModelResult("caramel", "Opus 5", "max", True)
        assert [s[-1] for s in fake.send_key_calls()] == [
            "/model opus",
            "Enter",
            "/effort max",
            "Enter",
        ]

    def test_an_effort_the_footer_does_not_show_is_unverified(self, fleet):
        fake, cfg = fleet
        fake.set_pane_after_send(f"x\n{CARET} \nOpus 5 {MID} high")
        result = control.set_model(cfg, "caramel", "opus", "max")
        assert result == control.ModelResult("caramel", "Opus 5", "high", False)

    def test_a_busy_session_is_a_conflict(self, fleet):
        fake, cfg = fleet
        fake.set_pane(BUSY)
        with pytest.raises(control.ControlError) as err:
            control.set_model(cfg, "caramel", "opus")
        assert err.value.details == {"reason": "busy", "pane_state": "busy"}

    def test_an_unknown_effort_is_invalid(self, fleet):
        _fake, cfg = fleet
        with pytest.raises(control.ControlError) as err:
            control.set_model(cfg, "caramel", "opus", "turbo")
        assert err.value.code == "invalid_request"

    @pytest.mark.parametrize("model", ["opus\n/quit", "opus sonnet", "-opus", "a;b"])
    def test_a_model_name_with_whitespace_or_punctuation_is_invalid(self, fleet, model):
        fake, cfg = fleet
        with pytest.raises(control.ControlError) as err:
            control.set_model(cfg, "caramel", model)
        assert err.value.code == "invalid_request"
        assert fake.send_key_calls() == []

    @pytest.mark.parametrize("model", ["opus", "claude-opus-5-5", "opus[1m]", "o4.1"])
    def test_real_model_spellings_pass(self, fleet, model):
        fake, cfg = fleet
        control.set_model(cfg, "caramel", model)
        assert fake.send_key_calls()[0][-1] == f"/model {model}"

    def test_a_switch_that_was_not_delivered_is_unavailable(self, fleet):
        fake, cfg = fleet
        fake.set_send_failure()
        with pytest.raises(control.ControlError) as err:
            control.set_model(cfg, "caramel", "opus")
        assert err.value.code == "unavailable"
        assert "psmux send failed" in err.value.message
        # One paste was refused, then the re-probe found the session alive.
        assert [s[-1] for s in fake.send_key_calls()] == ["/model opus"]
        assert [c for c in fake.calls() if "has-session" in c][-1][-1] == "caramel"

    def test_a_switch_on_a_session_that_died_is_not_found(self, fleet):
        fake, cfg = fleet
        fake.set_send_failure(gone=True)
        with pytest.raises(control.ControlError) as err:
            control.set_model(cfg, "caramel", "opus")
        assert (err.value.code, err.value.details) == (
            "not_found",
            {"reason": "not_live"},
        )

    def test_a_pane_that_does_not_answer_is_a_timeout(self, fleet, monkeypatch):
        fake, cfg = fleet
        _slow_pane(fake, monkeypatch)
        with pytest.raises(control.ControlError) as err:
            control.set_model(cfg, "caramel", "opus")
        assert err.value.code == "timeout"
        assert fake.send_key_calls() == []


class TestReadPane:
    def test_returns_the_last_lines(self, fleet):
        fake, cfg = fleet
        fake.set_pane("one\ntwo\nthree\nfour\n")
        result = control.read_pane(cfg, "caramel", lines=2)
        assert (result.text, result.timed_out) == ("three\nfour", False)
        assert result.captured_at > 0

    def test_a_slow_capture_reports_timed_out(self, fleet, monkeypatch):
        fake, cfg = fleet
        fake.set_capture_delay(1.5)
        monkeypatch.setattr(psmux, "CAPTURE_PANE_TIMEOUT_S", 0.3)
        assert control.read_pane(cfg, "caramel").timed_out is True

    @pytest.mark.parametrize("lines", [0, 2001])
    def test_lines_out_of_range_is_invalid(self, fleet, lines):
        _fake, cfg = fleet
        with pytest.raises(control.ControlError) as err:
            control.read_pane(cfg, "caramel", lines=lines)
        assert err.value.code == "invalid_request"

    @pytest.mark.parametrize("state", ["stale", "dead"])
    def test_a_node_session_that_is_not_live_is_not_found_without_dialling(
        self, fleet, monkeypatch, state
    ):
        _fake, cfg = fleet
        monkeypatch.setattr(
            fleetview, "rows", lambda *_a, **_k: [_node_row("remote1", "box", state)]
        )

        def _no_ssh(*_a, **_k):
            raise AssertionError("dialled the node")

        monkeypatch.setattr(control, "_node_pane", _no_ssh)
        with pytest.raises(control.ControlError) as err:
            control.read_pane(cfg, "remote1")
        assert (err.value.code, err.value.details) == (
            "not_found",
            {"reason": "not_live"},
        )
        assert state in err.value.message

    def test_a_live_node_session_is_read_on_its_node(self, fleet, monkeypatch):
        _fake, cfg = fleet
        monkeypatch.setattr(
            fleetview, "rows", lambda *_a, **_k: [_node_row("remote1", "box", "live")]
        )
        monkeypatch.setattr(control, "_node_pane", lambda *_a, **_k: "one\ntwo\n")
        result = control.read_pane(cfg, "remote1", lines=1)
        assert (result.text, result.timed_out) == ("two", False)


class TestStartStop:
    @pytest.fixture
    def typed(self, tmp_config, tmp_path, monkeypatch):
        for name in ("caramel", "upup"):
            (tmp_path / name).mkdir()
        fake = make_fake_psmux(tmp_path, live=["caramel"])
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: fake.path)
        cfg = tmp_config(
            {
                "version": 4,
                "projects": [
                    {"path": str(tmp_path / "caramel"), "group": "sweet"},
                    {"path": str(tmp_path / "upup"), "group": "sky"},
                ],
            }
        )
        return fake, cfg

    def test_a_named_session_outside_the_group_is_reported_as_such(
        self, typed, monkeypatch
    ):
        _fake, cfg = typed
        called = []
        monkeypatch.setattr(
            launch, "bring_up_psmux_quiet", lambda *a, **k: called.append((a, k))
        )
        result = control.start(cfg, ["caramel", "upup", "ghost"], group="sweet")
        assert called == []
        assert result == control.StartResult(
            started=[],
            already_live=["caramel"],
            failed=[
                control.StartFailure("upup", "not in group sweet"),
                control.StartFailure("ghost", "not a configured session"),
            ],
        )

    def test_start_and_stop_share_one_lock(self):
        assert isinstance(control._FLEET_LOCK, type(threading.Lock()))

    def test_start_reports_live_sessions_and_brings_up_the_rest(
        self, typed, monkeypatch
    ):
        _fake, cfg = typed
        seen = {}

        def _quiet(config, only=None, group=None, **_kw):
            seen["only"] = only
            return launch.BringUp(created=["upup"], local_failed={}, node_outcomes=[])

        monkeypatch.setattr(launch, "bring_up_psmux_quiet", _quiet)
        result = control.start(cfg, ["caramel", "upup", "ghost"])
        assert seen["only"] == ["upup"]
        assert result == control.StartResult(
            started=["upup"],
            already_live=["caramel"],
            failed=[control.StartFailure("ghost", "not a configured session")],
        )

    def test_start_needs_names_or_a_group(self, typed):
        _fake, cfg = typed
        with pytest.raises(control.ControlError) as err:
            control.start(cfg)
        assert err.value.code == "invalid_request"

    def test_stop_goes_through_the_verifying_shutdown(self, typed, monkeypatch):
        _fake, cfg = typed
        monkeypatch.setattr(
            psmux, "stop_sessions", lambda names, psmux=None: (list(names), [])
        )
        assert control.stop(cfg, ["caramel"]) == control.StopResult(["caramel"], [])

    def test_stop_refuses_an_unconfigured_name(self, typed):
        _fake, cfg = typed
        with pytest.raises(control.ControlError) as err:
            control.stop(cfg, ["caramel", "ghost"])
        assert (err.value.code, err.value.details) == (
            "not_found",
            {"unknown": ["ghost"]},
        )


class TestSendKey:
    def test_refuses_a_key_outside_the_closed_set(self):
        with pytest.raises(ValueError, match="not a key"):
            psmux.send_key("caramel", "C-c")

    def test_escape_is_sent_as_a_key_name(self, tmp_path):
        fake = make_fake_psmux(tmp_path)
        assert psmux.send_key("caramel", "Escape", psmux=fake.path) is True
        assert fake.send_key_calls() == [
            ["-L", "caramel", "send-keys", "-t", "caramel", "--", "Escape"]
        ]
