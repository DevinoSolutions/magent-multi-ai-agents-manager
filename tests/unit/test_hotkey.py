import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")


class TestProjectFromTitle:
    def test_extracts_name(self):
        from magent.hotkey import project_from_title

        assert project_from_title("magent:marka") == "marka"
        assert project_from_title("magent:upup") == "upup"

    def test_extracts_name_through_state_badge(self):
        # The attention daemon rewrites titles as "magent:[!] name" etc.; upload
        # routing must keep working while a window is badged.
        from magent.hotkey import project_from_title

        assert project_from_title("magent:[!] marka") == "marka"
        assert project_from_title("magent:[x] upup") == "upup"
        assert project_from_title("magent:[+] api") == "api"

    def test_returns_none_for_non_md(self):
        from magent.hotkey import project_from_title

        assert project_from_title("Windows Terminal") is None
        assert project_from_title("claude") is None
        assert project_from_title("") is None

    def test_agrees_with_the_titles_grammar(self):
        # hotkey consumes what titles.make_title produces — the round-trip
        # contract that replaced the old shared-MAGENT_TITLE_PREFIX pin.
        from magent.hotkey import project_from_title
        from magent.titles import make_title

        for state in (None, "needs-input", "error", "done"):
            assert project_from_title(make_title("proj", state)) == "proj"


class TestAltKeyDetection:
    def test_physical_alt_codes_recognized(self):
        # A low-level keyboard hook reports the physical Alt as VK_LMENU/VK_RMENU,
        # never the generic VK_MENU. All three must be treated as Alt or Alt+V
        # is never detected (the keystroke falls through to the focused app).
        from magent.hotkey import _ALT_KEYS, VK_LMENU, VK_MENU, VK_RMENU

        assert VK_LMENU == 0xA4
        assert VK_RMENU == 0xA5
        assert VK_LMENU in _ALT_KEYS
        assert VK_RMENU in _ALT_KEYS
        assert VK_MENU in _ALT_KEYS


