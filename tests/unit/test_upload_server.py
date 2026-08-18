import io
import json
import logging
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

    def _cfg(self, tmp_path, projects, base_dir=None):
        cfg = tmp_path / "magent.config.json"
        body: dict[str, object] = {"projects": projects}
        if base_dir is not None:
            body["baseDir"] = base_dir
        cfg.write_text(json.dumps(body))
        return str(cfg)

    def test_absolute_path_stays_itself(self, tmp_path):
        # `resolved` is what a client can actually act on (the F2 open-in-code
        # hotkey); `path` is the raw config value and may be relative.
        (tmp_path / "svc").mkdir()
        out = config_sessions(
            self._cfg(tmp_path, [{"path": str(tmp_path / "svc"), "tool": "claude"}])
        )
        assert out[0]["resolved"] == str(tmp_path / "svc")

    def test_relative_path_resolves_against_base_dir(self, tmp_path):
        (tmp_path / "INTERNAL" / "caly").mkdir(parents=True)
        out = config_sessions(
            self._cfg(
                tmp_path,
                [{"path": "INTERNAL/caly", "tool": "claude"}],
                base_dir=str(tmp_path),
            )
        )
        assert out[0]["path"] == "INTERNAL/caly"  # raw value untouched
        assert Path(out[0]["resolved"]) == tmp_path / "INTERNAL" / "caly"

    def test_unresolvable_path_is_empty_string(self, tmp_path):
        # Never None: a JSON consumer reads it as a plain string field.
        out = config_sessions(
            self._cfg(tmp_path, [{"path": "nope/missing", "tool": "claude"}])
        )
        assert out[0]["resolved"] == ""

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
    """Upload progress is flashed into the magent:<project> psmux status line
    -- for the MOBILE page, which has no other screen in that window.

    An Alt+V paste is different: it arrives with ``?project=`` and narrates
    itself (altv.handle_press said "capturing..." before the clipboard was even
    read, and will say the specific outcome when the reply lands). The status
    line is ONE line, so a second voice on it can only race the first, and the
    loser is whichever message the user actually needed. Hence: flagged
    uploads get silence from the server, by design.
    """

    @pytest.fixture(autouse=True)
    def _server(self, tmp_path, monkeypatch):
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "_UPLOAD_DIR", tmp_path / "uploads")
        import magent.psmux as psmux_mod

        monkeypatch.setattr(psmux_mod, "find_psmux", lambda: "psmux")

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

    def test_a_mobile_upload_is_confirmed_on_the_bar(self):
        assert self._post("/upload", project_field="marka")["ok"] is True
        assert self._wait_flash("image uploaded")
        # ...at the right session's own socket (a message-style tint may sit
        # between the socket flag and display-message).
        assert any(
            "-L marka" in f and "display-message" in f and "image uploaded" in f
            for f in self._flashes()
        )

    def test_a_mobile_failure_is_shown_too(self):
        assert self._post("/upload", project_field="evil")["ok"] is False
        # An unknown project has no window to flash into; a KNOWN one does.
        assert not self._flashes()

    def test_an_alt_v_upload_gets_no_second_voice_from_the_server(self):
        # Regression pin for the "which message won?" race: ?project= means the
        # listener is already narrating this press, so the server says nothing.
        assert self._post("/upload?project=marka")["ok"] is True
        assert not self._wait_flash("image uploaded", timeout=0.6)
        assert not self._wait_flash("uploading image", timeout=0.1)

    def test_an_alt_v_failure_is_left_to_the_listeners_specific_reason(self):
        # The listener's flash says WHICH failure ("serve said HTTP 400:
        # Unknown project"); a generic "upload failed" from here would stomp it.
        assert self._post("/upload?project=marka", project_field="evil")["ok"] is False
        assert not self._wait_flash("upload failed", timeout=0.6)


