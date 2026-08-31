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
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                # Drain BEFORE replying -- see the class docstring.
                outer.requests.append((self.path, self.rfile.read(length)))
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
            "not-a-magent-window",
            "no-image",
            "clipboard-unreadable",
            "serve-unreachable",
            "upload-rejected",
            "inject-failed",
            "inject-pending",
            "native-failed",
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
        # or it has not landed YET.
        assert set(altv.ALTV_SAFE_OUTCOMES) == {"ok", "ok-native", "inject-pending"}
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