class TestUploadImage:
    @pytest.fixture(autouse=True)
    def _server(self):
        self.last_request = {}

        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                parent.last_request = {
                    "path": self.path,
                    "body": body,
                    "content_type": self.headers.get("Content-Type", ""),
                }
                resp = json.dumps({"ok": True, "injected": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        yield
        self.server.shutdown()

    def test_uploads_image(self):
        from magent.hotkey import upload_image

        url = f"http://127.0.0.1:{self.port}"
        result = upload_image(url, "marka", b"FAKEBMP")
        assert result is True
        # project rides in the query string so the server can flash "uploading"
        # before reading the body, and still in the multipart body for validation.
        assert self.last_request["path"].startswith("/upload")
        assert "project=marka" in self.last_request["path"]
        assert b"marka" in self.last_request["body"]
        assert b"FAKEBMP" in self.last_request["body"]
        assert "multipart/form-data" in self.last_request["content_type"]
        # Boundary consistency: the Content-Type header's boundary must match
        # the delimiters actually written into the body.
        ct = self.last_request["content_type"]
        boundary = ct.split("boundary=")[1].strip()
        body = self.last_request["body"]
        assert f"--{boundary}\r\n".encode() in body
        assert f"\r\n--{boundary}--\r\n".encode() in body

    def test_returns_false_on_network_error(self):
        from magent.hotkey import upload_image

        result = upload_image("http://127.0.0.1:1", "marka", b"data")
        assert result is False

    def test_network_error_logs_specific_transport_cause(self, caplog):
        # P2-07: a failed paste must record WHY (connection refused vs. timeout
        # vs. malformed response), not just a bare False the caller can't
        # explain -- the cause (exception class + message) lands in hotkey.log.
        from magent.hotkey import upload_image

        with caplog.at_level("WARNING", logger="magent.hotkey"):
            result = upload_image("http://127.0.0.1:1", "marka", b"data")

        assert result is False
        assert "transport error" in caplog.text
        assert "URLError" in caplog.text  # the specific class, not a generic line


class TestDibToBmp:
    """Clipboard DIB -> BMP conversion (the all-black image bug)."""

    @staticmethod
    def _header(width, height, bpp, compression):
        import struct

        return struct.pack(
            "<IiiHHIIiiII",
            40,  # biSize (BITMAPINFOHEADER)
            width,
            height,
            1,  # planes
            bpp,
            compression,
            0,
            0,
            0,
            0,
            0,  # sizeImage, x/y ppm, clrUsed, clrImportant
        )

    def test_bitfields_offset_skips_masks(self):
        # 32bpp BI_BITFIELDS (what GDI / .NET / screenshots produce): 3 color
        # masks sit between the 40-byte header and the pixels.
        import struct

        from magent.hotkey import _dib_to_bmp

        header = self._header(2, 2, 32, 3)
        masks = struct.pack("<III", 0x00FF0000, 0x0000FF00, 0x000000FF)
        pixels = bytes([0, 0, 255, 0] * 4)  # opaque-red BGR with alpha=0
        bmp = _dib_to_bmp(bytearray(header + masks + pixels))

        assert bmp[:2] == b"BM"
        bf_off_bits = struct.unpack_from("<I", bmp, 10)[0]
        assert bf_off_bits == 14 + 40 + 12  # past header AND the 12 mask bytes
        # alpha forced opaque so decoders don't render it transparent/black
        for i in range(bf_off_bits + 3, len(bmp), 4):
            assert bmp[i] == 0xFF

    def test_rgb32_forces_alpha_opaque(self):
        import struct

        from magent.hotkey import _dib_to_bmp

        header = self._header(2, 2, 32, 0)  # BI_RGB, no masks
        pixels = bytes([10, 20, 30, 0] * 4)  # alpha = 0 (transparent -> black)
        bmp = _dib_to_bmp(bytearray(header + pixels))

        bf_off_bits = struct.unpack_from("<I", bmp, 10)[0]
        assert bf_off_bits == 14 + 40  # no masks for BI_RGB
        for i in range(bf_off_bits + 3, len(bmp), 4):
            assert bmp[i] == 0xFF

    def test_rgb24_untouched(self):
        import struct

        from magent.hotkey import _dib_to_bmp

        header = self._header(2, 2, 24, 0)
        pixels = bytes([1, 2, 3] * 4)
        bmp = _dib_to_bmp(bytearray(header + pixels))
        bf_off_bits = struct.unpack_from("<I", bmp, 10)[0]
        assert bf_off_bits == 14 + 40
        assert bmp[14 + 40 :] == pixels  # 24bpp pixels passed through verbatim

    def test_too_small_returns_none(self):
        from magent.hotkey import _dib_to_bmp

        assert _dib_to_bmp(bytearray(b"\x00" * 10)) is None

    def test_huge_header_size_returns_none(self):
        # F-D4-005: a header_size claiming ~4GB drives px_start past 2**32,
        # so the offset.to_bytes(4, "little") below crashes with
        # OverflowError on a clipboard payload we don't control.
        from magent.hotkey import _dib_to_bmp

        header = bytearray(self._header(2, 2, 32, 0))
        header[0:4] = b"\xff\xff\xff\xff"  # biSize
        pixels = bytes([0, 0, 0, 0] * 4)
        assert _dib_to_bmp(bytearray(bytes(header) + pixels)) is None

    def test_huge_clr_used_returns_none(self):
        # Same OverflowError, reached via clrUsed instead of biSize: bpp<=8
        # multiplies clr_used straight into the offset with no bound.
        from magent.hotkey import _dib_to_bmp

        header = bytearray(self._header(2, 2, 8, 0))
        header[32:36] = b"\xff\xff\xff\xff"  # clrUsed
        pixels = bytes([0] * 16)
        assert _dib_to_bmp(bytearray(bytes(header) + pixels)) is None


class TestListenerLifecycle:
    """Pid-file management for the background Alt+V listener."""

    def test_pid_none_when_no_file(self, tmp_path, monkeypatch):
        from magent import hotkey

        monkeypatch.setattr(hotkey, "_PID_PATH", tmp_path / "hotkey.pid")
        assert hotkey.listener_pid() is None

    def test_pid_returns_live_pid(self, tmp_path, monkeypatch):
        from magent import hotkey

        p = tmp_path / "hotkey.pid"
        p.write_text("4321")
        monkeypatch.setattr(hotkey, "_PID_PATH", p)
        monkeypatch.setattr(hotkey, "pid_alive", lambda pid: pid == 4321)
        assert hotkey.listener_pid() == 4321

    def test_pid_clears_stale_file(self, tmp_path, monkeypatch):
        from magent import hotkey

        p = tmp_path / "hotkey.pid"
        p.write_text("999999")
        monkeypatch.setattr(hotkey, "_PID_PATH", p)
        monkeypatch.setattr(hotkey, "pid_alive", lambda pid: False)
        assert hotkey.listener_pid() is None
        assert not p.exists()  # stale pid file is cleaned up

    def test_stop_kills_and_removes(self, tmp_path, monkeypatch):
        import subprocess

        from magent import hotkey

        p = tmp_path / "hotkey.pid"
        p.write_text("4321")
        monkeypatch.setattr(hotkey, "_PID_PATH", p)
        monkeypatch.setattr(hotkey, "pid_alive", lambda pid: True)
        calls = []

        class _Result:
            returncode = 0

        def _rec(*a, **k):
            calls.append(a[0])
            return _Result()

        monkeypatch.setattr(subprocess, "run", _rec)
        assert hotkey.stop_listener() is True
        assert calls and calls[0][0] == "taskkill" and "4321" in calls[0]
        assert not p.exists()

    def test_stop_noop_when_not_running(self, tmp_path, monkeypatch):
        from magent import hotkey

        monkeypatch.setattr(hotkey, "_PID_PATH", tmp_path / "hotkey.pid")
        assert hotkey.stop_listener() is False

    def test_stop_keeps_pid_file_when_taskkill_fails(self, tmp_path, monkeypatch):
        # F-IC-006 (honest half): a failed kill returns False and leaves the
        # pid file in place so `status`/a retry can still find the process.
        import subprocess

        from magent import hotkey

        p = tmp_path / "hotkey.pid"
        p.write_text("4321")
        monkeypatch.setattr(hotkey, "_PID_PATH", p)
        monkeypatch.setattr(hotkey, "pid_alive", lambda pid: True)

        class _Result:
            returncode = 1

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result())
        assert hotkey.stop_listener() is False
        assert p.exists()

    def test_write_then_clear_pid(self, tmp_path, monkeypatch):
        import os

        from magent import hotkey

        p = tmp_path / "hotkey.pid"
        monkeypatch.setattr(hotkey, "_PID_PATH", p)
        hotkey._write_pid()
        assert p.read_text().strip() == str(os.getpid())
        hotkey._clear_pid()
        assert not p.exists()


class TestListenerManifest:
    """The listener self-describes beside its pid file.

    Without this the pid file says only "something is alive": an old process
    survives a pip upgrade running old code (F2 silently dead), and a listener
    wired to loopback by a local launch blocks the ssh-wired one `magent
    attach` wants. The manifest is what makes those answerable.
    """

    @pytest.fixture
    def paths(self, tmp_path, monkeypatch):
        from magent import hotkey

        monkeypatch.setattr(hotkey, "_PID_PATH", tmp_path / "hotkey.pid")
        monkeypatch.setattr(hotkey, "_MANIFEST_PATH", tmp_path / "hotkey.json")
        return tmp_path

    def test_write_then_read_round_trip(self, paths, monkeypatch):
        from magent import __version__, hotkey

        hotkey._write_manifest("http://host:8034", "mdssh")
        assert hotkey.listener_manifest() == {
            "version": __version__,
            "server_url": "http://host:8034",
            "ssh_host": "mdssh",
        }

    def test_local_listener_records_no_ssh_host(self, paths):
        from magent import hotkey

        hotkey._write_manifest("http://127.0.0.1:8034", None)
        assert hotkey.listener_manifest()["ssh_host"] is None

    def test_missing_file_reads_as_none(self, paths):
        # A pre-3.6.0 listener wrote no manifest at all -- indistinguishable
        # from "no manifest", and treated the same way: stale.
        from magent import hotkey

        assert hotkey.listener_manifest() is None

    def test_corrupt_file_reads_as_none(self, paths):
        from magent import hotkey

        hotkey._MANIFEST_PATH.write_text("{ not json")
        assert hotkey.listener_manifest() is None

    def test_non_object_json_reads_as_none(self, paths):
        from magent import hotkey

        hotkey._MANIFEST_PATH.write_text("[1, 2, 3]")
        assert hotkey.listener_manifest() is None

    def test_non_string_fields_read_as_none(self, paths):
        from magent import hotkey

        hotkey._MANIFEST_PATH.write_text('{"version": 3, "server_url": []}')
        assert hotkey.listener_manifest() == {
            "version": None,
            "server_url": None,
            "ssh_host": None,
        }

    def test_stop_listener_clears_the_manifest(self, paths, monkeypatch):
        # A killed listener must not leave a manifest vouching for a process
        # that is gone.
        import subprocess

        from magent import hotkey

        hotkey._PID_PATH.write_text("4321")
        hotkey._write_manifest("http://host:8034", None)
        monkeypatch.setattr(hotkey, "pid_alive", lambda pid: True)

        class _Result:
            returncode = 0

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result())
        assert hotkey.stop_listener() is True
        assert hotkey.listener_manifest() is None

    def test_run_hotkey_writes_it_on_start_and_clears_on_exit(self, paths, monkeypatch):
        # The manifest rides with the pid: written only after the hook is
        # installed, removed with it when the message loop ends.
        from magent import __version__, hotkey

        monkeypatch.setattr(hotkey, "write_heartbeat", lambda _n: None)
        seen = {}

        class _FakeUser32:
            def SetWindowsHookExW(self, *a):
                return 1

            def SetWinEventHook(self, *a):
                return 7

            def GetMessageW(self, *a):
                # Sampled while the listener is "running" -- i.e. after
                # _write_pid/_write_manifest, before the finally clears them.
                seen["manifest"] = hotkey.listener_manifest()
                seen["pid_file"] = hotkey._PID_PATH.exists()
                return 0  # loop exits immediately

            def UnhookWindowsHookEx(self, *a):
                return 1

            def UnhookWinEvent(self, *a):
                return 1

        monkeypatch.setattr(hotkey, "user32", _FakeUser32())

        hotkey.run_hotkey("http://127.0.0.1:8034", "mdssh")

        assert seen["pid_file"] is True
        assert seen["manifest"] == {
            "version": __version__,
            "server_url": "http://127.0.0.1:8034",
            "ssh_host": "mdssh",
        }
        assert hotkey.listener_manifest() is None  # cleared with the pid file
        assert not hotkey._PID_PATH.exists()


