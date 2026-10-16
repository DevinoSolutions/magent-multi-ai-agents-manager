"""ApiLinkHost: the link's whole view of a running serve, through api.handle
only -- reads, the remote-control gate on writes, the routes a device may
reach at all, the snapshot, the event feed, the upload cap and the staged
upload."""

from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path

import pytest

import magent.upload_server as upload_server
from magent import api, events, fleetview, link_host, link_staged, linkfiles
from magent.link_host import ApiLinkHost
from magent.upload_server import UploadHandler

PORT = 8080


def _row(session: str) -> fleetview.SessionRow:
    return fleetview.SessionRow(
        session=session,
        name=session,
        path=f"work/{session}",
        cwd=None,
        group=None,
        tool=None,
        enabled=True,
        node=None,
        live=True,
        hook_state="working",
        hook_state_ts=100.0,
        hook_state_age_s=2.0,
        hook_state_stale=False,
        pane_state="busy",
        pane_state_ts=101.0,
        node_state=None,
        model="Opus",
        effort="high",
        session_id="abc",
    )


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "magent.config.json"
    path.write_text(json.dumps({"version": 4, "projects": []}), encoding="utf-8")
    return path


def _remote_control(config: Path, value: object) -> None:
    data = json.loads(config.read_text(encoding="utf-8"))
    data["settings"] = {"link": {"remoteControl": value}}
    config.write_text(json.dumps(data), encoding="utf-8")
    stamp = time.time_ns() + 10_000_000
    os.utime(config, ns=(stamp, stamp))  # a new mtime even on a coarse clock


@pytest.fixture
def bus():
    return events.EventBus("deadbeef")


@pytest.fixture
def ctx(config, bus, tmp_path, monkeypatch):
    monkeypatch.setattr(fleetview, "rows", lambda *_a, **_k: [_row("magent-alpha")])
    monkeypatch.setattr("magent.psmux.find_psmux", lambda: "psmux")
    return api.ApiContext(
        config_path=str(config),
        bus=bus,
        allowed_hosts=frozenset({"127.0.0.1"}),
        upload_dir=tmp_path / "uploads",
        upload_max_bytes=4321,
        request_limit=8000,
        health=lambda: api.Health("magent-upload", PORT, 1, 1.0, 1, 0.1),
        upload_sessions=lambda: {"magent-alpha"},
        inject_fn=lambda _s, _t: (True, False),
        heartbeat_s=0.05,
    )


@pytest.fixture
def host(ctx, config):
    return ApiLinkHost(lambda: ctx, str(config))


def _drained(req: api.ApiRequest) -> tuple[api.ApiRequest, bytes]:
    """``req``'s body read out, and the same request with a fresh body."""
    raw = req.body.read1(req.body.length or 0)
    return dataclasses.replace(req, body=api.Body.of(raw)), raw


@pytest.fixture
def seen(monkeypatch):
    """Every request that reached api.handle: (request, caller, body bytes)."""
    got: list[tuple[api.ApiRequest, api.Caller | None, bytes]] = []
    real = api.handle

    def spy(req, ctx, caller=None):
        req, raw = _drained(req)
        got.append((req, caller, raw))
        return real(req, ctx, caller)

    monkeypatch.setattr(api, "handle", spy)
    return got


@pytest.fixture
def reached(monkeypatch):
    """Every (method, path) that reached api.handle, which answers 200 {}
    without running anything."""
    got: list[tuple[str, str]] = []

    def stub(req, ctx, caller=None):
        got.append((req.method, req.path))
        return api.ApiResponse(200, {"ok": True, "data": {}})

    monkeypatch.setattr(api, "handle", stub)
    return got


def _reason(envelope: dict[str, object]) -> object:
    error = envelope["error"]
    assert isinstance(error, dict)
    return error.get("details", {}).get("reason")


def test_a_read_answers_like_the_local_api(host):
    status, envelope = host.handle("GET", "/api/v1/sessions", None, "dev_DECOY_phone")
    assert status == 200
    assert envelope["data"]["sessions"][0]["session"] == "magent-alpha"


