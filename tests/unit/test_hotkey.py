import json
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")


class TestDibToPng:
    """Clipboard DIB -> PNG encoding (the 1.7 MB-per-screenshot fix).

    A wrong image is worse than a big one, so every shape the encoder does not
    positively recognize must come back None (BMP fallback), and every shape it
    does must round-trip pixel-exactly -- the decode below is a real chunk
    parse + zlib inflate, not a prefix check."""

    @staticmethod
    def _header(width, height, bpp, compression):
        import struct

        # Same BITMAPINFOHEADER builder as TestDibToBmp (duplicated: that
        # class is defined further down this module).
        return struct.pack(
            "<IiiHHIIiiII", 40, width, height, 1, bpp, compression, 0, 0, 0, 0, 0
        )

    @staticmethod
    def _decode(png):
        import struct
        import zlib

        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        off, chunks = 8, {}
        while off < len(png):
            ln = int.from_bytes(png[off : off + 4], "big")
            tag = bytes(png[off + 4 : off + 8])
            payload = png[off + 8 : off + 8 + ln]
            crc = int.from_bytes(png[off + 8 + ln : off + 12 + ln], "big")
            assert crc == zlib.crc32(tag + payload) & 0xFFFFFFFF
            chunks[tag] = chunks.get(tag, b"") + payload
            off += 12 + ln
        w, h, depth, color, comp, filt, interlace = struct.unpack(
            ">IIBBBBB", chunks[b"IHDR"]
        )
        assert (depth, color, comp, filt, interlace) == (8, 2, 0, 0, 0)
        raw = zlib.decompress(chunks[b"IDAT"])
        stride = 1 + w * 3
        assert len(raw) == stride * h
        rows = [raw[r * stride : (r + 1) * stride] for r in range(h)]
        assert all(r[0] == 0 for r in rows)  # filter None on every scanline
        return w, h, [bytes(r[1:]) for r in rows]

    def test_bottom_up_rgb32_round_trips_top_first(self):
        from magent.hotkey import _dib_to_png

        header = self._header(2, 2, 32, 0)  # BI_RGB, positive height=bottom-up
        bottom = bytes([255, 0, 0, 0]) + bytes([0, 255, 0, 0])  # blue, green
        top = bytes([0, 0, 255, 0]) + bytes([255, 255, 255, 0])  # red, white
        png = _dib_to_png(bytearray(header + bottom + top))
        w, h, rows = self._decode(png)
        assert (w, h) == (2, 2)
        assert rows[0] == bytes([255, 0, 0, 255, 255, 255])  # red, white (top)
        assert rows[1] == bytes([0, 0, 255, 0, 255, 0])  # blue, green

    def test_top_down_negative_height(self):
        from magent.hotkey import _dib_to_png

        header = self._header(2, -2, 32, 0)  # top-down: storage row 0 IS the top
        top = bytes([0, 0, 255, 0]) + bytes([255, 255, 255, 0])
        bottom = bytes([255, 0, 0, 0]) + bytes([0, 255, 0, 0])
        png = _dib_to_png(bytearray(header + top + bottom))
        _w, _h, rows = self._decode(png)
        assert rows[0] == bytes([255, 0, 0, 255, 255, 255])
        assert rows[1] == bytes([0, 0, 255, 0, 255, 0])

    def test_bitfields_standard_masks_accepted(self):
        import struct

        from magent.hotkey import _dib_to_png

        header = self._header(1, 1, 32, 3)  # BI_BITFIELDS
        masks = struct.pack("<III", 0x00FF0000, 0x0000FF00, 0x000000FF)
        png = _dib_to_png(bytearray(header + masks + bytes([1, 2, 3, 0])))
        _w, _h, rows = self._decode(png)
        assert rows == [bytes([3, 2, 1])]

    def test_nonstandard_masks_fall_back(self):
        import struct

        from magent.hotkey import _dib_to_png

        header = self._header(1, 1, 32, 3)
        masks = struct.pack("<III", 0x000000FF, 0x0000FF00, 0x00FF0000)  # RGBA order
        assert _dib_to_png(bytearray(header + masks + bytes(4))) is None

    def test_rgb24_stride_padding_not_leaked(self):
        from magent.hotkey import _dib_to_png

        # width=1 at 24bpp: 3 pixel bytes + 1 pad byte per row (stride 4).
        header = self._header(1, 2, 24, 0)
        pixels = bytes([1, 2, 3, 0xEE]) + bytes([4, 5, 6, 0xEE])  # pad = 0xEE
        png = _dib_to_png(bytearray(header + pixels))
        _w, _h, rows = self._decode(png)
        assert rows[0] == bytes([6, 5, 4])  # top row, BGR -> RGB
        assert rows[1] == bytes([3, 2, 1])
        assert not any(0xEE in r for r in rows)

    def test_unrecognized_shapes_fall_back_to_bmp(self):
        from magent.hotkey import _dib_to_bmp, _dib_to_png

        # 16bpp: PNG refuses, the BMP wrap still delivers -- the fallback pair.
        header16 = self._header(2, 2, 16, 0)
        pixels16 = bytes(2 * 2 * 2)
        assert _dib_to_png(bytearray(header16 + pixels16)) is None
        assert _dib_to_bmp(bytearray(header16 + pixels16)) is not None
        # RLE compression and truncated pixel buffers refuse too.
        assert _dib_to_png(bytearray(self._header(2, 2, 24, 1) + bytes(16))) is None
        assert _dib_to_png(bytearray(self._header(4, 4, 32, 0) + bytes(8))) is None


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

    def test_a_pid_file_from_before_the_boot_is_not_a_listener(
        self, tmp_path, monkeypatch
    ):
        # A restart kills the listener without letting it remove its pid file,
        # and Windows hands pid numbers out again. Believed, the recycled pid
        # is a "running" listener: serve's supervisor never starts a real one
        # (Alt+V dead after every such reboot) and status calls it STALE.
        import os

        from magent import hotkey

        p = tmp_path / "hotkey.pid"
        p.write_text("4321")
        os.utime(p, (1000.0, 1000.0))
        monkeypatch.setattr(hotkey, "_PID_PATH", p)
        monkeypatch.setattr(hotkey, "pid_alive", lambda pid: True)  # recycled
        monkeypatch.setattr("magent.procs.boot_time", lambda: 5000.0)

        assert hotkey.listener_pid() is None
        assert not p.exists()

    def test_a_pid_file_written_since_the_boot_is_read_as_before(
        self, tmp_path, monkeypatch
    ):
        from magent import hotkey

        p = tmp_path / "hotkey.pid"
        p.write_text("4321")
        monkeypatch.setattr(hotkey, "_PID_PATH", p)
        monkeypatch.setattr(hotkey, "pid_alive", lambda pid: True)
        monkeypatch.setattr("magent.procs.boot_time", lambda: 1000.0)

        assert hotkey.listener_pid() == 4321

    def test_an_unknown_boot_time_changes_nothing(self, tmp_path, monkeypatch):
        import os

        from magent import hotkey

        p = tmp_path / "hotkey.pid"
        p.write_text("4321")
        os.utime(p, (1000.0, 1000.0))
        monkeypatch.setattr(hotkey, "_PID_PATH", p)
        monkeypatch.setattr(hotkey, "pid_alive", lambda pid: True)
        monkeypatch.setattr("magent.procs.boot_time", lambda: None)

        assert hotkey.listener_pid() == 4321

    def test_stop_never_kills_a_pid_recorded_before_the_boot(
        self, tmp_path, monkeypatch
    ):
        # `down --all` after a restart must not taskkill whatever process now
        # wears the old listener's pid number.
        import os
        import subprocess

        from magent import hotkey

        p = tmp_path / "hotkey.pid"
        p.write_text("4321")
        os.utime(p, (1000.0, 1000.0))
        monkeypatch.setattr(hotkey, "_PID_PATH", p)
        monkeypatch.setattr(hotkey, "pid_alive", lambda pid: True)
        monkeypatch.setattr("magent.procs.boot_time", lambda: 5000.0)
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))

        assert hotkey.stop_listener() is False
        assert calls == []

    def test_the_supervisor_starts_a_listener_past_a_pre_boot_pid_file(
        self, tmp_path, monkeypatch
    ):
        # The consequence that matters: serve's supervision entry point spawns
        # a listener instead of keeping a recycled pid it thinks is one.
        import os

        from magent import hotkey, launch

        p = tmp_path / "hotkey.pid"
        p.write_text("4321")
        os.utime(p, (1000.0, 1000.0))
        monkeypatch.setattr(hotkey, "_PID_PATH", p)
        monkeypatch.setattr(hotkey, "_MANIFEST_PATH", tmp_path / "hotkey.json")
        # A manifest that matches exactly, as the dead listener left it: on its
        # own it would make the supervisor keep the recorded pid.
        hotkey._write_manifest("http://127.0.0.1:8034", None)
        monkeypatch.setattr(hotkey, "pid_alive", lambda pid: True)
        monkeypatch.setattr("magent.procs.boot_time", lambda: 5000.0)
        stopped = []
        monkeypatch.setattr(hotkey, "stop_listener", lambda: stopped.append(1))
        spawned = []
        monkeypatch.setattr(
            "magent.launch.spawn_detached",
            lambda args, **k: spawned.append(args) or _StillStarting(rc=1),
        )

        launch.ensure_hotkey_listener("http://127.0.0.1:8034")

        assert len(spawned) == 1
        assert "hotkey" in spawned[0]
        assert stopped == []  # nothing of ours to stop: the pid is not ours

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

    def test_forget_drops_every_trace_without_signalling_anything(
        self, tmp_path, monkeypatch
    ):
        # The wedge replacement kills through procs.terminate_verified and then
        # calls this; it must never itself reach for taskkill.
        import subprocess

        from magent import hotkey, log

        pid_file = tmp_path / "hotkey.pid"
        manifest = tmp_path / "hotkey.json"
        pid_file.write_text("4321")
        manifest.write_text("{}")
        log.write_heartbeat("hotkey")
        monkeypatch.setattr(hotkey, "_PID_PATH", pid_file)
        monkeypatch.setattr(hotkey, "_MANIFEST_PATH", manifest)

        def _no_subprocess(*a, **k):
            raise AssertionError("forget_listener must not spawn a process")

        monkeypatch.setattr(subprocess, "run", _no_subprocess)

        hotkey.forget_listener()

        assert not pid_file.exists()
        assert not manifest.exists()
        assert log.heartbeat_age("hotkey") is None
        hotkey.forget_listener()  # and it is idempotent

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