class TestASlowPasteNeverBecomesAFailedUpload:
    """The reply must not be hostage to the multiplexer.

    Measured on a live machine: `psmux.send_keys` ran INLINE in this handler
    with no timeout at all, a control command stalled while the session's
    terminal was busy, and the request was answered 74 seconds after the press.
    The listener had given up at 20 s and flashed "upload failed - is `magent
    serve` running?" -- about an image that was already on disk, and that psmux
    went on to paste a minute later. The file being safe is exactly why calling
    that a failure was the damaging part: the user reruns the press, and the
    same screenshot is pasted twice.
    """

    @pytest.fixture(autouse=True)
    def _server(self, tmp_path, monkeypatch):
        import magent.psmux as psmux_mod
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "_UPLOAD_DIR", tmp_path / "uploads")
        monkeypatch.setattr(psmux_mod, "find_psmux", lambda: "psmux")
        # A short grace keeps the test honest AND fast: the assertion is that
        # the reply lands inside whatever the grace is, not that 3s is magic.
        monkeypatch.setattr(mod, "INJECT_GRACE_S", 0.3)

        self.release = threading.Event()
        self.entered = threading.Event()
        self.entered_at = 0.0
        self.worker: threading.Thread | None = None
        self.pastes: list[str] = []

        def _paste(name, *keys, target=None, psmux=None, timeout=None):
            # Recorded from INSIDE the worker: the product times its own paste
            # from that thread's clock, and on a loaded runner the thread can
            # start well after the handler's grace has already expired. Tests
            # that want a LATE paste have to synchronize on this, not on the
            # request's return -- see `_stall_past_the_grace`.
            self.pastes.append(name)
            self.entered_at = time.monotonic()
            self.worker = threading.current_thread()
            self.entered.set()
            self.release.wait(20)
            return True

        monkeypatch.setattr(psmux_mod, "send_keys", _paste)

        UploadHandler.config_path = None
        UploadHandler.cached_sessions = [{"name": "marka", "path": "INTERNAL/marka"}]
        UploadHandler.sessions_ts = time.time() + 9999

        from http.server import HTTPServer

        self.server = HTTPServer(("127.0.0.1", 0), UploadHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        yield
        self.release.set()  # never leave a stalled paste thread behind
        self.server.shutdown()

    def _upload(self) -> tuple[dict, float]:
        body = (
            b"------B\r\n"
            b'Content-Disposition: form-data; name="project"\r\n\r\nmarka\r\n'
            b"------B\r\n"
            b'Content-Disposition: form-data; name="inject"\r\n\r\n1\r\n'
            b"------B\r\n"
            b'Content-Disposition: form-data; name="file"; filename="c.png"\r\n\r\n'
            b"FAKEPNG\r\n"
            b"------B--\r\n"
        )
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        started = time.monotonic()
        conn.request(
            "POST",
            "/upload?project=marka",
            body=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=----B",
                "Content-Length": str(len(body)),
            },
        )
        data = json.loads(conn.getresponse().read())
        return data, time.monotonic() - started

    def _stall_past_the_grace_then_finish(self) -> None:
        """Release the paste only once it is provably LATE, then join it.

        Both halves close a real race, and the first one is why this test was
        red on a Windows CI runner while passing locally:

        * The product judges lateness against the WORKER's own clock. Releasing
          as soon as the request returns says nothing about that clock -- if the
          thread was slow to be scheduled it starts, returns instantly against
          an already-set event, measures ~1 ms, and correctly logs nothing. The
          poll that followed then waited out its budget against a line that was
          never going to be written. Sleeping to the worker's own deadline is
          the precondition of the assertion, not a guess at a duration.
        * Joining the worker is what makes the log line already WRITTEN when the
          assertion runs: the record is emitted in `_inject_paste`'s `finally`,
          on this thread, after the fake returns. No polling needed.
        """
        import magent.upload_server as mod

        assert self.entered.wait(15), "the paste worker never started"
        # `entered_at` is taken at or after the worker's own `started`, so
        # waiting out the grace from here guarantees the product sees it too.
        time.sleep(max(0.0, self.entered_at + mod.INJECT_GRACE_S - time.monotonic()))
        self.release.set()
        assert self.worker is not None
        self.worker.join(timeout=30)
        assert not self.worker.is_alive(), "the paste worker never finished"

    def test_the_reply_lands_inside_the_grace_and_the_file_is_on_disk(self):
        data, elapsed = self._upload()

        # The stalled paste blocks for 20s. Anything well under that proves the
        # reply is no longer hostage to it; the budget is deliberately loose
        # because a cold runner's cost belongs to the request, not the fix.
        assert elapsed < 5.0, f"the reply waited {elapsed:.2f}s on a stalled paste"
        # ok=True is the whole point: the bytes ARE stored.
        assert data["ok"] is True
        assert Path(data["path"]).read_bytes() == b"FAKEPNG"

    def test_a_stalled_paste_is_reported_as_pending_not_as_a_refusal(self):
        data, _ = self._upload()
        # Three states, not two. `injected: false` alone is indistinguishable
        # from "psmux said no", which is what the bar rendered as a failure.
        assert data["injected"] is False
        assert data["inject_pending"] is True

    def test_a_paste_that_lands_in_time_is_plainly_injected(self, monkeypatch):
        # The mirror race: with a 0.3s grace, a worker thread that is merely
        # SLOW TO BE SCHEDULED on a loaded runner would be reported pending
        # even though the paste itself is instant. The grace is a ceiling, not
        # a delay -- the handler returns the moment the worker does -- so a
        # generous one costs this test nothing and removes the flake.
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "INJECT_GRACE_S", 30.0)
        self.release.set()
        data, elapsed = self._upload()
        assert data["injected"] is True
        assert data["inject_pending"] is False
        # ...and it really returned on the paste, not on the ceiling.
        assert elapsed < 10.0, f"the reply took {elapsed:.2f}s on an instant paste"

    def test_the_stalled_paste_is_still_running_and_is_never_re_sent(self):
        # One attempt, ever. A `send-keys` that is merely slow is still in
        # flight; a retry on top of it pastes the same image twice.
        self._upload()
        assert self.entered.wait(15), "the paste worker never started"
        assert self.pastes == ["marka"]
        # ...and letting the (single) attempt run to completion adds no second
        # one -- a retry would have to happen after this point to exist at all.
        self._stall_past_the_grace_then_finish()
        assert self.pastes == ["marka"]

    def test_the_late_verdict_reaches_the_log_since_it_cannot_reach_the_bar(
        self, caplog
    ):
        # A flagged (?project=) upload has a narrator already and the server
        # must not become a second one -- so the deferred result is recorded
        # here instead. Silence would make "did it ever paste?" unanswerable.
        with caplog.at_level(logging.WARNING, logger="magent.upload"):
            self._upload()
            self._stall_past_the_grace_then_finish()
            assert "finished late" in caplog.text
            assert "marka" in caplog.text

    def test_the_pending_flag_is_in_the_outcome_log_line(self, caplog):
        # This one is written on the SERVER thread, in do_POST's `finally`,
        # after the response is already on the wire -- the same documented
        # race `_wait_log` exists for above, so it polls rather than joins.
        with caplog.at_level(logging.INFO, logger="magent.upload"):
            self._upload()
            deadline = time.time() + 10
            while time.time() < deadline and "pending=True" not in caplog.text:
                time.sleep(0.02)
            assert "pending=True" in caplog.text


