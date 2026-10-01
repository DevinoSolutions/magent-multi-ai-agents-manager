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
import socket
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
    """A real HTTP server standing in for `magent serve`'s /upload.

    ``reply`` is answered as JSON. ``raw=`` answers with those bytes verbatim
    instead, for the "serve said something that isn't JSON" case.

    Every reply is preceded by a full read of the request body, which is
    load-bearing rather than tidy. ``BaseHTTPRequestHandler`` speaks HTTP/1.0,
    so the connection is closed the moment the handler returns -- and closing a
    TCP socket that still has unread received data sends an RST rather than a
    FIN (RFC 1122 4.2.2.13). An RST discards whatever the client had buffered
    but not yet read, so `upload_image`'s `resp.read()` raises
    ConnectionResetError (an OSError), which it correctly classifies as
    `serve-unreachable` -- turning a test about a REPLY into a test about a
    dead server. It is a genuine race: urllib sends the headers and the body in
    two separate `send()` calls, so under load the body can still be in the
    kernel queue when the handler answers. Draining first removes the race
    instead of making it rarer.
    """

    def __init__(
        self, reply: dict | None = None, status: int = 200, raw: bytes | None = None
    ):
        self.reply = reply
        self.status = status
        self.raw = raw
        self.requests: list[tuple[str, bytes]] = []
        self.headers: list[dict[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                # Drain BEFORE replying -- see the class docstring.
                outer.requests.append((self.path, self.rfile.read(length)))
                outer.headers.append(dict(self.headers.items()))
                json_reply = outer.raw is None
                body = json.dumps(outer.reply).encode() if json_reply else outer.raw
                self.send_response(outer.status)
                self.send_header(
                    "Content-Type", "application/json" if json_reply else "text/plain"
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        # shutdown() stops the accept loop; server_close() releases the
        # listening socket, which a `serve_forever`-only teardown leaks for the
        # rest of the session. The join is bounded so a wedged handler fails
        # the run it belongs to rather than hanging the suite.
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=10)
        assert not self._thread.is_alive(), "the stand-in upload server never stopped"


class TestPhaseOrder:
    """The press narrates capture -> upload -> outcome, in that order."""

    def _dispatched(self, monkeypatch) -> list[str]:
        """Record every flash at the point it is DISPATCHED, not delivered --
        delivery is a different thread, and dispatch order is the contract."""
        seen: list[str] = []
        monkeypatch.setattr(
            altv,
            "flash_async",
            lambda url, project, message, duration_ms=None, tint=None: seen.append(
                message
            ),
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
            lambda url, project, message, duration_ms=None, tint=None: order.append(
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
            lambda url, project, message, duration_ms=None, tint=None: delivered.append(
                message
            ),
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
            lambda url, project, message, duration_ms=None, tint=None: durations.append(
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
            lambda url, project, message, duration_ms=None, tint=None: seen.append(
                message
            ),
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

    def test_a_pending_paste_is_narrated_as_saved_never_as_a_failure(self, monkeypatch):
        # The measured defect: a psmux control command that stalled 74s made the
        # listener time out at 20s and flash "upload failed - is `magent serve`
        # running?" about a file that was already on disk and that psmux went on
        # to paste. The server now answers early and says so; the bar must carry
        # that distinction rather than collapse it into a failure.
        seen = self._dispatched(monkeypatch)
        server = _Upload(
            {
                "ok": True,
                "path": "/tmp/x.bmp",
                "injected": False,
                "inject_pending": True,
            }
        )
        try:
            outcome = altv.handle_press(server.url, "marka", lambda: b"BMP")
        finally:
            server.close()

        assert outcome == "inject-pending"
        assert (
            seen[-1] == f"{altv.FLASH_PREFIX}{altv.OUTCOME_REASONS['inject-pending']}"
        )
        assert "fail" not in seen[-1].lower(), seen[-1]
        # The image is named as SAVED -- that is what stops a rerun, and a rerun
        # is what pastes the same screenshot twice.
        assert "saved" in seen[-1].lower(), seen[-1]

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
            "ok-native",
            "ok-paths",
            "not-a-magent-window",
            "no-image",
            "clipboard-unreadable",
            "folder-refused",
            "file-missing",
            "file-unreadable",
            "too-large",
            "serve-unreachable",
            "upload-rejected",
            "inject-failed",
            "inject-pending",
            "native-failed",
            "paths-failed",
            "path-unpasteable",
            "error",
        }
        # Every outcome the user can SEE needs words for the bar. The
        # pass-through is the one exception: it never reaches a magent window.
        assert set(altv.OUTCOME_REASONS) == set(altv.ALTV_OUTCOMES) - {
            "not-a-magent-window"
        }

    def test_the_safe_outcomes_are_the_ones_whose_image_is_on_disk(self):
        # The tint and the wording both key off this set, so it must not drift
        # into "every outcome that is not an exception". Exactly three
        # outcomes leave the screenshot recoverable: the paste landed (upload
        # or native -- native consumes nothing, the clipboard still holds it),
        # or it has not landed YET. `ok-paths` is the local file press: the
        # paths landed and the files themselves were never touched.
        assert set(altv.ALTV_SAFE_OUTCOMES) == {
            "ok",
            "ok-native",
            "ok-paths",
            "inject-pending",
        }
        assert set(altv.ALTV_SAFE_OUTCOMES) <= set(altv.ALTV_OUTCOMES)


class TestTheFlashCanNeverHurtThePress:
    def test_a_slow_flash_channel_does_not_delay_the_press(self, monkeypatch):
        # The regression this exists to catch: making the flash a plain call.
        # A status-bar round trip has been measured at seconds under load, and
        # a press must never wait on its own progress report.
        def _slow(url, project, message, duration_ms=None, tint=None):
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
        def _explode(url, project, message, duration_ms=None, tint=None):
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

    def test_the_pump_outwaits_the_servers_own_status_line_bound(self):
        # Ordering depends on it: /api/flash answers only once psmux has the
        # message, so a client timeout SHORTER than the server's psmux bound
        # lets the next phase overlap this one and arrive first.
        from magent import psmux

        assert altv.FLASH_HTTP_TIMEOUT_S > psmux.FLASH_TIMEOUT_S

    def test_the_press_outwaits_the_servers_own_answer_deadline(self):
        # The false "upload failed" was this inequality inverted: the handler
        # pasted inline with NO bound while the press gave up at 20s, so the
        # client timed out on a request the server was still (successfully)
        # working on. The server now owes an answer inside INJECT_GRACE_S, and
        # it must stay comfortably the smaller of the two.
        from magent import upload_server

        assert upload_server.INJECT_GRACE_S < altv.UPLOAD_HTTP_TIMEOUT_S
        # ...comfortably: the reply also has to carry a multi-megabyte body's
        # read time, so a grace that merely squeaked under would still be a race.
        assert upload_server.INJECT_GRACE_S * 2 < altv.UPLOAD_HTTP_TIMEOUT_S

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


class TestTint:
    """psmux's ``message-style`` is GLOBAL on that socket, so a tint set once
    for a failure survives into the next message. Every flash therefore carries
    its own: a green "cannot reach magent serve" is worse than no colour."""

    def _tints(self, monkeypatch) -> list[str]:
        seen: list[str] = []
        monkeypatch.setattr(
            altv,
            "flash_status",
            lambda url, project, message, duration_ms=None, tint=None: seen.append(
                f"{tint}:{message}"
            ),
        )
        return seen

    def test_a_healthy_press_is_green_throughout(self, monkeypatch):
        from magent.sessions import FLASH_TINT_OK

        seen = self._tints(monkeypatch)
        monkeypatch.setattr(
            altv, "upload_image", lambda *a: ("ok", altv.OUTCOME_REASONS["ok"], "")
        )
        altv.handle_press("http://x:8034", "marka", lambda: b"BMP")
        _drain()
        assert all(m.startswith(f"{FLASH_TINT_OK}:") for m in seen), seen

    def test_a_failure_turns_the_bar_red(self, monkeypatch):
        from magent.sessions import FLASH_TINT_ERR, FLASH_TINT_OK

        seen = self._tints(monkeypatch)
        altv.handle_press("http://127.0.0.1:1", "marka", lambda: b"BMP")
        _drain()
        assert seen[-1].startswith(f"{FLASH_TINT_ERR}:"), seen
        # ...and the phases before it were not pre-emptively red.
        assert seen[0].startswith(f"{FLASH_TINT_OK}:"), seen

    def test_a_pending_paste_stays_green_because_the_image_is_safe(self, monkeypatch):
        # Red on this bar reads as "your screenshot is gone". The file is in
        # ~/.magent/uploads and psmux is still being asked to paste it, so red
        # would be the same lie as the old "upload failed".
        from magent.sessions import FLASH_TINT_OK

        seen = self._tints(monkeypatch)
        monkeypatch.setattr(
            altv,
            "upload_image",
            lambda *a: (
                "inject-pending",
                altv.OUTCOME_REASONS["inject-pending"],
                "inject_pending=true",
            ),
        )
        altv.handle_press("http://x:8034", "marka", lambda: b"BMP")
        _drain()
        assert all(m.startswith(f"{FLASH_TINT_OK}:") for m in seen), seen

    def test_the_url_carries_the_tint(self):
        from magent.sessions import FLASH_TINT_ERR, build_flash_url

        url = build_flash_url("http://x:1", "marka", "boom", 1000, FLASH_TINT_ERR)
        assert "tint=err" in url and "ms=1000" in url
        # Omitted, nothing is asked of the style at all.
        assert "tint=" not in build_flash_url("http://x:1", "marka", "boom")


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

    def test_no_image_covers_copied_files_too(self):
        # A press with nothing usable on the clipboard must not tell a user who
        # copied a file in Explorer that only images count.
        assert altv.OUTCOME_REASONS["no-image"] == (
            "clipboard has no image or file - copy one first"
        )

    def test_every_shipped_phrase_is_ascii(self):
        for text in [
            *altv.OUTCOME_REASONS.values(),
            altv.PHASE_CAPTURING,
            altv.PHASE_UPLOADING,
            altv.FLASH_PREFIX,
        ]:
            assert text.isascii(), text


class TestUploadImage:
    def test_the_filename_and_mime_follow_the_bytes(self):
        # The capture emits PNG for the common screenshot DIBs and BMP for
        # exotic ones; the multipart filename is what the server derives the
        # on-disk suffix from, so it must follow the actual magic rather than
        # a hardcoded ".bmp" (1.7 MB BMPs were piling up in
        # ~/.magent/uploads before the PNG capture landed).
        server = _Upload({"ok": True, "injected": True})
        try:
            altv.upload_image(server.url, "marka", b"\x89PNG\r\n\x1a\nrest")
            altv.upload_image(server.url, "marka", b"BM-not-a-png")
        finally:
            server.close()
        png_body, bmp_body = (body for _path, body in server.requests)
        assert b'filename="clipboard.png"' in png_body
        assert b"Content-Type: image/png" in png_body
        assert b'filename="clipboard.bmp"' in bmp_body
        assert b"Content-Type: image/bmp" in bmp_body

    def test_a_healthy_upload_reports_ok(self):
        server = _Upload({"ok": True, "path": "/tmp/x.bmp", "injected": True})
        try:
            outcome, reason, _ = altv.upload_image(server.url, "marka", b"FAKEBMP")
        finally:
            server.close()
        assert outcome == "ok"
        assert reason == altv.OUTCOME_REASONS["ok"]

    def test_the_three_paste_states_are_read_as_three_different_outcomes(self):
        # `injected` alone cannot distinguish "psmux refused" from "psmux has
        # not answered yet", and conflating them is what produced a failure
        # message for a screenshot that was safe on disk.
        cases = {
            ("ok",): {"ok": True, "injected": True},
            ("inject-pending",): {
                "ok": True,
                "injected": False,
                "inject_pending": True,
            },
            ("inject-failed",): {"ok": True, "injected": False},
        }
        for (expected,), reply in cases.items():
            server = _Upload(reply)
            try:
                outcome, reason, _ = altv.upload_image(server.url, "marka", b"x")
            finally:
                server.close()
            assert outcome == expected, reply
            assert reason == altv.OUTCOME_REASONS[expected]

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
        # A 200 whose body is not JSON is the server's problem, not the
        # network's -- so it must reach the JSONDecodeError branch and be
        # named a rejection, never a transport failure. This used its own
        # bare handler that answered without reading the request body, which
        # made the reply race an RST; it now shares `_Upload`'s drained one.
        server = _Upload(raw=b"not-json!")
        try:
            outcome, reason, _ = altv.upload_image(server.url, "marka", b"x")
        finally:
            server.close()
        assert outcome == "upload-rejected"
        assert reason
        # ...and specifically the unreadable-reply reason, not a transport one:
        # the distinction is exactly what the RST race used to erase.
        assert reason == "serve sent an unreadable reply"


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


class TestNativePress:
    """A LOCAL press is one Ctrl+V, not a pipeline.

    ``native=True`` means the listener's manifest carries no ssh host: the
    pane's agent shares the presser's clipboard, so the press delivers the
    paste key and the agent reads the image itself. Nothing is captured,
    nothing is uploaded, and -- exactly-one-attempt law, same as the server's
    inject -- nothing is ever retried.
    """

    def _sends(self, monkeypatch, delivered: bool = True) -> list[tuple]:
        from magent import psmux

        calls: list[tuple] = []

        def _send_keys(name, *keys, target=None, timeout=psmux.SEND_KEYS_TIMEOUT_S):
            calls.append((name, keys, target))
            return delivered

        monkeypatch.setattr(psmux, "send_keys", _send_keys)
        return calls

    def test_one_ctrl_v_no_capture_no_upload(self, monkeypatch):
        calls = self._sends(monkeypatch)
        monkeypatch.setattr(
            altv,
            "upload_image",
            lambda *a, **k: pytest.fail("the native path must never upload"),
        )
        monkeypatch.setattr(altv, "flash_async", lambda *a, **k: None)
        outcome = altv.handle_press(
            "http://127.0.0.1:1",
            "proj",
            capture=lambda: pytest.fail("the native path must never capture"),
            native=True,
        )
        assert outcome == "ok-native"
        # Mirrors the server's inject exactly: same primitive, same -t target.
        assert calls == [("proj", ("C-v",), "proj")]

    def test_the_press_is_acknowledged_before_the_send(self, monkeypatch):
        from magent import psmux

        order: list[str] = []
        monkeypatch.setattr(
            altv,
            "flash_async",
            lambda url, project, message, duration_ms=None, tint=None: order.append(
                f"flash:{message}"
            ),
        )
        monkeypatch.setattr(
            psmux,
            "send_keys",
            lambda name, *keys, **kw: order.append("send") or True,
        )
        altv.handle_press(
            "http://127.0.0.1:1", "proj", capture=lambda: b"", native=True
        )
        assert order[0] == "flash:" + altv.FLASH_PREFIX + altv.PHASE_PASTING
        assert "send" in order

    def test_a_failed_send_reports_native_failed_with_its_own_reason(self, monkeypatch):
        self._sends(monkeypatch, delivered=False)
        flashes: list[tuple[str, str]] = []
        monkeypatch.setattr(
            altv,
            "flash_async",
            lambda url, project, message, duration_ms=None, tint=None: flashes.append(
                (message, tint)
            ),
        )
        outcome = altv.handle_press(
            "http://127.0.0.1:1", "proj", capture=lambda: b"", native=True
        )
        assert outcome == "native-failed"
        message, tint = flashes[-1]
        assert altv.OUTCOME_REASONS["native-failed"] in message
        # An error tint, but an honest one: the reason says the clipboard
        # still holds the image, so nothing sends the user hunting for a file.
        from magent.sessions import FLASH_TINT_ERR

        assert tint == FLASH_TINT_ERR
        assert "clipboard still has the image" in message

    def test_the_native_outcomes_are_vocabulary_members(self):
        assert "ok-native" in altv.ALTV_OUTCOMES
        assert "native-failed" in altv.ALTV_OUTCOMES
        # Success is safe by the simplest argument in the module: nothing was
        # consumed. Failure is NOT safe -- the press did not do its job.
        assert "ok-native" in altv.ALTV_SAFE_OUTCOMES
        assert "native-failed" not in altv.ALTV_SAFE_OUTCOMES

    def test_without_the_flag_the_upload_path_is_byte_for_byte_today_s(
        self, monkeypatch
    ):
        # Compatibility pin: every existing caller that does not pass `native`
        # (the e2e tiers, the remote-wired listener) gets the capture/upload
        # pipeline unchanged -- capture IS called.
        captured: list[bool] = []
        monkeypatch.setattr(altv, "flash_async", lambda *a, **k: None)
        monkeypatch.setattr(
            altv,
            "upload_image",
            lambda url, project, data: ("ok", altv.OUTCOME_REASONS["ok"], ""),
        )
        altv.handle_press(
            "http://127.0.0.1:1", "proj", capture=lambda: captured.append(True) or b"x"
        )
        assert captured == [True]


class TestNativeEnabledGate:
    """MAGENT_ALTV_NATIVE=1 is an OPT-IN to the native local-paste path.
    Off by default because Claude Code on Windows ignores an injected 0x16
    (it acts only on a physical Ctrl+V), so a default-on fork reports
    ok-native while the press pastes nothing -- a silently dead hotkey,
    verified live 2026-08-31. Same degradation doctrine as
    psmux.boost_enabled: a long-lived listener must never die of a bad
    environment, and it degrades to the upload path (the default)."""

    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("MAGENT_ALTV_NATIVE", raising=False)
        monkeypatch.setattr("magent.env._cached_env", None)
        assert altv.native_enabled() is False

    def test_one_opts_in(self, monkeypatch):
        monkeypatch.setenv("MAGENT_ALTV_NATIVE", "1")
        monkeypatch.setattr("magent.env._cached_env", None)
        assert altv.native_enabled() is True

    def test_zero_stays_on_the_upload_path(self, monkeypatch):
        monkeypatch.setenv("MAGENT_ALTV_NATIVE", "0")
        monkeypatch.setattr("magent.env._cached_env", None)
        assert altv.native_enabled() is False


class TestTheUploadLimitIsShared:
    """One number, named the same way on every path that enforces it."""

    def test_the_limit_is_one_hundred_megabytes(self):
        from magent import sessions, upload_server

        assert sessions.MAX_UPLOAD_BYTES == 100 * 1024 * 1024
        # The server enforces the very constant the listener pre-checks against.
        assert upload_server.MAX_UPLOAD_BYTES == sessions.MAX_UPLOAD_BYTES

    def test_the_limit_reads_in_megabytes(self):
        from magent.sessions import upload_limit_text

        assert upload_limit_text(100 * 1024 * 1024) == "100 MB"
        # A test-lowered cap still names itself honestly instead of "0 MB".
        assert upload_limit_text(10) == "10 bytes"

    def test_the_too_large_reason_names_the_limit(self):
        assert "100 MB" in altv.OUTCOME_REASONS["too-large"]


class TestPathsLine:
    """Several files paste as ONE line; a path is quoted only when it has to be."""

    def test_plain_paths_are_space_separated_and_bare(self):
        from magent.sessions import paths_line

        assert paths_line(["C:\\a\\x.py", "/tmp/y.zip"]) == "C:\\a\\x.py /tmp/y.zip"

    def test_a_single_plain_path_is_byte_for_byte_itself(self):
        # Compatibility: the server's one-file inject pasted str(dest) verbatim.
        from magent.sessions import paths_line

        assert paths_line(["/home/u/.magent/uploads/1_x.png"]) == (
            "/home/u/.magent/uploads/1_x.png"
        )

    def test_spaces_and_shell_specials_are_double_quoted(self):
        from magent.sessions import paths_line

        assert paths_line(["C:\\My Docs\\a b.txt", "/tmp/plain"]) == (
            '"C:\\My Docs\\a b.txt" /tmp/plain'
        )
        for special in ("a;b", "a&b", "a(1)", "a'b", "a|b", "a<b>", "a#b"):
            assert paths_line([f"/tmp/{special}"]) == f'"/tmp/{special}"', special

    def test_what_double_quotes_still_expand_falls_back_to_single_quotes(self):
        # Inside "..." a POSIX shell still expands $ and backticks and ends the
        # string at a ", so those paths go in '...' (a ' inside is '\'').
        from magent.sessions import paths_line

        assert paths_line(['/tmp/say "hi"']) == "'/tmp/say \"hi\"'"
        assert paths_line(['/tmp/it\'s "x"']) == "'/tmp/it'\\''s \"x\"'"
        assert paths_line(["/tmp/a$b"]) == "'/tmp/a$b'"
        assert paths_line(["/tmp/a`b"]) == "'/tmp/a`b'"

    # Every character here can end or rewrite the line it is pasted into, and a
    # line break SUBMITS in an agent pane -- quoting cannot make one safe.
    _UNPASTEABLE = ("\n", "\r", "\t", "\x1b", "\x7f", "\x85", "\u2028", "\u2029")

    @pytest.mark.parametrize("ch", _UNPASTEABLE)
    def test_a_control_or_line_break_character_is_never_pasted(self, ch):
        from magent.sessions import paths_line, unpasteable_path

        path = f"C:\\x\\a{ch}b.txt"
        assert unpasteable_path(path)
        with pytest.raises(ValueError, match="control or line-break"):
            paths_line(["C:\\x\\fine.txt", path])

    def test_ordinary_non_ascii_names_still_paste(self):
        from magent.sessions import paths_line, unpasteable_path

        name = (
            "/tmp/caf\N{LATIN SMALL LETTER E WITH ACUTE} "
            "\N{CJK UNIFIED IDEOGRAPH-6587}.txt"
        )
        assert not unpasteable_path(name)
        assert paths_line([name]) == f'"{name}"'


def _files(tmp_path, **named: bytes) -> list[str]:
    """Real files on disk. ``__`` in a key becomes a space, ``_dot_`` a dot."""
    out = []
    for name, data in named.items():
        path = tmp_path / name.replace("__", " ").replace("_dot_", ".")
        path.write_bytes(data)
        out.append(str(path))
    return out


class TestFilePress:
    """Alt+V with files copied in Explorer (CF_HDROP).

    LOCAL (no ssh host): the pane's agent shares this filesystem, so the
    ORIGINAL paths are pasted -- nothing is uploaded or copied, and there is no
    size cap. REMOTE: every file travels in ONE upload request and the server
    pastes all their paths in one line. Either way: one paste attempt, never a
    retry, and a folder anywhere in the copy refuses the whole press.
    """

    def _flashes(self, monkeypatch) -> list[tuple[str, str | None]]:
        seen: list[tuple[str, str | None]] = []
        monkeypatch.setattr(
            altv,
            "flash_async",
            lambda url, project, message, duration_ms=None, tint=None: seen.append(
                (message, tint)
            ),
        )
        return seen

    def _sends(self, monkeypatch, delivered: bool = True) -> list[tuple]:
        from magent import psmux

        calls: list[tuple] = []

        def _send_keys(name, *keys, target=None, literal=False, **kw):
            calls.append((name, keys, target, literal))
            return delivered

        monkeypatch.setattr(psmux, "send_keys", _send_keys)
        return calls

    def _no_upload(self, monkeypatch):
        monkeypatch.setattr(
            altv,
            "upload_files",
            lambda *a, **k: pytest.fail("this press must never upload"),
        )

    def test_local_pastes_the_original_paths_in_one_line_and_uploads_nothing(
        self, monkeypatch, tmp_path
    ):
        from magent.sessions import FLASH_TINT_OK, paths_line

        seen = self._flashes(monkeypatch)
        calls = self._sends(monkeypatch)
        self._no_upload(monkeypatch)
        paths = _files(tmp_path, a_dot_py=b"print(1)", my__notes_dot_txt=b"hi")

        outcome = altv.handle_file_press(
            "http://127.0.0.1:1", "proj", lambda: paths, local=True
        )

        assert outcome == "ok-paths"
        # ONE send, the whole selection, literal text, aimed at the pane.
        assert calls == [("proj", (paths_line(paths),), "proj", True)]
        assert f'"{paths[1]}"' in calls[0][1][0]  # the spaced one is quoted
        assert seen[-1] == (f"{altv.FLASH_PREFIX}2 file paths pasted", FLASH_TINT_OK)

    def test_local_has_no_size_cap(self, monkeypatch, tmp_path):
        self._flashes(monkeypatch)
        calls = self._sends(monkeypatch)
        monkeypatch.setattr(altv, "MAX_UPLOAD_BYTES", 4)
        paths = _files(tmp_path, big_dot_bin=b"x" * 64)
        assert (
            altv.handle_file_press("http://x:1", "proj", lambda: paths, local=True)
            == "ok-paths"
        )
        assert len(calls) == 1

    def test_local_narrates_acknowledgement_then_pasting_then_outcome(
        self, monkeypatch, tmp_path
    ):
        from magent import psmux

        order: list[str] = []
        monkeypatch.setattr(
            altv,
            "flash_async",
            lambda url, project, message, duration_ms=None, tint=None: order.append(
                message
            ),
        )
        monkeypatch.setattr(
            psmux, "send_keys", lambda *a, **k: order.append("send") or True
        )
        paths = _files(tmp_path, a_dot_py=b"x")

        def _capture():
            order.append("capture")
            return paths

        altv.handle_file_press("http://x:1", "proj", _capture, local=True)
        assert order == [
            altv.FLASH_PREFIX + altv.PHASE_CAPTURING,
            "capture",
            altv.FLASH_PREFIX + altv.PHASE_PASTING,
            "send",
            altv.FLASH_PREFIX + "file path pasted",
        ]

    def test_a_failed_local_paste_is_reported_once_and_never_retried(
        self, monkeypatch, tmp_path
    ):
        from magent.sessions import FLASH_TINT_ERR

        seen = self._flashes(monkeypatch)
        calls = self._sends(monkeypatch, delivered=False)
        self._no_upload(monkeypatch)
        paths = _files(tmp_path, a_dot_py=b"x")

        outcome = altv.handle_file_press(
            "http://x:1", "proj", lambda: paths, local=True
        )

        assert outcome == "paths-failed"
        assert len(calls) == 1  # exactly one attempt: a killed send may have landed
        assert seen[-1] == (
            altv.FLASH_PREFIX + altv.OUTCOME_REASONS["paths-failed"],
            FLASH_TINT_ERR,
        )

    @pytest.mark.parametrize("local", [True, False])
    def test_a_folder_refuses_the_whole_press_and_nothing_moves(
        self, monkeypatch, tmp_path, local
    ):
        seen = self._flashes(monkeypatch)
        calls = self._sends(monkeypatch)
        self._no_upload(monkeypatch)
        folder = tmp_path / "some dir"
        folder.mkdir()
        # Mixed: a real file AND a folder -- still refused whole, no partial.
        paths = [*_files(tmp_path, a_dot_py=b"x"), str(folder)]

        outcome = altv.handle_file_press(
            "http://127.0.0.1:1", "proj", lambda: paths, local=local
        )

        assert outcome == "folder-refused"
        assert calls == []
        assert seen[-1][0] == altv.FLASH_PREFIX + "folders not supported - copy files"

    @pytest.mark.parametrize("local", [True, False])
    def test_a_file_that_vanished_is_named_and_nothing_moves(
        self, monkeypatch, tmp_path, local
    ):
        seen = self._flashes(monkeypatch)
        calls = self._sends(monkeypatch)
        self._no_upload(monkeypatch)
        paths = [*_files(tmp_path, a_dot_py=b"x"), str(tmp_path / "gone.txt")]

        outcome = altv.handle_file_press(
            "http://127.0.0.1:1", "proj", lambda: paths, local=local
        )

        assert outcome == "file-missing"
        assert calls == []
        assert seen[-1][0] == altv.FLASH_PREFIX + altv.OUTCOME_REASONS["file-missing"]

    @pytest.mark.parametrize("ch", ["\n", "\x1b", "\x85", "\u2028"])
    def test_a_local_path_with_a_control_character_is_refused_not_typed(
        self, monkeypatch, tmp_path, ch
    ):
        # The original path IS what a local press types; one carrying a line
        # break would submit whatever the user had in the input line.
        import pathlib

        seen = self._flashes(monkeypatch)
        calls = self._sends(monkeypatch)
        self._no_upload(monkeypatch)
        weird = str(tmp_path / f"a{ch}b.txt")
        monkeypatch.setattr(pathlib.Path, "is_file", lambda self: True)
        monkeypatch.setattr(pathlib.Path, "is_dir", lambda self: False)

        outcome = altv.handle_file_press(
            "http://x:1",
            "proj",
            lambda: [*_files(tmp_path, ok_dot_txt=b"x"), weird],
            local=True,
        )

        assert outcome == "path-unpasteable"
        assert calls == [], "nothing is typed -- not even the clean paths"
        assert seen[-1][0] == (
            altv.FLASH_PREFIX + altv.OUTCOME_REASONS["path-unpasteable"]
        )

    def test_an_empty_read_is_unreadable_not_no_image(self, monkeypatch):
        seen = self._flashes(monkeypatch)
        self._no_upload(monkeypatch)
        outcome = altv.handle_file_press("http://x:1", "proj", list, local=True)
        assert outcome == "clipboard-unreadable"
        assert "copied files" in seen[-1][0]

    def test_remote_uploads_every_file_in_one_request(self, monkeypatch, tmp_path):
        seen = self._flashes(monkeypatch)
        calls = self._sends(monkeypatch)
        zip_bytes = bytes(range(256)) * 3  # binary, CR/LF included
        paths = _files(tmp_path, a_dot_zip=zip_bytes, script_dot_py=b"print(1)\r\n")
        server = _Upload({"ok": True, "paths": ["/u/1", "/u/2"], "injected": True})
        try:
            outcome = altv.handle_file_press(
                server.url, "proj", lambda: paths, local=False
            )
        finally:
            server.close()

        assert outcome == "ok"
        assert calls == [], "the SERVER pastes a remote press, never the listener"
        assert len(server.requests) == 1, "one request carries the whole selection"
        path, body = server.requests[0]
        assert path == "/upload?project=proj"
        assert b'filename="a.zip"' in body and zip_bytes in body
        assert b'filename="script.py"' in body and b"print(1)\r\n" in body
        assert b'name="inject"\r\n\r\n1\r\n' in body
        assert seen[-1][0] == f"{altv.FLASH_PREFIX}2 files sent"

    def test_remote_narrates_uploading_before_the_post(self, monkeypatch, tmp_path):
        order: list[str] = []
        monkeypatch.setattr(
            altv,
            "flash_async",
            lambda url, project, message, duration_ms=None, tint=None: order.append(
                message
            ),
        )
        monkeypatch.setattr(
            altv,
            "upload_files",
            lambda url, project, files: (
                order.append("upload") or ("ok", "file sent", "")
            ),
        )
        paths = _files(tmp_path, a_dot_py=b"x")
        altv.handle_file_press("http://x:1", "proj", lambda: paths, local=False)
        assert order == [
            altv.FLASH_PREFIX + altv.PHASE_CAPTURING,
            altv.FLASH_PREFIX + altv.PHASE_UPLOADING,
            "upload",
            altv.FLASH_PREFIX + "file sent",
        ]

    def test_remote_over_the_limit_is_refused_before_any_file_is_read(
        self, monkeypatch, tmp_path
    ):
        import pathlib

        seen = self._flashes(monkeypatch)
        self._no_upload(monkeypatch)
        monkeypatch.setattr(altv, "MAX_UPLOAD_BYTES", 10)
        paths = _files(tmp_path, a_dot_bin=b"x" * 8, b_dot_bin=b"y" * 8)
        monkeypatch.setattr(
            pathlib.Path,
            "read_bytes",
            lambda self: pytest.fail("an over-limit file must never be read"),
        )

        outcome = altv.handle_file_press(
            "http://x:1", "proj", lambda: paths, local=False
        )

        assert outcome == "too-large"
        assert seen[-1][0] == altv.FLASH_PREFIX + "too large - 10 bytes limit"

    def test_remote_pending_keeps_its_meaning(self, monkeypatch, tmp_path):
        seen = self._flashes(monkeypatch)
        paths = _files(tmp_path, a_dot_py=b"x")
        server = _Upload({"ok": True, "injected": False, "inject_pending": True})
        try:
            outcome = altv.handle_file_press(
                server.url, "proj", lambda: paths, local=False
            )
        finally:
            server.close()
        assert outcome == "inject-pending"
        assert seen[-1][0] == (
            altv.FLASH_PREFIX + "file saved - psmux is slow, paste still pending"
        )

    def test_an_unexpected_error_is_caught_logged_and_shown(self, monkeypatch, caplog):
        seen = self._flashes(monkeypatch)

        def _boom():
            raise OSError("clipboard went away")

        with caplog.at_level("INFO", logger="magent.hotkey"):
            outcome = altv.handle_file_press("http://x:1", "proj", _boom, local=True)
        assert outcome == "error"
        assert "ALTV outcome=error project=proj" in caplog.text
        assert seen[-1][0] == altv.FLASH_PREFIX + altv.OUTCOME_REASONS["error"]


def _server_view(headers: dict[str, str], body: bytes):
    """What the REAL server parser makes of a request the listener sent."""
    import io

    from magent.upload_server import _parse_multipart

    class _Handler:
        pass

    handler = _Handler()
    handler.headers = {
        "Content-Type": headers["Content-Type"],
        "Content-Length": str(len(body)),
    }
    handler.rfile = io.BytesIO(body)
    return _parse_multipart(handler)


class TestUploadLimitText:
    def test_exactly_one_megabyte_reads_in_megabytes(self):
        from magent.sessions import upload_limit_text

        assert upload_limit_text(1024 * 1024) == "1 MB"

    def test_a_zero_limit_never_reads_as_zero_megabytes(self):
        from magent.sessions import upload_limit_text

        assert upload_limit_text(0) == "0 bytes"


class TestTheListenerRequestRoundTrips:
    """The listener's multipart, read back by the server's own parser."""

    def _send(self, monkeypatch, files):
        monkeypatch.setattr(altv, "flash_async", lambda *a, **k: None)
        server = _Upload({"ok": True, "injected": True})
        try:
            assert altv.upload_files(server.url, "proj", files)[0] == "ok"
        finally:
            server.close()
        return _server_view(server.headers[0], server.requests[0][1])

    def test_a_file_ending_in_crlf_keeps_its_last_bytes(self, monkeypatch):
        fields, files = self._send(monkeypatch, [("a.bat", b"echo 1\r\n\r\n")])
        assert files["file"] == [("a.bat", b"echo 1\r\n\r\n")]
        assert fields == {"project": "proj", "inject": "1"}

    def test_a_quote_in_a_name_cannot_cut_the_name_short(self, monkeypatch):
        _fields, files = self._send(monkeypatch, [('say "hi".txt', b"x")])
        assert files["file"] == [("say _hi_.txt", b"x")]

    def test_a_line_break_in_a_name_cannot_end_the_header(self, monkeypatch):
        _fields, files = self._send(monkeypatch, [("a\r\nb.txt", b"x")])
        assert files["file"] == [("a__b.txt", b"x")]


class TestReport:
    def test_ok_paths_is_logged_as_a_success_not_a_warning(self, monkeypatch, caplog):
        monkeypatch.setattr(altv, "flash_async", lambda *a, **k: None)
        with caplog.at_level("INFO", logger="magent.hotkey"):
            altv.report("http://x:1", "p", "ok-paths", "file path pasted")
        records = [r for r in caplog.records if "outcome=ok-paths" in r.getMessage()]
        assert records
        assert all(r.levelname == "INFO" for r in records)


class TestRemoteLimitBoundary:
    def test_a_selection_exactly_at_the_limit_is_sent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(altv, "flash_async", lambda *a, **k: None)
        monkeypatch.setattr(altv, "MAX_UPLOAD_BYTES", 16)
        sent: list[object] = []
        monkeypatch.setattr(
            altv,
            "upload_files",
            lambda url, project, files: sent.append(files) or ("ok", "sent", ""),
        )
        paths = _files(tmp_path, a_dot_bin=b"x" * 8, b_dot_bin=b"y" * 8)
        outcome = altv.handle_file_press("http://x:1", "p", lambda: paths, local=False)
        assert outcome == "ok"
        assert len(sent) == 1


# What the kernel may hold of a body on each end of the stand-in slow link.
# Loopback is not a slow link: Linux autotunes the sender's buffer to
# tcp_wmem's max (4 MiB, measured) against a reader that is slow on purpose,
# so it swallows most of a 6 MiB body in a few quick sends and the only slow
# operation left is the wait for the reply while that backlog drains -- ~1.9 s
# against a 2 s budget, the 8-in-10 "serve-unreachable" on an idle Linux box.
# Capped, the slowness stays in the sends, where a real slow link puts it.
_LINK_BUFFER_BYTES = 64 * 1024


class _SlowUpload:
    """A stand-in /upload that reads the body slowly but steadily: a link that
    is always making progress, just not fast.

    Both kernel buffers are capped (``_LINK_BUFFER_BYTES``): the server's here,
    the client's through ``monkeypatch`` on the connection urllib opens.
    """

    def __init__(self, chunk: int, pause_s: float, monkeypatch: pytest.MonkeyPatch):
        self.received: list[int] = []
        real_connect = socket.create_connection

        def capped_connect(*args, **kwargs):
            sock = real_connect(*args, **kwargs)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, _LINK_BUFFER_BYTES)
            return sock

        monkeypatch.setattr(socket, "create_connection", capped_connect)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                left = int(self.headers.get("Content-Length", 0))
                got = 0
                while left > 0:
                    data = self.rfile.read(min(chunk, left))
                    if not data:
                        break
                    got += len(data)
                    left -= len(data)
                    time.sleep(pause_s)
                outer.received.append(got)
                reply = json.dumps({"ok": True, "injected": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args):
                pass

        # Set before listen() so every accepted socket inherits it.
        self.server = HTTPServer(("127.0.0.1", 0), Handler, bind_and_activate=False)
        self.server.socket.setsockopt(
            socket.SOL_SOCKET, socket.SO_RCVBUF, _LINK_BUFFER_BYTES
        )
        self.server.server_bind()
        self.server.server_activate()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=10)


class TestTheRemoteBodyIsStreamed:
    """A remote press sends its files straight off disk, a block at a time."""

    def test_a_file_containing_the_old_fixed_boundary_arrives_byte_identical(
        self, monkeypatch, tmp_path
    ):
        # Any file goes now -- a log, a .eml, a HAR, a test fixture -- and one
        # that happened to hold the listener's FIXED boundary line used to be
        # cut there, with the server saying ok.
        monkeypatch.setattr(altv, "flash_async", lambda *a, **k: None)
        payload = b"line one\r\n------MagentUpload\r\nline three\r\n"
        paths = _files(tmp_path, dump_dot_txt=payload)
        server = _Upload({"ok": True, "injected": True})
        try:
            outcome = altv.handle_file_press(
                server.url, "proj", lambda: paths, local=False
            )
        finally:
            server.close()
        assert outcome == "ok"
        _fields, files = _server_view(server.headers[0], server.requests[0][1])
        assert files["file"] == [("dump.txt", payload)]

    def test_every_request_draws_its_own_boundary(self, monkeypatch):
        monkeypatch.setattr(altv, "flash_async", lambda *a, **k: None)
        server = _Upload({"ok": True, "injected": True})
        try:
            for _ in range(2):
                altv.upload_files(server.url, "proj", [("a.txt", b"x")])
        finally:
            server.close()
        first, second = (h["Content-Type"] for h in server.headers)
        assert first != second
        assert len(first.split("boundary=", 1)[1]) >= len("----MagentUpload") + 32

    def test_the_body_length_is_declared_up_front(self, monkeypatch, tmp_path):
        # The server reads exactly Content-Length and speaks no chunked
        # encoding, so a streamed body must still say how long it is.
        monkeypatch.setattr(altv, "flash_async", lambda *a, **k: None)
        paths = _files(tmp_path, a_dot_bin=bytes(range(256)) * 40)
        server = _Upload({"ok": True, "injected": True})
        try:
            altv.handle_file_press(server.url, "proj", lambda: paths, local=False)
        finally:
            server.close()
        headers = server.headers[0]
        assert "Transfer-Encoding" not in headers
        assert int(headers["Content-Length"]) == len(server.requests[0][1])

    def test_a_remote_press_never_loads_a_whole_file(self, monkeypatch, tmp_path):
        # The listener is long-lived; holding every file (and then a joined
        # copy of them all) is 200 MB for one 100 MB press.
        import pathlib

        monkeypatch.setattr(altv, "flash_async", lambda *a, **k: None)
        paths = _files(tmp_path, a_dot_bin=b"a" * 5000, b_dot_bin=b"b" * 7000)
        monkeypatch.setattr(
            pathlib.Path,
            "read_bytes",
            lambda self: pytest.fail("a remote press must stream, not read_bytes"),
        )
        server = _Upload({"ok": True, "injected": True})
        try:
            outcome = altv.handle_file_press(
                server.url, "proj", lambda: paths, local=False
            )
        finally:
            server.close()
        assert outcome == "ok"
        _fields, files = _server_view(server.headers[0], server.requests[0][1])
        assert files["file"] == [("a.bin", b"a" * 5000), ("b.bin", b"b" * 7000)]

    def test_a_slow_but_steady_link_is_not_a_timeout(self, monkeypatch, tmp_path):
        # The timeout bounds each socket operation, not the whole send: a
        # large selection over a slow tailnet takes as long as it takes, and
        # only a link that STOPS moving is "cannot reach magent serve".
        monkeypatch.setattr(altv, "flash_async", lambda *a, **k: None)
        # ~60 ms between reads against a 2 s per-operation budget: wide enough
        # that a loaded CI box never starves one operation past it, while the
        # whole send (~2.9 s) still outlasts it -- a single-sendall body, whose
        # timeout is a TOTAL budget, fails here.
        monkeypatch.setattr(altv, "UPLOAD_HTTP_TIMEOUT_S", 2.0)
        size = 6 * 1024 * 1024
        paths = _files(tmp_path, big_dot_bin=b"z" * size)
        server = _SlowUpload(chunk=128 * 1024, pause_s=0.06, monkeypatch=monkeypatch)
        # When the last block of the body left, so the slowness is proven to be
        # in the SEND -- the thing this test is about -- and not in the wait for
        # the reply, which one operation's budget bounds as a whole.
        real_read = altv._StreamedBody.read
        last_block: list[float] = []

        def timed_read(body, size=-1):
            chunk = real_read(body, size)
            if chunk:
                last_block[:] = [time.monotonic()]
            return chunk

        monkeypatch.setattr(altv._StreamedBody, "read", timed_read)
        started = time.monotonic()
        try:
            outcome = altv.handle_file_press(
                server.url, "proj", lambda: paths, local=False
            )
        finally:
            server.close()
        assert outcome == "ok"
        assert last_block and last_block[0] - started > altv.UPLOAD_HTTP_TIMEOUT_S, (
            "the send was not slow enough: the kernel buffered the body"
        )
        assert server.received and server.received[0] > size

    def test_a_file_that_grows_mid_send_sends_only_what_it_declared(
        self, monkeypatch, tmp_path
    ):
        # Content-Length is fixed when the body is built. A file that grows after
        # it was sized (a log still being written) must send exactly the size it
        # declared: one byte more and the server's read stops short of the
        # closing delimiter, so the press is refused as a cut-short body.
        monkeypatch.setattr(altv, "flash_async", lambda *a, **k: None)
        original = b"line one\r\n" * 50
        paths = _files(tmp_path, app_dot_log=original)
        real_init = altv._StreamedBody.__init__

        def sized_then_grown(self, segments):
            real_init(self, segments)
            with open(paths[0], "ab") as grow:
                grow.write(b"GROWN AFTER SIZING\r\n" * 20)

        monkeypatch.setattr(altv._StreamedBody, "__init__", sized_then_grown)
        server = _Upload({"ok": True, "injected": True})
        try:
            outcome = altv.handle_file_press(
                server.url, "proj", lambda: paths, local=False
            )
        finally:
            server.close()
        assert outcome == "ok"
        headers, (_path, body) = server.headers[0], server.requests[0]
        boundary = headers["Content-Type"].split("boundary=", 1)[1]
        assert len(body) == int(headers["Content-Length"])
        assert body.endswith(f"--{boundary}--\r\n".encode())
        assert original + b"\r\n--" + boundary.encode() in body
        assert b"GROWN" not in body


class TestARemoteFileThatWillNotRead:
    """A copied file that fails between the check and the send is NAMED -- never
    the generic "unexpected error", and never "cannot reach magent serve"."""

    def _flashes(self, monkeypatch) -> list[str]:
        seen: list[str] = []
        monkeypatch.setattr(
            altv,
            "flash_async",
            lambda url, project, message, duration_ms=None, tint=None: seen.append(
                message
            ),
        )
        return seen

    def test_a_file_held_by_another_app_is_unreadable(self, monkeypatch, tmp_path):
        import pathlib

        seen = self._flashes(monkeypatch)
        paths = _files(tmp_path, locked_dot_xlsx=b"x" * 64)

        def _locked(self, *a, **k):
            raise PermissionError(13, "The process cannot access the file", str(self))

        monkeypatch.setattr(pathlib.Path, "open", _locked)
        server = _Upload({"ok": True, "injected": True})
        try:
            outcome = altv.handle_file_press(
                server.url, "proj", lambda: paths, local=False
            )
        finally:
            server.close()
        assert outcome == "file-unreadable"
        assert server.requests == [], "nothing is sent for a file that would not open"
        assert seen[-1] == altv.FLASH_PREFIX + altv.OUTCOME_REASONS["file-unreadable"]

    def test_a_file_deleted_after_the_check_is_missing(self, monkeypatch, tmp_path):
        seen = self._flashes(monkeypatch)
        # The refusal check passed; the file went before it could be sized.
        monkeypatch.setattr(altv, "_refusal", lambda paths: None)
        monkeypatch.setattr(
            altv, "upload_files", lambda *a, **k: pytest.fail("nothing to upload")
        )
        outcome = altv.handle_file_press(
            "http://x:1", "proj", lambda: [str(tmp_path / "gone.bin")], local=False
        )
        assert outcome == "file-missing"
        assert seen[-1] == altv.FLASH_PREFIX + altv.OUTCOME_REASONS["file-missing"]

    def test_a_file_deleted_before_it_opens_is_missing(self, monkeypatch, tmp_path):
        seen = self._flashes(monkeypatch)
        src = tmp_path / "gone.bin"
        server = _Upload({"ok": True, "injected": True})
        try:
            outcome = altv.upload_files(server.url, "proj", [("gone.bin", src)])
        finally:
            server.close()
        assert outcome[0] == "file-missing"
        assert server.requests == []
        assert seen == []  # upload_files returns; the press is what reports

    def test_a_file_that_shrinks_mid_send_is_named_not_a_dead_server(
        self, monkeypatch, tmp_path
    ):
        import types

        seen = self._flashes(monkeypatch)
        paths = _files(tmp_path, growing_dot_log=b"x" * 100)
        # Sized at 10000 bytes when opened; only 100 are there to send.
        monkeypatch.setattr(
            altv,
            "os",
            types.SimpleNamespace(
                fstat=lambda fd: types.SimpleNamespace(st_size=10_000)
            ),
        )
        monkeypatch.setattr(altv, "MAX_UPLOAD_BYTES", 1 << 30)
        server = _Upload({"ok": True, "injected": True})
        # The shrink guard is the only thing that stops the body stream
        # looping forever on a file with fewer bytes than it was sized at. The
        # press runs on a thread with a deadline, and the stand-in's read of
        # the declared length is bounded, so losing the guard FAILS in seconds
        # instead of hanging the suite (and the CI job) with no result.
        server.server.RequestHandlerClass.timeout = 5
        result: list[str] = []
        press = threading.Thread(
            target=lambda: result.append(
                altv.handle_file_press(server.url, "proj", lambda: paths, local=False)
            ),
            daemon=True,
        )
        press.start()
        press.join(15)
        try:
            assert not press.is_alive(), "the body stream spun on a file that shrank"
        finally:
            server.close()
        assert result == ["file-unreadable"]
        assert seen[-1] == altv.FLASH_PREFIX + "a copied file changed while it was sent"

    def test_a_read_that_fails_mid_send_is_unreadable(self, monkeypatch, tmp_path):
        import io
        import pathlib
        import types

        seen = self._flashes(monkeypatch)
        paths = _files(tmp_path, flaky_dot_bin=b"x" * 64)

        class _Flaky(io.BytesIO):
            def read(self, size=-1):
                raise OSError(5, "Input/output error")

            def fileno(self):
                return 0

        monkeypatch.setattr(pathlib.Path, "open", lambda self, *a, **k: _Flaky())
        monkeypatch.setattr(
            altv,
            "os",
            types.SimpleNamespace(fstat=lambda fd: types.SimpleNamespace(st_size=64)),
        )
        server = _Upload({"ok": True, "injected": True})
        try:
            outcome = altv.handle_file_press(
                server.url, "proj", lambda: paths, local=False
            )
        finally:
            server.close()
        assert outcome == "file-unreadable"
        assert seen[-1] == altv.FLASH_PREFIX + altv.OUTCOME_REASONS["file-unreadable"]
