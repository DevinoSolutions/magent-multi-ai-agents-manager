"""Saving an upload and pasting its paths: the one implementation behind the
legacy ``POST /upload`` and ``POST /api/v1/uploads``.

Lifted out of ``upload_server.UploadHandler._handle_post`` with the contract
unchanged: every name is reserved before a byte is written, a refused request
takes its files back, and the paste is ONE attempt on a worker the reply waits
on for at most ``INJECT_GRACE_S`` -- so the answer has THREE paste states,
``injected``, ``pending`` (still trying; the reply is early, not wrong) and
``saved`` (no paste made, or a real refusal). Never retried, never re-sent.

A leaf over ``psmux``, ``sessions`` and ``log``; never imports the cli package
or the HTTP handler.
"""

from __future__ import annotations

import contextlib
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from magent import psmux
from magent.log import get_logger, log_safe
from magent.sessions import paths_line, upload_limit_text
from magent.wire import WireError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

PasteState = Literal["injected", "pending", "saved"]


class UploadError(WireError):
    """A refused upload, carrying the wire error code. Nothing was kept."""


@dataclass(frozen=True)
class UploadResult:
    """What one upload became: where every file landed, in the order sent,
    and the paste state. ``upload_id`` names the request."""

    upload_id: str
    path: str
    paths: list[str]
    paste: PasteState

    def legacy(self) -> dict[str, object]:
        """The ``POST /upload`` reply body (two flags, not one word)."""
        return {
            "ok": True,
            "path": self.path,
            "paths": self.paths,
            "injected": self.paste == "injected",
            "inject_pending": self.paste == "pending",
        }


# How long the HTTP response will wait for the paste before answering anyway.
#
# The file is already on disk by the time this wait starts, so everything past
# it is a courtesy: waiting a beat lets the overwhelmingly common fast paste be
# reported as the plain `injected: true` it is, and the bound is what stops a
# stalled multiplexer from turning a successful upload into a client timeout.
# Must stay comfortably under `altv.UPLOAD_HTTP_TIMEOUT_S` (a test pins that) --
# the whole defect being fixed here is a server that outlived its client's
# patience and left the user reading "upload failed" about a file that landed.
INJECT_GRACE_S = 3.0

# The whole life of one paste attempt, wherever it finishes. Deliberately ONE
# attempt: a `send-keys` that is merely slow is still in flight, and a retry on
# top of it pastes the same image twice into the agent's prompt. Past this the
# worker gives up and says so in upload.log, so a paste can never arrive
# minutes later on top of whatever the user did in the meantime.
INJECT_TIMEOUT_S = 60.0


def inject_paste(
    project: str,
    text: str,
    *,
    grace_s: float | None = None,
    timeout_s: float | None = None,
) -> tuple[bool, bool]:
    """Paste ``text`` -- the saved file paths as ONE line (``paths_line``) --
    into ``project``'s pane. Returns ``(injected, pending)``.

    The paste runs on its own thread and the caller waits only ``grace_s``
    for it, because an HTTP handler must not be hostage to a multiplexer: this
    call used to be inline and unbounded, and a control command that stalled for
    74 s answered a listener that had given up at 20 s -- so a screenshot that
    was safely on disk, and that psmux eventually pasted, was reported to the
    user as "upload failed".

    The two flags are exhaustive and honest: ``(True, False)`` pasted,
    ``(False, True)`` still trying (the reply is early, not wrong), and
    ``(False, False)`` a real refusal the caller may name as one. Nothing is
    retried and nothing is re-sent -- see ``INJECT_TIMEOUT_S``.

    The clocks default to this module's ``INJECT_GRACE_S`` / ``INJECT_TIMEOUT_S``
    read at CALL time, so a test that sets them governs the paste.
    """
    grace = INJECT_GRACE_S if grace_s is None else grace_s
    timeout = INJECT_TIMEOUT_S if timeout_s is None else timeout_s
    log = get_logger("upload")
    done = threading.Event()
    outcome: list[bool] = []

    def _run() -> None:
        started = time.monotonic()
        try:
            # `literal`: the line is TEXT -- quoted paths, several of them --
            # and must never be read back as a psmux key name. Same `-l` wire
            # as the local Alt+V paste and `magent send`; no Enter is sent.
            pasted = psmux.send_keys(
                project, text, target=project, literal=True, timeout=timeout
            )
            outcome.append(pasted)
        finally:
            done.set()
            elapsed = time.monotonic() - started
            if elapsed >= grace:
                # The reply already said `inject_pending`; this line is the only
                # place that late verdict is recorded, so it is a WARNING and it
                # carries the wait it cost.
                log.warning(
                    "inject project=%s finished late after %.1fs pasted=%s",
                    project,
                    elapsed,
                    outcome[-1] if outcome else False,
                )

    threading.Thread(target=_run, name="magent-upload-inject", daemon=True).start()
    if done.wait(grace):
        return (bool(outcome and outcome[0]), False)
    return (False, True)


