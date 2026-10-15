"""magent link: the host side of the relay link (spec 4.1, P2).

The transport first: a stdlib http.client client for the relay's
``/v1/host/*`` routes (every reply is the daemon envelope, contract C1), an SSE
parser for ``/v1/stream/host`` (C5), the reconnect backoff and the seen-id
ring. Then LinkRunner, the serve thread built on it: the relay connection,
the snapshot, and the commands paired devices send.

Outbound only: the relay never connects to this machine. MD013 keeps this
module the only new code that opens an outbound HTTP connection.
"""

from __future__ import annotations

import collections
import contextlib
import http.client
import importlib.metadata
import json
import random
import re
import socket
import ssl
import threading
import time
import unicodedata
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

from magent import linkcrypto, linkfiles
from magent.linkcrypto import AAD_CMD, AAD_RES, AAD_SNAP, SealError
from magent.linkfiles import SEEN_MAX, LinkState
from magent.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterable, Iterator
    from pathlib import Path

    from magent.env import MagentEnv
    from magent.linkcrypto import PairKeys
    from magent.linkfiles import LinkFile, TrustedDevice

PROTOCOL_HEADER = ("X-Magent-Link", "1")
DEFAULT_RELAY_URL = "https://api.magent.now"
DEFAULT_APP_URL = "https://app.magent.now"
MAX_JSON_BYTES = 8 * 1024 * 1024
# One SSE line, newline included. A relay frame is one JSON ``data:`` line well
# under this (a device command body is at most 64 KiB before sealing); the cap
# keeps a hostile or broken relay from growing one line without bound.
MAX_SSE_LINE = 1024 * 1024
_READ_CHUNK = 64 * 1024
# 429 is transient: call() waits (Retry-After, else full-jitter backoff) and
# tries again this many times before raising. A Retry-After past the cap is
# not waited out here: call() raises at once with it set.
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_CAP_S = 60.0