def _pump_idle() -> bool:
    """Wait for the shared flash pump to finish everything it was handed.

    A condition, not a guess at how long a delivery takes on a loaded box:
    bounded only by the pump's own worst case, every flash still pending
    running to its full ``FLASH_HTTP_TIMEOUT_S``.
    """
    from magent import altv

    pending = max(altv._flash_queue.unfinished_tasks, 1)
    deadline = time.monotonic() + pending * altv.FLASH_HTTP_TIMEOUT_S + 5.0
    while altv._flash_queue.unfinished_tasks:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


class _OpenCodeHarness:
    """Shared fake for the F2 handler's two round trips (see `_patch`)."""

    def _patch(self, monkeypatch, *, payload=None, code_bin="code"):
        import contextlib
        import io

        from magent import hotkey

        spawned: list[list[str]] = []
        self.spawn_envs: list[object] = []
        self.spawn_kwargs: list[dict[str, object]] = []
        self.flashed: list[str] = []
        self.flash_tints: list[object] = []
        monkeypatch.setattr(hotkey.shutil, "which", lambda _n: code_bin)

        def _popen(argv, **kwargs):
            spawned.append(argv)
            self.spawn_envs.append(kwargs.get("env"))
            self.spawn_kwargs.append(kwargs)

        monkeypatch.setattr(hotkey.subprocess, "Popen", _popen)

        body = json.dumps(payload if payload is not None else {}).encode()

        @contextlib.contextmanager
        def _fake_urlopen(url, timeout=None):
            assert url.endswith("/api/sessions")
            yield io.BytesIO(body)

        monkeypatch.setattr(hotkey, "urlopen", _fake_urlopen)
        # Flashes are QUEUED, never called inline (see altv.flash_async), so
        # they are recorded where they are dispatched -- delivery happens on
        # the pump thread and its timing is not this handler's contract.
        monkeypatch.setattr(
            hotkey,
            "flash_async",
            lambda url, project, message, duration_ms=None, tint=None: (
                self.flashed.append(message),
                self.flash_tints.append(tint),
            ),
        )
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

    def test_the_editor_spawn_opens_no_console_and_inherits_no_streams(
        self, monkeypatch
    ):
        # The listener is console-less (serve spawns it detached) and code.cmd
        # is a console-subsystem shim: without CREATE_NO_WINDOW Windows
        # allocates it a brand-new VISIBLE console that streams VS Code's
        # `[main ...]` logs at the user for as long as the editor runs
        # (observed live 2026-08-31; same incident family as
        # psmux._SPAWN_FLAGS). The devnull streams keep the shim from holding
        # -- or blocking on -- console handles it doesn't have.
        import subprocess

        from magent import hotkey

        self._patch(
            monkeypatch,
            payload={
                "ok": True,
                "sessions": [
                    {"name": "caly", "session": "caly", "resolved": "/base/caly"}
                ],
            },
        )
        hotkey._do_open_code("http://x:8034", "caly", None)
        (kwargs,) = self.spawn_kwargs
        assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL

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
        hotkey._do_open_code("http://x:8034", "caly", "demo@deck")
        assert spawned == [["code", "--remote", "ssh-remote+deck", "/base/caly"]]

    def test_the_editor_gets_a_scrubbed_environment(self, monkeypatch):
        # The listener is a long-lived descendant of whatever shell started
        # magent, so it carries that shell's agent-session markers for days.
        # The editor it opens hands its environment to the integrated terminal,
        # which is where a user runs `claude` -- so the same seam applies here
        # as at psmux new-session. See env.spawn_child_env.
        from magent import hotkey

        monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-keep-me")
        self._patch(
            monkeypatch,
            payload={
                "ok": True,
                "sessions": [
                    {"name": "caly", "session": "caly", "resolved": "/base/caly"}
                ],
            },
        )
        hotkey._do_open_code("http://x:8034", "caly", None)
        env = self.spawn_envs[0]
        assert env is not None
        assert "CLAUDE_CODE_CHILD_SESSION" not in env
        assert "NO_COLOR" not in env
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-keep-me"

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

        # The harness, for its recorded flashes: left real, this test's two
        # flashes went to the process-wide pump addressed to host `x` -- a real
        # DNS lookup each, ~2.7s on Windows, still in flight when later tests
        # ran and holding the pump the flash-URL pin below waits on.
        self._patch(monkeypatch)

        def _down(url, timeout=None):
            raise URLError("connection refused")

        monkeypatch.setattr(hotkey, "urlopen", _down)
        with caplog.at_level("ERROR", logger="magent.hotkey"):
            hotkey._do_open_code("http://x:8034", "caly", None)  # must not raise
        assert "F2: open project=caly failed" in caplog.text
        assert self.flashed[-1] == "F2: failed - see hotkey.log"


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

        def _boom(_argv, **_kwargs):
            raise OSError("no exe")

        monkeypatch.setattr(hotkey.subprocess, "Popen", _boom)
        hotkey._do_open_code("http://x:8034", "caly", None)
        assert self.flashed[-1] == "F2: failed - see hotkey.log"

    def test_a_dead_flash_endpoint_never_breaks_the_open(self, monkeypatch):
        # Feedback is strictly best-effort: an old host with no /api/flash
        # route (or a server that just died) must still open VS Code. Driven
        # through the REAL flash path (queue + pump + transport) against a port
        # that refuses instantly, so the whole chain is proven harmless.
        import contextlib
        import io

        from magent import altv, hotkey

        spawned: list[list[str]] = []
        monkeypatch.setattr(hotkey.shutil, "which", lambda _n: "code")
        monkeypatch.setattr(
            hotkey.subprocess, "Popen", lambda argv, **_k: spawned.append(argv)
        )
        body = json.dumps(
            {"ok": True, "sessions": [{"session": "caly", "resolved": "/base/caly"}]}
        ).encode()

        @contextlib.contextmanager
        def _sessions_only(url, timeout=None):
            assert url.endswith("/api/sessions")
            yield io.BytesIO(body)

        monkeypatch.setattr(hotkey, "urlopen", _sessions_only)
        hotkey._do_open_code("http://127.0.0.1:1", "caly", None)
        assert spawned == [["code", "/base/caly"]]
        # ...and the chain really ran to the end, inside this test: both refused
        # deliveries finished (a refused connect is ~2s on Windows, so returning
        # without this left them in flight for whichever test ran next), and
        # the pump that failed them is still there for the next flash.
        assert _pump_idle(), "the pump never finished the two refused flashes"
        assert altv._pump is not None
        assert altv._pump.is_alive()

    def test_flash_url_is_the_shared_builder_shape(self, monkeypatch):
        # Pin the client/server contract: the listener must hit the same route
        # upload_server serves, with the project it was invoked for. The flash
        # leaves on the pump thread, so this waits for the pump to finish it
        # rather than assuming it already happened.
        from magent import altv, hotkey

        seen: list[str] = []

        def _capture(url, timeout=None):
            seen.append(url)
            raise OSError("stop here")

        monkeypatch.setattr(hotkey.shutil, "which", lambda _n: None)
        monkeypatch.setattr(altv, "urlopen", _capture)
        # The pump is one process-wide queue: a flash another test left in it
        # would be delivered through _capture too, or hold the pump while this
        # test waits. That is a leak in THAT test, and it fails here by name.
        assert _pump_idle(), (
            f"{altv._flash_queue.unfinished_tasks} flash(es) from an earlier"
            " test still queued or in flight on the shared pump"
        )
        hotkey._do_open_code("http://127.0.0.1:8033", "caly", None)
        assert _pump_idle()

        def _ours() -> list[str]:
            return [u for u in seen if u.startswith("http://127.0.0.1:8033/")]

        assert _ours()[0].startswith("http://127.0.0.1:8033/api/flash?")
        assert parse_qs(urlparse(_ours()[0]).query)["project"] == ["caly"]


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
            "demo@deck",
        )
        # 1 == swallow: the agent pane must never also receive the F2.
        assert result == 1
        assert started[0][0] is hotkey._do_open_code
        assert started[0][1] == ("http://x:8034", "caly", "demo@deck")

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
        monkeypatch.setattr(hotkey, "clipboard_kind", lambda: "image")
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
        monkeypatch.setattr(hotkey, "clipboard_kind", lambda: None)
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
        assert "clipboard has no image or file" in started[0][1][3]

    def test_alt_v_with_copied_files_routes_to_the_file_press(self, monkeypatch):
        # Files copied in Explorer are an Alt+V press too: the chord is
        # swallowed and the press carries the clipboard KIND, so _do_upload
        # reads the files rather than a bitmap.
        from magent import hotkey
        from magent.hotkey import HC_ACTION, VK_V, WM_KEYDOWN, _hook_decide

        monkeypatch.setattr(hotkey, "get_active_window_title", lambda: "magent:caly")
        monkeypatch.setattr(hotkey, "clipboard_kind", lambda: "files")
        started: list[tuple[object, tuple[object, ...]]] = []
        self._fake_thread(monkeypatch, started)

        assert (
            _hook_decide(
                {"alt_held": True},
                "http://x:8034",
                HC_ACTION,
                WM_KEYDOWN,
                self._lparam(VK_V),
                "deskpc",
            )
            == 1
        )
        assert started[0][0] is hotkey._do_upload
        assert started[0][1] == ("http://x:8034", "caly", "deskpc", "files")

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


