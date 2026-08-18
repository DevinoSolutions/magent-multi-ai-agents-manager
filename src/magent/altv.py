"""One Alt+V press, narrated from chord to outcome.

The keyboard hook itself is win32-only (``hotkey.py`` raises ImportError
elsewhere by design), but *what a press does* is plain sockets and strings, so
it lives here: importable on every OS, and therefore testable on every OS --
including by a real-serve e2e that drives a press without a keyboard.

The narration is the point. The listener runs hidden with no terminal, so the
project's psmux status line is the ONLY screen it owns. Every press walks the
same phases through it::

    Alt+V: capturing...      the chord was ours; nothing has been read yet
    Alt+V: uploading...      the clipboard image is in hand, the POST starts
    Alt+V: image sent        (or a SPECIFIC reason it did not land)

Two rules hold the design together:

* **Never block the press.** Flashes are queued to one pump thread
  (``flash_async``) and the press thread continues immediately -- a dead or
  slow ``magent serve`` costs a press nothing.
* **One pump, so phases stay in order.** Three fire-and-forget threads would
  race, and a "sent" that overtakes an "uploading" leaves the bar lying. The
  pump is FIFO and waits for each flash to land before sending the next, which
  is exactly the pacing the status bar wants.

Everything on the wire here is ASCII (``_ascii_clip``). A status bar is where
the renderer's and the multiplexer's width arithmetic must agree, and an
ambiguous-width glyph has corrupted this bar before -- see psmux.py's
``_STATUS_HINTS`` note.
"""

from __future__ import annotations

import json
import queue
import threading
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from magent.log import get_logger
from magent.sessions import (
    FLASH_MSG_MAX,
    FLASH_TINT_ERR,
    FLASH_TINT_OK,
    build_flash_url,
)

if TYPE_CHECKING:
    from collections.abc import Callable

# Every Alt+V press ends in exactly one line carrying this prefix, so the whole
# history of the chord is one grep:
#
#     grep ALTV ~/.magent/logs/hotkey.log
#
# Outcomes are a closed vocabulary so the log stays greppable per-outcome, not
# just per-prefix -- and because each one maps to its own on-screen reason.
ALTV_LOG_PREFIX = "ALTV"

ALTV_OUTCOMES = (
    "ok",  # image uploaded and injected into the pane
    "not-a-magent-window",  # pass-through: the chord was not ours to handle
    "no-image",  # magent window focused, but the clipboard holds no image
    "clipboard-unreadable",  # CF_DIB said yes, the read came back empty
    "serve-unreachable",  # nothing answered on server_url
    "upload-rejected",  # the server answered, and said no
    "inject-failed",  # the server stored it, psmux would not paste it
    "error",  # anything unforeseen, with a traceback in the log
)

# What each outcome says on the status bar. Split from the log vocabulary so a
# reason can be reworded without breaking `grep ALTV outcome=...`, and kept
# here (not inline at the raise sites) so "does every outcome have a reason?"
# is one assertion. Generic text is the enemy: the complaint these answer is
# "it did nothing and I cannot tell why".
OUTCOME_REASONS: dict[str, str] = {
    "ok": "image sent",
    "no-image": "clipboard has no image - copy one first",
    "clipboard-unreadable": "could not read the image from the clipboard",
    "serve-unreachable": "cannot reach magent serve",
    "upload-rejected": "magent serve refused it",
    "inject-failed": "saved, but psmux would not paste it",
    "error": "unexpected error - see hotkey.log",
}

PHASE_CAPTURING = "capturing..."
PHASE_UPLOADING = "uploading..."

# Prefix every flash so a message on the bar is attributable at a glance -- the
# same window also carries F2's messages and the server's own.
FLASH_PREFIX = "Alt+V: "

# How long a queued flash may spend on the wire before the pump gives up on it
# and moves to the next.
#
# This must stay LARGER than the server's own status-line bound
# (``psmux.FLASH_TIMEOUT_S``), and a test pins that. The reason is the ordering
# guarantee: `/api/flash` answers only once psmux has the message, so the reply
# is what paces the pump. If the pump abandons a request the server is still
# working on, the next phase overlaps it and the two can land out of order --
# precisely under the load (a slow status line) this whole change exists to
# survive. The wait is affordable because it happens on the pump thread, never
# on the press; and abandoning a flash early is how the bar went blank in the
# first place (see psmux.flash_message).
FLASH_HTTP_TIMEOUT_S = 25.0

# Bound on the pump's backlog. A wedged server must cost memory nothing; 32 is
# far more than a human can generate (3 per press) and a full queue drops the
# NEWEST message, keeping the ordered story already queued intact.
FLASH_QUEUE_MAX = 32

# A PHASE message must outlive the step it narrates -- a "uploading..." that
# expires mid-upload leaves the bar blank, which is the silence this whole
# channel exists to end. Outcomes take the server's own (shorter) default.
PHASE_FLASH_MS = 20000

