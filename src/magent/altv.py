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

Files copied in Explorer (CF_HDROP) are a press too -- ``handle_file_press``.
Where the pane lives decides what moves: a LOCAL pane shares this disk, so
the original paths are pasted and nothing is copied; a REMOTE pane gets every
file in one upload and the server pastes their saved paths in one line.

A CLOUD pane takes no press at all (``cloud-pane``): it is the local viewer of a
session that runs in a VM, which cannot read this PC's disk. The server refuses
every upload to one; the two presses that never upload ask ``pane_is_cloud``.

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

import contextlib
import json
import os
import queue
import secrets
import threading
from http.client import HTTPException
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from magent.log import get_logger
from magent.sessions import (
    FLASH_MSG_MAX,
    FLASH_TINT_ERR,
    FLASH_TINT_OK,
    MAX_UPLOAD_BYTES,
    build_flash_url,
    paths_line,
    unpasteable_path,
    upload_limit_text,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import BinaryIO

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
    "ok-native",  # local press: one Ctrl+V delivered, the agent pastes natively
    "ok-paths",  # local files press: the original paths pasted, nothing copied
    "not-a-magent-window",  # pass-through: the chord was not ours to handle
    "no-image",  # magent window focused, but the clipboard holds no image or file
    "clipboard-unreadable",  # CF_DIB/CF_HDROP said yes, the read came back empty
    "serve-unreachable",  # nothing answered on server_url
    "upload-rejected",  # the server answered, and said no
    "inject-failed",  # the server stored it, psmux would not paste it
    "inject-pending",  # the server stored it, psmux is still being asked
    "native-failed",  # local press: the paste key never reached the pane
    "paths-failed",  # local files press: the paths never reached the pane
    "path-unpasteable",  # local files press: a path holds a control character
    "folder-refused",  # a folder was copied; the whole press is refused
    "file-missing",  # a copied file is gone (or is not a regular file)
    "file-unreadable",  # a copied file is there but would not be read (locked?)
    "too-large",  # remote files press past MAX_UPLOAD_BYTES; nothing was read
    "cloud-pane",  # the window is a cloud session's local viewer; nothing sent
    "error",  # anything unforeseen, with a traceback in the log
)

# Outcomes that mean THE IMAGE IS SAFE. They take the healthy tint and are not
# failures, even when the paste has not landed: red on this bar reads as "your
# screenshot is gone", and saying that about a file sitting in ~/.magent/uploads
# is the same lie as the "upload failed" this vocabulary exists to retire.
# (`ok-native` is safe for the simpler reason: nothing was consumed -- the
# image is still on the clipboard either way; `ok-paths` pasted paths to files
# that never moved.)
ALTV_SAFE_OUTCOMES = ("ok", "ok-native", "ok-paths", "inject-pending")

# What each outcome says on the status bar. Split from the log vocabulary so a
# reason can be reworded without breaking `grep ALTV outcome=...`, and kept
# here (not inline at the raise sites) so "does every outcome have a reason?"
# is one assertion. Generic text is the enemy: the complaint these answer is
# "it did nothing and I cannot tell why".
OUTCOME_REASONS: dict[str, str] = {
    "ok": "image sent",
    "ok-native": "pasted from clipboard",
    "native-failed": "paste key not delivered - clipboard still has the image",
    "ok-paths": "file path pasted",
    "no-image": "clipboard has no image or file - copy one first",
    "clipboard-unreadable": "could not read the image from the clipboard",
    "serve-unreachable": "cannot reach magent serve",
    "upload-rejected": "magent serve refused it",
    "inject-failed": "saved, but psmux would not paste it",
    "inject-pending": "image saved - psmux is slow, paste still pending",
    "paths-failed": "file paths not pasted - psmux did not deliver them",
    "path-unpasteable": "a copied path has a control character - not pasted",
    "folder-refused": "folders not supported - copy files",
    "file-missing": "a copied file no longer exists",
    "file-unreadable": "could not read a copied file - is it open elsewhere?",
    "too-large": f"too large - {upload_limit_text(MAX_UPLOAD_BYTES)} limit",
    "cloud-pane": "cloud session - paste at claude.ai/code (clipboard kept)",
    "error": "unexpected error - see hotkey.log",
}