class TestAltVIsDelegated:
    """The press pipeline itself lives in ``magent.altv`` (platform-neutral, so
    a real-serve e2e can drive a press on any OS). What must stay pinned HERE
    is only the win32 half: the hook hands a press to that pipeline, with this
    machine's clipboard reader as the capture step.

    The pipeline's own contract -- phase order, specific failure reasons, the
    flash never blocking a press -- is pinned in tests/unit/test_altv.py.
    """

    def test_do_upload_runs_the_shared_press_pipeline_with_the_win32_capture(
        self, monkeypatch
    ):
        from magent import altv, hotkey

        seen: list[tuple[str, str, object, bool]] = []
        monkeypatch.setattr(
            hotkey,
            "handle_press",
            lambda url, project, capture, native=False: seen.append(
                (url, project, capture, native)
            ),
        )
        # Native local paste is OPT-IN (Claude Code ignores an injected 0x16;
        # DESIGN.md section 2): by default even a LOCAL press takes the
        # capture/upload pipeline.
        monkeypatch.delenv("MAGENT_ALTV_NATIVE", raising=False)
        monkeypatch.setattr("magent.env._cached_env", None)
        hotkey._do_upload("http://x:8034", "marka")
        assert seen == [("http://x:8034", "marka", hotkey.get_clipboard_image, False)]

        # Opted in + no ssh host in the manifest = the panes are LOCAL and the
        # press is one native Ctrl+V, not the capture/upload pipeline.
        monkeypatch.setenv("MAGENT_ALTV_NATIVE", "1")
        monkeypatch.setattr("magent.env._cached_env", None)
        hotkey._do_upload("http://x:8034", "marka")
        assert seen[1] == ("http://x:8034", "marka", hotkey.get_clipboard_image, True)
        # A remote-wired listener keeps the upload path even when opted in --
        # the viewer's clipboard is not the host's, so native paste would read
        # the wrong one.
        hotkey._do_upload("http://x:8034", "marka", ssh_host="deskpc")
        assert seen[2] == ("http://x:8034", "marka", hotkey.get_clipboard_image, False)
        # ...and the names the hook branches reach for are that module's, so a
        # rename cannot leave the listener reporting into a void.
        assert hotkey._altv_report is altv.report
        assert hotkey.OUTCOME_REASONS is altv.OUTCOME_REASONS

    def test_copied_files_take_the_file_press_local_or_remote(self, monkeypatch):
        # A files press never goes near the image pipeline (or native Ctrl+V):
        # it is handle_file_press with the CF_HDROP reader, and "local" is
        # decided by the manifest's ssh_host exactly like the native gate.
        from magent import hotkey

        seen: list[tuple[str, str, object, bool]] = []
        monkeypatch.setattr(
            hotkey,
            "handle_file_press",
            lambda url, project, capture, *, local: seen.append(
                (url, project, capture, local)
            ),
        )
        monkeypatch.setattr(
            hotkey,
            "handle_press",
            lambda *a, **k: pytest.fail("a files press reached the image pipeline"),
        )
        monkeypatch.setenv("MAGENT_ALTV_NATIVE", "1")
        monkeypatch.setattr("magent.env._cached_env", None)

        hotkey._do_upload("http://x:8034", "marka", None, "files")
        hotkey._do_upload("http://x:8034", "marka", "deskpc", "files")
        assert seen == [
            ("http://x:8034", "marka", hotkey.get_clipboard_files, True),
            ("http://x:8034", "marka", hotkey.get_clipboard_files, False),
        ]