# How much of the original name a saved file keeps, in UTF-8 bytes. One path
# component is capped at 255 (bytes on Linux, UTF-16 units on Windows), and
# `<stamp>_<n>_` rides in front; 150 also keeps the whole path under Windows'
# 260-character MAX_PATH from a typical home directory.
_NAME_MAX_BYTES = 150
# A "suffix" longer than this is not an extension worth keeping whole (a name
# like `notes.from-the-meeting-with-everyone`), so it is truncated as the stem.
_SUFFIX_MAX_BYTES = 20


def saved_name(filename: str) -> str:
    """The part of a saved file's name that comes from the original: path
    stripped, anything but a word character, dot or dash made ``_`` (so no
    control character, quote, separator or line break survives into a pasted
    path), and capped at ``_NAME_MAX_BYTES`` by trimming the stem -- the
    suffix is what tells the agent what the file is, so it is kept.

    A dotfile keeps its name (``.env`` arrives as ``<stamp>_.env``; the prefix
    already stops it being hidden). Only a name that is nothing but dots
    becomes ``upload``, and trailing dots go: Windows drops them on create,
    and the returned path must name the file that exists.
    """
    basename = re.sub(r"[^\w.\-]", "_", Path(filename).name).rstrip(".")
    if not basename:
        return "upload"
    suffix = Path(basename).suffix
    if len(suffix.encode()) > _SUFFIX_MAX_BYTES:
        suffix = ""
    stem = basename[: len(basename) - len(suffix)]
    budget = _NAME_MAX_BYTES - len(suffix.encode())
    stem = stem.encode()[:budget].decode("utf-8", errors="ignore")
    if not suffix:
        stem = stem.rstrip(".")  # a cut can land on a dot, too
    return (stem + suffix) or "upload"


def dest_for(upload_root: Path, stamp: int, filename: str) -> Path | None:
    """Reserve where one uploaded file lands and return it:
    ``<stamp>_<saved name>`` under the uploads dir, created empty, or ``None``
    if the name would escape it.

    Two files with one name must never overwrite each other -- the paste would
    then name one file twice -- whether they came in one request or in two in
    the same second. Only an exclusive create settles that across requests
    (an ``exists()`` check cannot see a name another request chose but has not
    written yet), so a clash bumps to ``<stamp>_<n>_<name>`` until one create
    wins. That also covers a case-insensitive filesystem, where ``A.txt`` and
    ``a.txt`` are one file. The caller writes into the reservation, and
    removes it if the request is refused.
    """
    basename = saved_name(filename)
    name = f"{stamp}_{basename}"
    n = 1
    while True:
        dest = (upload_root / name).resolve()
        if not dest.is_relative_to(upload_root):
            return None
        try:
            with dest.open("xb"):
                return dest
        except FileExistsError:
            n += 1
            name = f"{stamp}_{n}_{basename}"