class _OpenCodeHarness:
    """Shared fake for the F2 handler's two round trips (see `_patch`)."""

    def _patch(self, monkeypatch, *, payload=None, code_bin="code"):
        import contextlib
        import io

        from magent import hotkey

        spawned: list[list[str]] = []
        self.flashed: list[str] = []
        monkeypatch.setattr(hotkey.shutil, "which", lambda _n: code_bin)
        monkeypatch.setattr(hotkey.subprocess, "Popen", spawned.append)

        body = json.dumps(payload if payload is not None else {}).encode()

        @contextlib.contextmanager
        def _fake_urlopen(url, timeout=None):
            # One fake stands in for both round trips the handler makes: the
            # /api/sessions lookup and every best-effort /api/flash report.
            if "/api/flash" in url:
                self.flashed.append(parse_qs(urlparse(url).query)["msg"][0])
                yield io.BytesIO(b"")
                return
            assert url.endswith("/api/sessions")
            yield io.BytesIO(body)

        monkeypatch.setattr(hotkey, "urlopen", _fake_urlopen)
        return spawned


class TestDoOpenCode(_OpenCodeHarness):
    """F2 -> open the focused project's folder in VS Code. Every failure mode
    is a log line and a no-op: the listener has to outlive a dead server, an
    unknown project, and a machine with no VS Code on it."""

    def test_opens_the_resolved_folder_locally(self, monkeypatch):
        from magent import hotkey

        spawned = self._patch(
            monkeypatch,
            payload={
                "ok": True,
                "sessions": [
                    {"name": "caly", "session": "caly", "resolved": "/base/caly"}
                ],
            },
        )
        hotkey._do_open_code("http://x:8034", "caly", None)
        assert spawned == [["code", "/base/caly"]]

    def test_opens_over_remote_ssh_when_attached(self, monkeypatch):
        from magent import hotkey

        spawned = self._patch(
            monkeypatch,
            payload={
                "ok": True,
                "sessions": [
                    {"name": "caly", "session": "caly", "resolved": "/base/caly"}
                ],
            },
        )
        hotkey._do_open_code("http://x:8034", "caly", "amin@deck")
        assert spawned == [["code", "--remote", "ssh-remote+deck", "/base/caly"]]

    def test_missing_code_binary_warns_and_does_nothing(self, monkeypatch, caplog):
        from magent import hotkey

        spawned = self._patch(monkeypatch, code_bin=None)
        with caplog.at_level("WARNING", logger="magent.hotkey"):
            hotkey._do_open_code("http://x:8034", "caly", None)
        assert spawned == []
        assert "not on PATH" in caplog.text

    def test_unknown_project_warns_and_does_nothing(self, monkeypatch, caplog):
        from magent import hotkey

        spawned = self._patch(monkeypatch, payload={"ok": True, "sessions": []})
        with caplog.at_level("WARNING", logger="magent.hotkey"):
            hotkey._do_open_code("http://x:8034", "ghost", None)
        assert spawned == []
        assert "no folder for project=ghost" in caplog.text

    def test_unreachable_server_is_caught_not_raised(self, monkeypatch, caplog):
        from urllib.error import URLError

        from magent import hotkey

        monkeypatch.setattr(hotkey.shutil, "which", lambda _n: "code")

        def _down(url, timeout=None):
            raise URLError("connection refused")

        monkeypatch.setattr(hotkey, "urlopen", _down)
        with caplog.at_level("ERROR", logger="magent.hotkey"):
            hotkey._do_open_code("http://x:8034", "caly", None)  # must not raise
        assert "F2: open project=caly failed" in caplog.text


class TestDoOpenCodeFeedback(_OpenCodeHarness):
    """F2's on-screen half. The listener is a hidden background process, so
    without these flashes a failed F2 is indistinguishable from a dead key --
    the whole point of the /api/flash endpoint."""

    def test_entry_flash_fires_before_anything_can_fail(self, monkeypatch):
        from magent import hotkey

        self._patch(
            monkeypatch,
            payload={"ok": True, "sessions": [{"session": "caly", "path": "/b/caly"}]},
        )
        hotkey._do_open_code("http://x:8034", "caly", None)
        assert self.flashed[0] == "F2: opening VS Code..."

    def test_success_flash_names_the_folder(self, monkeypatch):
        from magent import hotkey

        self._patch(
            monkeypatch,
            payload={
                "ok": True,
                "sessions": [{"session": "caly", "resolved": "/base/caly"}],
            },
        )
        hotkey._do_open_code("http://x:8034", "caly", None)
        assert self.flashed[-1] == "F2: VS Code -> /base/caly"

    def test_missing_code_binary_flashes_the_reason(self, monkeypatch):
        from magent import hotkey

        self._patch(monkeypatch, code_bin=None)
        hotkey._do_open_code("http://x:8034", "caly", None)
        assert self.flashed[-1] == "F2: 'code' not found on PATH"

    def test_unknown_project_flashes_the_version_hint(self, monkeypatch):
        from magent import hotkey

        self._patch(monkeypatch, payload={"ok": True, "sessions": []})
        hotkey._do_open_code("http://x:8034", "ghost", None)
        assert self.flashed[-1] == (
            "F2: no folder known for ghost (host magent too old?)"
        )

    def test_unexpected_failure_flashes_and_points_at_the_log(self, monkeypatch):
        from magent import hotkey

        self._patch(
            monkeypatch,
            payload={
                "ok": True,
                "sessions": [{"session": "caly", "resolved": "/base/caly"}],
            },
        )

        def _boom(_argv):
            raise OSError("no exe")

        monkeypatch.setattr(hotkey.subprocess, "Popen", _boom)
        hotkey._do_open_code("http://x:8034", "caly", None)
        assert self.flashed[-1] == "F2: failed - see hotkey.log"

    def test_a_dead_flash_endpoint_never_breaks_the_open(self, monkeypatch):
        # Feedback is strictly best-effort: an old host with no /api/flash
        # route (or a server that just died) must still open VS Code.
        import contextlib
        import io
        from urllib.error import URLError

        from magent import hotkey

        spawned: list[list[str]] = []
        monkeypatch.setattr(hotkey.shutil, "which", lambda _n: "code")
        monkeypatch.setattr(hotkey.subprocess, "Popen", spawned.append)
        body = json.dumps(
            {"ok": True, "sessions": [{"session": "caly", "resolved": "/base/caly"}]}
        ).encode()

        @contextlib.contextmanager
        def _flaky(url, timeout=None):
            if "/api/flash" in url:
                raise URLError("404")
            yield io.BytesIO(body)

        monkeypatch.setattr(hotkey, "urlopen", _flaky)
        hotkey._do_open_code("http://x:8034", "caly", None)
        assert spawned == [["code", "/base/caly"]]

    def test_flash_url_is_the_shared_builder_shape(self, monkeypatch):
        # Pin the client/server contract: the listener must hit the same route
        # upload_server serves, with the project it was invoked for.
        from magent import hotkey

        seen: list[str] = []

        def _capture(url, timeout=None):
            seen.append(url)
            raise OSError("stop here")

        monkeypatch.setattr(hotkey.shutil, "which", lambda _n: None)
        monkeypatch.setattr(hotkey, "urlopen", _capture)
        hotkey._do_open_code("http://127.0.0.1:8033", "caly", None)
        assert seen[0].startswith("http://127.0.0.1:8033/api/flash?")
        assert parse_qs(urlparse(seen[0]).query)["project"] == ["caly"]