class TestClipboardFiles:
    """The CF_HDROP half of the win32 capture: which format wins, and reading
    the paths out of a DROPFILES block.

    Never the real clipboard (it would read -- and a clipboard test would
    stomp -- the developer's own copy): `_hdrop_paths` is fed a DROPFILES
    block this test builds in its own GlobalAlloc memory, and `clipboard_kind`
    is driven through a faked IsClipboardFormatAvailable.
    """

    @staticmethod
    def _dropfiles(paths: list[str]) -> tuple[object, int]:
        import ctypes
        import struct

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        k32.GlobalAlloc.restype = ctypes.c_void_p
        k32.GlobalFree.argtypes = [ctypes.c_void_p]
        k32.GlobalFree.restype = ctypes.c_void_p
        # DROPFILES: pFiles, POINT pt, fNC, fWide -- then a double-NUL list.
        header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
        body = ("\0".join(paths) + "\0\0").encode("utf-16-le")
        blob = header + body
        gmem_fixed = 0x0000
        hmem = k32.GlobalAlloc(gmem_fixed, len(blob))
        assert hmem
        ctypes.memmove(hmem, blob, len(blob))
        return k32, hmem

    def test_every_path_in_a_drop_list_is_read_in_order(self):
        from magent import hotkey

        paths = [
            "C:\\Users\\me\\a.zip",
            "C:\\Users\\me\\My Docs\\notes (1).txt",
            "D:\\\N{LATIN SMALL LETTER E WITH ACUTE}t\N{LATIN SMALL LETTER E WITH ACUTE}.py",
        ]
        k32, hmem = self._dropfiles(paths)
        try:
            assert hotkey._hdrop_paths(hmem) == paths
        finally:
            k32.GlobalFree(hmem)

    def test_the_buffer_holds_every_char_the_call_may_write(self, monkeypatch):
        # DragQueryFileW(h, i, buf, cch) may write cch WCHARs (the name plus its
        # NUL); a buffer smaller than that is a two-byte heap overrun that
        # ctypes will not report.
        import ctypes

        from magent import hotkey

        real = hotkey.shell32.DragQueryFileW
        overruns: list[tuple[int, int]] = []

        def _spy(hdrop, i, buf, cch):
            wchar = ctypes.sizeof(ctypes.c_wchar)
            if buf is not None and ctypes.sizeof(buf) < cch * wchar:
                overruns.append((ctypes.sizeof(buf), cch))
            return real(hdrop, i, buf, cch)

        class _Shell:
            DragQueryFileW = staticmethod(_spy)

        monkeypatch.setattr(hotkey, "shell32", _Shell)
        paths = ["C:\\a\\b.txt", "D:\\longer name\\c.zip"]
        k32, hmem = self._dropfiles(paths)
        try:
            assert hotkey._hdrop_paths(hmem) == paths
        finally:
            k32.GlobalFree(hmem)
        assert overruns == []

    def test_an_empty_drop_list_reads_as_no_paths(self):
        from magent import hotkey

        k32, hmem = self._dropfiles([])
        try:
            assert hotkey._hdrop_paths(hmem) == []
        finally:
            k32.GlobalFree(hmem)

    @pytest.mark.parametrize(
        ("formats", "kind"),
        [
            ({15, 8}, "files"),  # both on the clipboard: files win
            ({15}, "files"),
            ({8}, "image"),  # the CF_DIB-only path is unchanged
            (set(), None),
        ],
    )
    def test_files_win_over_an_image(self, monkeypatch, formats, kind):
        from magent import hotkey

        monkeypatch.setattr(
            hotkey.user32, "IsClipboardFormatAvailable", lambda fmt: fmt in formats
        )
        assert hotkey.clipboard_kind() == kind

    def test_cf_hdrop_is_the_shell_drop_format(self):
        from magent.hotkey import CF_HDROP

        assert CF_HDROP == 15


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
        from magent import hotkey

        monkeypatch.setattr(hotkey, "listener_manifest", lambda: None)
        monkeypatch.setattr(hotkey, "stop_listener", lambda: True)
        # The registration wait runs on procs' own clock (never the global time
        # module): nothing here really sleeps, and a wait that never closes
        # fails instead of hanging. A test that schedules events swaps its own.
        monkeypatch.setattr("magent.procs.time", _FakeTime())

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
        child = _StillStarting()
        monkeypatch.setattr("magent.launch.spawn_detached", lambda *a, **k: child)
        assert cli._maybe_start_hotkey("http://x:8034") is None
        # ...and the new child, which may yet come up, is left alone.
        assert child.ended == []

    def test_the_listener_start_is_bounded_by_the_same_window(self, monkeypatch):
        # A child that hangs alive without registering must not stall serve's
        # supervisor thread or a `--go` launch past the shared window.
        from magent import hotkey, launch
        from magent.procs import REGISTRATION_TIMEOUT_S

        monkeypatch.setattr(hotkey, "listener_pid", lambda: None)
        child = _StillStarting()
        monkeypatch.setattr("magent.launch.spawn_detached", lambda *a, **k: child)
        clock = _FakeTime()
        monkeypatch.setattr("magent.procs.time", clock)

        assert launch.start_hotkey_listener("http://x:8034") is None
        assert REGISTRATION_TIMEOUT_S <= clock.now < REGISTRATION_TIMEOUT_S + 0.5
        assert child.ended == []

    def test_a_listener_that_registers_after_five_seconds_is_returned(
        self, monkeypatch
    ):
        # The old 2s window returned None here -- and every caller read that as
        # "no listener" -- while the listener came up behind it on a busy box.
        from magent import cli, hotkey

        state = {"pid": None}
        monkeypatch.setattr(hotkey, "listener_pid", lambda: state["pid"])
        monkeypatch.setattr(
            "magent.launch.spawn_detached", lambda *a, **k: _StillStarting()
        )
        clock = _FakeTime()
        clock.at(5.0, lambda: state.update(pid=5678))  # the measured slow start
        monkeypatch.setattr("magent.procs.time", clock)
        assert cli._maybe_start_hotkey("http://x:8034") == 5678

    def test_a_listener_that_dies_starting_is_reported_at_once(self, monkeypatch):
        # A keyboard hook that fails to install exits the child: the launcher
        # must say so now, not after the whole window.
        from magent import cli, hotkey

        monkeypatch.setattr(hotkey, "listener_pid", lambda: None)
        monkeypatch.setattr(
            "magent.launch.spawn_detached", lambda *a, **k: _StillStarting(rc=1)
        )
        clock = _FakeTime()
        monkeypatch.setattr("magent.procs.time", clock)
        assert cli._maybe_start_hotkey("http://x:8034") is None
        assert clock.now < 1.0


