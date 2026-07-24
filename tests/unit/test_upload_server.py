import io
import json
import threading
import time
from http.client import HTTPConnection
from pathlib import Path
from typing import ClassVar

import pytest

from magent.psmux import config_sessions
from magent.upload_server import (
    UploadHandler,
    _build_html,
    _parse_multipart,
)


class TestBuildHtml:
    def test_renders_sessions(self):
        sessions = [
            {"name": "marka", "path": "INTERNAL/marka"},
            {"name": "upup", "path": "INTERNAL/upup"},
        ]
        html = _build_html(sessions)
        assert "marka" in html
        assert "upup" in html
        assert "pill" in html

    def test_no_sessions_shows_message(self):
        html = _build_html([])
        assert "no active sessions" in html

    def test_pill_wire_value_is_session_id(self):
        # P3-01: the pill's data-name (the value posted back as `project`) is
        # the psmux socket id, not the display name.
        html = _build_html([{"name": "my.api", "session": "my-api", "path": "x"}])
        assert 'data-name="my-api"' in html

    def test_clipboard_paste_ui_ships_on_the_page(self):
        # Ctrl+V flow contract: the staged-image confirm panel (preview img,
        # destination-project line, progress bar, explicit Send/Cancel) and the
        # window paste listener must all be present in the served page. The
        # real-browser behavioural proof lives in the `browser` e2e tier.
        html = _build_html([{"name": "p", "path": "x"}])
        for anchor in (
            'id="paste-box"',
            'id="paste-img"',
            'id="paste-dest"',
            'id="paste-bar"',
            'id="paste-send"',
            'id="paste-cancel"',
            "addEventListener('paste'",
            "XMLHttpRequest",
        ):
            assert anchor in html, f"paste-upload UI anchor missing: {anchor}"


class TestConfigSessions:
    def test_carries_display_name_and_sanitized_session(self, tmp_path):
        # P3-01: _config_sessions splits the display name from the psmux id.
        cfg = tmp_path / "magent.config.json"
        cfg.write_text(
            json.dumps(
                {
                    "projects": [
                        {
                            "path": str(tmp_path / "svc"),
                            "title": "my.api",
                            "tool": "claude",
                        }
                    ]
                }
            )
        )
        out = config_sessions(str(cfg))
        assert out[0]["name"] == "my.api"
        assert out[0]["session"] == "my-api"

    def test_html_escapes_names(self):
        sessions = [{"name": "<b>bad</b>", "path": "x&y"}]
        html = _build_html(sessions)
        assert "<b>bad</b>" not in html
        assert "&lt;b&gt;bad&lt;/b&gt;" in html


class TestParseMultipart:
    def _make_handler(self, body: bytes, boundary: str):
        class FakeHandler:
            headers: ClassVar[dict[str, str]] = {
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            }
            rfile = io.BytesIO(body)

        return FakeHandler()

    def test_parses_field_and_file(self):
        body = (
            b"------TestBoundary\r\n"
            b'Content-Disposition: form-data; name="project"\r\n'
            b"\r\n"
            b"marka\r\n"
            b"------TestBoundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="test.png"\r\n'
            b"Content-Type: image/png\r\n"
            b"\r\n"
            b"PNGDATA\r\n"
            b"------TestBoundary--\r\n"
        )

        handler = self._make_handler(body, "----TestBoundary")
        fields, files = _parse_multipart(handler)

        assert fields["project"] == "marka"
        assert "file" in files
        assert files["file"][0] == "test.png"
        assert files["file"][1] == b"PNGDATA"

    def test_parse_multipart_missing_boundary_returns_empty(self):
        # F-D3-006: Content-Type with no boundary= is treated as "no body".
        class FakeHandler:
            headers: ClassVar[dict[str, str]] = {
                "Content-Type": "multipart/form-data",
                "Content-Length": "0",
            }
            rfile = io.BytesIO(b"")

        assert _parse_multipart(FakeHandler()) == ({}, {})

    def test_bad_content_length_no_crash(self):
        # F-D3-002: a non-numeric Content-Length used to raise an uncaught
        # ValueError from int() inside the parser; it's now treated as "no
        # body" instead of crashing the request-handling thread.
        class FakeHandler:
            headers: ClassVar[dict[str, str]] = {
                "Content-Type": "multipart/form-data; boundary=X",
                "Content-Length": "abc",
            }
            rfile = io.BytesIO(b"")

        assert _parse_multipart(FakeHandler()) == ({}, {})


