"""One Alt+V press, narrated: what the status line says and WHEN it says it.

These are the unit pins behind the user-visible complaint "Alt+V works but the
status isn't showing, and when it does it's late". Every one of them is a
regression guard for a specific way the narration can silently die:

* the acknowledgement is DISPATCHED before the clipboard is read (not after the
  upload, which is the whole "late" half of the complaint);
* success flashes too -- it used to be deliberately silent, which is
  indistinguishable from a listener that never ran;
* every failure carries its OWN reason, so the bar answers "why";
* a dead, slow or hostile flash channel can never delay or break a press.

The module is deliberately platform-neutral (hotkey.py is win32-import-only),
so all of this runs on every OS.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from magent import altv


@pytest.fixture(autouse=True)
def _drain_pump():
    """Leave the shared flash pump empty between tests."""
    yield
    _drain()


def _drain(timeout: float = 5.0) -> None:
    """Wait (boundedly) for the pump to finish what it was handed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # A test may have swapped the queue for a stub; nothing to drain then.
        if not getattr(altv._flash_queue, "unfinished_tasks", 0):
            return
        time.sleep(0.01)


class _Upload:
    """A real HTTP server standing in for `magent serve`'s /upload."""

    def __init__(self, reply: dict, status: int = 200):
        self.reply = reply
        self.status = status
        self.requests: list[tuple[str, bytes]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                outer.requests.append((self.path, self.rfile.read(length)))
                body = json.dumps(outer.reply).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


class TestPhaseOrder:
    """The press narrates capture -> upload -> outcome, in that order."""

    def _dispatched(self, monkeypatch) -> list[str]:
        """Record every flash at the point it is DISPATCHED, not delivered --
        delivery is a different thread, and dispatch order is the contract."""
        seen: list[str] = []
        monkeypatch.setattr(
            altv,
            "flash_async",
            lambda url, project, message, duration_ms=None: seen.append(message),
        )
        return seen

    def test_the_press_is_acknowledged_before_the_clipboard_is_touched(
        self, monkeypatch
    ):
        # The headline fix: feedback answers the KEYPRESS. Reading a big image
        # off the clipboard and shipping it can take seconds; the bar must not
        # wait for either.
        order: list[str] = []
        monkeypatch.setattr(
            altv,
            "flash_async",
            lambda url, project, message, duration_ms=None: order.append(
                f"flash:{message}"
            ),
        )
        monkeypatch.setattr(
            altv,
            "upload_image",
            lambda url, project, data: (
                order.append("upload") or ("ok", "image sent", "")
            ),
        )

        def _capture():
            order.append("capture")
            return b"BMP"

        altv.handle_press("http://x:8034", "marka", _capture)

        assert order[0] == f"flash:{altv.FLASH_PREFIX}{altv.PHASE_CAPTURING}"
        assert order[1] == "capture"
        assert order[2] == f"flash:{altv.FLASH_PREFIX}{altv.PHASE_UPLOADING}"
        assert order[3] == "upload"
        assert order[4] == f"flash:{altv.FLASH_PREFIX}image sent"

    def test_a_successful_press_says_so_on_the_bar(self, monkeypatch):
        # Regression: success used to log "ALTV outcome=ok" and flash NOTHING,
        # leaving a working Alt+V indistinguishable from a dead listener.
        seen = self._dispatched(monkeypatch)
        monkeypatch.setattr(
            altv, "upload_image", lambda *a: ("ok", altv.OUTCOME_REASONS["ok"], "")
        )

        assert altv.handle_press("http://x:8034", "marka", lambda: b"BMP") == "ok"
        assert seen[-1] == f"{altv.FLASH_PREFIX}image sent"

    def test_phases_reach_the_wire_in_order_through_the_real_pump(self, monkeypatch):
        # Same sequence, but through the real queue + pump thread this time:
        # three fire-and-forget threads would be free to arrive in any order,
        # and an "image sent" that overtakes "uploading..." leaves the bar lying.
        delivered: list[str] = []
        monkeypatch.setattr(
            altv,
            "flash_status",
            lambda url, project, message, duration_ms=None: delivered.append(message),
        )
        monkeypatch.setattr(
            altv, "upload_image", lambda *a: ("ok", altv.OUTCOME_REASONS["ok"], "")
        )

        altv.handle_press("http://x:8034", "marka", lambda: b"BMP")
        _drain()

        assert delivered == [
            f"{altv.FLASH_PREFIX}{altv.PHASE_CAPTURING}",
            f"{altv.FLASH_PREFIX}{altv.PHASE_UPLOADING}",
            f"{altv.FLASH_PREFIX}image sent",
        ]

    def test_a_phase_message_outlives_the_step_it_narrates(self, monkeypatch):
        # A "uploading..." that expires mid-upload leaves a blank bar, which
        # reads exactly like the silence this channel exists to end.
        durations: list[int | None] = []
        monkeypatch.setattr(
            altv,
            "flash_async",
            lambda url, project, message, duration_ms=None: durations.append(
                duration_ms
            ),
        )
        monkeypatch.setattr(
            altv, "upload_image", lambda *a: ("ok", altv.OUTCOME_REASONS["ok"], "")
        )

        altv.handle_press("http://x:8034", "marka", lambda: b"BMP")

        assert durations[0] == altv.PHASE_FLASH_MS
        assert durations[1] == altv.PHASE_FLASH_MS
        assert durations[2] is None  # the outcome takes the server's default


class TestFailuresAreSpecific:
    """ "Why didn't it work?" has to be answerable from the bar alone."""

    def _dispatched(self, monkeypatch) -> list[str]:
        seen: list[str] = []
        monkeypatch.setattr(
            altv,
            "flash_async",
            lambda url, project, message, duration_ms=None: seen.append(message),
        )
        return seen

    def test_unreadable_clipboard_says_so(self, monkeypatch, caplog):
        seen = self._dispatched(monkeypatch)
        with caplog.at_level("INFO", logger="magent.hotkey"):
            outcome = altv.handle_press("http://x:8034", "marka", lambda: None)

        assert outcome == "clipboard-unreadable"
        assert "ALTV outcome=clipboard-unreadable project=marka" in caplog.text
        assert (
            seen[-1]
            == f"{altv.FLASH_PREFIX}{altv.OUTCOME_REASONS['clipboard-unreadable']}"
        )

    def test_a_dead_serve_is_named_as_such_not_as_a_generic_failure(
        self, monkeypatch, caplog
    ):
        # Port 1 on loopback refuses instantly -- a REAL connection error, no
        # mocked transport. "upload failed" would send the user hunting for the
        # wrong thing; "cannot reach magent serve" names the actual repair.
        seen = self._dispatched(monkeypatch)
        with caplog.at_level("INFO", logger="magent.hotkey"):
            outcome = altv.handle_press("http://127.0.0.1:1", "marka", lambda: b"BMP")

        assert outcome == "serve-unreachable"
        assert "ALTV outcome=serve-unreachable project=marka" in caplog.text
        assert "cannot reach magent serve" in seen[-1]

    def test_a_rejection_carries_the_servers_own_status_and_reason(self, monkeypatch):
        seen = self._dispatched(monkeypatch)
        server = _Upload({"ok": False, "error": "Unknown project"}, status=400)
        try:
            outcome = altv.handle_press(server.url, "marka", lambda: b"BMP")
        finally:
            server.close()

        assert outcome == "upload-rejected"
        assert "400" in seen[-1] and "Unknown project" in seen[-1]

    def test_a_stored_but_uninjected_upload_is_not_reported_as_a_failed_upload(
        self, monkeypatch
    ):
        # The bytes ARE on disk; only the psmux paste failed. Calling that
        # "upload failed" sends the user looking for a lost screenshot.
        seen = self._dispatched(monkeypatch)
        server = _Upload({"ok": True, "path": "/tmp/x.bmp", "injected": False})
        try:
            outcome = altv.handle_press(server.url, "marka", lambda: b"BMP")
        finally:
            server.close()

        assert outcome == "inject-failed"
        assert seen[-1] == f"{altv.FLASH_PREFIX}{altv.OUTCOME_REASONS['inject-failed']}"

    def test_an_unexpected_error_is_caught_logged_and_shown_not_raised(
        self, monkeypatch, caplog
    ):
        seen = self._dispatched(monkeypatch)

        def _boom():
            raise OverflowError("byte must be in range(0, 256)")

        with caplog.at_level("INFO", logger="magent.hotkey"):
            outcome = altv.handle_press("http://x:8034", "marka", _boom)

        assert outcome == "error"
        assert "ALTV outcome=error project=marka" in caplog.text
        assert "OverflowError" in caplog.text  # the traceback rides along
        assert seen[-1] == f"{altv.FLASH_PREFIX}{altv.OUTCOME_REASONS['error']}"

    def test_no_two_outcomes_share_a_reason(self):
        # A collapsed vocabulary is how "it failed" came back; if two outcomes
        # ever say the same sentence, the bar has stopped diagnosing anything.
        reasons = list(altv.OUTCOME_REASONS.values())
        assert len(reasons) == len(set(reasons))

    def test_every_outcome_name_is_declared_and_reasoned(self):
        # The vocabulary is closed on purpose: `grep 'ALTV outcome=no-image'`
        # has to keep working as a diagnosis, not just `grep ALTV`.
        assert set(altv.ALTV_OUTCOMES) == {
            "ok",
            "not-a-magent-window",
            "no-image",
            "clipboard-unreadable",
            "serve-unreachable",
            "upload-rejected",
            "inject-failed",
            "error",
        }
        # Every outcome the user can SEE needs words for the bar. The
        # pass-through is the one exception: it never reaches a magent window.
        assert set(altv.OUTCOME_REASONS) == set(altv.ALTV_OUTCOMES) - {
            "not-a-magent-window"
        }


class TestTheFlashCanNeverHurtThePress:
    def test_a_slow_flash_channel_does_not_delay_the_press(self, monkeypatch):
        # The regression this exists to catch: making the flash a plain call.
        # A status-bar round trip has been measured at seconds under load, and
        # a press must never wait on its own progress report.
        def _slow(url, project, message, duration_ms=None):
            time.sleep(0.4)

        monkeypatch.setattr(altv, "flash_status", _slow)
        monkeypatch.setattr(
            altv, "upload_image", lambda *a: ("ok", altv.OUTCOME_REASONS["ok"], "")
        )

        started = time.monotonic()
        altv.handle_press("http://x:8034", "marka", lambda: b"BMP")
        elapsed = time.monotonic() - started

        assert elapsed < 0.3, f"the press waited {elapsed:.2f}s on its own flashes"
        _drain(timeout=10)

    def test_a_broken_flash_channel_cannot_break_the_press(self, monkeypatch):
        def _explode(url, project, message, duration_ms=None):
            raise RuntimeError("status bar is on fire")

        monkeypatch.setattr(altv, "flash_status", _explode)
        monkeypatch.setattr(
            altv, "upload_image", lambda *a: ("ok", altv.OUTCOME_REASONS["ok"], "")
        )

        assert altv.handle_press("http://x:8034", "marka", lambda: b"BMP") == "ok"
        _drain()
        # ...and the pump is still there for the NEXT press. A pump that dies on
        # one bad message strands every message queued behind it, which is how a
        # status line goes quiet for an hour without anyone noticing.
        assert altv._pump is not None and altv._pump.is_alive()

    def test_a_dead_server_is_swallowed_by_the_transport_itself(self):
        # flash_status is best-effort by construction: nothing listening on
        # port 1, and the call still returns normally.
        altv.flash_status("http://127.0.0.1:1", "marka", "Alt+V: hello")

    def test_a_full_queue_drops_the_message_not_the_press(self, monkeypatch):
        # A wedged server must cost the press nothing -- not memory, and not an
        # exception on the thread doing the actual work.
        class _Full:
            def put_nowait(self, item):
                raise altv.queue.Full

        monkeypatch.setattr(altv, "_flash_queue", _Full())
        for _ in range(5):
            altv.flash_async("http://x:8034", "marka", "hello")  # must not raise


class TestStatusBarHygiene:
    def test_messages_are_clipped_to_ascii(self):
        # A status bar is where the renderer's and the multiplexer's width
        # arithmetic must agree; an ambiguous-width glyph has corrupted this
        # exact bar before (see psmux._STATUS_HINTS).
        assert altv._ascii_clip("Alt+V: ↑ sent ✓").isascii()

    def test_newlines_never_reach_the_bar(self):
        assert "\n" not in altv._ascii_clip("line one\nline two")

    def test_long_messages_are_clipped(self):
        from magent.sessions import FLASH_MSG_MAX

        assert len(altv._ascii_clip("x" * 400)) == FLASH_MSG_MAX

    def test_every_shipped_phrase_is_ascii(self):
        for text in [
            *altv.OUTCOME_REASONS.values(),
            altv.PHASE_CAPTURING,
            altv.PHASE_UPLOADING,
            altv.FLASH_PREFIX,
        ]:
            assert text.isascii(), text


class TestUploadImage:
    def test_a_healthy_upload_reports_ok(self):
        server = _Upload({"ok": True, "path": "/tmp/x.bmp", "injected": True})
        try:
            outcome, reason, _ = altv.upload_image(server.url, "marka", b"FAKEBMP")
        finally:
            server.close()
        assert outcome == "ok"
        assert reason == altv.OUTCOME_REASONS["ok"]

    def test_the_upload_flags_itself_so_the_server_stays_off_the_status_line(self):
        # ?project= is how the server knows this paste already has a narrator.
        server = _Upload({"ok": True, "injected": True})
        try:
            altv.upload_image(server.url, "marka", b"FAKEBMP")
        finally:
            server.close()
        assert server.requests[0][0] == "/upload?project=marka"
        assert b"FAKEBMP" in server.requests[0][1]

    def test_a_refused_connection_is_named(self):
        outcome, reason, _ = altv.upload_image("http://127.0.0.1:1", "marka", b"x")
        assert outcome == "serve-unreachable"
        assert "cannot reach magent serve" in reason

    def test_an_ok_false_body_is_a_rejection_not_a_transport_error(self):
        server = _Upload({"ok": False, "error": "Missing file or project"})
        try:
            outcome, reason, _ = altv.upload_image(server.url, "marka", b"x")
        finally:
            server.close()
        assert outcome == "upload-rejected"
        assert "Missing file or project" in reason

    def test_a_non_json_reply_is_a_rejection_with_a_reason(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Length", "9")
                self.end_headers()
                self.wfile.write(b"not-json!")

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            outcome, reason, _ = altv.upload_image(url, "marka", b"x")
        finally:
            server.shutdown()
        assert outcome == "upload-rejected"
        assert reason


class TestTransportReasons:
    def test_refused_and_timeout_read_differently(self):
        from urllib.error import URLError

        assert (
            altv._transport_reason(URLError(ConnectionRefusedError()))
            == "connection refused"
        )
        assert altv._transport_reason(URLError(TimeoutError())) == "timed out"
        assert altv._transport_reason(TimeoutError()) == "timed out"

    def test_a_windows_style_oserror_does_not_paste_a_paragraph_on_the_bar(self):
        from urllib.error import URLError

        blob = OSError(
            "[WinError 10061] No connection could be made because the target "
            "machine actively refused it, and here is a great deal more text"
        )
        assert len(altv._transport_reason(URLError(blob))) <= 60