# The two upload reasons that name WHAT was sent. For a clipboard image they
# read exactly as OUTCOME_REASONS["ok"] / ["inject-pending"] (the phone page's
# pending wording is pinned to the latter); a files press says "file" or
# "3 files" instead.
_SENT_REASON = "{} sent"
_PENDING_REASON = "{} saved - psmux is slow, paste still pending"

PHASE_CAPTURING = "capturing..."
PHASE_UPLOADING = "uploading..."
PHASE_PASTING = "pasting..."

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

# How long the press will wait for `/upload` to answer.
#
# The server owes an answer inside its own `upload_server.INJECT_GRACE_S` (a
# test pins that this stays the larger of the two), so this bound is a
# backstop against a wedged or half-dead serve, NOT the thing that decides
# whether a press succeeded. It used to be both: the handler pasted inline with
# no bound of its own, so a 74 s psmux stall hit this timeout at 20 s and the
# bar said "upload failed - is `magent serve` running?" about an image that was
# already on disk and that psmux went on to paste a minute later.
#
# It bounds each socket operation, never the whole request: the body is
# streamed a block at a time (`_StreamedBody`), so a 100 MB selection over a
# slow link takes as long as it takes, and only a link that stops moving for
# this long gives up.
UPLOAD_HTTP_TIMEOUT_S = 20.0