def test_a_query_string_reaches_the_route(host):
    status, envelope = host.handle(
        "GET", "/api/v1/events/poll?wait=0", None, "dev_DECOY_phone"
    )
    assert status == 200
    assert envelope["data"]["events"] == []


def test_a_command_names_its_device_and_is_always_json(host, config, seen):
    # The device chooses no header: its command always reaches the API as
    # application/json from a linked caller, so it can never pick the staged
    # upload form's content type.
    _remote_control(config, True)
    body = json.dumps({"sessions": []}).encode()
    host.handle("POST", "/api/v1/sessions/stop", body, "dev_DECOY_phone")
    [(req, caller, _raw)] = seen
    assert req.header("content-type") == "application/json"
    assert req.header("x-magent-client") == "magent-relay/dev_DECOY_phone"
    assert caller == api.Caller("linked", remote_control=True)


class TestRemoteControl:
    def test_writes_are_refused_while_it_is_off(self, host):
        body = json.dumps({"sessions": []}).encode()
        status, envelope = host.handle(
            "POST", "/api/v1/sessions/stop", body, "dev_DECOY_phone"
        )
        assert status == 403
        assert _reason(envelope) == "remote_control_off"

    def test_writes_pass_the_gate_once_it_is_on(self, host, config):
        _remote_control(config, True)
        body = json.dumps({"sessions": []}).encode()
        status, envelope = host.handle(
            "POST", "/api/v1/sessions/stop", body, "dev_DECOY_phone"
        )
        assert status == 400  # past the write gate: the route itself wants sessions
        assert envelope["error"]["code"] == "invalid_request"

    def test_meta_tells_the_device_whether_it_may_write(self, host, config):
        _status, envelope = host.handle("GET", "/api/v1/meta", None, "dev_DECOY_phone")
        assert envelope["data"]["write_allowed"] is False
        _remote_control(config, True)
        _status, envelope = host.handle("GET", "/api/v1/meta", None, "dev_DECOY_phone")
        assert envelope["data"]["write_allowed"] is True

    def test_it_is_re_read_when_the_config_changes(self, host, config):
        assert host.remote_control() is False
        _remote_control(config, True)
        assert host.remote_control() is True
        _remote_control(config, False)
        assert host.remote_control() is False

    def test_a_config_that_does_not_load_turns_it_off(self, host, config):
        _remote_control(config, True)
        assert host.remote_control() is True
        _remote_control(config, "yes")  # wrong type: a ConfigError, never "on"
        assert host.remote_control() is False

    def test_a_config_that_is_gone_turns_it_off(self, host, config):
        _remote_control(config, True)
        assert host.remote_control() is True
        config.unlink()
        assert host.remote_control() is False

    def test_no_config_means_off(self):
        assert ApiLinkHost(lambda: None, None).remote_control() is False

    def test_callers_at_once_share_one_config_read(self, host, config, monkeypatch):
        _remote_control(config, True)
        loads: list[int] = []
        real = link_host.load_config

        def slow(path):
            loads.append(1)
            time.sleep(0.2)
            return real(path)

        monkeypatch.setattr(link_host, "load_config", slow)
        got = _at_once(host.remote_control, 6)
        assert got == [True] * 6
        assert len(loads) == 1


def _at_once(fn, n: int) -> list[object]:
    """``fn()`` on ``n`` threads released together; their answers."""
    gate = threading.Barrier(n)
    out: list[object] = [None] * n

    def one(i: int) -> None:
        gate.wait(5)
        out[i] = fn()

    threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads)
    return out