_flash_queue: queue.Queue[tuple[str, str, str, int | None, str]] = queue.Queue(
    maxsize=FLASH_QUEUE_MAX
)
_pump_lock = threading.Lock()
_pump: threading.Thread | None = None


def _ascii_clip(message: str) -> str:
    """ASCII-only, status-bar-sized. Never let a server-supplied reason smuggle
    a wide or ambiguous-width glyph onto the bar (or a newline into the URL)."""
    flat = " ".join(message.split())
    ascii_only = flat.encode("ascii", "replace").decode("ascii")
    return ascii_only[:FLASH_MSG_MAX]


def flash_status(
    server_url: str,
    project: str,
    message: str,
    duration_ms: int | None = None,
    tint: str = FLASH_TINT_OK,
) -> None:
    """Blocking: show ``message`` in the magent:<project> status line.

    The whole call is swallowed on purpose -- feedback must never be able to
    break the action it reports on, and the log line beside each call site
    stays the durable record. Callers on a hot path want ``flash_async``.
    """
    try:
        with urlopen(
            build_flash_url(server_url, project, message, duration_ms, tint),
            timeout=FLASH_HTTP_TIMEOUT_S,
        ):
            pass
    except Exception as exc:  # noqa: BLE001  # reason: a flash is best-effort by construction; every failure mode (dead serve, DNS, timeout, malformed reply) must degrade to a log line
        get_logger("hotkey").debug(
            "flash not delivered project=%s (%s): %s", project, type(exc).__name__, exc
        )


def _pump_loop() -> None:
    """Deliver queued flashes forever, one at a time, and never die.

    The catch-all is load-bearing, not defensive habit: a pump that ends on one
    bad message strands every message queued behind it, so the failure mode is
    "the status line went quiet an hour ago and nobody noticed" -- the exact
    class of bug this whole module exists to close.
    """
    while True:
        server_url, project, message, duration_ms, tint = _flash_queue.get()
        try:
            flash_status(server_url, project, message, duration_ms, tint)
        except Exception:  # noqa: BLE001  # reason: the pump must outlive every possible bad message; see the docstring
            get_logger("hotkey").exception("flash pump: delivery raised")
        finally:
            _flash_queue.task_done()


def flash_async(
    server_url: str,
    project: str,
    message: str,
    duration_ms: int | None = None,
    tint: str = FLASH_TINT_OK,
) -> None:
    """Queue a status-line flash and return immediately.

    Ordering is the reason this is a queue and not a thread per call: the
    phases of one press only mean anything in sequence. Returning immediately
    is the reason it is not a plain call: a press must never wait on its own
    progress report.
    """
    global _pump  # noqa: PLW0603  # reason: one lazily-started daemon pump for the process; a module-level singleton is the point
    text = _ascii_clip(message)
    try:
        with _pump_lock:
            if _pump is None or not _pump.is_alive():
                _pump = threading.Thread(
                    target=_pump_loop, name="magent-altv-flash", daemon=True
                )
                _pump.start()
        _flash_queue.put_nowait((server_url, project, text, duration_ms, tint))
    except (RuntimeError, queue.Full) as exc:
        # Thread exhaustion or a wedged pump. The press carries on regardless.
        get_logger("hotkey").warning("flash dropped project=%s: %s", project, exc)


def report(server_url: str, project: str, outcome: str, detail: str = "") -> None:
    """Record one Alt+V outcome, and show it.

    Both halves are unconditional now. The log line is the durable record; the
    flash is what the user actually sees, and SUCCESS needs it as much as
    failure does -- a press whose only trace is a log file is indistinguishable
    from a listener that never ran.
    """
    log = get_logger("hotkey")
    reason = detail or OUTCOME_REASONS.get(outcome, outcome)
    if outcome == "ok":
        log.info("%s outcome=%s project=%s", ALTV_LOG_PREFIX, outcome, project)
    else:
        log.warning(
            "%s outcome=%s project=%s: %s", ALTV_LOG_PREFIX, outcome, project, reason
        )
    flash_async(
        server_url,
        project,
        FLASH_PREFIX + reason,
        tint=FLASH_TINT_OK if outcome == "ok" else FLASH_TINT_ERR,
    )


def _transport_reason(exc: BaseException) -> str:
    """A short, ASCII, human reason for a failed POST.

    Windows spells a refused connection ``[WinError 10061] No connection could
    be made because the target machine actively refused it``, which is a
    paragraph on a one-line bar -- so the cases worth distinguishing are named
    explicitly and everything else degrades to the exception type.
    """
    reason: object = getattr(exc, "reason", exc)
    if isinstance(reason, TimeoutError) or isinstance(exc, TimeoutError):
        return "timed out"
    if isinstance(reason, ConnectionRefusedError):
        return "connection refused"
    if isinstance(reason, ConnectionResetError):
        return "connection reset"
    if isinstance(reason, OSError):
        return type(reason).__name__
    return str(reason)[:60] or type(exc).__name__