class _DrainConn:
    """Socket stand-in exposing only the timeout knobs the drain touches."""

    def __init__(self):
        self._timeout = None

    def gettimeout(self):
        return self._timeout

    def settimeout(self, value):
        self._timeout = value


class _DrainReader:
    """rfile stand-in: yields buffered bytes, then (opt-in) signals "nothing
    more pending" the way a timed-out blocking socket read does -- by raising."""

    def __init__(self, data: bytes, *, raise_when_empty: bool = False):
        self._buf = io.BytesIO(data)
        self.consumed = 0
        self._raise_when_empty = raise_when_empty

    def read(self, n: int) -> bytes:
        chunk = self._buf.read(n)
        if not chunk and self._raise_when_empty:
            raise TimeoutError  # socket.timeout is an OSError subclass
        self.consumed += len(chunk)
        return chunk


class _DrainHandler:
    """Carries only the attributes _drain_request_body reads and writes."""

    def __init__(self, reader: _DrainReader, content_length: str | None):
        self.close_connection = False
        self.rfile = reader
        self.connection = _DrainConn()
        self.headers = (
            {} if content_length is None else {"Content-Length": content_length}
        )


class TestDrainRequestBody:
    """P4-02: the bounded body-drain that lets an early 4xx land cleanly rather
    than as a Windows TCP RST. It must never read past the cap, and must tolerate
    an absent/garbage Content-Length without blocking or propagating."""

    def _drain(self, reader: _DrainReader, content_length: str | None) -> _DrainHandler:
        handler = _DrainHandler(reader, content_length)
        # Call unbound: _drain_request_body only touches the stubbed attributes.
        UploadHandler._drain_request_body(handler)
        return handler

    def test_reads_at_most_the_cap(self, monkeypatch):
        # Feed a source far larger than the cap: consumption must stop at the cap.
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "_DRAIN_CAP_BYTES", 100)
        reader = _DrainReader(b"x" * 500)
        handler = self._drain(reader, str(500))

        assert reader.consumed == 100  # bounded -- never the full 500
        assert handler.close_connection is True

    def test_tolerates_garbage_content_length(self, monkeypatch):
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "_DRAIN_CAP_BYTES", 100)
        reader = _DrainReader(b"y" * 20)
        handler = self._drain(reader, "not-a-number")

        assert reader.consumed == 20  # drained all available, then hit EOF
        assert handler.close_connection is True

    def test_tolerates_absent_content_length(self, monkeypatch):
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "_DRAIN_CAP_BYTES", 100)
        reader = _DrainReader(b"z" * 15)
        handler = self._drain(reader, None)  # no Content-Length header at all

        assert reader.consumed == 15
        assert handler.close_connection is True

    def test_stops_on_read_timeout(self, monkeypatch):
        # A blocking socket that times out mid-drain (client declared more than
        # it sent and holds the connection open) raises OSError -- swallowed.
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "_DRAIN_CAP_BYTES", 100)
        reader = _DrainReader(b"w" * 5, raise_when_empty=True)
        handler = self._drain(reader, "garbage")

        assert reader.consumed == 5
        assert handler.close_connection is True

    def test_zero_length_is_noop(self, monkeypatch):
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "_DRAIN_CAP_BYTES", 100)
        reader = _DrainReader(b"unused", raise_when_empty=True)
        handler = self._drain(reader, "0")

        assert reader.consumed == 0  # nothing declared -> nothing read
        assert handler.close_connection is True
        assert handler.connection.gettimeout() is None  # socket never touched


