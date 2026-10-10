"""upload_server's /api/v1 branch and the origin guard, over a real socket."""

from __future__ import annotations

import json
import logging
import re
import socket
import sys
import threading
import time
from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path

import pytest

import magent.upload_server as mod
from magent import control, events, fleetview, tailnet
from magent.upload_server import UploadHandler


@pytest.fixture
def server(tmp_path, monkeypatch):
    cfg = tmp_path / "magent.config.json"
    cfg.write_text(
        json.dumps({"version": 4, "projects": [{"path": "work/api"}]}), encoding="utf-8"
    )
    monkeypatch.setattr(mod, "_UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr("magent.psmux.find_psmux", lambda: None)
    monkeypatch.setattr(UploadHandler, "config_path", str(cfg))
    monkeypatch.setattr(
        UploadHandler,
        "cached_sessions",
        [{"name": "api", "session": "api", "path": "work/api"}],
    )
    monkeypatch.setattr(UploadHandler, "sessions_ts", time.time() + 9999)
    monkeypatch.setattr(UploadHandler, "bus", events.EventBus("deadbeef"))
    monkeypatch.setattr(UploadHandler, "status_provider", None)
    httpd = HTTPServer(("127.0.0.1", 0), UploadHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None, headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    sent = dict(headers or {})
    raw = None
    if body is not None:
        raw = json.dumps(body).encode()
        sent.setdefault("Content-Type", "application/json")
    if method in {"POST", "PATCH", "DELETE"}:
        sent.setdefault("X-Magent-Client", "pytest")
    conn.request(method, path, body=raw, headers=sent)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp, json.loads(data) if data else None


def read_reply(s: socket.socket) -> bytes:
    """Read one response whole: headers, then Content-Length bytes of body."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(65536)
        if not chunk:
            return buf
        buf += chunk
    head, _, body = buf.partition(b"\r\n\r\n")
    match = re.search(rb"Content-Length: (\d+)", head, re.IGNORECASE)
    assert match is not None, head
    length = int(match[1])
    while len(body) < length:
        chunk = s.recv(65536)
        if not chunk:
            break
        body += chunk
    return head + b"\r\n\r\n" + body


class TestTheBranch:
    def test_meta_over_the_wire(self, server):
        resp, body = call(server, "GET", "/api/v1/meta")
        assert resp.status == 200
        assert resp.getheader("Content-Type") == "application/json"
        assert resp.getheader("Cache-Control") == "no-store"
        assert resp.getheader("Access-Control-Allow-Origin") is None
        assert body["data"]["epoch"] == "deadbeef"

    def test_health_is_one_shape_on_both_routes(self, server):
        _resp, legacy = call(server, "GET", "/health")
        _resp, v1 = call(server, "GET", "/api/v1/health")
        assert legacy.pop("ok") is True
        assert set(legacy) == set(v1["data"])

    def test_a_write_reaches_control(self, server, monkeypatch):
        monkeypatch.setattr(
            control,
            "interrupt",
            lambda _cfg, session: control.InterruptResult(session, "Escape", "idle"),
        )
        resp, body = call(server, "POST", "/api/v1/sessions/api/interrupt", {})
        assert resp.status == 200
        assert body["data"] == {
            "session": "api",
            "key": "Escape",
            "pane_state_after": "idle",
        }

    def test_patch_and_delete_are_routed(self, server):
        resp, body = call(server, "PATCH", "/api/v1/projects/api", {"enabled": False})
        assert (resp.status, body["data"]["enabled"]) == (200, False)
        resp, body = call(server, "DELETE", "/api/v1/projects/api")
        assert (resp.status, body["data"]["removed"]) == (200, ["api"])

    def test_options_is_405_without_cors(self, server):
        resp, body = call(server, "OPTIONS", "/api/v1/sessions")
        assert resp.status == 405
        assert body["error"]["code"] == "method_not_allowed"
        assert resp.getheader("Access-Control-Allow-Methods") is None

    def test_patch_outside_the_api_is_404(self, server):
        resp, body = call(server, "PATCH", "/nope", {})
        assert (resp.status, body) == (404, {"ok": False, "error": "Not found"})

    def test_an_oversized_upload_is_an_envelope_not_a_reset(self, server):
        with socket.create_connection(("127.0.0.1", server), timeout=5) as s:
            s.sendall(
                f"POST /api/v1/uploads HTTP/1.1\r\nHost: 127.0.0.1:{server}\r\n"
                "X-Magent-Client: pytest\r\n"
                "Content-Type: multipart/form-data; boundary=B\r\n"
                f"Content-Length: {mod._request_limit() + 1}\r\n\r\n".encode()
                + b"x" * 4096
            )
            reply = read_reply(s)
        assert reply.split(b" ", 2)[1] == b"413"
        assert b'"payload_too_large"' in reply


class TestAnIncompleteUpload:
    BODY = (
        b"--B\r\n"
        b'Content-Disposition: form-data; name="session"\r\n\r\napi\r\n'
        b"--B\r\n"
        b'Content-Disposition: form-data; name="file"; filename="v.bin"\r\n\r\n'
        + b"A" * 8192
        + b"\r\n--B--\r\n"
    )
    PARTIAL = BODY[:3000]

    def head(self, port: int) -> bytes:
        return (
            f"POST /api/v1/uploads HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            "X-Magent-Client: pytest\r\n"
            "Content-Type: multipart/form-data; boundary=B\r\n"
            f"Content-Length: {len(self.BODY)}\r\n\r\n"
        ).encode()

    def wait_for(self, caplog, text: str) -> None:
        deadline = time.time() + 5
        while time.time() < deadline and text not in caplog.text:
            time.sleep(0.02)
        assert text in caplog.text

    def quiet(self, caplog) -> None:
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert "Traceback" not in caplog.text

    def test_a_hangup_mid_body_is_one_warning_and_a_closed_400(
        self, server, tmp_path, caplog
    ):
        with caplog.at_level(logging.DEBUG, logger="magent.upload"):
            with socket.create_connection(("127.0.0.1", server), timeout=5) as s:
                s.sendall(self.head(server) + self.PARTIAL)
                s.shutdown(socket.SHUT_WR)
                reply = read_reply(s)
                closed = s.recv(65536) == b""
            self.wait_for(caplog, "went away mid-body")
            resp, _ = call(server, "GET", "/api/v1/meta")
            assert resp.status == 200
        assert reply.split(b" ", 2)[1] == b"400"
        assert closed
        assert b'"invalid_request"' in reply
        gone = [r for r in caplog.records if "went away" in r.getMessage()]
        assert len(gone) == 1
        assert gone[0].levelno == logging.WARNING
        assert gone[0].exc_info is None
        assert f"after {len(self.PARTIAL)} of {len(self.BODY)} bytes" in (
            gone[0].getMessage()
        )
        self.quiet(caplog)
        assert not (tmp_path / "uploads").exists()

    def test_a_stalled_client_that_stays_gets_one_400_then_eof(
        self, server, monkeypatch, caplog
    ):
        """The client does NOT half-close: it sends part of the body and
        stays connected. The server's read times out, answers one 400 and
        CLOSES.

        The handler speaks HTTP/1.0, which closes after every reply on its
        own, so the test switches it to HTTP/1.1 (keep-alive) to make the
        explicit ``close_connection`` the only thing that closes. Without
        it, the timed-out socket stays open, the server loops to read the
        next request line, and that ``readline`` raises ("cannot read from
        timed out object") inside the server: ``handle_error`` prints a
        traceback. The close prevents it; no ``handle_error`` call is the
        proof."""
        errors: list[str] = []

        def record(_self, _request, _client_address) -> None:
            errors.append(repr(sys.exc_info()[1]))

        monkeypatch.setattr(UploadHandler, "timeout", 0.5)
        monkeypatch.setattr(UploadHandler, "protocol_version", "HTTP/1.1")
        monkeypatch.setattr(HTTPServer, "handle_error", record)
        with caplog.at_level(logging.DEBUG, logger="magent.upload"):
            with socket.create_connection(("127.0.0.1", server), timeout=5) as s:
                s.sendall(self.head(server) + self.PARTIAL)
                reply = read_reply(s)
                extra = b""
                ended = False
                try:
                    s.sendall(self.BODY[len(self.PARTIAL) :])
                    while True:
                        chunk = s.recv(65536)
                        if not chunk:
                            ended = True
                            break
                        extra += chunk
                except (ConnectionResetError, ConnectionAbortedError):
                    ended = True  # the server closed with our tail unread
            self.wait_for(caplog, "went away mid-body")
            time.sleep(0.3)  # settle: a server-side error would land by now
        assert errors == []
        assert reply.split(b" ", 2)[1] == b"400"
        assert b'"invalid_request"' in reply
        assert b'"incomplete"' in reply
        assert ended
        assert b"HTTP/1." not in extra, extra
        assert reply.count(b"HTTP/1.") == 1
        self.quiet(caplog)

    def test_a_client_gone_before_the_answer_is_not_a_crash(
        self, server, monkeypatch, caplog
    ):
        def aborted(_self, _answer):
            raise ConnectionAbortedError(10053, "aborted by the software")

        real_write = UploadHandler._write_answer
        monkeypatch.setattr(UploadHandler, "_write_answer", aborted)
        with caplog.at_level(logging.DEBUG, logger="magent.upload"):
            with socket.create_connection(("127.0.0.1", server), timeout=5) as s:
                s.sendall(self.head(server) + self.PARTIAL)
                s.shutdown(socket.SHUT_WR)
                assert s.recv(65536) == b""
            self.wait_for(caplog, "went away mid-body")
            monkeypatch.setattr(UploadHandler, "_write_answer", real_write)
            resp, _ = call(server, "GET", "/api/v1/meta")
            assert resp.status == 200
        assert "before the reply" not in caplog.text
        assert "crashed" not in caplog.text
        self.quiet(caplog)


class TestTheGuardOnLegacyRoutes:
    @pytest.mark.parametrize(
        "path", ["/", "/health", "/api/sessions", "/focus?project=api"]
    )
    def test_a_foreign_host_is_refused(self, server, path):
        resp, body = call(
            server,
            "GET",
            path,
            headers={"Host": "evil.example", "User-Agent": "Mozilla/5.0"},
        )
        assert resp.status == 403
        assert body["reason"] == "bad_host"

    def test_a_cross_site_flash_never_reaches_psmux(self, server, monkeypatch):
        flashed = []
        monkeypatch.setattr(mod, "_flash", lambda *a, **k: flashed.append(a))
        resp, body = call(
            server,
            "GET",
            "/api/flash?project=api&msg=hi",
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert (resp.status, body["reason"]) == (403, "cross_site")
        assert flashed == []

    def test_a_same_origin_flash_still_works(self, server, monkeypatch):
        flashed = []
        monkeypatch.setattr(mod, "_flash", lambda *a, **k: flashed.append(a))
        resp, _body = call(
            server,
            "GET",
            "/api/flash?project=api&msg=hi",
            headers={
                "Sec-Fetch-Site": "same-origin",
                "Origin": f"http://127.0.0.1:{server}",
            },
        )
        assert resp.status == 200
        assert len(flashed) == 1

    def test_a_cross_origin_upload_is_refused_and_drained(self, server):
        with socket.create_connection(("127.0.0.1", server), timeout=5) as s:
            s.sendall(
                f"POST /upload HTTP/1.1\r\nHost: 127.0.0.1:{server}\r\n"
                "Origin: http://evil.example\r\n"
                "Content-Type: multipart/form-data; boundary=B\r\n"
                "Content-Length: 10\r\n\r\n0123456789".encode()
            )
            reply = read_reply(s)
        assert b" 403 " in reply.split(b"\r\n", 1)[0]
        assert b'"bad_origin"' in reply


class TestTheEventStream:
    def test_hello_live_event_and_a_freed_subscription(self, server, monkeypatch):
        monkeypatch.setattr(fleetview, "rows", lambda *_a, **_kw: [])
        bus = UploadHandler.bus
        with socket.create_connection(("127.0.0.1", server), timeout=5) as s:
            s.sendall(
                f"GET /api/v1/events HTTP/1.1\r\nHost: 127.0.0.1:{server}\r\n\r\n".encode()
            )
            buf = b""
            while b"event: hello" not in buf:
                buf += s.recv(4096)
            assert b"Content-Type: text/event-stream" in buf
            assert b"\r\nConnection: close\r\n" in buf
            assert bus.subscribers == 1
            bus.publish("session.added", {"session": "api"})
            while b"event: session.added" not in buf:
                buf += s.recv(4096)
            assert b"id: deadbeef:1" in buf
        deadline = time.time() + 5
        while bus.subscribers and time.time() < deadline:
            bus.publish(
                "session.removed", {"session": "api"}
            )  # a write notices the hang-up
            time.sleep(0.05)
        assert bus.subscribers == 0


class TestRunServerWiring:
    def _run(self, monkeypatch, tmp_path, *, enabled):
        started: list[tuple[object, str | None]] = []
        real_thread = threading.Thread

        class _Recording(real_thread):
            def __init__(self, *args, target=None, **kwargs) -> None:
                super().__init__(*args, target=target, **kwargs)
                self.recorded = (target, kwargs.get("name"))

            def start(self) -> None:
                started.append(self.recorded)

        class _FakeServer:
            def __init__(self, addr, _handler) -> None:
                self.server_address = addr

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def shutdown(self) -> None:
                return None

            def server_close(self) -> None:
                return None

        monkeypatch.setattr(mod.threading, "Thread", _Recording)
        monkeypatch.setattr(mod, "_bind_addresses", lambda _h: ["127.0.0.1"])
        monkeypatch.setattr(mod, "_NoFqdnHTTPServer", _FakeServer)
        monkeypatch.setattr(
            mod, "_pid_path", lambda port: tmp_path / f"upload-{port}.pid"
        )
        monkeypatch.setattr(mod.events, "events_enabled", lambda: enabled)
        for attr in ("bus", "allowed_hosts", "status_provider", "config_path", "fleet"):
            monkeypatch.setattr(UploadHandler, attr, getattr(UploadHandler, attr))

        def provider():
            return {"ok": True}

        with pytest.raises(KeyboardInterrupt):
            mod.run_server(port=0, host="myhost", status_provider=provider)
        assert UploadHandler.status_provider is provider
        assert "myhost" in UploadHandler.allowed_hosts
        # serve's one row source, never the poller's engine.
        assert isinstance(UploadHandler.fleet, mod.api.FleetSource)
        assert UploadHandler.fleet.config_path is None
        names = [name for _target, name in started]
        # The tailnet names are learned off-thread whether or not events are
        # on: serve never waits on the tailscale subprocess.
        assert "magent-hosts" in names
        return names

    def test_the_poller_thread_starts_when_events_are_on(self, monkeypatch, tmp_path):
        assert "magent-events" in self._run(monkeypatch, tmp_path, enabled=True)

    def test_magent_events_0_starts_no_poller(self, monkeypatch, tmp_path):
        assert "magent-events" not in self._run(monkeypatch, tmp_path, enabled=False)

    @pytest.mark.learn_hosts  # the autouse stub is off: tailnet is patched here
    def test_learn_hosts_adds_the_tailnet_names(self, monkeypatch):
        monkeypatch.setattr(UploadHandler, "allowed_hosts", frozenset())
        monkeypatch.setattr(tailnet, "ip4", lambda: "100.64.0.7")
        dns = "box.tailDECOY.ts.net"  # a DECOY canary, see test_api.MAGICDNS
        monkeypatch.setattr(tailnet, "magicdns_host", lambda: dns)
        monkeypatch.setattr(mod.api.socket, "gethostname", lambda: "DESK")
        mod._learn_hosts(None)
        assert UploadHandler.allowed_hosts == mod.api.LOOPBACK_NAMES | {
            "100.64.0.7",
            dns.lower(),
            "box",  # the MagicDNS short label: what `magent attach box` types
            "desk",
        }


def test_the_schema_file_is_where_the_app_reads_it():
    assert (
        Path(__file__).resolve().parents[2] / "docs" / "api" / "v1.schema.json"
    ).exists()