class TestF2HookDecision:
    def _lparam(self, vk_code):
        import ctypes

        from magent.hotkey import KBDLLHOOKSTRUCT

        kb = KBDLLHOOKSTRUCT(
            vkCode=vk_code, scanCode=0, flags=0, time=0, dwExtraInfo=None
        )
        # Keep the struct alive for the duration of the call.
        self._kb_ref = kb
        return ctypes.cast(ctypes.pointer(kb), ctypes.c_void_p).value

    def _fake_thread(self, monkeypatch, started):
        from magent import hotkey

        class _FakeThread:
            def __init__(self, target=None, args=(), daemon=None):
                self.target, self.args = target, args

            def start(self):
                started.append((self.target, self.args))

        monkeypatch.setattr(hotkey.threading, "Thread", _FakeThread)

    def test_f2_in_a_magent_window_is_swallowed_and_handled(self, monkeypatch):
        from magent import hotkey
        from magent.hotkey import HC_ACTION, VK_F2, WM_KEYDOWN, _hook_decide

        monkeypatch.setattr(hotkey, "get_active_window_title", lambda: "magent:caly")
        started: list[tuple[object, tuple[object, ...]]] = []
        self._fake_thread(monkeypatch, started)

        result = _hook_decide(
            {"alt_held": False},
            "http://x:8034",
            HC_ACTION,
            WM_KEYDOWN,
            self._lparam(VK_F2),
            "amin@deck",
        )
        # 1 == swallow: the agent pane must never also receive the F2.
        assert result == 1
        assert started[0][0] is hotkey._do_open_code
        assert started[0][1] == ("http://x:8034", "caly", "amin@deck")

    def test_f2_outside_a_magent_window_passes_through(self, monkeypatch):
        from magent import hotkey
        from magent.hotkey import HC_ACTION, VK_F2, WM_KEYDOWN, _hook_decide

        monkeypatch.setattr(hotkey, "get_active_window_title", lambda: "Notepad")
        started: list[tuple[object, tuple[object, ...]]] = []
        self._fake_thread(monkeypatch, started)
        monkeypatch.setattr(hotkey.user32, "CallNextHookEx", lambda *a: 0)

        assert (
            _hook_decide(
                {"alt_held": False},
                "http://x:8034",
                HC_ACTION,
                WM_KEYDOWN,
                self._lparam(VK_F2),
            )
            == 0
        )
        assert started == []

    def test_alt_v_still_routes_to_the_uploader(self, monkeypatch):
        # Regression guard: the F2 branch sits above the Alt+V branch.
        from magent import hotkey
        from magent.hotkey import HC_ACTION, VK_V, WM_KEYDOWN, _hook_decide

        monkeypatch.setattr(hotkey, "get_active_window_title", lambda: "magent:caly")
        monkeypatch.setattr(hotkey, "clipboard_has_image", lambda: True)
        started: list[tuple[object, tuple[object, ...]]] = []
        self._fake_thread(monkeypatch, started)

        assert (
            _hook_decide(
                {"alt_held": True},
                "http://x:8034",
                HC_ACTION,
                WM_KEYDOWN,
                self._lparam(VK_V),
            )
            == 1
        )
        assert started[0][0] is hotkey._do_upload

    def test_alt_v_with_no_clipboard_image_reports_instead_of_going_quiet(
        self, monkeypatch
    ):
        # The exact "I pressed it in the right window and nothing happened"
        # case. The chord still PASSES THROUGH (the pane may want a plain
        # Alt+V) -- but the user is told why nothing was uploaded.
        from magent import hotkey
        from magent.hotkey import HC_ACTION, VK_V, WM_KEYDOWN, _hook_decide

        monkeypatch.setattr(hotkey, "get_active_window_title", lambda: "magent:caly")
        monkeypatch.setattr(hotkey, "clipboard_has_image", lambda: False)
        monkeypatch.setattr(hotkey.user32, "CallNextHookEx", lambda *a: 0)
        started: list[tuple[object, tuple[object, ...]]] = []
        self._fake_thread(monkeypatch, started)

        assert (
            _hook_decide(
                {"alt_held": True},
                "http://x:8034",
                HC_ACTION,
                WM_KEYDOWN,
                self._lparam(VK_V),
            )
            == 0
        )
        assert started[0][0] is hotkey._altv_report
        assert started[0][1][:3] == ("http://x:8034", "caly", "no-image")
        assert "clipboard has no image" in started[0][1][3]

    def test_alt_v_outside_a_magent_window_stays_a_silent_pass_through(
        self, monkeypatch
    ):
        # Not a failure -- the chord belongs to whatever app is focused. It is
        # recorded at DEBUG only; reporting it would fire on every Alt+V the
        # user ever presses anywhere.
        from magent import hotkey
        from magent.hotkey import HC_ACTION, VK_V, WM_KEYDOWN, _hook_decide

        monkeypatch.setattr(hotkey, "get_active_window_title", lambda: "Notepad")
        monkeypatch.setattr(hotkey.user32, "CallNextHookEx", lambda *a: 0)
        started: list[tuple[object, tuple[object, ...]]] = []
        self._fake_thread(monkeypatch, started)

        assert (
            _hook_decide(
                {"alt_held": True},
                "http://x:8034",
                HC_ACTION,
                WM_KEYDOWN,
                self._lparam(VK_V),
            )
            == 0
        )
        assert started == []