class TestUploadServerIntegration:
    @pytest.fixture(autouse=True)
    def _server(self, tmp_path, monkeypatch):
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "_UPLOAD_DIR", tmp_path / "uploads")
        import magent.psmux as psmux_mod

        monkeypatch.setattr(psmux_mod, "find_psmux", lambda: None)
        self.upload_dir = tmp_path / "uploads"

        UploadHandler.config_path = None
        UploadHandler.cached_sessions = [
            {"name": "marka", "session": "marka", "path": "INTERNAL/marka"},
            {"name": "upup", "session": "upup", "path": "INTERNAL/upup"},
        ]
        UploadHandler.sessions_ts = time.time() + 9999

        from http.server import HTTPServer

        self.server = HTTPServer(("127.0.0.1", 0), UploadHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        yield
        self.server.shutdown()

    def _conn(self):
        return HTTPConnection("127.0.0.1", self.port, timeout=5)

    def test_get_index(self):
        conn = self._conn()
        conn.request("GET", "/")
        resp = conn.getresponse()
        assert resp.status == 200
        body = resp.read().decode()
        assert "marka" in body
        assert "upup" in body

    def test_get_api_sessions(self):
        conn = self._conn()
        conn.request("GET", "/api/sessions")
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read())
        # P3-04/P3-18: ok-envelope + the LIST lives under `sessions`.
        assert data["ok"] is True
        assert len(data["sessions"]) == 2
        # P3-01: each entry carries the display `name` and psmux `session`.
        assert data["sessions"][0]["name"] == "marka"
        assert data["sessions"][0]["session"] == "marka"

    def test_get_on_post_only_path_is_405(self):
        # P3-16: wrong method on a real route -> 405 (not 404).
        conn = self._conn()
        conn.request("GET", "/upload")
        resp = conn.getresponse()
        assert resp.status == 405
        data = json.loads(resp.read())
        assert data["ok"] is False
        assert data["error"]

    def test_post_on_get_only_path_is_405(self):
        conn = self._conn()
        conn.request("POST", "/", body=b"", headers={"Content-Length": "0"})
        resp = conn.getresponse()
        assert resp.status == 405
        assert json.loads(resp.read())["ok"] is False

    def test_unknown_path_is_404_json_envelope(self):
        # P3-16: a genuinely unknown path stays 404, as the JSON error envelope.
        conn = self._conn()
        conn.request("GET", "/does-not-exist")
        resp = conn.getresponse()
        assert resp.status == 404
        data = json.loads(resp.read())
        assert data["ok"] is False
        assert data["error"]

    def test_post_unknown_path_is_404(self):
        conn = self._conn()
        conn.request("POST", "/nope", body=b"", headers={"Content-Length": "0"})
        resp = conn.getresponse()
        assert resp.status == 404
        assert json.loads(resp.read())["ok"] is False

    def test_upload_saves_file(self):
        body = (
            b"------WebKitFormBoundary\r\n"
            b'Content-Disposition: form-data; name="project"\r\n'
            b"\r\n"
            b"marka\r\n"
            b"------WebKitFormBoundary\r\n"
            b'Content-Disposition: form-data; name="inject"\r\n'
            b"\r\n"
            b"0\r\n"
            b"------WebKitFormBoundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="screenshot.png"\r\n'
            b"Content-Type: image/png\r\n"
            b"\r\n"
            b"FAKEPNG\r\n"
            b"------WebKitFormBoundary--\r\n"
        )

        conn = self._conn()
        conn.request(
            "POST",
            "/upload",
            body=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=----WebKitFormBoundary",
                "Content-Length": str(len(body)),
            },
        )
        resp = conn.getresponse()
        data = json.loads(resp.read())

        assert data["ok"] is True
        assert "screenshot.png" in data["path"]
        assert not data["injected"]
        saved = Path(data["path"])
        assert saved.exists()
        assert saved.read_bytes() == b"FAKEPNG"

    def _wait_log(self, caplog, substr: str, timeout: float = 3.0) -> bool:
        # The outcome INFO logs in the do_POST `finally` block, which runs on
        # the server thread after the HTTP response is already on the wire --
        # same race as the status-line flash (see TestInSessionFeedback), so
        # poll rather than assert immediately.
        deadline = time.time() + timeout
        while time.time() < deadline:
            if substr in caplog.text:
                return True
            time.sleep(0.02)
        return False

    def test_upload_logs_outcome_without_filename(self, caplog):
        # F-hygiene: the outcome log must carry the project + byte-count +
        # injected flag, but never the original filename (personal data).
        body = (
            b"------WebKitFormBoundary\r\n"
            b'Content-Disposition: form-data; name="project"\r\n'
            b"\r\n"
            b"marka\r\n"
            b"------WebKitFormBoundary\r\n"
            b'Content-Disposition: form-data; name="inject"\r\n'
            b"\r\n"
            b"0\r\n"
            b"------WebKitFormBoundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="my_diagnosis.png"\r\n'
            b"Content-Type: image/png\r\n"
            b"\r\n"
            b"FAKEPNG\r\n"
            b"------WebKitFormBoundary--\r\n"
        )

        with caplog.at_level("INFO", logger="magent.upload"):
            conn = self._conn()
            conn.request(
                "POST",
                "/upload",
                body=body,
                headers={
                    "Content-Type": "multipart/form-data; boundary=----WebKitFormBoundary",
                    "Content-Length": str(len(body)),
                },
            )
            resp = conn.getresponse()
            data = json.loads(resp.read())
            assert data["ok"] is True

            assert self._wait_log(caplog, "upload project=marka")
        assert "bytes=7" in caplog.text  # len(b"FAKEPNG")
        assert "injected=False" in caplog.text
        assert "my_diagnosis.png" not in caplog.text
        assert "my_diagnosis" not in caplog.text

    def test_upload_missing_project(self):
        body = (
            b"------Boundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="x.png"\r\n'
            b"\r\n"
            b"data\r\n"
            b"------Boundary--\r\n"
        )

        conn = self._conn()
        conn.request(
            "POST",
            "/upload",
            body=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=----Boundary",
                "Content-Length": str(len(body)),
            },
        )
        resp = conn.getresponse()
        data = json.loads(resp.read())
        assert data["ok"] is False

    def test_upload_rejects_unknown_project(self):
        body = (
            b"------Boundary\r\n"
            b'Content-Disposition: form-data; name="project"\r\n'
            b"\r\n"
            b"evil-project\r\n"
            b"------Boundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="x.png"\r\n'
            b"\r\n"
            b"data\r\n"
            b"------Boundary--\r\n"
        )

        conn = self._conn()
        conn.request(
            "POST",
            "/upload",
            body=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=----Boundary",
                "Content-Length": str(len(body)),
            },
        )
        resp = conn.getresponse()
        data = json.loads(resp.read())
        assert data["ok"] is False
        assert "Unknown project" in data["error"]

    def test_upload_strips_path_traversal(self):
        body = (
            b"------Boundary\r\n"
            b'Content-Disposition: form-data; name="project"\r\n'
            b"\r\n"
            b"marka\r\n"
            b"------Boundary\r\n"
            b'Content-Disposition: form-data; name="inject"\r\n'
            b"\r\n"
            b"0\r\n"
            b"------Boundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="../../etc/passwd"\r\n'
            b"\r\n"
            b"malicious\r\n"
            b"------Boundary--\r\n"
        )

        conn = self._conn()
        conn.request(
            "POST",
            "/upload",
            body=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=----Boundary",
                "Content-Length": str(len(body)),
            },
        )
        resp = conn.getresponse()
        data = json.loads(resp.read())
        assert data["ok"] is True
        saved = Path(data["path"])
        assert saved.parent == self.upload_dir
        assert ".." not in saved.name

    def test_404(self):
        conn = self._conn()
        conn.request("GET", "/nonexistent")
        resp = conn.getresponse()
        assert resp.status == 404

    def test_rejects_oversized_body(self, monkeypatch):
        # F-D3-002: a body over the cap is rejected before it's fully read
        # into memory, so a malicious/oversized upload can't exhaust RAM.
        # P4-02: the reject drains the pending body first, so the client
        # deterministically reads the 413 + JSON envelope instead of a Windows
        # TCP RST ("connection reset") -- no retries, no flake.
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "MAX_UPLOAD_BYTES", 10)

        body = (
            b"------Boundary\r\n"
            b'Content-Disposition: form-data; name="project"\r\n'
            b"\r\n"
            b"marka\r\n"
            b"------Boundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="x.png"\r\n'
            b"\r\n"
            b"well past ten bytes of file data\r\n"
            b"------Boundary--\r\n"
        )
        assert len(body) > 10  # sanity: genuinely exceeds the lowered cap

        conn = self._conn()
        conn.request(
            "POST",
            "/upload",
            body=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=----Boundary",
                "Content-Length": str(len(body)),
            },
        )
        resp = conn.getresponse()
        assert resp.status == 413
        data = json.loads(resp.read())  # body arrives intact, not a reset
        assert data["ok"] is False
        assert "large" in data["error"].lower()

    def test_rejects_bad_content_length(self):
        # Sibling of the oversized-body guard: a non-numeric Content-Length
        # reaching do_POST used to propagate an uncaught ValueError (dropped
        # connection); it now gets a clean 400 before any body is read.
        # P4-02: the reject drains the (garbage-length) body first, so the
        # client deterministically reads the 400 + JSON envelope rather than a
        # Windows TCP RST -- no retries, no flake.
        conn = self._conn()
        conn.request(
            "POST",
            "/upload",
            body=b"irrelevant",
            headers={
                "Content-Type": "multipart/form-data; boundary=----Boundary",
                "Content-Length": "abc",
            },
        )
        resp = conn.getresponse()
        assert resp.status == 400
        data = json.loads(resp.read())  # body arrives intact, not a reset
        assert data["ok"] is False
        assert "Content-Length" in data["error"]

    def test_get_handler_crash_returns_500_and_logs(self, monkeypatch, caplog):
        # P2-03: an unexpected error inside a GET handler must become a clean
        # 500 + an ERROR log record (-> logfile + Sentry), never a dropped
        # connection whose traceback vanishes into socketserver stderr.
        import magent.upload_server as mod

        def boom(_sessions):
            raise RuntimeError("boom in GET")

        monkeypatch.setattr(mod, "_build_html", boom)

        with caplog.at_level("ERROR", logger="magent.upload"):
            conn = self._conn()
            conn.request("GET", "/")
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 500
            assert self._wait_log(caplog, "GET handler crashed")

        # the server survived the crash: a normal request still succeeds
        conn2 = self._conn()
        conn2.request("GET", "/api/sessions")
        assert conn2.getresponse().status == 200

    def test_post_handler_crash_returns_500_and_logs(self, monkeypatch, caplog):
        # P2-03: same guarantee for the POST path -- the finding's motivating
        # case (an unexpected fault while handling an upload).
        import magent.upload_server as mod

        def boom(_handler):
            raise RuntimeError("boom in POST")

        monkeypatch.setattr(mod, "_parse_multipart", boom)

        with caplog.at_level("ERROR", logger="magent.upload"):
            conn = self._conn()
            conn.request(
                "POST",
                "/upload",
                body=b"x",
                headers={
                    "Content-Type": "multipart/form-data; boundary=----B",
                    "Content-Length": "1",
                },
            )
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 500
            assert self._wait_log(caplog, "POST handler crashed")

        conn2 = self._conn()
        conn2.request("GET", "/api/sessions")
        assert conn2.getresponse().status == 200


