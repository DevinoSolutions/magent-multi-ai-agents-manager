"""CLI ``--json``/``--v1`` against the ``/api/v1`` route on ONE fake psmux:
the same JSON once clocks and pids are masked (spec 3.9 "Parity").

The HTTP half calls ``api.handle`` (the socket layer has its own tests in
test_upload_server_api.py); the CLI half is the real Click entry point."""

from __future__ import annotations

import json
import time

import pytest

from magent import api, cli, events, psmux
from magent.cli.mobile import _status_provider
from tests.unit._fake_psmux import make_fake_psmux

MID = "·"
CARET = chr(0x276F)
IDLE = f"done.\n{CARET} \nFable 5.1 {MID} high"
DIALOG = f"Do you want to proceed?\n{CARET} 1. Yes\n  2. No\nFable 5.1 {MID} high"
SWITCHED = f"done.\n{CARET} \nOpus 5 {MID} max"
_CLOCK_KEYS = frozenset({"ts", "pid", "uptime_s"})


def mask(value: object) -> object:
    """Every clock- or process-dependent value replaced by a marker."""
    if isinstance(value, dict):
        return {
            k: "<masked>"
            if k in _CLOCK_KEYS or k.endswith(("_ts", "_at", "age_s", "_age"))
            else mask(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [mask(v) for v in value]
    return value


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_: None)


@pytest.fixture(autouse=True)
def _patient_capture(monkeypatch):
    monkeypatch.setattr(psmux, "CAPTURE_PANE_TIMEOUT_S", 60.0)


def make_ctx(cfg: str, tmp_path) -> api.ApiContext:
    """What ``magent serve`` lends the routes, over ``cfg`` -- the status
    provider being the real one ``serve_cmd`` hands down."""
    return api.ApiContext(
        config_path=cfg,
        bus=events.EventBus("deadbeef"),
        allowed_hosts=api.LOOPBACK_NAMES,
        upload_dir=tmp_path / "uploads",
        upload_max_bytes=1000,
        request_limit=2000,
        health=lambda: api.Health("magent-upload", 8080, 1, 0.0, None, None),
        upload_sessions=set,
        status_provider=_status_provider(cfg),
    )


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
            ],
        }
    )
    return fake, cfg, make_ctx(cfg, tmp_path)


def http(ctx, method, path, payload=None, query=None):
    body = b"" if payload is None else json.dumps(payload).encode()
    req = api.ApiRequest(
        method=method,
        path=path,
        query=query or {},
        headers={
            "host": "127.0.0.1:8080",
            "x-magent-client": "parity",
            "content-type": "application/json",
        },
        body=api.Body.of(body),
        peer="127.0.0.1",
        bind="127.0.0.1",
        port=8080,
    )
    resp = api.handle(req, ctx)
    assert isinstance(resp, api.ApiResponse)
    return resp.status, resp.body


def run_cli(runner, cfg, *args):
    result = runner.invoke(cli.main, ["--config", cfg, *args])
    return result.exit_code, json.loads(result.stdout)


class TestReads:
    def test_sessions_v1_is_get_sessions_fresh(self, runner, fleet):
        _fake, cfg, ctx = fleet
        code, out = run_cli(runner, cfg, "sessions", "--v1")
        status, body = http(ctx, "GET", "/api/v1/sessions", query={"fresh": ["1"]})
        assert (code, status) == (0, 200)
        assert mask(out) == mask(body)
        assert [r["session"] for r in out["data"]["sessions"]] == ["caramel", "upup"]

    def test_status_v1_is_get_status(self, runner, fleet):
        _fake, cfg, ctx = fleet
        _code, out = run_cli(runner, cfg, "status", "--v1")
        status, body = http(ctx, "GET", "/api/v1/status")
        assert status == 200
        assert mask(out) == mask(body)

    def test_peek_json_is_get_pane(self, runner, fleet):
        _fake, cfg, ctx = fleet
        code, out = run_cli(runner, cfg, "peek", "caramel", "-n", "40", "--json")
        status, body = http(
            ctx, "GET", "/api/v1/sessions/caramel/pane", query={"lines": ["40"]}
        )
        assert (code, status) == (0, 200)
        assert mask(out) == mask(body)
        assert out["data"]["text"].endswith(f"Fable 5.1 {MID} high")