# How long a LOCAL press waits for serve to say whether its pane is a cloud
# viewer (``pane_is_cloud``). Short on purpose: the answer is the server's own
# cached session list, and a press that cannot learn it proceeds as it always
# did rather than sit on the keypress.
CLOUD_LOOKUP_TIMEOUT_S = 3.0

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
    if outcome in ("ok", "ok-paths"):
        log.info("%s outcome=%s project=%s", ALTV_LOG_PREFIX, outcome, project)
    else:
        log.warning(
            "%s outcome=%s project=%s: %s", ALTV_LOG_PREFIX, outcome, project, reason
        )
    flash_async(
        server_url,
        project,
        FLASH_PREFIX + reason,
        tint=FLASH_TINT_OK if outcome in ALTV_SAFE_OUTCOMES else FLASH_TINT_ERR,
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
    # The capture hands over encoded bytes, not a format promise: the win32
    # side emits PNG for the common screenshot DIBs and falls back to BMP for
    # exotic ones, so the filename the server suffixes from is sniffed off the
    # actual magic rather than hardcoded.
    ext, mime = (
        ("png", "image/png")
        if image_data.startswith(b"\x89PNG\r\n\x1a\n")
        else ("bmp", "image/bmp")
    )
    return _post_upload(
        server_url, project, [(f"clipboard.{ext}", mime, image_data)], "image"
    )


def upload_files(
    server_url: str, project: str, files: list[tuple[str, bytes | Path]]
) -> tuple[str, str, str]:
    """POST every copied file in ONE request; same return as ``upload_image``.

    One request, not one per file, because the server pastes what it saved as
    one line -- per-file requests would each paste, which is N paste attempts
    for one press racing each other into the input line. A ``Path`` is streamed
    off disk as it is sent, never loaded whole.
    """
    noun = "file" if len(files) == 1 else f"{len(files)} files"
    parts = [(name, "application/octet-stream", data) for name, data in files]
    return _post_upload(server_url, project, parts, noun)


def _header_safe(filename: str) -> str:
    """A filename that cannot break out of its multipart header: a ``"`` would
    end the quoted value and a CR/LF would end the header itself."""
    return "".join("_" if ch in '"\r\n' else ch for ch in filename)


# One read's worth of the body. http.client asks for its own block size; this
# only bounds a read that names none.
_BODY_BLOCK_BYTES = 64 * 1024

# A copied file that shrank under the send: the server gets a short body and
# saves nothing, so the bar says what happened to the FILE.
_CHANGED_MID_SEND = "a copied file changed while it was sent"


class _SourceFileError(OSError):
    """A copied file failed while its bytes were being sent.

    Its own type because urllib wraps whatever the body raises mid-send in a
    ``URLError``, and that must not read as "cannot reach magent serve": the
    server was fine, the file was not. ``bar`` is the status-line reason.
    """

    def __init__(self, bar: str, detail: str) -> None:
        super().__init__(detail)
        self.bar = bar


def _source_failure(exc: OSError) -> tuple[str, str, str]:
    """``(outcome, reason, log_detail)`` for a copied file that could not be
    opened or sized: gone since the refusal check, or there but held by
    another app (a locked file on Windows is a ``PermissionError``)."""
    get_logger("hotkey").warning("a copied file could not be read: %s", exc)
    if isinstance(exc, FileNotFoundError):
        return ("file-missing", OUTCOME_REASONS["file-missing"], str(exc))
    return ("file-unreadable", OUTCOME_REASONS["file-unreadable"], str(exc))


class _StreamedBody:
    """A multipart body produced as it is sent, with its length known up front.

    Handed to urllib as a file-like object, so http.client sends it a block at
    a time and the socket timeout bounds each block, not the whole send: as a
    single ``bytes`` it went out in one ``sendall`` whose timeout is a TOTAL
    budget, and a large selection over a slow link failed as "cannot reach
    magent serve" while still moving. It also means the listener -- a
    long-lived process -- never holds the files, let alone a joined copy.

    A segment is ``bytes`` or an open file with the size it declared; exactly
    that many bytes are sent from it.
    """

    def __init__(self, segments: list[bytes | tuple[BinaryIO, int]]) -> None:
        self._segments = segments
        self._index = 0
        self._offset = 0
        self.length = sum(
            len(seg) if isinstance(seg, bytes) else seg[1] for seg in segments
        )

    def _advance(self) -> None:
        self._index += 1
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        size = size if size > 0 else _BODY_BLOCK_BYTES
        while self._index < len(self._segments):
            seg = self._segments[self._index]
            if isinstance(seg, bytes):
                chunk = seg[self._offset : self._offset + size]
                total = len(seg)
            else:
                handle, total = seg
                want = min(size, total - self._offset)
                try:
                    chunk = handle.read(want) if want > 0 else b""
                except OSError as exc:
                    raise _SourceFileError(
                        OUTCOME_REASONS["file-unreadable"], str(exc)
                    ) from exc
                if want > 0 and not chunk:
                    raise _SourceFileError(
                        _CHANGED_MID_SEND, "a copied file shrank while it was sent"
                    )
            self._offset += len(chunk)
            if self._offset >= total:
                self._advance()
            if chunk:
                return chunk
        return b""


def _post_upload(
    server_url: str,
    project: str,
    parts: list[tuple[str, str, bytes | Path]],
    noun: str,
) -> tuple[str, str, str]:
    """The one POST behind both presses. ``parts`` is ``(filename, mime,
    data)`` per file, the data in memory or a file to stream; ``noun``
    ("image", "file", "3 files") names them on the bar."""
    with contextlib.ExitStack() as files:
        # A random boundary per request, as every browser draws one: any file
        # goes now, and a fixed one cut short every file that contained it.
        boundary = "----MagentUpload" + secrets.token_hex(16)
        delim = f"--{boundary}"
        segments: list[bytes | tuple[BinaryIO, int]] = [
            (
                f"{delim}\r\n"
                f'Content-Disposition: form-data; name="project"\r\n'
                f"\r\n"
                f"{project}\r\n"
                f"{delim}\r\n"
                f'Content-Disposition: form-data; name="inject"\r\n'
                f"\r\n"
                f"1\r\n"
            ).encode()
        ]
        for filename, mime, data in parts:
            segments.append(
                (
                    f"{delim}\r\n"
                    f'Content-Disposition: form-data; name="file"; '
                    f'filename="{_header_safe(filename)}"\r\n'
                    f"Content-Type: {mime}\r\n"
                    f"\r\n"
                ).encode()
            )
            if isinstance(data, Path):
                try:
                    handle = files.enter_context(data.open("rb"))
                    size = os.fstat(handle.fileno()).st_size
                except OSError as exc:
                    return _source_failure(exc)
                segments.append((handle, size))
            else:
                segments.append(data)
            segments.append(b"\r\n")
        segments.append(f"{delim}--\r\n".encode())
        body = _StreamedBody(segments)
        return _send_upload(server_url, project, body, boundary, noun)


def _send_upload(
    server_url: str, project: str, body: _StreamedBody, boundary: str, noun: str
) -> tuple[str, str, str]:
    """Send one streamed multipart body; same return as ``upload_image``."""
    # ?project= tells the server this upload has a narrator of its own, so it
    # keeps its hands off the status line (see upload_server._handle_post).
    req = Request(
        f"{server_url}/upload?project={quote(project)}",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            # Explicit: the server reads exactly this many bytes and speaks no
            # chunked encoding, which urllib would otherwise pick for a stream.
            "Content-Length": str(body.length),
        },
        method="POST",
    )
    log = get_logger("hotkey")
    try:
        # The wait for the reply is one operation, and it also covers the link
        # draining whatever the kernel's send buffer still holds -- which only
        # outlasts the budget when that buffer / link rate exceeds ~20 s.
        with urlopen(req, timeout=UPLOAD_HTTP_TIMEOUT_S) as resp:
            payload = json.loads(resp.read())
    except HTTPError as exc:
        # The server answered and said no -- carry ITS words, not ours.
        detail = ""
        body_json: object = None
        try:
            body_json = json.loads(exc.read())
            if isinstance(body_json, dict):
                error = body_json.get("error")
                detail = error if isinstance(error, str) else ""
        except (OSError, ValueError):
            detail = ""
        # A cloud pane is a local viewer of a session in a VM that cannot read
        # this PC's disk: the server refused BEFORE writing a byte, and says so
        # with a flag, so the bar can name the real reason instead of a status.
        if (
            exc.code == 409
            and isinstance(body_json, dict)
            and body_json.get("cloud") is True
        ):
            log.info("upload refused: %s is a cloud pane", project)
            return ("cloud-pane", OUTCOME_REASONS["cloud-pane"], detail)
        log.warning("upload rejected HTTP %s: %s", exc.code, detail or exc.reason)
        tail = f": {detail}" if detail else ""
        return ("upload-rejected", f"serve said HTTP {exc.code}{tail}", detail)
    except (URLError, OSError) as exc:
        cause = getattr(exc, "reason", exc)
        if isinstance(cause, _SourceFileError):
            log.warning("upload abandoned, a copied file failed mid-send: %s", cause)
            return ("file-unreadable", cause.bar, str(cause))
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
        if payload.get("inject_pending", False):
            # Not a verdict at all: the server answered early ON PURPOSE so the
            # press stays bounded, and psmux is still being asked. The image is
            # on disk either way, so the one thing the bar must not do is call
            # this a failure -- the user would rerun the press and end up with
            # the screenshot pasted twice.
            return (
                "inject-pending",
                _PENDING_REASON.format(noun),
                "inject_pending=true",
            )
        # The bytes are safe on disk; only the paste failed. Saying "upload
        # failed" here would send the user hunting for a lost screenshot.
        return ("inject-failed", OUTCOME_REASONS["inject-failed"], "injected=false")
    return ("ok", _SENT_REASON.format(noun), "")