class TestAltVOutcomeReporting:
    """Every Alt+V press ends in exactly one greppable ``ALTV outcome=...``
    line, and every FAILURE also reaches the user's screen through the flash
    channel.

    The listener runs hidden with no terminal, so a press that silently does
    nothing is indistinguishable from a listener that is not running -- which
    is the whole "we don't know when Alt+V doesn't work" complaint. Silence was
    the old contract here (``test_no_image_is_a_silent_noop``); it is now a
    regression.
    """

    def _flashes(self, monkeypatch):
        seen: list[tuple[str, str, str]] = []
        from magent import hotkey

        monkeypatch.setattr(
            hotkey,
            "_flash_status",
            lambda url, project, message: seen.append((url, project, message)),
        )
        return seen

    def test_success_logs_ok_and_flashes_nothing(self, monkeypatch, caplog):
        from magent import hotkey

        monkeypatch.setattr(hotkey, "get_clipboard_image", lambda: b"FAKEBMP")
        monkeypatch.setattr(hotkey, "upload_image", lambda url, project, data: True)
        flashes = self._flashes(monkeypatch)

        with caplog.at_level("INFO", logger="magent.hotkey"):
            hotkey._do_upload("http://x:8034", "marka")

        assert "ALTV outcome=ok project=marka" in caplog.text
        # The server drives the progress line on the happy path; a second
        # message from the listener would double-report it.
        assert flashes == []

    def test_rejected_upload_is_logged_and_shown(self, monkeypatch, caplog):
        from magent import hotkey

        monkeypatch.setattr(hotkey, "get_clipboard_image", lambda: b"FAKEBMP")
        monkeypatch.setattr(hotkey, "upload_image", lambda url, project, data: False)
        flashes = self._flashes(monkeypatch)

        with caplog.at_level("INFO", logger="magent.hotkey"):
            hotkey._do_upload("http://x:8034", "marka")

        assert "ALTV outcome=upload-rejected project=marka" in caplog.text
        assert flashes == [
            (
                "http://x:8034",
                "marka",
                "Alt+V: upload failed - is `magent serve` running?",
            )
        ]

    def test_unreadable_clipboard_is_logged_and_shown_not_silent(
        self, monkeypatch, caplog
    ):
        # clipboard_has_image() said yes at the hook, so an empty read is a
        # real failure -- it used to return with no trace at all.
        from magent import hotkey

        monkeypatch.setattr(hotkey, "get_clipboard_image", lambda: None)
        called = []
        monkeypatch.setattr(hotkey, "upload_image", lambda *a: called.append(a))
        flashes = self._flashes(monkeypatch)

        with caplog.at_level("INFO", logger="magent.hotkey"):
            hotkey._do_upload("http://x:8034", "marka")

        assert called == []  # still never uploads a non-image
        assert "ALTV outcome=clipboard-unreadable project=marka" in caplog.text
        assert len(flashes) == 1

    def test_unexpected_error_is_caught_logged_and_shown_not_raised(
        self, monkeypatch, caplog
    ):
        from magent import hotkey

        def _boom():
            raise OverflowError("byte must be in range(0, 256)")

        monkeypatch.setattr(hotkey, "get_clipboard_image", _boom)
        flashes = self._flashes(monkeypatch)

        with caplog.at_level("INFO", logger="magent.hotkey"):
            hotkey._do_upload("http://x:8034", "marka")  # must not raise

        assert "ALTV outcome=error project=marka" in caplog.text
        assert "OverflowError" in caplog.text  # the traceback rides along
        assert len(flashes) == 1

    def test_a_broken_flash_channel_cannot_break_the_report(self, monkeypatch, caplog):
        # Feedback must never be able to break the action it reports on --
        # _flash_status swallows everything, so a dead server is still logged.
        from magent import hotkey

        monkeypatch.setattr(hotkey, "get_clipboard_image", lambda: b"FAKEBMP")
        monkeypatch.setattr(hotkey, "upload_image", lambda url, project, data: False)

        with caplog.at_level("INFO", logger="magent.hotkey"):
            hotkey._do_upload("http://127.0.0.1:1", "marka")  # nothing listening

        assert "ALTV outcome=upload-rejected project=marka" in caplog.text

    def test_every_outcome_name_is_declared(self):
        # The vocabulary is closed on purpose: `grep 'ALTV outcome=no-image'`
        # has to keep working as a diagnosis, not just `grep ALTV`.
        from magent import hotkey

        assert set(hotkey.ALTV_OUTCOMES) == {
            "ok",
            "not-a-magent-window",
            "no-image",
            "clipboard-unreadable",
            "upload-rejected",
            "error",
        }


class TestHeartbeatWiring:
    """Heartbeat FILE semantics (freshness/staleness) are already covered
    cross-platform in test_log.py; here we assert only that run_hotkey's
    heartbeat thread is wired to write_heartbeat("hotkey") -- without
    spinning a real message loop (GetMessageW needs a real hook)."""

    def test_heartbeat_loop_writes_and_stops_on_event(self, monkeypatch):
        from magent import hotkey

        calls = []
        monkeypatch.setattr(hotkey, "write_heartbeat", calls.append)
        monkeypatch.setattr(hotkey, "HEARTBEAT_INTERVAL", 0.01)  # don't wait a real 10s

        stop_event = threading.Event()
        t = threading.Thread(
            target=hotkey._heartbeat_loop, args=(stop_event,), daemon=True
        )
        t.start()
        time.sleep(0.1)
        stop_event.set()
        t.join(timeout=2)

        assert not t.is_alive()  # stops promptly once the event is set
        assert calls.count("hotkey") >= 1