class TestHealth:
    """GET /health proves the handler thread is serving -- a session COUNT,
    never names (hygiene) -- without spawning any psmux subprocess."""

    @pytest.fixture(autouse=True)
    def _server(self, tmp_path, monkeypatch):
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "_UPLOAD_DIR", tmp_path / "uploads")
        import magent.psmux as psmux_mod

        monkeypatch.setattr(psmux_mod, "find_psmux", lambda: None)

        UploadHandler.config_path = None
        UploadHandler.cached_sessions = [
            {"name": "marka", "session": "marka", "path": "INTERNAL/marka"},
            {"name": "upup", "session": "upup", "path": "INTERNAL/upup"},
        ]
        UploadHandler.sessions_ts = time.time() + 9999
        UploadHandler.port = 8080
        UploadHandler.pid = 4321
        UploadHandler.started_at = time.time() - 5

        from http.server import HTTPServer

        self.server = HTTPServer(("127.0.0.1", 0), UploadHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        yield
        self.server.shutdown()

    def test_health_reports_ok_and_shape(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read())
        assert data["ok"] is True
        assert data["service"] == "magent-upload"
        assert data["port"] == 8080
        assert data["pid"] == 4321
        # P3-18: the COUNT is `session_count`; `sessions` is the LIST route.
        assert data["session_count"] == 2
        assert "sessions" not in data
        assert data["uptime_s"] >= 0


class TestInSessionFeedback:
    """Upload progress is flashed into the magent:<project> psmux status line."""

    @pytest.fixture(autouse=True)
    def _server(self, tmp_path, monkeypatch):
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "_UPLOAD_DIR", tmp_path / "uploads")
        import magent.psmux as psmux_mod

        monkeypatch.setattr(psmux_mod, "find_psmux", lambda: "psmux")
        monkeypatch.setattr(mod, "_inflight", {})

        self.calls: list[list[str]] = []

        def _rec(args, **kwargs):
            self.calls.append(list(args))

            class R:
                returncode = 0
                stdout = b""
                stderr = b""

            return R()

        monkeypatch.setattr(mod.subprocess, "run", _rec)
        monkeypatch.setattr(psmux_mod.subprocess, "run", _rec)

        UploadHandler.config_path = None
        UploadHandler.cached_sessions = [{"name": "marka", "path": "INTERNAL/marka"}]
        UploadHandler.sessions_ts = time.time() + 9999

        from http.server import HTTPServer

        self.server = HTTPServer(("127.0.0.1", 0), UploadHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        yield
        self.server.shutdown()

    def _post(self, path: str, project_field: str = "marka") -> dict:
        body = (
            f"------B\r\n"
            f'Content-Disposition: form-data; name="project"\r\n\r\n'
            f"{project_field}\r\n"
            f"------B\r\n"
            f'Content-Disposition: form-data; name="inject"\r\n\r\n0\r\n'
            f"------B\r\n"
            f'Content-Disposition: form-data; name="file"; filename="c.png"\r\n\r\n'
            f"DATA\r\n"
            f"------B--\r\n"
        ).encode()
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(
            "POST",
            path,
            body=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=----B",
                "Content-Length": str(len(body)),
            },
        )
        return json.loads(conn.getresponse().read())

    def _flashes(self) -> list[str]:
        return [" ".join(c) for c in self.calls if "display-message" in c]

    def _wait_flash(self, substr: str, timeout: float = 3.0) -> bool:
        # The result flash fires after the HTTP response is sent (so the client
        # isn't blocked on the status-bar subprocess), so poll for it.
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(substr in f for f in self._flashes()):
                return True
            time.sleep(0.02)
        return False

    def test_query_flashes_uploading_then_uploaded(self):
        assert self._post("/upload?project=marka")["ok"] is True
        # early flash lands before the response, so it's already recorded
        assert any("uploading image" in f for f in self._flashes())
        # the early flash targets the right session socket (a message-style
        # tint may sit between the socket flag and display-message)
        assert any(
            "-L marka" in f and "display-message" in f and "uploading" in f
            for f in self._flashes()
        )
        # result flash lands just after the response
        assert self._wait_flash("image uploaded")

    def test_no_query_skips_early_flash_but_confirms(self):
        assert self._post("/upload", project_field="marka")["ok"] is True
        assert self._wait_flash("image uploaded")  # still confirmed
        assert not any(
            "uploading image" in f for f in self._flashes()
        )  # no early flash

    def test_failure_flashes_when_flagged_upload_rejected(self):
        # query flags marka (valid early flash) but the body names an unknown
        # project -> the upload is rejected and the session sees a failure.
        assert self._post("/upload?project=marka", project_field="evil")["ok"] is False
        assert any("uploading image" in f for f in self._flashes())
        assert self._wait_flash("upload failed")

    def test_inflight_count_clears_after_upload(self):
        import magent.upload_server as mod

        self._post("/upload?project=marka")
        self._wait_flash("image uploaded")
        assert mod._inflight.get("marka", 0) == 0