class TestFlashEndpoint:
    """GET /api/flash -- the on-screen voice of callers that have no screen.
    The Alt+V/F2 listener runs hidden with no terminal, so this route is the
    only way an F2 failure reaches the user instead of only hotkey.log."""

    @pytest.fixture(autouse=True)
    def _server(self, tmp_path, monkeypatch):
        import magent.psmux as psmux_mod
        import magent.upload_server as mod

        monkeypatch.setattr(mod, "_UPLOAD_DIR", tmp_path / "uploads")
        monkeypatch.setattr(psmux_mod, "find_psmux", lambda: "psmux")

        self.calls: list[list[str]] = []

        def _rec(args, **kwargs):
            self.calls.append(list(args))

            class R:
                returncode = 0
                stdout = b""
                stderr = b""

            return R()

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

    def _get(self, path: str):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())

    def _flashes(self) -> list[list[str]]:
        return [c for c in self.calls if "display-message" in c]

    def test_flashes_the_decoded_message_at_the_named_project(self):
        status, data = self._get("/api/flash?project=marka&msg=F2%3A%20opening...")
        assert status == 200
        assert data == {"ok": True}
        flash = self._flashes()[0]
        # Routed to that project's own psmux socket, with the message decoded.
        assert flash[:3] == ["psmux", "-L", "marka"]
        assert flash[-1] == "F2: opening..."

    def test_plus_and_percent_escapes_are_decoded(self):
        # quote() emits %20 for spaces, but a hand-built or browser-issued URL
        # can use "+" -- parse_qs decodes both, and neither may leak literally.
        self._get("/api/flash?project=marka&msg=a+b%20c%26d")
        assert self._flashes()[0][-1] == "a b c&d"

    def test_long_messages_are_clamped_server_side(self):
        from magent.sessions import FLASH_MSG_MAX

        self._get(f"/api/flash?project=marka&msg={'z' * (FLASH_MSG_MAX + 200)}")
        # Clamped independently of the client: a status bar is one line wide.
        assert self._flashes()[0][-1] == "z" * FLASH_MSG_MAX

    def test_missing_msg_is_400_and_flashes_nothing(self):
        status, data = self._get("/api/flash?project=marka")
        assert status == 400
        assert data["ok"] is False
        assert data["error"]
        assert self._flashes() == []

    def test_missing_project_is_400_and_flashes_nothing(self):
        status, data = self._get("/api/flash?msg=hello")
        assert status == 400
        assert data["ok"] is False
        assert self._flashes() == []

    def test_no_query_at_all_is_400(self):
        status, data = self._get("/api/flash")
        assert status == 400
        assert data["ok"] is False

    def test_a_phase_message_can_ask_to_linger(self):
        # A phase ("uploading...") that expires while the step is still running
        # leaves a blank bar, which reads exactly like the silence this route
        # exists to end -- so the caller may set its own duration.
        self._get("/api/flash?project=marka&msg=working&ms=20000")
        assert "20000" in self._flashes()[0]

    def test_an_absurd_or_broken_duration_is_clamped_not_obeyed(self):
        import magent.upload_server as mod

        self._get("/api/flash?project=marka&msg=a&ms=99999999")
        self._get("/api/flash?project=marka&msg=b&ms=notanumber")
        self._get("/api/flash?project=marka&msg=c&ms=-5")
        durations = [f[f.index("-d") + 1] for f in self._flashes()]
        assert durations == [
            str(mod._FLASH_MSG_MS_MAX),
            str(mod._FLASH_MSG_MS),  # unparseable falls back, never fails the flash
            str(mod._FLASH_MSG_MS_MIN),
        ]

    def test_the_tint_reaches_the_message_style(self):
        # psmux's message-style is GLOBAL on the socket, so the caller sets it
        # on every message; err must not leak into the next ok (and vice versa).
        import magent.upload_server as mod

        self._get("/api/flash?project=marka&msg=bad&tint=err")
        self._get("/api/flash?project=marka&msg=fine&tint=ok")
        styled = [c for c in self.calls if "message-style" in c]
        assert mod._MSG_RED in styled[0]
        assert mod._MSG_GREEN in styled[1]

    def test_an_unknown_tint_leaves_the_style_alone_and_still_flashes(self):
        self._get("/api/flash?project=marka&msg=hello&tint=chartreuse")
        assert self._flashes()[0][-1] == "hello"
        assert not any("message-style" in c for c in self.calls)

    def test_post_on_the_flash_route_is_405_not_404(self):
        # P3-16: /api/flash is a real GET route, so the wrong verb answers 405.
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/flash", body=b"", headers={"Content-Length": "0"})
        resp = conn.getresponse()
        assert resp.status == 405
        assert json.loads(resp.read())["ok"] is False


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
        # ...and it now also supervises the Alt+V listener, which spawns a
        # process that installs a SYSTEM-WIDE keyboard hook. Never from a unit
        # test: the wiring is pinned separately, with a stub.
        supervised = []
        monkeypatch.setattr(
            mod, "_supervise_hotkey", lambda url, stop, **kw: supervised.append(url)
        )

        with pytest.raises(KeyboardInterrupt):
            mod.run_server(port=0)

        assert constructed == [("127.0.0.1", 0)]  # loopback only, never 0.0.0.0
        assert supervised == ["http://127.0.0.1:0"]  # the listener gets an owner

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