class TestMaybeStartHotkey:
    """attach starts the listener in the background, never a second copy --
    but a listener that no longer matches this version/target is replaced
    rather than kept (see launch.hotkey_restart_reason)."""

    @pytest.fixture(autouse=True)
    def _never_touch_the_real_listener(self, monkeypatch):
        """The starter now reads a manifest and can taskkill a pid, so both
        are stubbed for every test here -- an unstubbed run would read (and
        kill) the developer's own live listener."""
        from magent import hotkey, launch

        monkeypatch.setattr(hotkey, "listener_manifest", lambda: None)
        monkeypatch.setattr(hotkey, "stop_listener", lambda: True)
        monkeypatch.setattr(launch.time, "sleep", lambda _s: None)

    @staticmethod
    def _manifest(server_url="http://x:8034", ssh_host=None, version=None):
        from magent import __version__

        return {
            "version": version or __version__,
            "server_url": server_url,
            "ssh_host": ssh_host,
        }

    def test_returns_matching_listener_without_spawning(self, monkeypatch):
        from magent import cli, hotkey

        monkeypatch.setattr(hotkey, "listener_pid", lambda: 1234)
        monkeypatch.setattr(hotkey, "listener_manifest", self._manifest)
        killed = []
        monkeypatch.setattr(hotkey, "stop_listener", lambda: killed.append(True))
        spawned = []
        monkeypatch.setattr(
            "magent.launch.spawn_detached", lambda *a, **k: spawned.append(a)
        )
        assert cli._maybe_start_hotkey("http://x:8034") == 1234
        # Idempotent: attach re-runs this on every attach, and a needless
        # restart drops the keyboard hook for a moment.
        assert spawned == [] and killed == []

    def test_spawns_when_none_running(self, monkeypatch):
        from magent import cli, hotkey

        state = {"pid": None}
        monkeypatch.setattr(hotkey, "listener_pid", lambda: state["pid"])
        killed = []
        monkeypatch.setattr(hotkey, "stop_listener", lambda: killed.append(True))

        def fake_spawn(args, *a, **k):
            state["pid"] = 5678  # the detached child comes up and writes its pid

        monkeypatch.setattr("magent.launch.spawn_detached", fake_spawn)
        assert cli._maybe_start_hotkey("http://x:8034") == 5678
        assert killed == []  # nothing was running, so nothing to kill

    def _restart_harness(self, monkeypatch, manifest):
        """A live listener described by `manifest`; returns (killed, spawned)."""
        from magent import hotkey

        state = {"pid": 1234}
        killed: list[int] = []
        spawned: list[list[str]] = []

        def _stop():
            killed.append(state["pid"])
            state["pid"] = None  # taskkill took; the pid file is gone
            return True

        def _spawn(args, *a, **k):
            spawned.append(args)
            state["pid"] = 5678

        monkeypatch.setattr(hotkey, "listener_pid", lambda: state["pid"])
        monkeypatch.setattr(hotkey, "listener_manifest", lambda: manifest)
        monkeypatch.setattr(hotkey, "stop_listener", _stop)
        monkeypatch.setattr("magent.launch.spawn_detached", _spawn)
        return killed, spawned

    def test_version_skew_restarts(self, monkeypatch, caplog):
        # The pip-upgrade bug: the OLD process keeps running OLD code -- an F2
        # handler it may not even have -- until someone hand-kills it.
        import logging

        from magent import cli

        killed, spawned = self._restart_harness(
            monkeypatch, self._manifest(version="0.0.1-ancient")
        )
        with caplog.at_level(logging.INFO, logger="magent.hotkey"):
            assert cli._maybe_start_hotkey("http://x:8034") == 5678
        assert killed == [1234] and len(spawned) == 1
        assert "version skew" in caplog.text  # the why is logged, not silent

    def test_missing_manifest_restarts(self, monkeypatch):
        # Any pre-3.6.0 listener: it cannot describe itself, so it is stale.
        from magent import cli

        killed, spawned = self._restart_harness(monkeypatch, None)
        assert cli._maybe_start_hotkey("http://x:8034") == 5678
        assert killed == [1234] and len(spawned) == 1

    def test_server_url_change_restarts(self, monkeypatch):
        # A loopback-wired listener (local launch) can't serve the host tailnet
        # URL `magent attach` needs.
        from magent import cli

        killed, spawned = self._restart_harness(
            monkeypatch, self._manifest(server_url="http://127.0.0.1:8034")
        )
        assert cli._maybe_start_hotkey("http://host.tailnet:8034") == 5678
        assert killed == [1234]
        assert "http://host.tailnet:8034" in spawned[0]

    def test_ssh_host_change_restarts_and_forwards_the_new_target(
        self, monkeypatch, caplog
    ):
        # Same bug, other direction: F2 must open the folder on the machine the
        # magent: windows are actually attached to.
        import logging

        from magent import cli

        killed, spawned = self._restart_harness(monkeypatch, self._manifest())
        with caplog.at_level(logging.INFO, logger="magent.hotkey"):
            assert cli._maybe_start_hotkey("http://x:8034", "mdssh") == 5678
        assert killed == [1234]
        assert "--ssh-host" in spawned[0] and "mdssh" in spawned[0]
        assert "ssh_host" in caplog.text

    def test_a_kill_that_did_not_take_reports_no_listener(self, monkeypatch):
        # If the old pid survives taskkill, the wait loop must not read it back
        # as "the new listener came up" -- that would report a stale listener
        # as freshly started.
        from magent import cli, hotkey

        monkeypatch.setattr(hotkey, "listener_pid", lambda: 1234)
        monkeypatch.setattr(hotkey, "listener_manifest", lambda: None)
        monkeypatch.setattr(hotkey, "stop_listener", lambda: False)
        monkeypatch.setattr("magent.launch.spawn_detached", lambda *a, **k: None)
        assert cli._maybe_start_hotkey("http://x:8034") is None


class TestHookStructsAndConstants:
    def test_kbdllhookstruct_size(self):
        import ctypes

        from magent.hotkey import KBDLLHOOKSTRUCT

        size = ctypes.sizeof(KBDLLHOOKSTRUCT)
        assert size > 0

    def test_constants(self):
        from magent.hotkey import CF_DIB, VK_MENU, VK_V, WH_KEYBOARD_LL

        assert VK_V == 0x56
        assert VK_MENU == 0x12
        assert CF_DIB == 8
        assert WH_KEYBOARD_LL == 13

    def test_hookproc_type(self):
        from magent.hotkey import HOOKPROC

        assert HOOKPROC is not None


class TestHookProc:
    """_hook_decide (pure decision logic) and _make_hook_proc (the
    exception-safe wrap around it) -- extracted so the callback that runs on
    every keystroke system-wide is unit-testable without a live hook."""

    @staticmethod
    def _kb(vk_code):
        from magent.hotkey import KBDLLHOOKSTRUCT

        return KBDLLHOOKSTRUCT(
            vkCode=vk_code, scanCode=0, flags=0, time=0, dwExtraInfo=None
        )

    def test_decide_eats_altv_in_md_window(self, monkeypatch):
        import ctypes

        from magent import hotkey
        from magent.hotkey import HC_ACTION, VK_V, WM_KEYDOWN, _hook_decide

        kb = self._kb(VK_V)
        lparam = ctypes.cast(ctypes.pointer(kb), ctypes.c_void_p).value

        monkeypatch.setattr(hotkey, "get_active_window_title", lambda: "magent:marka")
        monkeypatch.setattr(hotkey, "clipboard_has_image", lambda: True)

        started = []

        class _FakeThread:
            def __init__(self, target=None, args=(), daemon=None):
                self.target, self.args = target, args

            def start(self):
                started.append((self.target, self.args))

        monkeypatch.setattr(hotkey.threading, "Thread", _FakeThread)

        state = {"alt_held": True}
        result = _hook_decide(state, "http://x:8034", HC_ACTION, WM_KEYDOWN, lparam)

        assert result == 1
        assert started  # a thread was started
        assert started[0][1] == ("http://x:8034", "marka")

    def test_wrap_calls_callnext_on_exception(self, monkeypatch):
        # The hook callback runs in a ctypes WINFUNCTYPE callback: an
        # uncaught exception can't cross the C boundary, so CPython prints
        # the traceback and returns the restype default -- silently breaking
        # the rest of the hook chain for that event. The wrap must always
        # call CallNextHookEx itself instead of relying on that fallback.
        from magent import hotkey

        def _boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(hotkey, "_hook_decide", _boom)

        calls = []

        def _fake_call_next(*args):
            calls.append(args)
            return 999

        monkeypatch.setattr(hotkey.user32, "CallNextHookEx", _fake_call_next)

        hook_proc = hotkey._make_hook_proc({"alt_held": False}, "url")
        result = hook_proc(0, 0, 0)

        assert calls  # CallNextHookEx was still called
        assert result == 999  # and its return value is what's passed through

    def test_run_hotkey_signature_has_no_session_names(self):
        import inspect

        from magent.hotkey import run_hotkey

        # The listener resolves projects from window titles + the server's
        # /api/sessions, never from a snapshot handed in at start-up. ssh_host
        # is the attach target F2 opens through, not a session list.
        assert set(inspect.signature(run_hotkey).parameters) == {
            "server_url",
            "ssh_host",
        }