def discard(dests: list[Path]) -> None:
    """Remove a refused request's reservations: best-effort, never raises."""
    for dest in dests:
        with contextlib.suppress(OSError):
            dest.unlink()


# A quoted filename taken whole, so a name with a `;` in it (legal on every OS,
# and now that any file uploads, a real case) is not cut at the `;` by the
# token split below. Browsers percent-encode a `"` inside it.
_FILENAME_RE = re.compile(r'\bfilename="([^"]*)"')


_BODY_CHUNK_BYTES = 256 * 1024


class UploadIncomplete(UploadError):
    """The body ended before its declared length, or before the delimiter that
    closes its last part. What did arrive is not the file the user sent, so
    nothing of it is saved or pasted. An ``invalid_request`` on the wire, so
    one ``except UploadError`` covers it.

    ``received``/``declared`` carry the byte counts (also in ``details``) so
    the one log line the handler writes can say how far the client got before
    it went away."""

    def __init__(self, reason: str, *, received: int = 0, declared: int = 0) -> None:
        super().__init__(
            "invalid_request", reason, {"received": received, "declared": declared}
        )
        self.received = received
        self.declared = declared


def disposition(header_str: str) -> tuple[str, str]:
    """``(name, filename)`` from one part's headers."""
    name = ""
    filename = ""
    for line in header_str.split("\r\n"):
        if "Content-Disposition:" in line:
            for raw_token in line.split(";"):
                token = raw_token.strip()
                if token.startswith("name="):
                    name = token.split("=", 1)[1].strip('"')
                elif token.startswith("filename="):
                    filename = token.split("=", 1)[1].strip('"')
            quoted = _FILENAME_RE.search(line)
            if quoted:
                filename = quoted.group(1)
    return name, filename


def next_delimiter(body: bytes | bytearray, delim: bytes, start: int) -> int:
    """Index of the CRLF that opens the next real delimiter at or after
    ``start``, or -1. A delimiter is ``CRLF--boundary`` followed by CRLF (another
    part) or ``--`` (the end); the same bytes followed by anything else are the
    file's own content."""
    needle = b"\r\n" + delim
    at = body.find(needle, start)
    while at >= 0:
        after = at + len(needle)
        if body.startswith((b"\r\n", b"--"), after):
            return at
        at = body.find(needle, at + 1)
    return -1


def parse_multipart(
    content_type: str,
    content_length: str | None,
    read1: Callable[[int], bytes],
    *,
    limit: int,
) -> tuple[dict[str, str], dict[str, list[tuple[str, memoryview]]]]:
    """Minimal multipart/form-data parser. Returns (fields, files).

    ``files`` keeps EVERY part sent under a name, in order: one Alt+V press
    carries a whole Explorer selection as several ``file`` parts of one request.
    Each file's data is a ``memoryview`` into the one body read off the socket:
    at 100 MB a request, splitting and slicing copies held four bodies at once.

    Raises ``UploadIncomplete`` when fewer bytes arrived than were declared, or
    the closing delimiter never came -- a cut-short last part still has its
    headers, and saving it would announce a truncated file as uploaded.
    """
    if "boundary=" not in content_type:
        return {}, {}

    boundary = content_type.split("boundary=")[1].strip()
    if boundary.startswith('"') and boundary.endswith('"'):
        boundary = boundary[1:-1]

    try:
        length = int(content_length or 0)
    except (TypeError, ValueError):
        length = 0
    if length <= 0:
        return {}, {}
    # Read in chunks (one raw recv each) rather than one rfile.read(length): when
    # the client vanishes mid-body the exception would carry none of what had
    # already arrived, and the log line needs the byte count.
    want = min(length, limit)
    body = bytearray()
    try:
        while len(body) < want:
            chunk = read1(min(_BODY_CHUNK_BYTES, want - len(body)))
            if not chunk:
                break
            body += chunk
    except OSError as exc:  # a stalled or reset client, mid-body
        raise UploadIncomplete(str(exc), received=len(body), declared=length) from exc
    if len(body) < length:
        raise UploadIncomplete(
            f"{len(body)} of {length} bytes arrived",
            received=len(body),
            declared=length,
        )

    view = memoryview(body)
    delim = f"--{boundary}".encode()
    fields: dict[str, str] = {}
    files: dict[str, list[tuple[str, memoryview]]] = {}

    # The first delimiter has no CRLF before it (it may follow a preamble).
    at = body.find(delim)
    while at >= 0:
        after = at + len(delim)
        if body.startswith(b"--", after):
            return fields, files  # the closing delimiter: the body is whole
        start = after + 2  # past the CRLF that ends the delimiter line
        end = next_delimiter(body, delim, start)
        if end < 0:
            break
        head_end = body.find(b"\r\n\r\n", start, end)
        if head_end >= 0:
            name, filename = disposition(
                str(view[start:head_end], "utf-8", errors="replace")
            )
            data = view[head_end + 4 : end]
            if filename:
                files.setdefault(name, []).append((filename, data))
            elif name:
                fields[name] = str(data, "utf-8", errors="replace")
        at = end + 2
    raise UploadIncomplete("the closing delimiter never arrived")


