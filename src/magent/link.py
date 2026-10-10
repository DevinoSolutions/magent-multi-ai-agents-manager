"""magent link: the host side of the relay link (spec 4.1, P2).

This slice is the transport: a stdlib http.client client for the relay's
``/v1/host/*`` routes (every reply is the daemon envelope, contract C1), an SSE
parser for ``/v1/stream/host`` (C5), the reconnect backoff and the seen-id
ring. LinkRunner, the serve thread built on it, follows below in this module.

Outbound only: the relay never connects to this machine. MD013 keeps this
module the only new code that opens an outbound HTTP connection.
"""

from __future__ import annotations

import collections
import http.client
import importlib.metadata
import json
import random
import ssl
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from magent.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from magent.env import MagentEnv

PROTOCOL_HEADER = ("X-Magent-Link", "1")
DEFAULT_RELAY_URL = "https://api.magent.now"
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
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            # The URL itself stays out of the message: it may carry userinfo.
            raise ValueError("not a relay URL: need http(s)://host")
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
        """One request on its own connection: status, body, Retry-After."""
        conn, target = self._connection(self.base_url + path, timeout)
        try:
            resp = _send(conn, method, target, payload, headers)
            return resp.status, _read(resp, MAX_JSON_BYTES + 1), _retry_after(resp)
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
    ) -> dict[str, object]:
        """One JSON request; the envelope's ``data`` on success.

        A 429 is retried (up to ``retries`` times) after its Retry-After, or a
        full-jitter backoff when it has none. A Retry-After past
        RATE_LIMIT_CAP_S raises at once with ``retry_after`` set, for the
        caller to wait out. The relay rate-limits before it acts on a request,
        so a retried POST is never applied twice. ``timeout`` overrides the
        client's for this call (the long-poll path passes its own).
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
        backoff = Backoff(cap=RATE_LIMIT_CAP_S)
        attempt = 0
        while True:
            status, raw, retry_after = self._exchange(
                method, path, payload, headers, wait
            )
            too_long = retry_after is not None and retry_after > RATE_LIMIT_CAP_S
            if status != 429 or attempt >= self._retries or too_long:
                return _decode(status, raw, retry_after=retry_after)
            attempt += 1
            self._sleep(backoff.next_delay() if retry_after is None else retry_after)

    def stream(self, path: str, *, read_timeout: float = 60.0) -> Iterator[SseEvent]:
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