def pane_is_cloud(server_url: str, project: str) -> bool:
    """Whether ``project``'s pane is a CLOUD pane, as ``magent serve`` sees it.

    A cloud pane is the local viewer of a session that runs in a VM, so a paste
    into it is wrong twice over: a local path means nothing to the VM, and a
    pane sitting at a bare shell is not a session at all. Every press that
    UPLOADS is refused by the server itself (its 409, ``_send_upload``); these
    are the two that never reach ``/upload`` -- the opt-in native Ctrl+V and the
    LOCAL files press, which types the original paths -- so they ask.

    The answer is ``/api/sessions``' ``node`` field read through
    ``psmux.cloud_pane_ids``, whose FIRST-row-wins rule is the one the create
    gate uses: in a ``[local, cloud]`` pair for one folder the pane is a local
    agent's and stays pasteable.

    Fails OPEN, on purpose and noisily: these two presses were built to work
    with no server at all, so a lookup that cannot answer (serve down, a stalled
    reply, a body of the wrong shape) must not turn every local pane's paste
    into a refusal. It never raises -- a press must not die of a lookup.
    """
    # In-body, as in ``native_paste``: psmux is a leaf over `log`.
    from magent import psmux

    try:
        with urlopen(
            f"{server_url.rstrip('/')}/api/sessions", timeout=CLOUD_LOOKUP_TIMEOUT_S
        ) as resp:
            payload = json.loads(resp.read())
    except (OSError, ValueError, HTTPException) as exc:
        get_logger("hotkey").warning(
            "cloud-pane lookup unanswered for project=%s (%s): pasting as if local",
            project,
            type(exc).__name__,
        )
        return False
    rows = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return False
    return project in psmux.cloud_pane_ids(row for row in rows if isinstance(row, dict))