class _StillStarting:
    """The spawned detached child: alive (``rc=None``) or already exited.
    Records every attempt to end it -- the launcher must never make one."""

    def __init__(self, rc: int | None = None) -> None:
        self.rc = rc
        self.ended: list[str] = []

    def poll(self) -> int | None:
        return self.rc

    def kill(self) -> None:
        self.ended.append("kill")

    def terminate(self) -> None:
        self.ended.append("terminate")

    def send_signal(self, sig: int) -> None:
        self.ended.append(f"signal {sig}")


class _FakeTime:
    """Stands in for ``procs.time``: ``sleep`` advances ``monotonic`` instead of
    sleeping, and fires anything scheduled with ``at`` once its time comes.
    Patched onto the procs module only, never onto the global time module. A
    wait that never closes FAILS here instead of hanging the suite."""

    def __init__(self) -> None:
        self.now = 0.0
        self._due: list[tuple[float, object]] = []

    def at(self, when: float, action) -> None:
        self._due.append((when, action))

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        assert self.now < 120.0, "the registration wait never closed"
        for when, action in list(self._due):
            if self.now >= when:
                self._due.remove((when, action))
                action()


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
        monkeypatch.setattr(hotkey, "clipboard_kind", lambda: "image")

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
        # The third member is the ssh_host the decide call runs with (None =
        # local wiring), threaded through so _do_upload can pick the native
        # path without re-reading the manifest on a keypress.
        assert started[0][1] == ("http://x:8034", "marka", None, "image")

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