class TestWrites:
    def test_send_json_is_post_send(self, runner, fleet):
        fake, cfg, ctx = fleet
        fake.set_pane_after_send(IDLE)
        code, out = run_cli(runner, cfg, "send", "caramel", "Refactor it", "--json")
        fake.set_pane_after_send(IDLE)
        status, body = http(
            ctx, "POST", "/api/v1/sessions/caramel/send", {"text": "Refactor it"}
        )
        assert (code, status) == (0, 200)
        assert out == body
        assert out["data"] == {
            "session": "caramel",
            "confirmed": True,
            "pane_state_after": "idle",
        }

    def test_choose_json_is_post_choose(self, runner, fleet):
        fake, cfg, ctx = fleet
        fake.set_pane(DIALOG)
        fake.set_pane_after_send(IDLE)
        code, out = run_cli(runner, cfg, "choose", "caramel", "2", "--json")
        fake.set_pane(DIALOG)
        fake.set_pane_after_send(IDLE)
        status, body = http(
            ctx, "POST", "/api/v1/sessions/caramel/choose", {"option": 2}
        )
        assert (code, status) == (0, 200)
        assert out == body

    def test_interrupt_json_is_post_interrupt(self, runner, fleet):
        _fake, cfg, ctx = fleet
        code, out = run_cli(runner, cfg, "interrupt", "caramel", "--json")
        status, body = http(ctx, "POST", "/api/v1/sessions/caramel/interrupt", {})
        assert (code, status) == (0, 200)
        assert out == body

    def test_model_json_is_post_model(self, runner, fleet):
        fake, cfg, ctx = fleet
        fake.set_pane_after_send(SWITCHED)
        code, out = run_cli(
            runner, cfg, "model", "caramel", "opus", "--effort", "max", "--json"
        )
        fake.set_pane(IDLE)
        fake.set_pane_after_send(SWITCHED)
        status, body = http(
            ctx,
            "POST",
            "/api/v1/sessions/caramel/model",
            {"model": "opus", "effort": "max"},
        )
        assert (code, status) == (0, 200)
        assert out == body
        assert out["data"]["verified"] is True

    def test_a_refusal_is_the_same_error_envelope(self, runner, fleet):
        _fake, cfg, ctx = fleet
        code, out = run_cli(runner, cfg, "choose", "caramel", "1", "--json")
        status, body = http(
            ctx, "POST", "/api/v1/sessions/caramel/choose", {"option": 1}
        )
        assert (code, status) == (2, 409)
        assert out == body
        assert out["error"]["details"] == {
            "reason": "not_in_dialog",
            "pane_state": "idle",
        }


class TestConfigRefusals:
    """A broken config is the one ``unavailable`` envelope on both sides,
    same ``config:`` text: the CLI exits 1 (``status``'s legacy contract,
    which ``sessions --v1`` shares), the route answers 503. A MISSING config
    splits by route, the same way on both sides: ``status`` refuses it as
    ``unavailable`` too, while ``sessions`` answers an empty ``ok`` list."""

    @pytest.fixture(params=["missing", "broken"])
    def bad_cfg(self, request, tmp_path):
        path = tmp_path / "magent.config.json"
        if request.param == "broken":
            path.write_text('{"projects": "nope"}', encoding="utf-8")
        return str(path)

    def test_status_v1_refusal_is_get_status_refusal(self, runner, tmp_path, bad_cfg):
        ctx = make_ctx(bad_cfg, tmp_path)
        code, out = run_cli(runner, bad_cfg, "status", "--v1")
        status, body = http(ctx, "GET", "/api/v1/status")
        assert (code, status) == (1, 503)
        assert out == body
        assert out["error"]["code"] == "unavailable"
        assert out["error"]["message"].startswith("config: ")

    def test_sessions_v1_answer_is_get_sessions_answer(
        self, runner, tmp_path, bad_cfg, request
    ):
        """A broken config is the ``unavailable`` envelope on both sides. A
        MISSING one is not a refusal on this route (``fleetview.rows`` lists
        nothing for a config that is not there), and the CLI says the same:
        an empty ``ok`` list, exit 0, exactly what the route answers."""
        missing = request.node.callspec.params["bad_cfg"] == "missing"
        ctx = make_ctx(bad_cfg, tmp_path)
        code, out = run_cli(runner, bad_cfg, "sessions", "--v1")
        status, body = http(ctx, "GET", "/api/v1/sessions", query={"fresh": ["1"]})
        assert mask(out) == mask(body)
        assert out["ok"] is missing
        if missing:
            assert (code, status) == (0, 200)
            assert out["data"]["sessions"] == []
        else:
            assert (code, status) == (1, 503)
            assert out["error"]["code"] == "unavailable"
            assert out["error"]["message"].startswith("config: ")