class TestFocusDecide:
    """The pure decision behind the focus geometry reclaim. Same split as
    `_hook_decide`: the code that runs on every foreground change system-wide
    is reachable here without a live hook, a message loop, or a real window."""

    @staticmethod
    def _decide(title, last_nudge, now, *, mouse_down=False):
        from magent.hotkey import _focus_decide

        return _focus_decide(title, last_nudge, now, lambda: mouse_down)

    def test_magent_window_is_nudged_and_stamped(self):
        last = {}
        assert self._decide("magent:caly", last, 100.0) == "caly"
        assert last == {"caly": 100.0}

    def test_state_badge_in_the_title_still_resolves(self):
        # The titles grammar is the gate -- a badged title (titles.make_title)
        # must not read as "not one of ours".
        from magent.titles import make_title

        last = {}
        title = make_title("caly", state="needs-input")
        assert self._decide(title, last, 100.0) == "caly"

    def test_foreign_window_is_skipped_and_stamps_nothing(self):
        last = {}
        assert self._decide("Notepad", last, 100.0) is None
        assert self._decide("", last, 100.0) is None
        assert last == {}

    def test_second_focus_inside_the_debounce_is_skipped(self):
        from magent.hotkey import FOCUS_NUDGE_DEBOUNCE_S

        last = {}
        assert self._decide("magent:caly", last, 100.0) == "caly"
        # Alt-tabbing back and forth must not storm nudges.
        assert self._decide("magent:caly", last, 100.0) is None
        assert self._decide("magent:caly", last, 100.0 + 0.5) is None
        assert (
            self._decide("magent:caly", last, 100.0 + FOCUS_NUDGE_DEBOUNCE_S - 0.01)
            is None
        )
        assert last == {"caly": 100.0}  # the stamp never moved

    def test_focus_after_the_debounce_expires_nudges_again(self):
        from magent.hotkey import FOCUS_NUDGE_DEBOUNCE_S

        last = {}
        self._decide("magent:caly", last, 100.0)
        later = 100.0 + FOCUS_NUDGE_DEBOUNCE_S
        assert self._decide("magent:caly", last, later) == "caly"
        assert last == {"caly": later}

    def test_separate_windows_debounce_independently(self):
        last = {}
        assert self._decide("magent:caly", last, 100.0) == "caly"
        # marka has never been nudged: caly's fresh stamp must not silence it.
        assert self._decide("magent:marka", last, 100.5) == "marka"
        assert self._decide("magent:caly", last, 101.0) is None
        assert last == {"caly": 100.0, "marka": 100.5}

    def test_mouse_down_skips_without_burning_the_debounce(self):
        # Never fight a user mid-drag/mid-resize -- and because the skip stamps
        # nothing, the very next focus event reclaims instead of waiting 15s.
        last = {}
        assert self._decide("magent:caly", last, 100.0, mouse_down=True) is None
        assert last == {}
        assert self._decide("magent:caly", last, 100.1) == "caly"

    def test_mouse_probe_is_not_consulted_for_foreign_windows(self):
        # The title gate is first: a click anywhere else on the desktop must not
        # even cost a GetAsyncKeyState round trip.
        from magent.hotkey import _focus_decide

        probed = []

        def _probe():
            probed.append(1)
            return False

        assert _focus_decide("Notepad", {}, 100.0, _probe) is None
        assert probed == []

    def test_default_probe_reads_the_mouse_buttons(self, monkeypatch):
        # The production default is the real GetAsyncKeyState probe: assert the
        # down-bit is what it looks at, so a signed-short return (the API
        # reports "down" as the 0x8000 bit, i.e. a negative c_short) reads as
        # down and not as "no button".
        from magent import hotkey

        asked = []

        def _fake_get_async_key_state(vk):
            asked.append(vk)
            return -32768 if vk == hotkey.VK_LBUTTON else 0

        monkeypatch.setattr(
            hotkey.user32, "GetAsyncKeyState", _fake_get_async_key_state
        )
        assert hotkey._mouse_button_down() is True
        assert hotkey.VK_LBUTTON in asked

        monkeypatch.setattr(hotkey.user32, "GetAsyncKeyState", lambda vk: 0)
        assert hotkey._mouse_button_down() is False

        # And that default is what _focus_decide uses when nothing is injected.
        monkeypatch.setattr(hotkey.user32, "GetAsyncKeyState", lambda vk: -32768)
        assert hotkey._focus_decide("magent:caly", {}, 100.0) is None


class TestDoNudge:
    """`_do_nudge` reuses the platform primitive `magent attach` reclaims with
    -- it must not reimplement MoveWindow arithmetic of its own."""

    def _platform(self, monkeypatch, plat):
        import magent.platform

        monkeypatch.setattr(magent.platform, "get_platform", lambda: plat)

    def test_delegates_the_handle_to_the_platform_nudge(self, monkeypatch, caplog):
        from magent import hotkey
        from tests.conftest import FakePlatform

        plat = FakePlatform(supports_nudge=True)
        self._platform(monkeypatch, plat)
        with caplog.at_level("INFO", logger="magent.hotkey"):
            hotkey._do_nudge(4242, "caly")
        assert plat.nudged == [[4242]]
        assert "focus nudge project=caly" in caplog.text

    def test_platform_without_nudge_support_is_a_noop(self, monkeypatch):
        from magent import hotkey
        from tests.conftest import FakePlatform

        plat = FakePlatform()  # supports_nudge=False
        self._platform(monkeypatch, plat)
        hotkey._do_nudge(1, "caly")
        assert plat.nudged == []

    def test_a_failing_nudge_is_logged_not_raised(self, monkeypatch, caplog):
        # It runs on a daemon thread: an exception here would vanish into an
        # invisible stderr, so the log line is the only record there can be.
        from magent import hotkey
        from tests.conftest import FakePlatform

        plat = FakePlatform(
            supports_nudge=True, nudge_error=OSError("invalid window handle")
        )
        self._platform(monkeypatch, plat)
        with caplog.at_level("ERROR", logger="magent.hotkey"):
            hotkey._do_nudge(1, "caly")
        assert "focus nudge project=caly failed" in caplog.text