class TestF2OpensANodeFolderOverRemoteSsh(_OpenCodeHarness):
    """A node project's folder is on its pool machine, and the node map knows
    where: no server round trip, and the user magent resolved stays in the
    authority (C3)."""

    def _map(
        self, monkeypatch, tmp_path, *, nick="second", cwd="/home/demo/magent/api"
    ):
        from magent import nodes
        from magent.nodes import NodeMapEntry

        monkeypatch.setattr(nodes, "NODE_MAP_PATH", tmp_path / "node-map.json")
        nodes.write_node_map(
            {
                "api": NodeMapEntry(
                    nick=nick,
                    sid="api",
                    placed_ts=1.0,
                    attached_existing=False,
                    remote_root="~/magent/api",
                    target="demo@box-second",
                    cwd=cwd,
                )
            }
        )

    def test_a_node_project_opens_on_its_node(self, monkeypatch, tmp_path):
        from magent import hotkey

        self._map(monkeypatch, tmp_path)
        spawned = self._patch(monkeypatch)

        # Recorded as well as raised: the handler's broad `except` swallows the
        # raise, so a caught-and-ignored round trip would otherwise pass.
        round_trips: list[object] = []

        def no_server(*a, **_k):
            round_trips.append(a)
            raise AssertionError("a node project needs no /api/sessions round trip")

        monkeypatch.setattr(hotkey, "urlopen", no_server)
        hotkey._do_open_code("http://x:8034", "api", None)
        assert spawned == [
            [
                "code",
                "--remote",
                "ssh-remote+demo@box-second",
                "/home/demo/magent/api",
            ]
        ]
        assert self.flashed[-1] == "F2: VS Code -> /home/demo/magent/api"
        assert round_trips == []

    def test_a_cloud_placement_falls_through_to_the_server(self, monkeypatch, tmp_path):
        from magent import hotkey

        self._map(monkeypatch, tmp_path, nick="cloud")
        spawned = self._patch(
            monkeypatch,
            payload={
                "ok": True,
                "sessions": [
                    {"name": "api", "session": "api", "resolved": "/base/api"}
                ],
            },
        )
        hotkey._do_open_code("http://x:8034", "api", None)
        assert spawned == [["code", "/base/api"]]

    def test_a_local_project_is_byte_for_byte_todays_path(self, monkeypatch, tmp_path):
        from magent import hotkey, nodes

        monkeypatch.setattr(nodes, "NODE_MAP_PATH", tmp_path / "node-map.json")
        spawned = self._patch(
            monkeypatch,
            payload={
                "ok": True,
                "sessions": [
                    {"name": "caly", "session": "caly", "resolved": "/base/caly"}
                ],
            },
        )
        hotkey._do_open_code("http://x:8034", "caly", "me@host")
        assert spawned == [["code", "--remote", "ssh-remote+host", "/base/caly"]]

    def test_a_torn_node_map_falls_through_to_the_server(self, monkeypatch, tmp_path):
        """The map is best-effort here: an unreadable one must not cost F2 the
        server's answer (read_node_map, never load_node_map_strict)."""
        from magent import hotkey, nodes

        torn = tmp_path / "node-map.json"
        torn.write_text('{"api": {"nick": "sec', encoding="utf-8")
        monkeypatch.setattr(nodes, "NODE_MAP_PATH", torn)
        spawned = self._patch(
            monkeypatch,
            payload={
                "ok": True,
                "sessions": [
                    {"name": "api", "session": "api", "resolved": "/base/api"}
                ],
            },
        )
        hotkey._do_open_code("http://x:8034", "api", None)
        assert spawned == [["code", "/base/api"]]

    # --- code.cmd re-parses its command line (cq-D15 I1) ----------------------
    # CreateProcess runs a .cmd through `cmd.exe /c`, and list2cmdline quotes
    # only whitespace: `R&D` opens `R` and runs a stray `D`, `%USERNAME%`
    # expands. F2 refuses such an argv -- on BOTH paths -- instead of opening
    # the wrong folder and flashing success. Nothing is launched: `_patch`'s
    # Popen double records the argv it would have run.
    _SHIM = r"C:\VS Code\bin\code.cmd"
    _REFUSED = "F2: folder name has a character code.cmd can't pass"

    def _assert_refused(self, spawned):
        from magent import hotkey

        assert spawned == []
        assert self.flashed[-1] == self._REFUSED
        assert self.flash_tints[-1] == hotkey.FLASH_TINT_ERR
        assert not any(m.startswith("F2: VS Code ->") for m in self.flashed)

    def test_an_ampersand_node_folder_is_refused_through_code_cmd(
        self, monkeypatch, tmp_path
    ):
        from magent import hotkey

        self._map(monkeypatch, tmp_path, cwd="/home/demo/magent/R&D")
        spawned = self._patch(monkeypatch, code_bin=self._SHIM)
        hotkey._do_open_code("http://x:8034", "api", None)
        self._assert_refused(spawned)

    def test_a_percent_variable_node_folder_is_refused_through_code_cmd(
        self, monkeypatch, tmp_path
    ):
        from magent import hotkey

        self._map(monkeypatch, tmp_path, cwd="/home/demo/magent/%USERNAME%")
        spawned = self._patch(monkeypatch, code_bin=self._SHIM)
        hotkey._do_open_code("http://x:8034", "api", None)
        self._assert_refused(spawned)

    def test_a_local_ampersand_folder_on_the_server_path_is_refused(
        self, monkeypatch, tmp_path
    ):
        from magent import hotkey, nodes

        monkeypatch.setattr(nodes, "NODE_MAP_PATH", tmp_path / "node-map.json")
        spawned = self._patch(
            monkeypatch,
            code_bin=self._SHIM,
            payload={
                "ok": True,
                "sessions": [
                    {"name": "rd", "session": "rd", "resolved": r"C:\dev\R&D"}
                ],
            },
        )
        hotkey._do_open_code("http://x:8034", "rd", None)
        self._assert_refused(spawned)

    @pytest.mark.parametrize("char", sorted('&|<>^%"!'))
    @pytest.mark.parametrize("shim", ["code.cmd", "CODE.CMD", "code.bat", "Code.Bat"])
    def test_every_metacharacter_is_refused_through_any_batch_shim(
        self, monkeypatch, tmp_path, char, shim
    ):
        from magent import hotkey

        self._map(monkeypatch, tmp_path, cwd=f"/home/demo/magent/a{char}b")
        spawned = self._patch(monkeypatch, code_bin=rf"C:\VS Code\bin\{shim}")
        hotkey._do_open_code("http://x:8034", "api", None)
        self._assert_refused(spawned)

    def test_a_plain_spaced_folder_still_opens_through_code_cmd(
        self, monkeypatch, tmp_path
    ):
        from magent import hotkey

        self._map(monkeypatch, tmp_path, cwd="/home/demo/magent/my api")
        spawned = self._patch(monkeypatch, code_bin=self._SHIM)
        hotkey._do_open_code("http://x:8034", "api", None)
        assert spawned == [
            [
                self._SHIM,
                "--remote",
                "ssh-remote+demo@box-second",
                "/home/demo/magent/my api",
            ]
        ]
        assert self.flashed[-1] == "F2: VS Code -> /home/demo/magent/my api"

    @pytest.mark.parametrize(
        "code_bin", [r"C:\VS Code\Code.exe", "/usr/bin/code", "code"]
    )
    def test_no_batch_shim_means_no_cmd_exe_and_nothing_refused(
        self, monkeypatch, tmp_path, code_bin
    ):
        from magent import hotkey

        self._map(monkeypatch, tmp_path, cwd="/home/demo/magent/R&D")
        spawned = self._patch(monkeypatch, code_bin=code_bin)
        hotkey._do_open_code("http://x:8034", "api", None)
        assert spawned == [
            [
                code_bin,
                "--remote",
                "ssh-remote+demo@box-second",
                "/home/demo/magent/R&D",
            ]
        ]

    def test_a_map_json_cannot_nest_still_costs_f2_nothing(self, monkeypatch, tmp_path):
        """json.loads raises RecursionError -- not a ValueError -- past ~1000
        levels; read_node_map must still read that as no placements."""
        from magent import hotkey, nodes

        deep = tmp_path / "node-map.json"
        deep.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
        monkeypatch.setattr(nodes, "NODE_MAP_PATH", deep)
        spawned = self._patch(
            monkeypatch,
            payload={
                "ok": True,
                "sessions": [
                    {"name": "api", "session": "api", "resolved": "/base/api"}
                ],
            },
        )
        hotkey._do_open_code("http://x:8034", "api", None)
        assert spawned == [["code", "/base/api"]]

    def test_an_unvetted_node_folder_falls_through_to_the_server(
        self, monkeypatch, tmp_path
    ):
        """A map value that is no clean absolute path never reaches the argv
        (it would be a VS Code flag here): F2 asks the server instead."""
        from magent import hotkey

        self._map(monkeypatch, tmp_path, cwd="--install-extension=evil.vsix")
        spawned = self._patch(
            monkeypatch,
            payload={
                "ok": True,
                "sessions": [
                    {"name": "api", "session": "api", "resolved": "/base/api"}
                ],
            },
        )
        hotkey._do_open_code("http://x:8034", "api", None)
        assert spawned == [["code", "/base/api"]]

    def test_the_node_map_wins_over_an_attach_listeners_host(
        self, monkeypatch, tmp_path
    ):
        from magent import hotkey

        self._map(monkeypatch, tmp_path)
        spawned = self._patch(monkeypatch)
        hotkey._do_open_code("http://x:8034", "api", "me@desktop")
        assert spawned == [
            [
                "code",
                "--remote",
                "ssh-remote+demo@box-second",
                "/home/demo/magent/api",
            ]
        ]

    def test_a_node_open_spawns_the_resolved_code_bin(self, monkeypatch, tmp_path):
        # A bare "code" handed to CreateProcess never finds the .cmd shim.
        from magent import hotkey

        self._map(monkeypatch, tmp_path)
        shim = r"C:\VS Code\bin\code.cmd"
        spawned = self._patch(monkeypatch, code_bin=shim)
        hotkey._do_open_code("http://x:8034", "api", None)
        assert [argv[0] for argv in spawned] == [shim]

    def test_a_failing_node_lookup_is_reported_never_raised(self, monkeypatch):
        from magent import hotkey, nodes

        spawned = self._patch(monkeypatch)

        def boom(*_a, **_k):
            raise RuntimeError("map exploded")

        monkeypatch.setattr(nodes, "read_node_map", boom)
        try:
            hotkey._do_open_code("http://x:8034", "api", None)
        except RuntimeError:
            raise AssertionError("an F2 failure escaped the handler thread") from None
        assert spawned == []
        assert self.flashed[-1] == "F2: failed - see hotkey.log"

    # A remote POSIX folder may carry what no Windows name can: cmd.exe ends
    # the command at a LF (a truncated folder, then a false success flash),
    # and `!` expands under delayed expansion. Only the server path can
    # deliver these -- open_target already drops a control-bearing node cwd.
    _ODD = (
        "/srv/a\nb",
        "/srv/a\rb",
        "/srv/a\tb",
        "/srv/a\x1fb",  # the top of C0: a `< " "` bound, not `<= "\x1e"`
        "/srv/a\x7fb",
        "/srv/a!b",
    )

    def _serve(self, monkeypatch, tmp_path, folder, code_bin):
        from magent import nodes

        monkeypatch.setattr(nodes, "NODE_MAP_PATH", tmp_path / "node-map.json")
        return self._patch(
            monkeypatch,
            code_bin=code_bin,
            payload={
                "ok": True,
                "sessions": [{"name": "api", "session": "api", "resolved": folder}],
            },
        )

    @pytest.mark.parametrize("folder", _ODD)
    def test_a_control_character_or_bang_is_refused_through_code_cmd(
        self, monkeypatch, tmp_path, folder
    ):
        from magent import hotkey

        spawned = self._serve(monkeypatch, tmp_path, folder, self._SHIM)
        hotkey._do_open_code("http://x:8034", "api", "me@host")
        self._assert_refused(spawned)

    @pytest.mark.parametrize("code_bin", [r"C:\VS Code\Code.exe", "/usr/bin/code"])
    @pytest.mark.parametrize("folder", _ODD)
    def test_a_control_character_or_bang_passes_without_cmd_exe(
        self, monkeypatch, tmp_path, folder, code_bin
    ):
        from magent import hotkey

        spawned = self._serve(monkeypatch, tmp_path, folder, code_bin)
        hotkey._do_open_code("http://x:8034", "api", "me@host")
        assert spawned == [[code_bin, "--remote", "ssh-remote+host", folder]]

    def test_a_shim_path_code_cmd_would_split_is_refused_too(
        self, monkeypatch, tmp_path
    ):
        # The shim's own path is on the re-joined command line too: cmd.exe
        # splits an unquoted C:\Users\R&D\bin\code.cmd at the `&`.
        from magent import hotkey, nodes

        monkeypatch.setattr(nodes, "NODE_MAP_PATH", tmp_path / "node-map.json")
        spawned = self._patch(
            monkeypatch,
            code_bin=r"C:\Users\R&D\bin\code.cmd",
            payload={
                "ok": True,
                "sessions": [
                    {"name": "api", "session": "api", "resolved": "/base/api"}
                ],
            },
        )
        hotkey._do_open_code("http://x:8034", "api", None)
        assert spawned == []
        assert self.flashed[-1] == self._REFUSED