class TestLocalUrl:
    """Which URL the supervised listener is handed. It must be reachable from
    THIS machine without Tailscale being up -- the listener posts every Alt+V
    image through it."""

    def test_default_bind_uses_loopback(self):
        import magent.upload_server as mod

        assert mod.local_url(["127.0.0.1", "100.64.0.1"], 8034) == (
            "http://127.0.0.1:8034"
        )

    def test_lan_wildcard_still_resolves_to_loopback(self):
        # `serve --host 0.0.0.0` binds loopback too; "http://0.0.0.0:..." is
        # not a URL a client should be handed.
        import magent.upload_server as mod

        assert mod.local_url(["0.0.0.0"], 8034) == "http://127.0.0.1:8034"

    def test_a_bind_that_excluded_loopback_uses_what_was_bound(self):
        # `serve --host <tailscale-ip>`: loopback is genuinely not listening,
        # so claiming it would hand the listener a dead URL.
        import magent.upload_server as mod

        assert mod.local_url(["100.64.0.1"], 8034) == "http://100.64.0.1:8034"


class _Env:
    """Stand-in for MagentEnv over the fields this code path reads: the
    supervisor's own switch, plus log_level (get_logger consults it)."""

    log_level = None

    def __init__(self, hotkey_supervisor: bool) -> None:
        self.hotkey_supervisor = hotkey_supervisor