class RelayError(Exception):
    """A relay call that did not succeed. ``status`` 0 = never got a reply.

    ``retry_after`` is the relay's Retry-After in seconds on a 429, else None.
    """

    def __init__(
        self,
        status: int,
        code: str,
        message: str = "",
        *,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(
            f"{status} {code}: {message}" if message else f"{status} {code}"
        )
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


class Revoked(RelayError):
    """401: the host token is no longer valid. Stop; never retry."""


class TooOld(RelayError):
    """426: this daemon speaks a protocol the relay no longer accepts."""


@dataclass(frozen=True)
class SseEvent:
    event: str
    data: str
    id: str | None


def iter_sse(
    lines: Iterable[bytes], *, max_event_bytes: int = MAX_JSON_BYTES
) -> Iterator[SseEvent]:
    """Server-sent events from raw lines (WHATWG rules, the parts the relay uses).

    ``event``/``data``/``id`` fields, multi-line data joined with newlines,
    comment lines ignored, dispatch on a blank line, a block with no data not
    dispatched, an unterminated final block dropped. ``id`` is the id field of
    that block only (None when it had none). An event whose data lines add up
    to more than ``max_event_bytes`` (counted as decoded text, newlines
    included; the relay's frames are ASCII JSON) is a RelayError, not a
    buffer that grows.
    """
    event = ""
    data: list[str] = []
    size = 0
    ident: str | None = None
    for raw in lines:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data:
                yield SseEvent(event or "message", "\n".join(data), ident)
            event, data, size, ident = "", [], 0, None
            continue
        if line.startswith(":"):
            continue
        field, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            size += len(value) + 1
            if size > max_event_bytes:
                raise RelayError(0, "invalid_response", "SSE event too large")
            data.append(value)
        elif field == "id" and "\x00" not in value:
            ident = value


def _user_agent() -> str:
    try:
        version = importlib.metadata.version("magent-multi-ai-agents-manager")
    except importlib.metadata.PackageNotFoundError:
        version = "dev"
    return f"magent/{version}"


def _error_message(raw: bytes) -> str:
    try:
        body = json.loads(raw)
    except ValueError:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return message if isinstance(message, str) else ""


def _retry_after(resp: http.client.HTTPResponse) -> float | None:
    """A 429's Retry-After in seconds (the relay sends delta-seconds only)."""
    if resp.status != 429:
        return None
    value = (resp.getheader("Retry-After") or "").strip()
    return float(value) if value.isascii() and value.isdigit() else None


def _decode(
    status: int, raw: bytes, *, retry_after: float | None = None
) -> dict[str, object]:
    """The envelope's ``data``, or the matching RelayError."""
    if 300 <= status < 400:
        # http.client never follows a redirect, and neither does the link: the
        # bearer must not be replayed to whatever host a Location names.
        raise RelayError(status, "invalid_response", "the relay answered a redirect")
    if status == 401:
        raise Revoked(401, "unauthorized", _error_message(raw))
    if status == 426:
        raise TooOld(426, "upgrade_required", _error_message(raw))
    if len(raw) > MAX_JSON_BYTES:
        raise RelayError(status, "invalid_response", "reply too large")
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise RelayError(status, "invalid_response", "reply is not JSON") from exc
    if not isinstance(body, dict) or not isinstance(body.get("ok"), bool):
        raise RelayError(status, "invalid_response", "reply is not an envelope")
    if body["ok"]:
        data = body.get("data")
        if not isinstance(data, dict):
            raise RelayError(status, "invalid_response", "reply data is not an object")
        return {str(k): v for k, v in data.items()}
    error = body.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    raise RelayError(
        status,
        code if isinstance(code, str) else "invalid_response",
        message if isinstance(message, str) else "",
        retry_after=retry_after,
    )


def _send(
    conn: http.client.HTTPConnection,
    method: str,
    target: str,
    body: bytes | None,
    headers: dict[str, str],
) -> http.client.HTTPResponse:
    try:
        conn.request(method, target, body=body, headers=headers)
        return conn.getresponse()
    except (OSError, http.client.HTTPException) as exc:
        raise RelayError(0, "network", type(exc).__name__) from exc


def _read(resp: http.client.HTTPResponse, size: int) -> bytes:
    try:
        return resp.read(size)
    except (OSError, http.client.HTTPException) as exc:
        raise RelayError(0, "stream_lost", type(exc).__name__) from exc


def _connect(conn: http.client.HTTPConnection) -> None:
    try:
        conn.connect()
    except (OSError, http.client.HTTPException) as exc:
        raise RelayError(0, "network", type(exc).__name__) from exc


def _too_slow() -> RelayError:
    return RelayError(0, "timeout", "the relay did not answer in time")


class _Deadline:
    """Cuts a connected socket once ``seconds`` have passed.

    The socket timeout bounds each read, so a relay that drips a byte at a
    time never trips it. A timer thread shuts the socket down instead, which
    wakes a blocked read; ``expired`` tells that cut from a dropped
    connection. The socket is held here, not read off the connection later:
    http.client hands it to the response (and clears ``conn.sock``) when the
    reply will close the connection.
    """

    def __init__(self, sock: socket.socket | None, seconds: float) -> None:
        self.expired = False
        self._sock = sock
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True

    def __enter__(self) -> _Deadline:
        self._timer.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._timer.cancel()

    def _expire(self) -> None:
        self.expired = True
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.shutdown(socket.SHUT_RDWR)


class RelayClient:
    """One relay, plain request/response plus the SSE stream. Holds no
    connection between calls, so threads may share it."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        retries: int = RATE_LIMIT_RETRIES,
        sleep: Callable[[float], object] = time.sleep,
    ) -> None:
        parts = urlsplit(base_url)
        try:
            # Read now, not at the first call: a bad port is a bad URL.
            port = parts.port
        except ValueError:
            port = -1
        if parts.scheme not in ("https", "http") or not parts.hostname or port == -1:
            # The URL itself stays out of the message: it may carry userinfo.
            raise ValueError("not a relay URL: need http(s)://host[:port]")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._retries = retries
        self._sleep = sleep

    def _connection(
        self, url: str, timeout: float
    ) -> tuple[http.client.HTTPConnection, str]:
        parts = urlsplit(url)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        host = parts.hostname or ""
        if parts.scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                host, parts.port, timeout=timeout, context=ssl.create_default_context()
            )
        else:
            conn = http.client.HTTPConnection(host, parts.port, timeout=timeout)
        return conn, target

    def _headers(
        self, token: str | None, *, accept: str, json_body: bool
    ) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": _user_agent(),
            PROTOCOL_HEADER[0]: PROTOCOL_HEADER[1],
        }
        bearer = self._token if token is None else token
        if bearer:
            if not all("!" <= ch <= "~" for ch in bearer):
                # http.client would refuse it with the whole header value in
                # its message; refuse it first, without the token.
                raise ValueError("bad token")
            headers["Authorization"] = f"Bearer {bearer}"
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _exchange(
        self,
        method: str,
        path: str,
        payload: bytes | None,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, bytes, float | None]:
        """One request on its own connection: status, body, Retry-After.

        ``timeout`` bounds the TCP connect, then the request and its whole
        reply: past it the call is a RelayError ``timeout``, however slowly the
        reply trickles in. Known gap: the name lookup is not bounded, and a TLS
        handshake only per read.
        """
        conn, target = self._connection(self.base_url + path, timeout)
        try:
            _connect(conn)
            with _Deadline(conn.sock, timeout) as deadline:
                try:
                    resp = _send(conn, method, target, payload, headers)
                    raw = _read(resp, MAX_JSON_BYTES + 1)
                except RelayError:
                    if deadline.expired:
                        raise _too_slow() from None
                    raise
                if deadline.expired:
                    raise _too_slow()
                return resp.status, raw, _retry_after(resp)
        finally:
            conn.close()

    def call(
        self,
        method: str,
        path: str,
        body: object | None = None,
        *,
        token: str | None = None,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> dict[str, object]:
        """One JSON request; the envelope's ``data`` on success.

        A 429 is retried (up to ``retries`` times) after its Retry-After, or a
        full-jitter backoff when it has none. A Retry-After past
        RATE_LIMIT_CAP_S raises at once with ``retry_after`` set, for the
        caller to wait out. The relay rate-limits before it acts on a request,
        so a retried POST is never applied twice. ``timeout`` overrides the
        client's for this call (the long-poll path passes its own), and
        ``retries`` its 429 retries (0: raise the first 429 at once, for a
        caller that keeps its own clock). ``timeout`` is also each attempt's
        total deadline (see _exchange).
        """
        payload = (
            None
            if body is None
            else json.dumps(body, separators=(",", ":")).encode("utf-8")
        )
        headers = self._headers(
            token, accept="application/json", json_body=payload is not None
        )
        wait = self._timeout if timeout is None else timeout
        limit = self._retries if retries is None else retries
        backoff = Backoff(cap=RATE_LIMIT_CAP_S)
        attempt = 0
        while True:
            status, raw, retry_after = self._exchange(
                method, path, payload, headers, wait
            )
            too_long = retry_after is not None and retry_after > RATE_LIMIT_CAP_S
            if status != 429 or attempt >= limit or too_long:
                return _decode(status, raw, retry_after=retry_after)
            attempt += 1
            self._sleep(backoff.next_delay() if retry_after is None else retry_after)

    def stream(
        self, path: str, *, read_timeout: float = 60.0
    ) -> Generator[SseEvent, None, None]:
        """The relay's SSE stream. Ends when the relay closes it; RelayError
        when it is refused or the connection drops; ``read_timeout`` bounds the
        silence between two frames (the relay sends a ping every 15 s).

        A 429 refusal is not retried here: it raises with ``retry_after`` set,
        for the reconnect loop to wait out.
        """
        conn, target = self._connection(self.base_url + path, read_timeout)
        try:
            resp = _send(
                conn,
                "GET",
                target,
                None,
                self._headers(None, accept="text/event-stream", json_body=False),
            )
            if resp.status != 200:
                _decode(
                    resp.status,
                    _read(resp, MAX_JSON_BYTES + 1),
                    retry_after=_retry_after(resp),
                )
                raise RelayError(resp.status, "stream_refused")
            ctype = (resp.getheader("Content-Type") or "").partition(";")[0]
            if ctype.strip().lower() != "text/event-stream":
                raise RelayError(200, "invalid_response", "not an event stream")
            lines = iter(lambda: _readline(resp), b"")
            yield from iter_sse(lines, max_event_bytes=MAX_JSON_BYTES)
        finally:
            conn.close()

    def get_bytes(
        self, url: str, sink: Callable[[bytes], None], *, max_bytes: int
    ) -> int:
        """Stream an absolute (presigned) URL into ``sink``; the byte count.

        No relay headers: a presigned GET carries its own authority, and an
        extra Authorization header would make the storage service refuse it.
        Only http(s) is fetched; the URL never appears in an error.
        """
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise RelayError(0, "invalid_response", "download URL is not http(s)")
        conn, target = self._connection(url, self._timeout)
        try:
            resp = _send(conn, "GET", target, None, {"User-Agent": _user_agent()})
            if resp.status != 200:
                raise RelayError(resp.status, "download_failed")
            total = 0
            while True:
                chunk = _read(resp, _READ_CHUNK)
                if not chunk:
                    return total
                total += len(chunk)
                if total > max_bytes:
                    raise RelayError(
                        413, "payload_too_large", f"more than {max_bytes} bytes"
                    )
                sink(chunk)
        finally:
            conn.close()


def _readline(resp: http.client.HTTPResponse) -> bytes:
    """One SSE line, at most MAX_SSE_LINE bytes (newline included)."""
    try:
        line = resp.readline(MAX_SSE_LINE + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise RelayError(0, "stream_lost", type(exc).__name__) from exc
    if len(line) > MAX_SSE_LINE:
        raise RelayError(0, "invalid_response", "SSE line too long")
    return line


class Backoff:
    """Full-jitter exponential backoff: ``rand() * min(cap, base * 2**n)``."""

    def __init__(
        self,
        *,
        base: float = 1.0,
        cap: float = 60.0,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._base = base
        self._cap = cap
        self._rand = rand
        self._attempt = 0

    def next_delay(self) -> float:
        ceiling = min(self._cap, self._base * (2**self._attempt))
        self._attempt = min(self._attempt + 1, 32)
        return self._rand() * ceiling

    def reset(self) -> None:
        self._attempt = 0


class SeenRing:
    """The last ``size`` command and upload ids already handled (spec 4.1)."""

    def __init__(self, items: Iterable[str] = (), size: int = 1024) -> None:
        self._order: collections.deque[str] = collections.deque(maxlen=size)
        self._set: set[str] = set()
        for item in items:
            self.add(item)

    def __contains__(self, item: object) -> bool:
        return item in self._set

    def add(self, item: str) -> bool:
        """Remember ``item``; False when it was already there."""
        if item in self._set:
            return False
        if len(self._order) == self._order.maxlen:
            self._set.discard(self._order[0])
        self._order.append(item)
        self._set.add(item)
        return True

    def items(self) -> tuple[str, ...]:
        return tuple(self._order)


def _env() -> MagentEnv | None:
    """The env singleton, or None when it no longer validates (logged)."""
    from pydantic import ValidationError

    from magent.env import get_env

    try:
        return get_env()
    except ValidationError:
        get_logger("link").warning(
            "link: the MAGENT_* environment no longer validates; using defaults"
        )
        return None


def link_enabled() -> bool:
    """MAGENT_LINK. Fails open: the thread is inert without link.json anyway."""
    env = _env()
    return True if env is None else env.link


def relay_url() -> str:
    """MAGENT_LINK_URL without a trailing slash (pydantic adds one)."""
    env = _env()
    return DEFAULT_RELAY_URL if env is None else str(env.link_url).rstrip("/")


def app_url() -> str:
    """MAGENT_LINK_APP_URL without a trailing slash: the only origin a pairing
    QR may point at."""
    env = _env()
    return DEFAULT_APP_URL if env is None else str(env.link_app_url).rstrip("/")


# --- The serve thread: LinkRunner (spec 4.1) ----------------------------------

IDLE_RECHECK_S = 10.0  # how often an unlinked runner looks for link.json
STREAM_READ_TIMEOUT_S = 60.0  # the relay pings every 15 s; 60 s of silence = dead
BACKOFF_RESET_AFTER_S = 60.0  # a connection this long resets the backoff
FAILED_CONNECTS_FOR_POLL = 3  # this many failed connects...
FAILED_CONNECT_WINDOW_S = 300.0  # ...inside this window switch to long-polling...
LONG_POLL_FOR_S = 1800.0  # ...for this long, then the stream is tried again
LONG_POLL_WAIT_S = 25
LONG_POLL_TIMEOUT_S = 35.0  # the relay holds a poll 25 s; room for the tunnel
POLL_MIN_INTERVAL_S = 1.0  # an empty poll answered sooner waits out the rest
PERMANENT_RETRY_S = 300.0  # the wait after a refusal a retry cannot fix (a 4xx)
RETRY_AFTER_MAX_S = 3600.0  # the longest Retry-After the runner waits out
COMMAND_MAX_AGE_MS = 120_000  # a command signed further than 120 s from now
RESULT_CACHE_MAX = 256  # results kept to re-send when a command is replayed
SAVE_ATTEMPTS = 5  # link-state.json writes, 20 ms apart (see LinkRunner._save)
SAVE_RETRY_S = 0.02
SNAPSHOT_SESSIONS_MAX = 1000  # rows the relay takes in one snapshot (C4)
SESSION_NAME_MAX = 200
SESSION_STATE_MAX = 32
DAEMON_FIELD_MAX = 32
COMMAND_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
UPGRADE_HINT = (
    "magent link: the relay needs a newer magent. "
    'Upgrade with: pip install -U "magent-multi-ai-agents-manager[link]"'
)
# A command id as the relay issues them (C3): a UUID, hex in any case. The
# signing input joins fields with bare newlines, so nothing else gets near it.
_COMMAND_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.ASCII | re.IGNORECASE,
)
# The RelayError code of a LinkHost failure: backed off like a dropped
# connection, but not counted toward the long-poll switch.
_HOST_FAILED = "host_failed"
# A session id the relay stores (C3's tunnel charset), never "." or "..".
_SESSION_ID = re.compile(r"(?!\.{1,2}\Z)[A-Za-z0-9._-]{1,128}", re.ASCII)


class EventFeed(Protocol):
    """The daemon's event stream as the link reads it."""

    def next_batch(self, timeout: float) -> list[dict[str, object]]:
        """Daemon events ``{id, type, ts, data}`` (a ``reset`` included), or
        ``[]`` after ``timeout`` seconds of quiet."""
        ...

    def close(self) -> None: ...


class LinkHost(Protocol):
    """The running daemon as the link sees it (``link_host.ApiLinkHost``)."""

    def handle(
        self, method: str, path: str, body: bytes | None, device_id: str
    ) -> tuple[int, dict[str, object]]:
        """One /api/v1 request from a paired device: (HTTP status, envelope)."""
        ...

    def stage_upload(
        self, device_id: str, session: str, file_path: Path, inject: bool
    ) -> dict[str, object]:
        """Hand a decrypted upload to the daemon; the /api/v1 envelope."""
        ...

    def snapshot(self) -> tuple[list[dict[str, object]], dict[str, str]]:
        """(every SessionRow as a dict, ``{version, platform, epoch}``)."""
        ...

    def subscribe(self) -> EventFeed: ...

    def remote_control(self) -> bool: ...

    def upload_max_bytes(self) -> int: ...


class ClientFactory(Protocol):
    """Builds the runner's RelayClient. ``sleep`` is the runner's stop-aware
    wait, so a rate-limit pause inside a call ends when the runner stops."""

    def __call__(
        self, url: str, token: str, *, sleep: Callable[[float], object]
    ) -> RelayClient: ...


@dataclass(frozen=True)
class _Peer:
    """A trusted device and the two keys this host shares with it."""

    device: TrustedDevice
    sign_pub: bytes
    keys: PairKeys


class _Refused(Exception):
    """A command this host will not run; answered with a sealed error."""

    def __init__(self, status: int, code: str, reason: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.reason = reason


def _default_client(
    url: str, token: str, *, sleep: Callable[[float], object]
) -> RelayClient:
    return RelayClient(url, token=token, sleep=sleep)


def _transient(exc: RelayError) -> bool:
    """Worth retrying soon: no reply at all, a relay fault, or a rate limit.
    Any other 4xx is permanent (401 and 426 are handled before this)."""
    return exc.status == 0 or exc.status >= 500 or exc.status == 429


def _object(raw: str) -> dict[str, object] | None:
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    return {str(k): v for k, v in value.items()}


def _objects(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [
        {str(k): v for k, v in item.items()} for item in value if isinstance(item, dict)
    ]


def _error(code: str, message: str, reason: str) -> dict[str, object]:
    """The daemon error envelope (contract C1)."""
    return {
        "ok": False,
        "error": {"code": code, "message": message, "details": {"reason": reason}},
    }


def _api_path(path: str) -> bool:
    """A path under the /api/v1 prefix with no control character in it."""
    # Read here, not at import: api pulls in the whole /api/v1 layer, which
    # `magent link` (pairing only) has no use for.
    from magent.api import PREFIX

    return path.startswith(PREFIX + "/") and not any(
        unicodedata.category(ch) == "Cc" for ch in path
    )


def _state_text(value: object) -> str | None:
    if isinstance(value, str) and len(value) <= SESSION_STATE_MAX:
        return value
    return None


def session_meta(row: dict[str, object]) -> dict[str, object] | None:
    """The routing-only view of a SessionRow the relay may read (C4), or None
    for a row the relay would refuse (its id is not a session id). Values
    past the relay's bounds are cut (a name) or dropped (a state); the full
    row still reaches the devices, sealed."""
    session = row.get("session")
    if not isinstance(session, str) or not _SESSION_ID.fullmatch(session):
        return None
    name = row.get("name")
    return {
        "session": session,
        "name": name[:SESSION_NAME_MAX] if isinstance(name, str) else "",
        "live": row.get("live") is True,
        "hookState": _state_text(row.get("hook_state")),
        "paneState": _state_text(row.get("pane_state")),
        "nodeState": _state_text(row.get("node_state")),
    }


def _daemon_field(value: object) -> str:
    """1-32 characters, as the relay takes ``daemon.*``."""
    if isinstance(value, str) and value:
        return value[:DAEMON_FIELD_MAX]
    return "unknown"


def _check_host_token(link: LinkFile) -> None:
    """A LinkFileError for a host token no request could carry. Pairing never
    writes one; a hand edit can, and the client would refuse it on every call,
    so it is a bad file (reported once), not a failure retried forever."""
    if not all("!" <= ch <= "~" for ch in link.host_token):
        raise linkfiles.LinkFileError("link.json holds an unusable host token")


class LinkRunner:
    """The link thread's body: idle while unlinked, else one relay connection
    at a time, run until stopped, revoked or refused as too old.

    State in ``link-state.json`` (contract C8): ``connecting``, ``online``,
    ``offline`` with ``retry_at`` between attempts; ``revoked`` and
    ``too_old`` end the run. Every (re)connect PUTs a fresh snapshot, then
    reads the SSE stream from the saved cursor. A failure with no reply, a
    5xx or a 429 is retried after a full-jitter backoff (at least the
    relay's Retry-After); any other 4xx waits PERMANENT_RETRY_S. Three failed
    connects inside five minutes switch to long-polling ``/v1/host/commands``
    for thirty.

    A command is checked field by field (a UUID id, a known method, a path
    under the API prefix with no control character, an int ts), then verified
    (trusted device, Ed25519 signature, age), decrypted, checked against its
    outer method and path, run through ``LinkHost.handle`` and answered
    sealed. Each id is saved before it runs, so a command the relay replays
    never runs twice: its cached result is re-sent, or, after a restart, it
    is only acknowledged. The cursor moves only once the relay has the
    answer, so a lost result post is replayed, not dropped.
    """

    def __init__(
        self,
        host: LinkHost,
        *,
        client_factory: ClientFactory = _default_client,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], bool] | None = None,
        link_path: Path | None = None,
        state_path: Path | None = None,
    ) -> None:
        self._host = host
        self._client_factory = client_factory
        self._clock = clock
        self._sleep = sleep
        self._link_path = link_path
        self._state_path = state_path
        self._log = get_logger("link")
        self._lock = threading.RLock()
        self._link: LinkFile | None = None
        self._peers: dict[str, _Peer] = {}
        self._cursor = 0
        self._seen = SeenRing(size=SEEN_MAX)
        self._results: collections.OrderedDict[str, dict[str, object]] = (
            collections.OrderedDict()
        )
        self._state = "unlinked"
        self._retry_at: float | None = None
        self._last_connected: float | None = None
        self._frames = 0
        self._warned_bad_file = False

    # -- the loop ------------------------------------------------------------

    def run(self, stop: threading.Event) -> str:
        """Run until ``stop``; the terminal state: revoked, too_old or stopped."""
        sleep = self._sleep or stop.wait
        while not stop.is_set():
            # Loading the keys needs cryptography as much as running does, so
            # both sit under the one guard: without the extra the thread idles.
            try:
                client = self._linked_client(sleep)
                if client is None:
                    if sleep(IDLE_RECHECK_S):
                        break
                    continue
                return self._connect_loop(client, stop, sleep)
            except linkcrypto.LinkCryptoUnavailable:
                self._log.warning(
                    "link: the link needs the cryptography package: %s",
                    linkcrypto.INSTALL_HINT,
                )
                while not sleep(IDLE_RECHECK_S):
                    pass
                break
        return "stopped"

    def _linked_client(self, sleep: Callable[[float], bool]) -> RelayClient | None:
        """The client for the saved link, with its keys loaded; None while
        unlinked or while link.json is unusable (reported once)."""
        try:
            link = self._read_link()
            if link is None:
                return None
            _check_host_token(link)
            self._load(link)
            return self._client_factory(link.relay_url, link.host_token, sleep=sleep)
        except (linkfiles.LinkFileError, ValueError) as exc:
            if not self._warned_bad_file:
                self._warned_bad_file = True
                self._log.warning(
                    "link: %s; repair with `magent unlink`, then `magent link`",
                    exc,
                )
            return None

    def _connect_loop(
        self, client: RelayClient, stop: threading.Event, sleep: Callable[[float], bool]
    ) -> str:
        backoff = Backoff()
        failures: collections.deque[float] = collections.deque()
        poll_until = 0.0
        need_snapshot = True
        while not stop.is_set():
            polling = self._clock() < poll_until
            started: float | None = None
            error: RelayError | None = None
            self._frames = 0
            try:
                if need_snapshot:
                    self._set_state("connecting")
                    self.put_snapshot(client)
                    need_snapshot = False
                started = self._clock()
                self._set_state("online", last_connected=started)
                if polling:
                    self._poll_round(client, sleep)
                    continue
                need_snapshot = True
                self._read_stream(client, stop)
            except Revoked:
                self._set_state("revoked")
                self._log.warning(
                    "link: the relay revoked this host; run `magent link` to pair again"
                )
                return "revoked"
            except TooOld:
                self._set_state("too_old")
                self._log.warning("%s", UPGRADE_HINT)
                return "too_old"
            except RelayError as exc:
                need_snapshot = True
                error = exc
                if _transient(exc):
                    self._log.info("link: relay connection ended: %s", exc)
                else:
                    self._log.warning("link: the relay refused this host: %s", exc)
            except linkcrypto.LinkCryptoUnavailable:
                raise
            except Exception:  # noqa: BLE001  # reason: a LinkHost failure (snapshot, settings) backs off and retries like a dropped connection; it must never end the link thread
                need_snapshot = True
                self._log.exception("link: the daemon side of the link failed")
                error = RelayError(0, _HOST_FAILED, "the link host failed")
            if stop.is_set():
                break
            now = self._clock()
            if started is not None and now - started >= BACKOFF_RESET_AFTER_S:
                backoff.reset()
            # A failed connect: no frame and not a mid-stream drop. A failure
            # on this side of the link says nothing about the stream.
            failed = (
                error is not None
                and error.code != _HOST_FAILED
                and (
                    started is None
                    or (self._frames == 0 and error.code != "stream_lost")
                )
            )
            if failed and not polling:
                failures.append(now)
                while failures and now - failures[0] > FAILED_CONNECT_WINDOW_S:
                    failures.popleft()
                if len(failures) >= FAILED_CONNECTS_FOR_POLL:
                    failures.clear()
                    poll_until = now + LONG_POLL_FOR_S
                    self._log.warning(
                        "link: the stream keeps failing; long-polling for %d min",
                        int(LONG_POLL_FOR_S // 60),
                    )
            delay = _delay(backoff, error)
            self._set_state("offline", retry_at=now + delay)
            if sleep(delay):
                break
        return "stopped"

    def _read_stream(self, client: RelayClient, stop: threading.Event) -> None:
        # Closed on the way out, whatever ends it: the relay holds two streams
        # per host, and a redial must not find this one still counted.
        path = f"/v1/stream/host?since={self._cursor}"
        with contextlib.closing(
            client.stream(path, read_timeout=STREAM_READ_TIMEOUT_S)
        ) as frames:
            for event in frames:
                self._frames += 1
                self._on_frame(client, event)
                if stop.is_set():
                    return

    def _poll_round(self, client: RelayClient, sleep: Callable[[float], bool]) -> None:
        """One long-poll of ``/v1/host/commands`` (the stream's fallback), then
        the device list, which the stream would have pushed. A round that
        moved nothing forward (nothing came, or nothing that could be acked)
        and came back sooner than POLL_MIN_INTERVAL_S waits out the rest, so
        a relay that answers at once is never polled in a tight loop."""
        began = time.monotonic()
        since = self._cursor
        path = f"/v1/host/commands?since={since}&wait={LONG_POLL_WAIT_S}"
        data = client.call("GET", path, timeout=LONG_POLL_TIMEOUT_S)
        for command in _objects(data.get("commands")):
            self._on_command(client, command)
        self._on_devices(client, client.call("GET", "/v1/host/devices"))
        quick = time.monotonic() - began
        if self._cursor == since and quick < POLL_MIN_INTERVAL_S:
            sleep(POLL_MIN_INTERVAL_S - quick)

    def _on_frame(self, client: RelayClient, event: SseEvent) -> None:
        data = _object(event.data)
        if data is None:
            self._log.warning("link: ignoring a %s frame that is not JSON", event.event)
            return
        if event.event == "command":
            self._on_command(client, data)
        elif event.event == "devices":
            self._on_devices(client, data)
        elif event.event == "revoked":
            raise Revoked(401, "unauthorized", "the relay revoked this host")
        else:
            self._log.debug("link: ignoring a %s frame", event.event)

    # -- commands --------------------------------------------------------------

    def _on_command(self, client: RelayClient, command: dict[str, object]) -> None:
        cid, seq = command.get("id"), command.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            self._log.warning("link: ignoring a command without a seq")
            return
        if not isinstance(cid, str) or not _COMMAND_ID.fullmatch(cid):
            # No answer can name it (the relay takes results for UUID ids
            # only), and it never reaches the signature check.
            self._log.warning("link: acknowledged command seq %d: malformed id", seq)
            self._ack(client, seq)
            return
        with self._lock:
            seen = cid in self._seen
            cached = self._results.get(cid)
            peer = self._peers.get(str(command.get("deviceId")))
        if seen:
            self._replay(client, cid, seq, cached)
            return
        if peer is None:
            self._log.warning("link: refused command %s from an untrusted device", cid)
            self._ack(client, seq)
            return
        remembered = False
        try:
            request = self._open_command(cid, peer, command)
        except _Refused as refused:
            # Never remembered: only a verified id may take a place in the
            # ring (or the result cache), so a relay cannot flush a real id
            # out with junk. A replayed refusal is just refused again.
            self._log.warning("link: refused command %s: %s", cid, refused.reason)
            status, body = (
                refused.status,
                _error(refused.code, str(refused), refused.reason),
            )
        else:
            remembered = True
            with self._lock:
                # The add is the seen check: of two deliveries of one id,
                # only the first to get here runs it, on any thread.
                fresh = self._seen.add(cid)
                saved = fresh and self._save()
                cached = None if fresh else self._results.get(cid)
            if not fresh:
                self._replay(client, cid, seq, cached)
                return
            if saved:
                status, body = self._run_command(cid, peer, *request)
            else:
                # Unsaved, a restart would not know it ran: so it does not run.
                status, body = (
                    503,
                    _error(
                        "unavailable",
                        "the link state could not be saved",
                        "state_unsaved",
                    ),
                )
        answer = json.dumps({"status": status, "body": body}, separators=(",", ":"))
        result: dict[str, object] = {
            "type": "command",
            "commandId": cid,
            "status": status,
            "sealed": linkcrypto.seal(peer.keys.h2d, answer.encode(), AAD_RES + cid),
        }
        if remembered:
            with self._lock:
                self._results[cid] = result
                while len(self._results) > RESULT_CACHE_MAX:
                    self._results.popitem(last=False)
        self._answer(client, cid, seq, result)

    def _replay(
        self,
        client: RelayClient,
        cid: str,
        seq: int,
        cached: dict[str, object] | None,
    ) -> None:
        """A command id already handled: its cached result again, or an ack."""
        if cached is not None:
            self._answer(client, cid, seq, cached)
        else:
            self._ack(client, seq)

    def _run_command(
        self, cid: str, peer: _Peer, method: str, path: str, body: bytes | None
    ) -> tuple[int, dict[str, object]]:
        try:
            return self._host.handle(method, path, body, peer.device.device_id)
        except Exception:  # noqa: BLE001  # reason: a command that crashes the handler is answered 500, never the end of the link thread; logged with its traceback
            self._log.exception("link: command %s crashed", cid)
            return 500, _error("internal", "internal error", "crashed")

    def _open_command(
        self, cid: str, peer: _Peer, command: dict[str, object]
    ) -> tuple[str, str, bytes | None]:
        """Check, verify and decrypt one command: (method, path, body bytes)."""
        method, path = command.get("method"), command.get("path")
        sealed, sig, ts = command.get("sealed"), command.get("sig"), command.get("ts")
        if not (
            isinstance(method, str)
            and isinstance(path, str)
            and isinstance(sealed, str)
            and isinstance(sig, str)
            and isinstance(ts, int)
            and not isinstance(ts, bool)
            and method in COMMAND_METHODS
            and _api_path(path)
        ):
            raise _Refused(
                400, "invalid_request", "malformed", "the command is malformed"
            )
        if abs(int(self._clock() * 1000) - ts) > COMMAND_MAX_AGE_MS:
            raise _Refused(410, "expired", "stale", "the command is over 120 s old")
        host_id = self._link.host_id if self._link is not None else ""
        try:
            signed = linkcrypto.command_signing_input(
                cid, host_id, method, path, ts, sealed
            )
        except ValueError:
            signed = b""
        if not signed or not linkcrypto.verify(peer.sign_pub, signed, sig):
            raise _Refused(
                403,
                "forbidden",
                "bad_signature",
                "the command signature did not verify",
            )
        try:
            plain = linkcrypto.open_sealed(peer.keys.d2h, sealed, AAD_CMD + cid)
            inner = _object(plain.decode("utf-8"))
        except (SealError, UnicodeDecodeError):
            inner = None
        if inner is None:
            raise _Refused(403, "forbidden", "bad_seal", "the command did not decrypt")
        if inner.get("method") != method or inner.get("path") != path:
            raise _Refused(
                403,
                "forbidden",
                "mismatch",
                "the sealed request differs from its routing fields",
            )
        body = inner.get("body")
        return method, path, None if body is None else json.dumps(body).encode("utf-8")

    def _answer(
        self, client: RelayClient, cid: str, seq: int, result: dict[str, object]
    ) -> None:
        """Post one result with ``ack=seq``. A permanent refusal (a 4xx) would
        be replayed on every reconnect, ahead of every later command, so the
        seq is acknowledged alone instead and the device's command expires."""
        try:
            client.call("POST", "/v1/host/results", {"results": [result], "ack": seq})
        except (Revoked, TooOld):
            raise
        except RelayError as exc:
            if _transient(exc):
                raise
            self._log.warning(
                "link: the relay refused the answer to command %s (%s %s);"
                " acknowledged without it",
                cid,
                exc.status,
                exc.code,
            )
            client.call("POST", "/v1/host/ack", {"seq": seq})
        self._advance(seq)

    def _ack(self, client: RelayClient, seq: int) -> None:
        client.call("POST", "/v1/host/ack", {"seq": seq})
        self._advance(seq)

    def _advance(self, seq: int) -> None:
        with self._lock:
            if seq > self._cursor:
                self._cursor = seq
                self._save()

    # -- devices and the snapshot ----------------------------------------------

    def _on_devices(self, client: RelayClient, data: dict[str, object]) -> None:
        """Apply the revocations in the relay's device list. Trust is only
        ever added locally (`magent link`); link.json is re-read, so a device
        paired while this runs is picked up here too. When link.json cannot
        be read, the revocations still apply, in memory."""
        revoked = {
            str(d.get("deviceId"))
            for d in _objects(data.get("devices"))
            if d.get("revokedAt") is not None
        }
        try:
            current = self._read_link()
            if current is not None:
                current = self._forget(current, revoked)
                peers = self._peers_for(current)
        except linkfiles.LinkFileError as exc:
            self._log.warning("link: %s; applying revocations in memory only", exc)
            current = None
        with self._lock:
            if current is None:
                peers = {k: p for k, p in self._peers.items() if k not in revoked}
            else:
                self._link = current
            changed = set(peers) != set(self._peers)
            self._peers = peers
        if changed:
            self.put_snapshot(client)

    def _forget(self, link: LinkFile, revoked: set[str]) -> LinkFile:
        """``link`` without the revoked devices, saved when that changed it."""
        kept = tuple(d for d in link.devices if d.device_id not in revoked)
        if len(kept) == len(link.devices):
            return link
        link = replace(link, devices=kept)
        self._log.info("link: the relay revoked a paired device; forgetting it")
        # Retried like _save: on Windows a reader holding link.json open makes
        # the atomic replace fail for a moment.
        for attempt in range(SAVE_ATTEMPTS):
            try:
                linkfiles.save_link(link, self._link_path)
            except OSError as exc:
                if attempt == SAVE_ATTEMPTS - 1:
                    self._log.warning("link: could not save link.json: %s", exc)
                else:
                    time.sleep(SAVE_RETRY_S)
            else:
                break
        return link

    def _read_link(self) -> LinkFile | None:
        """link.json. On Windows a writer's atomic replace (`magent link`, or
        this runner forgetting a device) makes a read fail for a moment, so a
        failed read is retried briefly before it counts."""
        for _ in range(SAVE_ATTEMPTS - 1):
            try:
                return linkfiles.load_link(self._link_path)
            except linkfiles.LinkFileError:
                time.sleep(SAVE_RETRY_S)
        return linkfiles.load_link(self._link_path)

    def put_snapshot(self, client: RelayClient) -> None:
        """PUT /v1/host/snapshot: routing metadata, plus the rows sealed per device."""
        rows, daemon = self._host.snapshot()
        plain = json.dumps({"sessions": rows}, separators=(",", ":")).encode("utf-8")
        with self._lock:
            host_id = self._link.host_id if self._link is not None else ""
        metas = [m for m in (session_meta(r) for r in rows) if m is not None]
        body = {
            "daemon": {
                key: _daemon_field(daemon.get(key))
                for key in ("version", "platform", "epoch")
            },
            "remoteControl": self._host.remote_control() is True,
            "uploadMaxBytes": max(1, self._host.upload_max_bytes()),
            "sessions": metas[:SNAPSHOT_SESSIONS_MAX],
            "sealed": self.seal_for_devices(plain, AAD_SNAP + host_id),
        }
        client.call("PUT", "/v1/host/snapshot", body)

    def online(self) -> bool:
        with self._lock:
            return self._state == "online"

    def seal_for_devices(self, plaintext: bytes, aad: str) -> dict[str, str]:
        """``plaintext`` sealed once per trusted device, with its h2d key."""
        with self._lock:
            peers = list(self._peers.values())
        return {
            p.device.device_id: linkcrypto.seal(p.keys.h2d, plaintext, aad)
            for p in peers
        }

    # -- files -----------------------------------------------------------------

    def _peers_for(self, link: LinkFile) -> dict[str, _Peer]:
        """The pair keys for every trusted device. A device whose saved keys
        are unusable (a link.json from before pairing refused weak keys) is
        left out and named in the log; a bad host key is a LinkFileError."""
        try:
            own = linkcrypto.b64u_decode(link.host_kex_priv)
        except ValueError:
            own = b""
        if len(own) != 32:
            raise linkfiles.LinkFileError("link.json holds an unusable host key")
        peers: dict[str, _Peer] = {}
        for device in link.devices:
            try:
                sign_pub = linkcrypto.b64u_decode(device.sign_pub)
                keys = linkcrypto.pair_keys(
                    own,
                    linkcrypto.b64u_decode(device.kex_pub),
                    link.host_id,
                    device.device_id,
                )
            except ValueError:
                sign_pub = b""
            if len(sign_pub) != 32:
                self._log.warning(
                    "link: ignoring paired device %s: its keys are unusable;"
                    " pair it again with `magent link`",
                    device.device_id,
                )
                continue
            peers[device.device_id] = _Peer(device, sign_pub, keys)
        return peers

    def _load(self, link: LinkFile) -> None:
        peers = self._peers_for(link)
        state = linkfiles.load_state(self._state_path)
        with self._lock:
            self._link = link
            self._peers = peers
            self._cursor = state.cursor
            self._seen = SeenRing(state.seen_ids, size=SEEN_MAX)
            self._last_connected = state.last_connected

    def _set_state(
        self,
        state: str,
        *,
        retry_at: float | None = None,
        last_connected: float | None = None,
    ) -> None:
        with self._lock:
            unchanged = state == self._state and retry_at == self._retry_at
            if unchanged and last_connected is None:
                return
            self._state = state
            self._retry_at = retry_at
            if last_connected is not None:
                self._last_connected = last_connected
            self._save()

    def _save(self) -> bool:
        """Persist the state; False when it could not be written. On Windows a
        reader holding link-state.json open (`magent status`) makes the atomic
        replace fail for a moment, so a failed write is retried briefly, then
        logged: the file is rewritten on the next change, and the in-memory
        state stays right meanwhile."""
        with self._lock:
            state = LinkState(
                cursor=self._cursor,
                seen_ids=self._seen.items(),
                last_connected=self._last_connected,
                state=self._state,
                retry_at=self._retry_at,
            )
            for attempt in range(SAVE_ATTEMPTS):
                try:
                    linkfiles.save_state(state, self._state_path)
                except OSError as exc:
                    if attempt == SAVE_ATTEMPTS - 1:
                        self._log.warning(
                            "link: could not save the link state: %s", exc
                        )
                    else:
                        time.sleep(SAVE_RETRY_S)
                else:
                    return True
            return False


def _delay(backoff: Backoff, error: RelayError | None) -> float:
    """The wait before the next attempt: the backoff, at least the relay's
    Retry-After on a transient refusal, PERMANENT_RETRY_S on any other."""
    delay = backoff.next_delay()
    if error is None:
        return delay
    if not _transient(error):
        return max(delay, PERMANENT_RETRY_S)
    if error.retry_after is not None:
        return max(delay, min(error.retry_after, RETRY_AFTER_MAX_S))
    return delay