def upload_image(
    server_url: str, project: str, image_data: bytes
) -> tuple[str, str, str]:
    """POST one clipboard image. Returns ``(outcome, reason, log_detail)``.

    ``outcome`` is a member of ``ALTV_OUTCOMES``; ``reason`` is what the status
    line should say; ``log_detail`` carries the full (possibly long, possibly
    non-ASCII) cause for hotkey.log. Splitting the three is what lets the bar
    stay short and specific while the log stays complete.
    """
    boundary = "----MagentUpload"
    delim = f"--{boundary}"
    body = (
        (
            f"{delim}\r\n"
            f'Content-Disposition: form-data; name="project"\r\n'
            f"\r\n"
            f"{project}\r\n"
            f"{delim}\r\n"
            f'Content-Disposition: form-data; name="inject"\r\n'
            f"\r\n"
            f"1\r\n"
            f"{delim}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="clipboard.bmp"\r\n'
            f"Content-Type: image/bmp\r\n"
            f"\r\n"
        ).encode()
        + image_data
        + f"\r\n{delim}--\r\n".encode()
    )

    # ?project= tells the server this upload has a narrator of its own, so it
    # keeps its hands off the status line (see upload_server._handle_post).
    req = Request(
        f"{server_url}/upload?project={quote(project)}",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    log = get_logger("hotkey")
    try:
        with urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read())
    except HTTPError as exc:
        # The server answered and said no -- carry ITS words, not ours.
        detail = ""
        try:
            body_json = json.loads(exc.read())
            if isinstance(body_json, dict):
                error = body_json.get("error")
                detail = error if isinstance(error, str) else ""
        except (OSError, ValueError):
            detail = ""
        log.warning("upload rejected HTTP %s: %s", exc.code, detail or exc.reason)
        tail = f": {detail}" if detail else ""
        return ("upload-rejected", f"serve said HTTP {exc.code}{tail}", detail)
    except (URLError, OSError) as exc:
        reason = _transport_reason(exc)
        log.warning("upload transport error (%s): %s", type(exc).__name__, exc)
        return ("serve-unreachable", f"cannot reach magent serve ({reason})", str(exc))
    except json.JSONDecodeError as exc:
        log.warning("upload reply was not JSON: %s", exc)
        return ("upload-rejected", "serve sent an unreadable reply", str(exc))

    if not isinstance(payload, dict) or not payload.get("ok", False):
        error = payload.get("error") if isinstance(payload, dict) else None
        detail = error if isinstance(error, str) else ""
        tail = f": {detail}" if detail else ""
        return ("upload-rejected", f"magent serve refused it{tail}", detail)
    if not payload.get("injected", False):
        # The bytes are safe on disk; only the paste failed. Saying "upload
        # failed" here would send the user hunting for a lost screenshot.
        return ("inject-failed", OUTCOME_REASONS["inject-failed"], "injected=false")
    return ("ok", OUTCOME_REASONS["ok"], "")


def handle_press(
    server_url: str, project: str, capture: Callable[[], bytes | None]
) -> str:
    """Run one Alt+V press to completion and return its outcome.

    Called on a background thread (a system-wide keyboard hook must return in
    microseconds), so this is allowed to be slow -- but it may never be silent.
    The phase flashes bracket the two operations that can actually take time:
    reading a large image off the clipboard, and shipping it.
    """
    log = get_logger("hotkey")
    # First statement on purpose: the acknowledgement is dispatched BEFORE the
    # clipboard is touched, so the bar answers the keypress, not the upload.
    flash_async(server_url, project, FLASH_PREFIX + PHASE_CAPTURING, PHASE_FLASH_MS)
    try:
        image_data = capture()
        if not image_data:
            # clipboard_has_image() said yes at the hook, so this is a real read
            # failure (a format we cannot decode, or a race with another app
            # taking the clipboard), not an empty clipboard.
            report(server_url, project, "clipboard-unreadable")
            return "clipboard-unreadable"
        flash_async(server_url, project, FLASH_PREFIX + PHASE_UPLOADING, PHASE_FLASH_MS)
        outcome, reason, detail = upload_image(server_url, project, image_data)
        report(server_url, project, outcome, reason)
        if detail:
            log.info(
                "%s outcome=%s project=%s detail=%s",
                ALTV_LOG_PREFIX,
                outcome,
                project,
                detail,
            )
    except Exception:
        # This runs on a detached background thread with no console: anything
        # that escapes here would vanish, so everything is logged AND shown.
        log.exception("%s outcome=error project=%s", ALTV_LOG_PREFIX, project)
        flash_async(
            server_url,
            project,
            FLASH_PREFIX + OUTCOME_REASONS["error"],
            tint=FLASH_TINT_ERR,
        )
        return "error"
    else:
        return outcome