class TestStopServer:
    """Truthful stop_server: True only when the kill actually succeeded; the
    pid file survives a failed kill so `status`/a retry can still find it."""

    def test_no_pid_file_returns_false(self, tmp_path, monkeypatch):
        # Pin: this invariant is unchanged by the taskkill-rc behavior below.
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "_pid_path", lambda port: tmp_path / "nonexistent.pid")
        assert mod.stop_server(9999) is False

    def test_keeps_pid_file_when_taskkill_fails(self, tmp_path, monkeypatch):
        import magent.upload_server as mod

        pid_file = tmp_path / "upload_server-9999.pid"
        pid_file.write_text("4321")
        monkeypatch.setattr(mod, "_pid_path", lambda port: pid_file)
        monkeypatch.setattr(mod.sys, "platform", "win32")

        class _Result:
            returncode = 1

        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Result())

        assert mod.stop_server(9999) is False
        assert pid_file.exists()

    def test_removes_pid_file_when_taskkill_succeeds(self, tmp_path, monkeypatch):
        import magent.upload_server as mod

        pid_file = tmp_path / "upload_server-9999.pid"
        pid_file.write_text("4321")
        monkeypatch.setattr(mod, "_pid_path", lambda port: pid_file)
        monkeypatch.setattr(mod.sys, "platform", "win32")

        class _Result:
            returncode = 0

        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Result())

        assert mod.stop_server(9999) is True
        assert not pid_file.exists()


