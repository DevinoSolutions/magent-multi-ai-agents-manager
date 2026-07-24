import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

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


class TestDoUploadLogging:
    """_do_upload discards the upload result (F-IC-003/F-D4-001) no longer --
    it now logs the outcome, and an unexpected error (e.g. the OverflowError
    _dib_to_bmp can raise) is caught and logged instead of vanishing on the
    background thread it runs on."""

    def test_logs_info_with_project_and_result(self, monkeypatch, caplog):
        from magent import hotkey

        monkeypatch.setattr(hotkey, "get_clipboard_image", lambda: b"FAKEBMP")
        monkeypatch.setattr(hotkey, "upload_image", lambda url, project, data: True)

        with caplog.at_level("INFO", logger="magent.hotkey"):
            hotkey._do_upload("http://x:8034", "marka")

        assert "project=marka" in caplog.text
        assert "ok=True" in caplog.text

    def test_logs_ok_false_on_failed_upload(self, monkeypatch, caplog):
        from magent import hotkey

        monkeypatch.setattr(hotkey, "get_clipboard_image", lambda: b"FAKEBMP")
        monkeypatch.setattr(hotkey, "upload_image", lambda url, project, data: False)

        with caplog.at_level("INFO", logger="magent.hotkey"):
            hotkey._do_upload("http://x:8034", "marka")

        assert "ok=False" in caplog.text

    def test_no_image_is_a_silent_noop(self, monkeypatch, caplog):
        from magent import hotkey

        monkeypatch.setattr(hotkey, "get_clipboard_image", lambda: None)
        called = []
        monkeypatch.setattr(hotkey, "upload_image", lambda *a: called.append(a))

        with caplog.at_level("INFO", logger="magent.hotkey"):
            hotkey._do_upload("http://x:8034", "marka")

        assert called == []
        assert "project=marka" not in caplog.text

    def test_unexpected_error_is_caught_and_logged_not_raised(
        self, monkeypatch, caplog
    ):
        from magent import hotkey

        def _boom():
            raise OverflowError("byte must be in range(0, 256)")

        monkeypatch.setattr(hotkey, "get_clipboard_image", _boom)

        with caplog.at_level("INFO", logger="magent.hotkey"):
            hotkey._do_upload("http://x:8034", "marka")  # must not raise

        assert "upload project=marka failed" in caplog.text


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
    """attach starts the listener in the background, never a second copy."""

    def test_returns_existing_without_spawning(self, monkeypatch):
        from magent import cli, hotkey

        monkeypatch.setattr(hotkey, "listener_pid", lambda: 1234)
        spawned = []
        monkeypatch.setattr(
            "magent.launch.spawn_detached", lambda *a, **k: spawned.append(a)
        )
        assert cli._maybe_start_hotkey("http://x:8034") == 1234
        assert spawned == []  # an already-running listener isn't duplicated

    def test_spawns_when_none_running(self, monkeypatch):
        from magent import cli, hotkey

        state = {"pid": None}
        monkeypatch.setattr(hotkey, "listener_pid", lambda: state["pid"])

        def fake_spawn(args, *a, **k):
            state["pid"] = 5678  # the detached child comes up and writes its pid

        monkeypatch.setattr("magent.launch.spawn_detached", fake_spawn)
        assert cli._maybe_start_hotkey("http://x:8034") == 5678


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

        assert set(inspect.signature(run_hotkey).parameters) == {"server_url"}