class TestFocusEventProc:
    """The EVENT_SYSTEM_FOREGROUND callback: dispatch off the hook thread, and
    never let an exception cross the ctypes boundary."""

    def _fake_thread(self, monkeypatch, started):
        from magent import hotkey

        class _FakeThread:
            def __init__(self, target=None, args=(), daemon=None):
                self.target, self.args, self.daemon = target, args, daemon

            def start(self):
                started.append((self.target, self.args, self.daemon))

        monkeypatch.setattr(hotkey.threading, "Thread", _FakeThread)

    @staticmethod
    def _fire(proc, hwnd):
        from magent.hotkey import EVENT_SYSTEM_FOREGROUND

        proc(1, EVENT_SYSTEM_FOREGROUND, hwnd, 0, 0, 0, 0)

    def test_dispatches_the_event_hwnd_to_a_worker_thread(self, monkeypatch):
        from magent import hotkey

        monkeypatch.setattr(hotkey, "window_title", lambda hwnd: "magent:caly")
        monkeypatch.setattr(hotkey.user32, "GetAsyncKeyState", lambda vk: 0)
        started = []
        self._fake_thread(monkeypatch, started)

        self._fire(hotkey._make_win_event_proc({}), 4242)

        # The HWND comes from the event, not from a second GetForegroundWindow
        # query that could race it.
        assert started == [(hotkey._do_nudge, (4242, "caly"), True)]

    def test_foreign_window_starts_nothing(self, monkeypatch):
        from magent import hotkey

        monkeypatch.setattr(hotkey, "window_title", lambda hwnd: "Notepad")
        monkeypatch.setattr(hotkey.user32, "GetAsyncKeyState", lambda vk: 0)
        started = []
        self._fake_thread(monkeypatch, started)

        self._fire(hotkey._make_win_event_proc({}), 4242)
        assert started == []

    def test_null_hwnd_is_ignored(self, monkeypatch):
        # SetWinEventHook can deliver events with no window; asking for that
        # window's title would be a wasted round trip at best.
        from magent import hotkey

        titled = []

        def _title(hwnd):
            titled.append(hwnd)
            return "magent:caly"

        monkeypatch.setattr(hotkey, "window_title", _title)
        started = []
        self._fake_thread(monkeypatch, started)

        self._fire(hotkey._make_win_event_proc({}), 0)
        self._fire(hotkey._make_win_event_proc({}), None)
        assert titled == []
        assert started == []

    def test_debounce_state_persists_across_events(self, monkeypatch):
        # One dict lives in the closure for the listener's whole life -- so two
        # focus events in quick succession produce exactly one nudge.
        from magent import hotkey

        monkeypatch.setattr(hotkey, "window_title", lambda hwnd: "magent:caly")
        monkeypatch.setattr(hotkey.user32, "GetAsyncKeyState", lambda vk: 0)
        started = []
        self._fake_thread(monkeypatch, started)

        proc = hotkey._make_win_event_proc({})
        self._fire(proc, 4242)
        self._fire(proc, 4242)
        assert len(started) == 1

    def test_callback_exception_never_propagates(self, monkeypatch, caplog):
        # A ctypes WINFUNCTYPE callback cannot carry a Python exception across
        # the C boundary: without this guard a bad event would dump a traceback
        # to a hidden daemon's invisible stderr and take the hook with it.
        from magent import hotkey

        def _boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(hotkey, "_focus_decide", _boom)
        proc = hotkey._make_win_event_proc({})
        with caplog.at_level("ERROR", logger="magent.hotkey"):
            self._fire(proc, 4242)  # must not raise
        assert "foreground-event callback error" in caplog.text

    def test_the_proc_survives_the_ctypes_round_trip(self, monkeypatch):
        # Realism check on the WINEVENTPROC signature itself: wrapping the
        # callback and calling it through ctypes proves the argument types line
        # up with what Windows will actually deliver.
        from magent import hotkey
        from magent.hotkey import EVENT_SYSTEM_FOREGROUND, WINEVENTPROC

        monkeypatch.setattr(hotkey, "window_title", lambda hwnd: "magent:caly")
        monkeypatch.setattr(hotkey.user32, "GetAsyncKeyState", lambda vk: 0)
        started = []
        self._fake_thread(monkeypatch, started)

        trampoline = WINEVENTPROC(hotkey._make_win_event_proc({}))
        trampoline(1, EVENT_SYSTEM_FOREGROUND, 4242, 0, 0, 0, 0)
        assert started[0][1] == (4242, "caly")


class TestFocusHookLifecycle:
    """The event hook is registered and unregistered alongside the keyboard
    hook, and the existing message loop pumps both."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        from magent import hotkey

        monkeypatch.setattr(hotkey, "_PID_PATH", tmp_path / "hotkey.pid")
        monkeypatch.setattr(hotkey, "_MANIFEST_PATH", tmp_path / "hotkey.json")
        monkeypatch.setattr(hotkey, "write_heartbeat", lambda _n: None)

    def _drive(self, monkeypatch, *, event_hook=7):
        """Run run_hotkey against a fully faked user32 (never the real desktop:
        no hook is installed and no window is touched). Returns the call log."""
        from magent import hotkey

        calls = []

        class _FakeUser32:
            def SetWindowsHookExW(self, *a):
                calls.append(("SetWindowsHookExW", a))
                return 1

            def SetWinEventHook(self, *a):
                calls.append(("SetWinEventHook", a))
                return event_hook

            def GetMessageW(self, *a):
                return 0  # loop exits immediately

            def UnhookWindowsHookEx(self, *a):
                calls.append(("UnhookWindowsHookEx", a))
                return 1

            def UnhookWinEvent(self, *a):
                calls.append(("UnhookWinEvent", a))
                return 1

        monkeypatch.setattr(hotkey, "user32", _FakeUser32())
        hotkey.run_hotkey("http://127.0.0.1:8034")
        return calls

    def test_foreground_hook_installed_and_removed_in_the_lifecycle(self, monkeypatch):
        from magent.hotkey import EVENT_SYSTEM_FOREGROUND, WINEVENT_OUTOFCONTEXT

        calls = self._drive(monkeypatch)
        names = [name for name, _ in calls]
        assert names == [
            "SetWindowsHookExW",
            "SetWinEventHook",
            "UnhookWindowsHookEx",
            "UnhookWinEvent",  # same finally as the keyboard hook
        ]
        args = dict(calls)["SetWinEventHook"]
        # Exactly the one event, delivered out-of-context (a pure-Python
        # listener cannot host an in-context hook).
        assert args[0] == EVENT_SYSTEM_FOREGROUND
        assert args[1] == EVENT_SYSTEM_FOREGROUND
        assert args[2] is None  # hmodWinEventProc
        assert args[4:] == (0, 0, WINEVENT_OUTOFCONTEXT)  # all processes/threads

    def test_the_unhook_gets_the_handle_that_was_returned(self, monkeypatch):
        calls = self._drive(monkeypatch, event_hook=31337)
        assert dict(calls)["UnhookWinEvent"] == (31337,)

    def test_a_refused_event_hook_still_leaves_altv_working(self, monkeypatch, caplog):
        # The focus reclaim is a bonus, not the product: a listener that got its
        # keyboard hook must keep Alt+V and F2 even if SetWinEventHook refuses.
        with caplog.at_level("WARNING", logger="magent.hotkey"):
            calls = self._drive(monkeypatch, event_hook=0)
        names = [name for name, _ in calls]
        assert "SetWindowsHookExW" in names
        assert "UnhookWinEvent" not in names  # nothing to unhook
        assert "focus geometry reclaim disabled" in caplog.text

    def test_the_callback_trampoline_outlives_the_hook(self, monkeypatch):
        # A WINEVENTPROC that gets garbage-collected while the hook is live is
        # a crash waiting for the next foreground change, so the trampoline has
        # to be a local of the frame that owns the message loop.
        import inspect

        from magent.hotkey import run_hotkey

        source = inspect.getsource(run_hotkey)
        assert "event_fn = WINEVENTPROC(" in source