class TestHotkeySupervisor:
    """serve owns the Alt+V listener's liveness.

    Before this, the listener was a one-shot spawn by whichever launch/attach
    ran last: a reboot or a crash left Alt+V dead with nothing ever re-checking
    it, and `status` reported that as a benign default.
    """

    def _mod(self):
        import magent.upload_server as mod

        return mod

    def _fake_platform(self, monkeypatch, *, supports_hotkey):
        from tests.conftest import FakePlatform

        fp = FakePlatform(supports_hotkey=supports_hotkey)
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)

    def _ensure(self, monkeypatch, result=4242):
        calls: list[str] = []

        def _fake(url):
            calls.append(url)
            return result

        monkeypatch.setattr("magent.launch.ensure_hotkey_listener", _fake)
        return calls

    def test_checks_immediately_and_then_every_interval(self, monkeypatch):
        self._fake_platform(monkeypatch, supports_hotkey=True)
        calls = self._ensure(monkeypatch)
        stop = threading.Event()
        waits: list[float] = []

        def _wait(timeout):
            waits.append(timeout)
            return len(waits) >= 3  # stop on the third pass

        monkeypatch.setattr(stop, "wait", _wait)

        self._mod()._supervise_hotkey("http://127.0.0.1:8034", stop, interval=30.0)

        # The first check is NOT deferred by an interval: a serve that just
        # started must not leave Alt+V dead for 30 more seconds.
        assert calls == ["http://127.0.0.1:8034"] * 3
        assert waits == [30.0, 30.0, 30.0]

    def test_the_env_opt_out_stops_it_before_it_touches_anything(self, monkeypatch):
        # MAGENT_HOTKEY_SUPERVISOR=0 is what keeps a test that starts a real
        # serve from installing a system-wide keyboard hook on a dev machine.
        self._fake_platform(monkeypatch, supports_hotkey=True)
        calls = self._ensure(monkeypatch)
        monkeypatch.setattr("magent.env.get_env", lambda: _Env(False))

        self._mod()._supervise_hotkey("http://127.0.0.1:8034", threading.Event())

        assert calls == []

    def test_a_broken_environment_still_supervises(self, monkeypatch, caplog):
        # A detached daemon must not lose a feature because some unrelated
        # MAGENT_* var went bad after it started (same posture as log.py).
        from pydantic import ValidationError

        self._fake_platform(monkeypatch, supports_hotkey=True)
        calls = self._ensure(monkeypatch)

        def _bad():
            raise ValidationError.from_exception_data("MagentEnv", [])

        monkeypatch.setattr("magent.env.get_env", _bad)
        stop = threading.Event()
        monkeypatch.setattr(stop, "wait", lambda timeout: True)

        with caplog.at_level("WARNING", logger="magent.hotkey"):
            self._mod()._supervise_hotkey("http://127.0.0.1:8034", stop, interval=1.0)

        assert calls == ["http://127.0.0.1:8034"]
        assert "did not validate" in caplog.text

    def test_returns_immediately_where_the_platform_has_no_hotkey(self, monkeypatch):
        self._fake_platform(monkeypatch, supports_hotkey=False)
        calls = self._ensure(monkeypatch)

        self._mod()._supervise_hotkey("http://127.0.0.1:8034", threading.Event())

        assert calls == []  # and no unbounded loop on a non-Windows serve

    def test_a_failed_spawn_is_logged_and_retried_not_fatal(self, monkeypatch, caplog):
        self._fake_platform(monkeypatch, supports_hotkey=True)
        calls = self._ensure(monkeypatch, result=None)  # child never confirmed
        stop = threading.Event()
        monkeypatch.setattr(stop, "wait", lambda timeout: len(calls) >= 2)

        with caplog.at_level("WARNING", logger="magent.hotkey"):
            self._mod()._supervise_hotkey("http://127.0.0.1:8034", stop, interval=1.0)

        assert len(calls) == 2  # it tried again rather than giving up
        assert "no Alt+V listener came up" in caplog.text

    def test_an_exception_cannot_take_down_the_server_thread(self, monkeypatch, caplog):
        self._fake_platform(monkeypatch, supports_hotkey=True)
        boom: list[int] = []

        def _explode(url):
            boom.append(1)
            raise RuntimeError("pid file on fire")

        monkeypatch.setattr("magent.launch.ensure_hotkey_listener", _explode)
        stop = threading.Event()
        monkeypatch.setattr(stop, "wait", lambda timeout: len(boom) >= 2)

        with caplog.at_level("ERROR", logger="magent.hotkey"):
            self._mod()._supervise_hotkey("http://127.0.0.1:8034", stop, interval=1.0)

        assert len(boom) == 2
        assert "listener check failed" in caplog.text

    def test_a_second_server_supervising_backs_off_instead_of_racing(self, monkeypatch):
        # Two serve daemons on different ports would otherwise both decide the
        # listener is missing and both spawn one.
        mod = self._mod()
        self._fake_platform(monkeypatch, supports_hotkey=True)
        calls = self._ensure(monkeypatch)

        def _held(name):
            raise mod.LockHeld(name)

        monkeypatch.setattr(mod, "exclusive_lock", _held)
        stop = threading.Event()
        seen: list[int] = []

        def _wait(timeout):
            seen.append(1)
            return True

        monkeypatch.setattr(stop, "wait", _wait)

        mod._supervise_hotkey("http://127.0.0.1:8034", stop, interval=1.0)

        assert calls == []  # never spawned behind the other supervisor's back
        assert seen == [1]  # and still slept rather than spinning