class TestTunnel:
    """A device reaches only the routes contract C3 tunnels (reads) and the
    write verbs, which P1's remote-control gate then judges. Everything else
    is refused before it is routed, whatever the relay let through."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/api/v1/meta"),
            ("GET", "/api/v1/health"),
            ("GET", "/api/v1/status"),
            ("GET", "/api/v1/sessions?fresh=1"),
            ("GET", "/api/v1/sessions/magent-alpha"),
            ("GET", "/api/v1/sessions/magent-alpha/pane?lines=20"),
            ("GET", "/api/v1/projects"),
            ("GET", "/api/v1/events/poll?since=deadbeef:0&wait=0"),
            ("POST", "/api/v1/sessions/start"),
            ("POST", "/api/v1/sessions/magent-alpha/send"),
            ("POST", "/api/v1/sessions/magent-alpha/choose"),
            ("POST", "/api/v1/sessions/magent-alpha/interrupt"),
            ("POST", "/api/v1/sessions/magent-alpha/model"),
            ("POST", "/api/v1/projects"),
            ("PATCH", "/api/v1/projects/alpha"),
            ("DELETE", "/api/v1/projects/alpha?stop=1"),
        ],
    )
    def test_the_tunnelled_routes_reach_the_api(self, host, reached, method, path):
        assert host.handle(method, path, None, "dev_DECOY_phone")[0] == 200
        assert reached == [(method, path.split("?")[0])]

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/api/v1/events"),  # a stream
            ("POST", "/api/v1/uploads"),  # uploads come only through the puller
            ("GET", "/api/v1/sessions/../pane"),
            ("GET", "/api/v1/sessions/./pane"),
            ("GET", "/api/v1/sessions/.."),
            ("GET", "/api/v1/sessions/%2e%2e/pane"),
            ("GET", "/api/v1/sessions/a%2Fb/pane"),
            ("GET", "/api/v1//sessions"),
            ("GET", "/api/v1/meta/"),
            ("GET", "/api/v1/sessions/a\\b"),
            ("GET", "/api/v1/sessions/" + "s" * 129),
            ("GET", "/api/v1/sessions/magent-alpha/transcript"),
            ("GET", "/api/v1/meta#frag"),
            ("GET", "/API/v1/meta"),
            ("POST", "/api/v1/meta"),
            ("HEAD", "/api/v1/health"),
            ("OPTIONS", "/api/v1/sessions"),
            ("DELETE", "/api/v1/sessions"),
            ("GET", "/api/v1/sessions/start/send"),
            ("PATCH", "/api/v1/projects/../x"),
        ],
    )
    def test_anything_else_is_refused_before_routing(self, host, reached, method, path):
        status, envelope = host.handle(method, path, None, "dev_DECOY_phone")
        assert status == 400
        assert _reason(envelope) == "not_tunnelled"
        assert reached == []

    def test_a_longest_session_id_is_tunnelled(self, host, reached):
        host.handle("GET", "/api/v1/sessions/" + "s" * 128, None, "dev_DECOY_phone")
        assert len(reached) == 1


def test_a_stream_route_is_not_served_over_the_link(host):
    status, envelope = host.handle("GET", "/api/v1/events", None, "dev_DECOY_phone")
    assert status == 400
    assert envelope["error"]["code"] == "invalid_request"


def test_a_device_command_can_never_use_the_staged_form(host, config, tmp_path):
    _remote_control(config, True)
    secret = linkfiles.INCOMING_DIR / "up_DECOY_1" / "x.png"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_bytes(b"PNG")
    body = json.dumps(
        {"staged_path": str(secret), "name": "x.png", "session": "magent-alpha"}
    ).encode()
    status, _envelope = host.handle("POST", "/api/v1/uploads", body, "dev_DECOY_phone")
    assert status == 400
    assert not (tmp_path / "uploads").exists()


def test_an_answer_that_is_not_json_is_a_500(host, monkeypatch):
    # The runner serialises the body outside its own guard: an answer that
    # cannot be is caught here.
    monkeypatch.setattr(
        api, "handle", lambda *_a, **_k: api.ApiResponse(200, {"x": object()})
    )
    status, envelope = host.handle("GET", "/api/v1/meta", None, "dev_DECOY_phone")
    assert status == 500
    assert envelope["error"]["code"] == "internal"
    json.dumps(envelope)


class TestStagedFormOverHttp:
    """Caller("linked") exists only in this process: no HTTP request, however
    it is dressed, reaches the staged upload form."""

    @pytest.fixture
    def server(self, tmp_path, monkeypatch, config):
        monkeypatch.setattr(upload_server, "_UPLOAD_DIR", tmp_path / "uploads")
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: "psmux")
        monkeypatch.setattr(UploadHandler, "config_path", str(config))
        monkeypatch.setattr(
            UploadHandler,
            "cached_sessions",
            [{"name": "magent-alpha", "session": "magent-alpha", "path": "w"}],
        )
        monkeypatch.setattr(UploadHandler, "sessions_ts", time.time() + 9999)
        monkeypatch.setattr(UploadHandler, "bus", events.EventBus("deadbeef"))
        monkeypatch.setattr(UploadHandler, "status_provider", None)
        httpd = HTTPServer(("127.0.0.1", 0), UploadHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        yield httpd.server_address[1]
        httpd.shutdown()
        httpd.server_close()

    @pytest.mark.parametrize(
        "headers",
        [
            {"X-Magent-Client": "pytest"},
            {"X-Magent-Client": "magent-relay/dev_DECOY_phone"},
            {"X-Magent-Client": "magent-relay", "Host": "magent-relay"},
        ],
    )
    def test_a_loopback_request_is_refused(self, server, tmp_path, headers):
        staged = linkfiles.INCOMING_DIR / "up_DECOY_3" / "x.png"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"PNG")
        raw = json.dumps(
            {"staged_path": str(staged), "name": "x.png", "session": "magent-alpha"}
        ).encode()
        conn = HTTPConnection("127.0.0.1", server, timeout=5)
        conn.request(
            "POST",
            "/api/v1/uploads",
            body=raw,
            headers={"Content-Type": link_staged.CONTENT_TYPE, **headers},
        )
        resp = conn.getresponse()
        envelope = json.loads(resp.read())
        conn.close()
        assert resp.status == 403
        assert _reason(envelope) == "not_linked"
        assert not (tmp_path / "uploads").exists()


class TestSnapshot:
    def test_the_snapshot_is_the_session_rows_and_the_daemon(self, host, bus):
        rows, daemon = host.snapshot()
        assert [r["session"] for r in rows] == ["magent-alpha"]
        assert rows[0]["model"] == "Opus"  # the full row: it is sealed, not routed
        assert daemon["epoch"] == bus.epoch
        assert daemon["platform"] in {"windows", "linux", "darwin"}
        assert daemon["version"]

    def test_a_failed_read_keeps_the_last_rows_and_never_raises(
        self, host, monkeypatch
    ):
        rows, _daemon = host.snapshot()
        assert [r["session"] for r in rows] == ["magent-alpha"]

        def broken(*_a, **_k):
            raise ValueError("DECOY config broke")

        monkeypatch.setattr(fleetview, "rows", broken)
        rows, daemon = host.snapshot()
        assert [r["session"] for r in rows] == ["magent-alpha"]
        assert daemon["epoch"] == "deadbeef"

    def test_a_context_that_fails_is_an_empty_snapshot_not_a_crash(self):
        def no_server():
            raise RuntimeError("DECOY serve is not up")

        rows, daemon = ApiLinkHost(no_server, None).snapshot()
        assert rows == []
        assert set(daemon) == {"version", "platform", "epoch"}

    def test_each_caller_gets_its_own_rows(self, host, monkeypatch):
        # Two threads (the runner, a forwarder resync) may hold one at once:
        # what one does to its rows never reaches the other's.
        first, _ = host.snapshot()
        first[0]["session"] = "changed"

        def broken(*_a, **_k):
            raise ValueError("DECOY config broke")

        monkeypatch.setattr(fleetview, "rows", broken)
        again, _ = host.snapshot()  # the kept rows
        again[0]["model"] = "changed"
        last, _ = host.snapshot()
        assert (last[0]["session"], last[0]["model"]) == ("magent-alpha", "Opus")


class TestUploadCap:
    def test_the_upload_cap_comes_from_meta(self, host):
        assert host.upload_max_bytes() == 4321

    def test_a_cap_it_cannot_read_is_zero_and_read_again(self, ctx, config):
        calls: list[int] = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("DECOY not yet")
            return ctx

        host = ApiLinkHost(flaky, str(config))
        assert host.upload_max_bytes() == 0
        assert host.upload_max_bytes() == 4321

    def test_callers_at_once_share_one_meta_read(self, host, monkeypatch):
        metas: list[int] = []
        real = api.handle

        def slow(req, ctx, caller=None):
            if req.path.endswith("/meta"):
                metas.append(1)
                time.sleep(0.2)
            return real(req, ctx, caller)

        monkeypatch.setattr(api, "handle", slow)
        assert _at_once(host.upload_max_bytes, 6) == [4321] * 6
        assert len(metas) == 1


def test_stage_upload_saves_and_pastes_a_staged_file(host, tmp_path, seen):
    staged = linkfiles.INCOMING_DIR / "up_DECOY_2" / "photo.png"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"PNG")
    envelope = host.stage_upload("dev_DECOY_phone", "magent-alpha", staged, True)
    assert envelope["ok"] is True
    assert envelope["data"]["paste"] == "injected"
    assert Path(envelope["data"]["path"]).read_bytes() == b"PNG"
    [(req, caller, raw)] = seen
    assert req.header("content-type") == link_staged.CONTENT_TYPE
    assert json.loads(raw) == {
        "staged_path": str(staged),
        "name": "photo.png",
        "session": "magent-alpha",
        "inject": True,
    }
    assert caller == api.Caller("linked")


class TestFeed:
    def test_a_quiet_bus_gives_an_empty_batch(self, host):
        assert host.subscribe().next_batch(0.05) == []

    def test_the_wait_is_bounded_by_the_timeout(self, host):
        feed = host.subscribe()
        feed.next_batch(0.0)
        started = time.monotonic()
        assert feed.next_batch(0.3) == []
        assert 0.2 <= time.monotonic() - started < 2.0

    def test_events_after_the_first_read_arrive_in_order(self, host, bus):
        feed = host.subscribe()
        feed.next_batch(0.0)
        bus.publish("session.added", {"session": "magent-alpha"})
        bus.publish("session.removed", {"session": "magent-alpha"})
        batch = feed.next_batch(0.5)
        assert [e["type"] for e in batch] == ["session.added", "session.removed"]
        assert batch[0]["id"] == "deadbeef:1"

    def test_a_reset_becomes_one_reset_event(self, host, bus):
        feed = host.subscribe()
        feed.next_batch(0.0)
        feed._next = "cafebabe:7"  # a serve that restarted: another epoch
        batch = feed.next_batch(0.0)
        assert [e["type"] for e in batch] == ["reset"]
        assert batch[0]["data"] == {"reason": "epoch"}
        assert batch[0]["id"] == "deadbeef:0"

    def test_a_failed_poll_is_an_empty_batch_within_the_timeout(self, host):
        feed = host.subscribe()
        host._context = lambda: None  # api.handle crashes inside: a 500 envelope
        started = time.monotonic()
        assert feed.next_batch(0.2) == []
        assert time.monotonic() - started < 1.0

    def test_close_is_idempotent_and_ends_the_feed(self, host, bus):
        feed = host.subscribe()
        feed.next_batch(0.0)
        feed.close()
        feed.close()
        bus.publish("session.added", {"session": "magent-alpha"})
        assert feed.next_batch(0.0) == []

    def test_only_well_formed_events_are_handed_on(self, host, monkeypatch):
        # Dicts whose id is 1-64 UTF-16 units and whose type is a string: what
        # the forwarder seals and the relay takes.
        answer = {
            "events": [
                {"id": "deadbeef:1", "type": "upload", "ts": 1.0, "data": {}},
                {"id": "", "type": "upload", "ts": 1.0, "data": {}},
                {"id": "x" * 65, "type": "upload", "ts": 1.0, "data": {}},
                {"id": "deadbeef:2", "type": 7, "ts": 1.0, "data": {}},
                "not a dict",
            ],
            "next": "deadbeef:2",
        }
        monkeypatch.setattr(
            host,
            "call",
            lambda *_a, **_k: (200, {"ok": True, "data": answer}),
        )
        assert [e["id"] for e in host.subscribe().next_batch(0.0)] == ["deadbeef:1"]