class TestBindAddresses:
    """R7: the upload server must never bind the LAN wildcard 0.0.0.0 --
    only loopback (so the cli.py liveness probe + localhost URL keep
    working) plus the Tailscale IP when one is available."""

    def test_auto_bind_loopback_only_when_no_tailscale(self, monkeypatch):
        import magent.upload_server as mod

        monkeypatch.setattr(mod.tailnet, "ip4", lambda: None)
        assert mod._bind_addresses(None) == ["127.0.0.1"]
        assert "0.0.0.0" not in mod._bind_addresses(None)

    def test_auto_bind_includes_tailscale(self, monkeypatch):
        import magent.upload_server as mod

        monkeypatch.setattr(mod.tailnet, "ip4", lambda: "100.64.1.2")
        assert mod._bind_addresses(None) == ["127.0.0.1", "100.64.1.2"]

    def test_explicit_host_honored(self):
        import magent.upload_server as mod

        # The --host escape hatch is honored verbatim, including 0.0.0.0.
        assert mod._bind_addresses("0.0.0.0") == ["0.0.0.0"]

    def test_run_server_binds_expected(self, tmp_path, monkeypatch):
        import magent.upload_server as mod

        monkeypatch.setattr(mod.tailnet, "ip4", lambda: None)
        monkeypatch.setattr(
            mod, "_pid_path", lambda port: tmp_path / f"upload-{port}.pid"
        )

        constructed = []

        class _FakeServer:
            def __init__(self, address, handler_cls):
                self.server_address = address
                constructed.append(address)

            def serve_forever(self):
                raise KeyboardInterrupt

            def shutdown(self):
                pass

            def server_close(self):
                pass

        # run_server constructs _NoFqdnHTTPServer (the no-reverse-DNS subclass).
        monkeypatch.setattr(mod, "_NoFqdnHTTPServer", _FakeServer)

        with pytest.raises(KeyboardInterrupt):
            mod.run_server(port=0)

        assert constructed == [("127.0.0.1", 0)]  # loopback only, never 0.0.0.0

    def test_server_bind_never_reverse_resolves(self, monkeypatch):
        """Pin the macOS-wedge fix: server_bind must not call socket.getfqdn.

        HTTPServer.server_bind's getfqdn(host) goes through mDNSResponder on
        macOS and was observed blocking forever on CI -- socket bound, listen()
        never reached, clients hanging. _NoFqdnHTTPServer records the bind host
        verbatim; a regression back to the stdlib bind trips the bomb below.
        """
        import socket

        import magent.upload_server as mod

        def _bomb(name: str = "") -> str:
            raise AssertionError(
                "server_bind must never reverse-resolve (macOS mdns wedge)"
            )

        monkeypatch.setattr(socket, "getfqdn", _bomb)
        srv = mod._NoFqdnHTTPServer(("127.0.0.1", 0), mod.UploadHandler)
        try:
            assert srv.server_name == "127.0.0.1"
            assert srv.server_port == srv.server_address[1]
            assert srv.server_port != 0
        finally:
            srv.server_close()