def paste_state(injected: bool, pending: bool) -> PasteState:
    """``inject_paste``'s two flags as the one wire word."""
    if injected:
        return "injected"
    return "pending" if pending else "saved"


def _write_all(
    upload_dir: Path, files: Sequence[tuple[str, bytes | memoryview]]
) -> list[Path]:
    """Reserve every name, then write every file. Every name is reserved
    before any byte is written, so an invalid one refuses the request whole
    instead of leaving half of it saved -- and a request that fails part-way
    takes its files back."""
    upload_dir.mkdir(parents=True, exist_ok=True)
    upload_root = upload_dir.resolve()
    stamp = int(time.time())
    dests: list[Path] = []
    try:
        for filename, _data in files:
            dest = dest_for(upload_root, stamp, filename)
            if dest is None:
                break
            dests.append(dest)
        else:
            for dest, (_name, data) in zip(dests, files, strict=True):
                dest.write_bytes(data)
            return dests
    except BaseException:
        discard(dests)
        raise
    discard(dests)
    raise UploadError("invalid_request", "Invalid filename")


def save(
    files: Sequence[tuple[str, bytes | memoryview]],
    session: str,
    *,
    inject: bool,
    upload_dir: Path,
    max_bytes: int,
    inject_fn: Callable[[str, str], tuple[bool, bool]] | None = None,
) -> UploadResult:
    """Save ``files`` under ``upload_dir`` and, when ``inject``, paste every
    saved path into ``session`` as ONE line. ``session`` is the psmux socket
    id and must already be validated by the caller. Raises ``UploadError``
    -- ``invalid_request`` (no file, a name that would escape the folder) or
    ``payload_too_large`` -- with nothing left on disk."""
    if not files:
        raise UploadError("invalid_request", "Missing file")
    total = sum(len(data) for _name, data in files)
    if total > max_bytes:
        raise UploadError(
            "payload_too_large",
            f"File too large - {upload_limit_text(max_bytes)} limit",
        )
    dests = _write_all(upload_dir, files)
    injected = pending = False
    if inject and psmux.find_psmux():
        paste = inject_fn or inject_paste
        injected, pending = paste(session, paths_line([str(d) for d in dests]))
    elif inject:
        get_logger("upload").warning(
            "upload project=%s requested inject but psmux is unavailable",
            log_safe(session),
        )
    return UploadResult(
        upload_id=secrets.token_hex(8),
        path=str(dests[0]),
        paths=[str(d) for d in dests],
        paste=paste_state(injected, pending),
    )