def native_enabled() -> bool:
    """Whether ``MAGENT_ALTV_NATIVE=1`` opts in to the local native-paste path.

    Opt-in, not opt-out: Claude Code on Windows acts only on a physical
    Ctrl+V and ignores the 0x16 a psmux ``send-keys`` injects (see
    ``native_paste``), so defaulting to native made a press silently dead in
    the flagship pane. Same degradation doctrine as ``psmux.boost_enabled``:
    the listener is a long-lived hidden process, and an environment that has
    gone bad underneath it must degrade to the default (the upload path)
    rather than kill the press.
    """
    from pydantic import ValidationError

    from magent.env import get_env

    try:
        return get_env().altv_native
    except ValidationError:
        get_logger("hotkey").warning(
            "altv native: environment did not validate; using the upload path"
        )
        return False


def native_paste(project: str) -> tuple[str, str]:
    """Deliver ONE Ctrl+V into the project's pane; the agent does the rest.

    This is the whole local pipeline: an agent that honors the injected key
    reads the image off the clipboard ITSELF, and locally that clipboard is
    the very one the user just copied into -- so there is nothing to capture,
    upload, save, or path-inject. ``C-v`` is a plain control byte (0x16) that
    psmux delivers as a functional Ctrl+V (verified live: PSReadLine pastes on
    it), and the send mirrors the server's inject exactly: same primitive,
    same ``-t`` target, and the same exactly-one-attempt law -- ``send_keys``
    is bounded and a killed send may or may not have landed, so a retry is how
    a screenshot gets pasted twice.

    OPT-IN, because delivery is only half the story: Claude Code on Windows
    acts on a PHYSICAL Ctrl+V but ignores this injected byte (verified live
    2026-08-31 -- the same send pastes in a PSReadLine pane and does nothing
    in a Claude pane), so with the flagship agent this path reports
    ``ok-native`` while nothing pastes. ``native_enabled`` gates it off by
    default; the upload path's path-text inject is the one delivery every
    agent demonstrably accepts.

    Returns ``(outcome, reason)`` -- ``ok-native`` or ``native-failed``, both
    members of ``ALTV_OUTCOMES``. Failure keeps the honest half of the story:
    the clipboard still holds the image, nothing was consumed.
    """
    # In-body on the same grounds as the config import in `sessions`: psmux is
    # a leaf over `log`, so this keeps altv import-light without a cycle risk.
    from magent import psmux

    delivered = psmux.send_keys(project, "C-v", target=project)
    if delivered:
        return ("ok-native", OUTCOME_REASONS["ok-native"])
    return ("native-failed", OUTCOME_REASONS["native-failed"])


def handle_press(
    server_url: str,
    project: str,
    capture: Callable[[], bytes | None],
    *,
    native: bool = False,
) -> str:
    """Run one Alt+V press to completion and return its outcome.

    Called on a background thread (a system-wide keyboard hook must return in
    microseconds), so this is allowed to be slow -- but it may never be silent.
    The phase flashes bracket the two operations that can actually take time:
    reading a large image off the clipboard, and shipping it.

    ``native=True`` is the local short-circuit (the listener's manifest carries
    no ssh host, so the pane's agent shares the presser's clipboard): the press
    becomes one ``send-keys C-v`` and ``capture`` is never called -- the BMP
    capture/upload/inject pipeline exists to move an image between MACHINES,
    and locally there is only one. Remote-wired listeners and the phone page
    keep the upload path, where it is the only correct one.
    """
    log = get_logger("hotkey")
    if native:
        # Same acknowledgement-first law as the upload path: the bar answers
        # the keypress before anything that can take time (a loaded psmux
        # socket has stalled a control command past 70s).
        flash_async(server_url, project, FLASH_PREFIX + PHASE_PASTING, PHASE_FLASH_MS)
        try:
            if pane_is_cloud(server_url, project):
                outcome = "cloud-pane"
                report(server_url, project, outcome)
            else:
                outcome, reason = native_paste(project)
                report(server_url, project, outcome, reason)
        except Exception:
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
    # First statement on purpose: the acknowledgement is dispatched BEFORE the
    # clipboard is touched, so the bar answers the keypress, not the upload.
    flash_async(server_url, project, FLASH_PREFIX + PHASE_CAPTURING, PHASE_FLASH_MS)
    try:
        image_data = capture()
        if not image_data:
            # clipboard_kind() said image at the hook, so this is a real read
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


# CF_HDROP said files were there and the read came back with none -- a real
# read failure, worded for files rather than for an image.
_FILES_UNREADABLE = "could not read the copied files from the clipboard"


def paste_paths(project: str, paths: list[str]) -> tuple[str, str]:
    """Paste the ORIGINAL paths of locally copied files into the pane, as ONE
    line. Returns ``(outcome, reason)`` -- ``ok-paths`` or ``paths-failed``.

    Exactly one attempt, like every other paste here: ``send_keys`` is bounded,
    and a send that timed out may still have landed, so a retry is how a path
    list gets pasted twice. ``literal`` because this is TEXT: a path must never
    be read back as a psmux key name.
    """
    from magent import psmux  # leaf over `log`; in-body as in native_paste

    if psmux.send_keys(project, paths_line(paths), target=project, literal=True):
        noun = "file path" if len(paths) == 1 else f"{len(paths)} file paths"
        return ("ok-paths", f"{noun} pasted")
    return ("paths-failed", OUTCOME_REASONS["paths-failed"])


def _refusal(paths: list[str]) -> str | None:
    """Why a copied selection cannot be sent at all, or ``None`` if it can.

    Checked for EVERY path before anything moves: a folder anywhere refuses
    the whole press, so a mixed selection is never half-sent.
    """
    if any(Path(p).is_dir() for p in paths):
        return "folder-refused"
    if not all(Path(p).is_file() for p in paths):
        return "file-missing"
    return None


def handle_file_press(
    server_url: str,
    project: str,
    capture: Callable[[], list[str] | None],
    *,
    local: bool,
) -> str:
    """Run one Alt+V press whose clipboard holds copied FILES; return its outcome.

    ``local`` is decided by the listener's manifest (no ssh host = the pane is
    on this machine). A LOCAL pane shares this filesystem, so the original
    absolute paths are pasted as they are: no upload, no copy into
    ~/.magent/uploads, and no size cap -- nothing travels. A REMOTE pane gets
    every file in one upload request, pre-checked against the server's limit
    BEFORE a byte is read, and the server pastes their saved paths in one line.

    Same phases and the same never-silent, never-raise contract as
    ``handle_press``.
    """
    log = get_logger("hotkey")
    flash_async(server_url, project, FLASH_PREFIX + PHASE_CAPTURING, PHASE_FLASH_MS)
    try:
        paths = capture()
        if not paths:
            report(server_url, project, "clipboard-unreadable", _FILES_UNREADABLE)
            return "clipboard-unreadable"
        if local and pane_is_cloud(server_url, project):
            # Before the per-path refusals: whatever is copied, the pane is the
            # reason. A REMOTE press is refused by the server's 409 instead.
            report(server_url, project, "cloud-pane")
            return "cloud-pane"
        refusal = _refusal(paths)
        if refusal:
            report(server_url, project, refusal)
            return refusal
        if local:
            if any(unpasteable_path(p) for p in paths):
                # The original path IS what would be typed, and a line break in
                # it submits whatever came before. A remote press is safe: the
                # server pastes names it sanitized itself.
                report(server_url, project, "path-unpasteable")
                return "path-unpasteable"
            flash_async(
                server_url, project, FLASH_PREFIX + PHASE_PASTING, PHASE_FLASH_MS
            )
            outcome, reason = paste_paths(project, paths)
            report(server_url, project, outcome, reason)
            return outcome
        # Read at call time, so the refusal names the limit actually enforced.
        limit = MAX_UPLOAD_BYTES
        try:
            total = sum(Path(p).stat().st_size for p in paths)
        except OSError as exc:
            # Gone or held since the refusal check: named, never "error".
            outcome, reason, detail = _source_failure(exc)
        else:
            if total > limit:
                report(
                    server_url,
                    project,
                    "too-large",
                    f"too large - {upload_limit_text(limit)} limit",
                )
                return "too-large"
            flash_async(
                server_url, project, FLASH_PREFIX + PHASE_UPLOADING, PHASE_FLASH_MS
            )
            files: list[tuple[str, bytes | Path]] = [
                (Path(p).name, Path(p)) for p in paths
            ]
            outcome, reason, detail = upload_files(server_url, project, files)
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
        # Detached thread, no console: logged AND shown, as in handle_press.
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
