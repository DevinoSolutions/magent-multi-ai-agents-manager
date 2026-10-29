"""magent link: the host side of the relay link (spec 4.1, P2).

The transport first: a stdlib http.client client for the relay's
``/v1/host/*`` routes (every reply is the daemon envelope, contract C1), an SSE
parser for ``/v1/stream/host`` (C5), the reconnect backoff and the seen-id
ring. Then LinkRunner, the serve thread built on it: the relay connection,
the snapshot, and the commands paired devices send. Then EventForwarder,
the runner's second thread: the daemon's events, sealed per device. Then
UploadPuller, its third: the files paired devices upload, pulled from
storage, decrypted and handed to the daemon. Last, supervise, the body of
serve's link thread around the runner, and the one config key the link
writes (``settings.link.remoteControl``).

Outbound only: the relay never connects to this machine. MD013 keeps this
module the only new code that opens an outbound HTTP connection.
"""

from __future__ import annotations

import collections
import contextlib
import http.client
import importlib.metadata
import ipaddress
import json
import logging
import math
import os
import queue
import random
import re
import secrets
import shutil
import socket
import ssl
import threading
import time
import unicodedata
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

from magent import linkcrypto, linkfiles, lockfile
from magent.linkcrypto import (
    AAD_CMD,
    AAD_EVT,
    AAD_RES,
    AAD_SNAP,
    AAD_UKEY,
    AAD_UMETA,
    SealError,
)
from magent.linkfiles import SEEN_MAX, LinkState
from magent.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterable, Iterator
    from pathlib import Path
    from typing import BinaryIO

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
# The longest one presigned download may take, start to finish: 100 MB at
# about 1 Mbit/s. Each read is still bounded by the client's timeout.
DOWNLOAD_DEADLINE_S = 900.0


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
    reply will close the connection. A deadline started before the socket
    exists is handed it with ``watch`` (cut at once if the time is up), and
    ``left`` bounds what a cut cannot wake on every OS (a TLS handshake).
    """

    def __init__(self, sock: socket.socket | None, seconds: float) -> None:
        self.expired = False
        self._sock = sock
        self._seconds = seconds
        self._ends = time.monotonic() + seconds
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True

    def __enter__(self) -> _Deadline:
        self._ends = time.monotonic() + self._seconds
        self._timer.start()
        return self

    def left(self) -> float:
        """Seconds until the cut (0 or less once it is due)."""
        return self._ends - time.monotonic()

    @property
    def passed(self) -> bool:
        """Due, whether or not the timer thread has run yet."""
        return self.expired or self.left() <= 0

    def __exit__(self, *_: object) -> None:
        self._timer.cancel()

    def watch(self, sock: socket.socket | None) -> None:
        # Set before the check, as _expire sets expired before it reads the
        # socket: whichever runs second does the cut.
        self._sock = sock
        if self.expired:
            self._cut()

    def _expire(self) -> None:
        self.expired = True
        self._cut()

    def _cut(self) -> None:
        _shutdown(self._sock)


def _shutdown(sock: socket.socket | None) -> None:
    """Shut ``sock`` down, which wakes a read blocked on it on any thread."""
    if sock is not None:
        with contextlib.suppress(OSError):
            # The plain socket's shutdown: a TLS socket's own would also
            # drop its TLS state under the thread still reading it.
            socket.socket.shutdown(sock, socket.SHUT_RDWR)


class StreamCut:
    """A stream another thread can end: ``cut`` shuts its socket down, which
    wakes the read blocked on it, and the stream ends (or raises RelayError
    ``stream_lost``). A cut before the stream has its socket takes effect
    as soon as ``watch`` hands it one."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._done = False

    def watch(self, sock: socket.socket | None) -> None:
        with self._lock:
            self._sock = sock
            done = self._done
        if done:
            _shutdown(sock)

    def cut(self) -> None:
        with self._lock:
            self._done = True
            sock = self._sock
        _shutdown(sock)


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
        total deadline (see _exchange). A ``sleep`` that returns True (a
        stop-aware wait cut short) ends the retries: the 429 is raised.
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
            delay = backoff.next_delay() if retry_after is None else retry_after
            if self._sleep(delay) is True:
                # The caller is stopping: the 429 stands, no retry back to back.
                return _decode(status, raw, retry_after=retry_after)

    def stream(
        self,
        path: str,
        *,
        read_timeout: float = 60.0,
        cut: StreamCut | None = None,
    ) -> Generator[SseEvent, None, None]:
        """The relay's SSE stream. Ends when the relay closes it; RelayError
        when it is refused or the connection drops; ``read_timeout`` bounds the
        silence between two frames (the relay sends a ping every 15 s).
        ``cut`` lets another thread end it at once.

        A 429 refusal is not retried here: it raises with ``retry_after`` set,
        for the reconnect loop to wait out.
        """
        conn, target = self._connection(self.base_url + path, read_timeout)
        try:
            _connect(conn)
            if cut is not None:
                cut.watch(conn.sock)
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
        self,
        url: str,
        sink: Callable[[bytes], None],
        *,
        max_bytes: int,
        deadline: float = DOWNLOAD_DEADLINE_S,
    ) -> int:
        """Stream an absolute (presigned) URL into ``sink``; the byte count.

        No relay headers: a presigned GET carries its own authority, and an
        extra Authorization header would make the storage service refuse it.
        A relay on https hands out https URLs on a public storage host, so
        then only https on port 443 is fetched, and never from a loopback,
        private or otherwise non-public address: the name is resolved once,
        every address it has must be public, and the connection goes to the
        one that was checked. A relay on plain http (a local one) may name
        any http(s) URL. A URL refused here is a RelayError
        ``invalid_response``. ``deadline`` bounds the whole download, the TLS
        handshake included, however slowly it trickles in (the name lookup
        and the TCP connect only by the client's timeout). The URL never
        appears in an error.
        """
        parts = urlsplit(url)
        secure = urlsplit(self.base_url).scheme == "https"
        try:
            port = parts.port  # read now: a bad port is a bad URL
        except ValueError:
            port = -1
        if secure:
            allowed = parts.scheme == "https" and port in (None, 443)
        else:
            allowed = parts.scheme in ("https", "http") and port != -1
        if not allowed or not parts.hostname:
            raise RelayError(0, "invalid_response", "download URL is not allowed")
        with _Deadline(None, deadline) as cut:
            conn: http.client.HTTPConnection | None = None
            try:
                try:
                    if secure:
                        conn, target = self._public_connection(url, cut.left)
                    else:
                        conn, target = self._connection(url, self._timeout)
                except ssl.SSLError as exc:
                    # No TLS context here (a broken certificate store): a
                    # retry would fail the same way, so the URL is refused.
                    raise RelayError(
                        0, "invalid_response", "TLS could not be set up here"
                    ) from exc
                _connect(conn)
                cut.watch(conn.sock)
                total = self._download(conn, target, sink, max_bytes)
            except RelayError:
                if cut.passed:
                    raise _too_slow() from None
                raise
            finally:
                if conn is not None:
                    conn.close()
            if cut.passed:
                # A body without a Content-Length reads a cut as its end.
                raise _too_slow()
            return total

    def _public_connection(
        self, url: str, budget: Callable[[], float]
    ) -> tuple[http.client.HTTPConnection, str]:
        parts = urlsplit(url)
        host = parts.hostname or ""
        address = _public_address(host, 443)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        conn = _PinnedHTTPSConnection(
            host,
            443,
            address=address,
            timeout=self._timeout,
            context=ssl.create_default_context(),
            budget=budget,
        )
        return conn, target

    @staticmethod
    def _download(
        conn: http.client.HTTPConnection,
        target: str,
        sink: Callable[[bytes], None],
        max_bytes: int,
    ) -> int:
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


# NAT64's well-known prefix: behind a NAT64 gateway, 64:ff9b::a00:1 reaches
# the IPv4 address 10.0.0.1.
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")


def _is_public(address: str) -> bool:
    """A globally routable unicast address that is not reserved. An IPv6
    address that carries an IPv4 one (IPv4-mapped, 6to4, NAT64) is judged
    as the IPv4 address it reaches."""
    try:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(
            address.split("%", 1)[0]
        )
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None:
            ip = ip.sixtofour
        elif ip in _NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def _public_address(host: str, port: int) -> str:
    """The address to dial for ``host``: a RelayError when it resolves to
    nothing, or to any address that is not public."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise RelayError(0, "network", type(exc).__name__) from exc
    addresses = [str(info[4][0]) for info in infos]
    if not addresses or not all(_is_public(a) for a in addresses):
        raise RelayError(0, "invalid_response", "download host is not public")
    return addresses[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to one address checked beforehand, so a second name lookup can
    never swap in another; the certificate is still verified for the URL's
    host name."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        address: str,
        timeout: float,
        context: ssl.SSLContext,
        budget: Callable[[], float],
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._address = address
        self._tls = context
        self._budget = budget
        self._read_timeout = timeout

    def connect(self) -> None:
        """Dial, then shake hands within what is left of the caller's
        deadline (``budget``): the socket timeout bounds the whole handshake,
        which a cut from another thread does not wake on every OS."""
        sock = socket.create_connection((self._address, self.port), self.timeout)
        tls: ssl.SSLSocket | None = None
        try:
            tls = self._tls.wrap_socket(
                sock, server_hostname=self.host, do_handshake_on_connect=False
            )
            tls.settimeout(max(0.001, min(self._read_timeout, self._budget())))
            tls.do_handshake()
            tls.settimeout(self._read_timeout)
        except BaseException:
            (tls or sock).close()
            raise
        self.sock = tls


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


# A log line that can repeat (a refusal per command, a cut per pane) goes out
# at most once per kind this often.
LOG_QUIET_S = 60.0
FRAME_NAME_MAX = 40  # chars of a relay-chosen frame name that reach a log line


class _LogBudget:
    """At most one line per ``kind`` every LOG_QUIET_S seconds, so a relay
    replaying junk cannot fill link.log. The lines held back meanwhile are
    counted, and the next line of that kind that goes out says how many."""

    def __init__(
        self, log: logging.Logger, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._log = log
        self._clock = clock
        self._lock = threading.Lock()
        self._last: dict[str, tuple[float, int]] = {}  # kind -> (sent at, held)

    def log(self, level: int, kind: str, message: str, *args: object) -> None:
        now = self._clock()
        with self._lock:
            last = self._last.get(kind)
            if last is not None and now - last[0] < LOG_QUIET_S:
                self._last[kind] = (last[0], last[1] + 1)
                return
            held = 0 if last is None else last[1]
            self._last[kind] = (now, 0)
        if held:
            message += " (%d more like it since the last one)"
            args = (*args, held)
        self._log.log(level, message, *args)


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


def shown_url(url: str) -> str:
    """``url`` as a screen may show it: scheme, host, port and path, never
    the userinfo (a relay URL can carry credentials), query or fragment."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "(unreadable URL)"
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    where = f"{host}:{port}" if port is not None else host
    return f"{parts.scheme}://{where}{parts.path}".rstrip("/")


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
SNAPSHOT_MAX_BYTES = 4 * 1024 * 1024  # the relay's cap on one snapshot PUT body
# The relay's cap on one sealed blob (C4), in base64url characters: a command
# result, or one device's copy of the snapshot or of an event.
SEALED_MAX_CHARS = 1_400_000
# What a device gets instead of an answer too large to reach it: the link, as
# a gateway, could not deliver what the daemon said.
RESULT_TOO_LARGE_STATUS = 502
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


class _Relinked(Exception):
    """link.json names another host or token than the one this run holds
    (`magent link` paired again): the run ends, and the link thread starts
    a new one on the new credentials."""


class _Unlinked(Exception):
    """link.json is gone (`magent unlink`): the run ends, holding nothing."""


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


def utf16_len(text: str) -> int:
    """``text``'s length as the relay counts it: UTF-16 code units, so a
    character outside the Basic Multilingual Plane (an emoji) counts two."""
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)


def utf16_cut(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` UTF-16 code units (the relay's
    measure), never through the middle of a surrogate pair: a character that
    would not fit whole is left out."""
    units = 0
    for i, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > limit:
            return text[:i]
    return text


def _state_text(value: object) -> str | None:
    if isinstance(value, str) and utf16_len(value) <= SESSION_STATE_MAX:
        return value
    return None


def session_meta(row: dict[str, object]) -> dict[str, object] | None:
    """The routing-only view of a SessionRow the relay may read (C4), or None
    for a row the relay would refuse (its id is not a session id). Values
    past the relay's bounds are cut (a name) or dropped (a state), measured
    as the relay counts, in UTF-16 code units; the full row still reaches the
    devices, sealed."""
    session = row.get("session")
    if not isinstance(session, str) or not _SESSION_ID.fullmatch(session):
        return None
    name = row.get("name")
    return {
        "session": session,
        "name": utf16_cut(name, SESSION_NAME_MAX) if isinstance(name, str) else "",
        "live": row.get("live") is True,
        "hookState": _state_text(row.get("hook_state")),
        "paneState": _state_text(row.get("pane_state")),
        "nodeState": _state_text(row.get("node_state")),
    }


def _daemon_field(value: object) -> str:
    """1-32 characters, as the relay takes ``daemon.*`` (it counts UTF-16
    code units)."""
    if isinstance(value, str) and value:
        return utf16_cut(value, DAEMON_FIELD_MAX)
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
    ``too_old`` end the run, and so does a link.json that names another host
    or token (``relinked``) or is gone (``unlinked``: `magent unlink` deletes
    it even when the relay could not be told). link.json is checked at every
    connect, device list and command, and by the event forwarder every
    LINK_CHECK_S, so a quiet stream is closed too; a run that ends on it
    drops its link (token and keys) and trusted devices from memory and
    writes no more state. Every
    (re)connect PUTs a fresh snapshot (bounded to what the relay takes), then
    reads the SSE stream from the saved cursor. A failure with no reply, a
    5xx or a 429 is retried after a full-jitter backoff (at least the
    relay's Retry-After); any other 4xx waits PERMANENT_RETRY_S. Three failed
    connects inside five minutes switch to long-polling ``/v1/host/commands``
    for thirty.

    A command is checked field by field (a UUID id, a known method, a path
    under the API prefix with no control character, an int ts), then verified
    (trusted device, Ed25519 signature, age), decrypted, checked against its
    outer method and path, run through ``LinkHost.handle`` and answered
    sealed (an answer too large for the relay as a sealed 502
    ``result_too_large`` instead). Each id is saved before it runs, so a command the relay replays
    never runs twice: its cached result is re-sent, or, after a restart, it
    is only acknowledged. The cursor moves only once the relay has the
    answer, so a lost result post is replayed, not dropped, and a second
    delivery of a command still running is not acknowledged at all.
    ``upload.ready`` (a stream frame, or a poll's ``uploads``) goes to the
    UploadPuller.
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
        self._quiet = _LogBudget(self._log)
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
        # put_snapshot runs on the runner and on the event forwarder (a
        # reset): one at a time, so an older read never lands last.
        self._snapshot_lock = threading.Lock()
        # Command ids being run now. Seen with no cached answer used to mean
        # "ran before a restart"; with a second thread delivering, it can
        # mean "still running", and that duplicate must not be acked.
        self._inflight: set[str] = set()
        self._uploads: UploadPuller | None = None
        # The remoteControl the relay was last told (None: no snapshot yet).
        self._rc_put: bool | None = None
        # link.json's stamp when this run last read it, how the run was ended
        # from link.json ("unlinked", "relinked"; None: it was not), and the
        # cut for the stream being read (None between streams).
        self._stamp: tuple[int, int, int] | None = None
        self._end: str | None = None
        self._cut: StreamCut | None = None
        # Every device the relay listed revoked during this run: a later
        # link.json that still lists one (a revocation that could only apply
        # in memory) never trusts it again. One trust refresh at a time.
        self._revoked: set[str] = set()
        self._trust_lock = threading.Lock()

    # -- the loop ------------------------------------------------------------

    def run(self, stop: threading.Event) -> str:
        """Run until ``stop``; how it ended: revoked, too_old, stopped,
        relinked (link.json now names another host or token: run again) or
        unlinked (link.json is gone: run again, which idles until linked)."""
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
                return self._run_linked(client, stop, sleep)
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
            # Taken before the read: a change after it is seen next time.
            stamp = _link_stamp(self._link_path)
            link = self._read_link()
            if link is None:
                return None
            _check_host_token(link)
            self._load(link)
            with self._lock:
                self._stamp = stamp
            return self._client_factory(link.relay_url, link.host_token, sleep=sleep)
        except (linkfiles.LinkFileError, ValueError) as exc:
            if not self._warned_bad_file:
                self._warned_bad_file = True
                self._log.warning(
                    "link: %s; repair with `magent unlink`, then `magent link`",
                    exc,
                )
            return None

    def _run_linked(
        self, client: RelayClient, stop: threading.Event, sleep: Callable[[float], bool]
    ) -> str:
        """The connection loop, with the event forwarder and the upload
        puller beside it for as long as it runs, however it ends: the puller
        is stopped even when stopping the forwarder fails."""
        forwarder = EventForwarder(self, client, self._host.subscribe)
        puller = UploadPuller(self, client, self._host)
        forwarder.start()
        puller.start()
        self._uploads = puller
        try:
            return self._connect_loop(client, stop, sleep)
        finally:
            self._uploads = None
            try:
                forwarder.stop()
            finally:
                puller.stop()

    def _connect_loop(
        self, client: RelayClient, stop: threading.Event, sleep: Callable[[float], bool]
    ) -> str:
        backoff = Backoff()
        failures: collections.deque[float] = collections.deque()
        poll_until = 0.0
        need_snapshot = True
        # The last attempt ended in a LinkHost failure: the next one logs a
        # line, not the same traceback again.
        host_failing = False
        while not stop.is_set():
            if (end := self._ended()) is not None:
                return end
            polling = self._clock() < poll_until
            started: float | None = None
            error: RelayError | None = None
            self._frames = 0
            try:
                if need_snapshot:
                    self._refresh_trust(None)
                    self._set_state("connecting")
                    self.put_snapshot(client)
                    need_snapshot = False
                    self._uploads_connected()
                started = self._clock()
                self._set_state("online", last_connected=started)
                if polling:
                    self._poll_round(client, sleep)
                    host_failing = False
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
            except (_Relinked, _Unlinked):
                return self._ended() or "relinked"
            except RelayError as exc:
                need_snapshot = True
                error = exc
                if _transient(exc):
                    self._log.info("link: relay connection ended: %s", exc)
                else:
                    self._log.warning("link: the relay refused this host: %s", exc)
            except linkcrypto.LinkCryptoUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001  # reason: a LinkHost failure (snapshot, settings) backs off and retries like a dropped connection; it must never end the link thread
                need_snapshot = True
                if host_failing:
                    self._log.warning(
                        "link: the daemon side of the link failed again (%s)",
                        type(exc).__name__,
                    )
                else:
                    self._log.exception("link: the daemon side of the link failed")
                error = RelayError(0, _HOST_FAILED, "the link host failed")
            if (end := self._ended()) is not None:
                # Ended from link.json on another thread, which cut the stream.
                return end
            host_failing = error is not None and error.code == _HOST_FAILED
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
        cut = StreamCut()
        with self._lock:
            self._cut = cut
        try:
            # Checked once the cut is in place: an end set before it would
            # find no stream to cut.
            self._check_end()
            with contextlib.closing(
                client.stream(path, read_timeout=STREAM_READ_TIMEOUT_S, cut=cut)
            ) as frames:
                for event in frames:
                    self._frames += 1
                    self._on_frame(client, event)
                    if stop.is_set():
                        return
        finally:
            with self._lock:
                self._cut = None

    def _poll_round(self, client: RelayClient, sleep: Callable[[float], bool]) -> None:
        """One long-poll of ``/v1/host/commands`` (the stream's fallback): its
        commands and ready uploads, then the device list, which the stream
        would have pushed. A round that moved nothing forward (nothing came,
        or nothing that could be acked) and came back sooner than
        POLL_MIN_INTERVAL_S waits out the rest, so a relay that answers at
        once is never polled in a tight loop."""
        began = time.monotonic()
        since = self._cursor
        path = f"/v1/host/commands?since={since}&wait={LONG_POLL_WAIT_S}"
        data = client.call("GET", path, timeout=LONG_POLL_TIMEOUT_S)
        for command in _objects(data.get("commands")):
            self._on_command(client, command)
        for ready in _objects(data.get("uploads")):
            self._offer_upload(ready)
        self._on_devices(client, client.call("GET", "/v1/host/devices"))
        quick = time.monotonic() - began
        if self._cursor == since and quick < POLL_MIN_INTERVAL_S:
            sleep(POLL_MIN_INTERVAL_S - quick)

    def _on_frame(self, client: RelayClient, event: SseEvent) -> None:
        self._check_end()
        data = _object(event.data)
        if data is None:
            self._quiet.log(
                logging.WARNING,
                "not-json",
                "link: ignoring a %s frame that is not JSON",
                event.event[:FRAME_NAME_MAX],  # relay-chosen: kept short
            )
            return
        if event.event == "command":
            self._on_command(client, data)
        elif event.event == "devices":
            self._on_devices(client, data)
        elif event.event == "upload.ready":
            self._offer_upload(data)
        elif event.event == "revoked":
            raise Revoked(401, "unauthorized", "the relay revoked this host")
        else:
            self._log.debug("link: ignoring a %s frame", event.event)

    # -- commands --------------------------------------------------------------

    def _on_command(self, client: RelayClient, command: dict[str, object]) -> None:
        # link.json first: a command that arrives after `magent unlink` must
        # not run on the keys the run still holds, and one from a device
        # `magent link --add-device` just saved must not be refused.
        self.watch_link(client)
        cid, seq = command.get("id"), command.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            self._quiet.log(
                logging.WARNING, "no-seq", "link: ignoring a command without a seq"
            )
            return
        if not isinstance(cid, str) or not _COMMAND_ID.fullmatch(cid):
            # No answer can name it (the relay takes results for UUID ids
            # only), and it never reaches the signature check.
            self._quiet.log(
                logging.WARNING,
                "malformed-id",
                "link: acknowledged command seq %d: malformed id",
                seq,
            )
            self._ack(client, seq)
            return
        with self._lock:
            seen = cid in self._seen
            running = cid in self._inflight
            cached = self._results.get(cid)
            peer = self._peers.get(str(command.get("deviceId")))
        if running:
            self._still_running(cid)
            return
        if seen:
            self._replay(client, cid, seq, cached, peer)
            return
        if peer is None:
            self._quiet.log(
                logging.WARNING,
                "untrusted",
                "link: refused command %s from an untrusted device",
                cid,
            )
            self._ack(client, seq)
            return
        remembered = claimed = False
        try:
            try:
                request = self._open_command(cid, peer, command)
            except _Refused as refused:
                # Never remembered: only a verified id may take a place in the
                # ring (or the result cache), so a relay cannot flush a real id
                # out with junk. A replayed refusal is just refused again.
                self._quiet.log(
                    logging.WARNING,
                    "refused",
                    "link: refused command %s: %s",
                    cid,
                    refused.reason,
                )
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
                    running = not fresh and cid in self._inflight
                    cached = None if fresh else self._results.get(cid)
                    if fresh:
                        self._inflight.add(cid)
                        claimed = True
                if running:
                    self._still_running(cid)
                    return
                if not fresh:
                    self._replay(client, cid, seq, cached, peer)
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
            result = _sealed_result(peer, cid, status, body)
            if len(str(result["sealed"])) > SEALED_MAX_CHARS:
                self._quiet.log(
                    logging.WARNING,
                    "too-large",
                    "link: the answer to command %s is %d characters sealed, over"
                    " the relay's %d; the device gets result_too_large instead",
                    cid,
                    len(str(result["sealed"])),
                    SEALED_MAX_CHARS,
                )
                result = _too_large(peer, cid)
            if remembered:
                with self._lock:
                    self._results[cid] = result
                    while len(self._results) > RESULT_CACHE_MAX:
                        self._results.popitem(last=False)
            self._answer(client, cid, seq, result, peer)
        finally:
            if claimed:
                with self._lock:
                    self._inflight.discard(cid)

    def _still_running(self, cid: str) -> None:
        """A second delivery of a command still running elsewhere: left
        unacknowledged, so the cursor never passes an answer not yet posted.
        The relay delivers it again, and by then its answer is cached."""
        self._log.info("link: command %s is still running; not acknowledged", cid)

    def _replay(
        self,
        client: RelayClient,
        cid: str,
        seq: int,
        cached: dict[str, object] | None,
        peer: _Peer | None,
    ) -> None:
        """A command id already handled: its cached result again, or an ack."""
        if cached is not None:
            self._answer(client, cid, seq, cached, peer)
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
        self,
        client: RelayClient,
        cid: str,
        seq: int,
        result: dict[str, object],
        peer: _Peer | None = None,
    ) -> None:
        """Post one result with ``ack=seq``. An answer the relay refuses as too
        large (413) is replaced, when the device ``peer`` is known, by a
        sealed 502 ``result_too_large`` (cached in its place), so the device
        learns why rather than waiting out its command. Any other permanent
        refusal (a 4xx) would be replayed on every reconnect, ahead of every
        later command, so the seq is acknowledged alone instead and the
        device's command expires."""
        try:
            try:
                client.call(
                    "POST", "/v1/host/results", {"results": [result], "ack": seq}
                )
            except RelayError as exc:
                if exc.status != 413 or peer is None:
                    raise
                self._quiet.log(
                    logging.WARNING,
                    "too-large",
                    "link: the relay refused the answer to command %s as too"
                    " large; the device gets result_too_large instead",
                    cid,
                )
                small = _too_large(peer, cid)
                with self._lock:
                    if cid in self._results:
                        self._results[cid] = small
                client.call(
                    "POST", "/v1/host/results", {"results": [small], "ack": seq}
                )
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
        self._refresh_trust(client, revoked)

    def _refresh_trust(
        self, client: RelayClient | None, revoked: set[str] | None = None
    ) -> None:
        """Trust what link.json lists now, less every device the relay has
        listed revoked during this run (``revoked``: the device list just
        received; None for a look of this host's own, which stays quiet when
        the file cannot be read). A file that is gone, or names another
        pairing, ends the run (see _end_run). A change in who is trusted
        re-PUTs the snapshot through ``client`` (None: the caller PUTs one
        next), so a device just paired gets its sessions at once. When
        link.json cannot be read, the revocations apply in memory only."""
        self._check_end()
        with self._trust_lock:
            with self._lock:
                self._revoked |= revoked or set()
                gone = set(self._revoked)
            # Taken before the read: a change after it is seen next time.
            stamp = _link_stamp(self._link_path)
            try:
                current = self._read_link()
                if current is None:
                    self._end_run("unlinked")
                    raise _Unlinked
                self._check_same_link(current)
                current = self._forget(current, gone)
                peers = self._peers_for(current)
            except linkfiles.LinkFileError as exc:
                if revoked is not None:
                    self._log.warning(
                        "link: %s; applying revocations in memory only", exc
                    )
                current = None
            with self._lock:
                changed = False
                if self._end is None:  # not ended on another thread meanwhile
                    if current is None:
                        peers = {k: p for k, p in self._peers.items() if k not in gone}
                    else:
                        self._link = current
                        self._stamp = stamp
                    changed = set(peers) != set(self._peers)
                    self._peers = peers
        self._check_end()
        if changed and client is not None:
            self.put_snapshot(client)

    def _check_same_link(self, current: LinkFile) -> None:
        """_Relinked when ``current`` names another host or token than the
        one this run connected with: its keys belong to the new pairing, and
        sealing for them under the old token would be neither link."""
        with self._lock:
            held = self._link
        if held is not None and (current.host_id, current.host_token) != (
            held.host_id,
            held.host_token,
        ):
            self._end_run("relinked")
            raise _Relinked

    def watch_link(self, client: RelayClient | None) -> None:
        """Refresh trust from link.json if it changed since this run read it
        (a stat when it did not): see _refresh_trust. Called on every
        command, and by the event forwarder every LINK_CHECK_S, so a device
        `magent link --add-device` saved is trusted, and sent a snapshot,
        without a reconnect (the relay announces it before it is saved), and
        a quiet stream is closed once link.json is gone. A file that cannot
        be read now is looked at again next time."""
        self._check_end()
        with self._lock:
            stamp = self._stamp
        if _link_stamp(self._link_path) != stamp:
            self._refresh_trust(client)

    def _end_run(self, outcome: str) -> None:
        """End this run from link.json (``unlinked``, ``relinked``): drop the
        link (host token and keys), the trusted devices and the cached
        results from memory, stop writing state, and cut the stream being
        read, from whichever thread found it. The first outcome stands."""
        with self._lock:
            if self._end is None:
                self._end = outcome
            self._link = None
            self._peers = {}
            self._results.clear()
            self._state = outcome  # in memory only: the forwarder stops posting
            cut = self._cut
        if cut is not None:
            cut.cut()

    def _check_end(self) -> None:
        """Raise how the run was ended from link.json, if it was."""
        with self._lock:
            end = self._end
        if end == "unlinked":
            raise _Unlinked
        if end is not None:
            raise _Relinked

    def _ended(self) -> str | None:
        """How the run was ended from link.json (logged), or None."""
        with self._lock:
            end = self._end
        if end == "unlinked":
            self._log.info(
                "link: this machine was unlinked; the relay connection is closed"
            )
        elif end is not None:
            self._log.info("link: this machine was paired again; reconnecting")
        return end

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
        """PUT /v1/host/snapshot: routing metadata, plus the rows sealed per
        device. Serialised, the read with its PUT: two threads put snapshots."""
        with self._snapshot_lock:
            rows, daemon = self._host.snapshot()
            remote_control = self._host.remote_control() is True
            fixed: dict[str, object] = {
                "daemon": {
                    key: _daemon_field(daemon.get(key))
                    for key in ("version", "platform", "epoch")
                },
                "remoteControl": remote_control,
                "uploadMaxBytes": max(1, self._host.upload_max_bytes()),
            }
            client.call("PUT", "/v1/host/snapshot", self._snapshot_body(rows, fixed))
            with self._lock:
                self._rc_put = remote_control

    def remote_control_changed(self) -> bool:
        """Has ``settings.link.remoteControl`` changed since the relay was
        last told (the last snapshot PUT)? The relay mirrors it for the app,
        and a healthy stream PUTs no snapshot for hours. False before the
        first PUT, and whenever the setting cannot be read."""
        with self._lock:
            told = self._rc_put
        if told is None:
            return False
        try:
            return (self._host.remote_control() is True) != told
        except Exception:  # noqa: BLE001  # reason: a LinkHost failure here only delays the re-PUT; the forwarder's quiet read must go on
            return False

    def _snapshot_body(
        self, rows: list[dict[str, object]], fixed: dict[str, object]
    ) -> dict[str, object]:
        """The PUT body for ``rows``, at most SNAPSHOT_SESSIONS_MAX of them,
        and, sealed once per device, at most the relay's SNAPSHOT_MAX_BYTES,
        each device's copy at most SEALED_MAX_CHARS: a fleet too big for that
        keeps the rows that fit, in order (with a warning, at most one per
        LOG_QUIET_S), rather than being refused on every connect."""
        aad = AAD_SNAP + self.host_id
        rows = rows[:SNAPSHOT_SESSIONS_MAX]
        total = len(rows)
        while True:
            plain = json.dumps({"sessions": rows}, separators=(",", ":")).encode()
            sealed = self.seal_for_devices(plain, aad)
            body = {
                **fixed,
                "sessions": [m for m in map(session_meta, rows) if m is not None],
                "sealed": sealed,
            }
            size = len(json.dumps(body, separators=(",", ":")))
            # Each cap as a share of what it allows; the worse one decides.
            over = max(
                size / SNAPSHOT_MAX_BYTES,
                max((len(v) for v in sealed.values()), default=0) / SEALED_MAX_CHARS,
            )
            if over <= 1 or not rows:
                break
            # Shrunk in proportion, a little past it: rows differ in size.
            rows = rows[: int(len(rows) / over * 0.95)]
        if len(rows) < total:
            self._quiet.log(
                logging.WARNING,
                "snapshot-trim",
                "link: the snapshot holds %d of %d sessions: sealed for every "
                "device, the rest would pass the relay's caps (%d bytes in all,"
                " %d characters per device)",
                len(rows),
                total,
                SNAPSHOT_MAX_BYTES,
                SEALED_MAX_CHARS,
            )
        return body

    def online(self) -> bool:
        with self._lock:
            return self._state == "online"

    def backing_off(self) -> bool:
        """Waiting out a backoff between attempts (``offline``), which can
        last minutes to hours while the relay is down."""
        with self._lock:
            return self._state == "offline"

    @property
    def host_id(self) -> str:
        """The linked host's id ("" before a link is loaded)."""
        with self._lock:
            return self._link.host_id if self._link is not None else ""

    def seal_for_devices(self, plaintext: bytes, aad: str) -> dict[str, str]:
        """``plaintext`` sealed once per trusted device, with its h2d key."""
        with self._lock:
            peers = list(self._peers.values())
        return {
            p.device.device_id: linkcrypto.seal(p.keys.h2d, plaintext, aad)
            for p in peers
        }

    # -- uploads (what the puller reads off the runner) ------------------------

    def _offer_upload(self, ready: dict[str, object]) -> None:
        puller = self._uploads
        if puller is not None:
            puller.offer(ready)

    def _uploads_connected(self) -> None:
        puller = self._uploads
        if puller is not None:
            puller.connected()

    def peer(self, device_id: str) -> _Peer | None:
        """The trusted device ``device_id`` and its keys, or None."""
        with self._lock:
            return self._peers.get(device_id)

    def has_seen(self, item_id: str) -> bool:
        with self._lock:
            return item_id in self._seen

    def remember(self, item_id: str) -> bool | None:
        """Add a verified upload id to the seen ring, persisted. The add is
        the seen check, one step under the lock: None when the id was
        already there (another delivery got here first), else whether the
        ring was saved."""
        with self._lock:
            if not self._seen.add(item_id):
                return None
            return self._save()

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
        if state.host_id is not None and state.host_id != link.host_id:
            # Another pairing's cursor would open the new host's stream past
            # its first commands, and its seen ring holds the old host's ids.
            # (None: a file from before the field, kept as it is.)
            self._log.info(
                "link: link-state.json belongs to another pairing; starting fresh"
            )
            state = LinkState()
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
            if self._end is not None:
                # Ended from link.json: `magent unlink` deleted this file,
                # or it belongs to the new pairing now.
                return False
            state = LinkState(
                cursor=self._cursor,
                seen_ids=self._seen.items(),
                last_connected=self._last_connected,
                state=self._state,
                retry_at=self._retry_at,
                host_id=self._link.host_id if self._link is not None else None,
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


def _sealed_result(
    peer: _Peer, cid: str, status: int, body: dict[str, object]
) -> dict[str, object]:
    """One command's result (C4): ``{status, body}`` sealed for ``peer``
    with its h2d key, AAD ``res:<commandId>``."""
    answer = json.dumps({"status": status, "body": body}, separators=(",", ":"))
    return {
        "type": "command",
        "commandId": cid,
        "status": status,
        "sealed": linkcrypto.seal(peer.keys.h2d, answer.encode(), AAD_RES + cid),
    }


def _too_large(peer: _Peer, cid: str) -> dict[str, object]:
    """The small result a device gets in place of an answer too large for
    the relay to take."""
    return _sealed_result(
        peer,
        cid,
        RESULT_TOO_LARGE_STATUS,
        _error(
            "payload_too_large",
            "the answer is too large to send over the link",
            "result_too_large",
        ),
    )


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


# --- Event forwarding (spec 4.1) ----------------------------------------------

EVENT_THREAD = "magent-relay-events"
UPLOAD_THREAD = "magent-relay-uploads"
EVENT_BATCH_S = 0.25  # events arriving this close together share one POST
EVENT_IDLE_WAIT_S = 1.0  # how long one quiet read blocks (bounds the stop latency)
LINK_CHECK_S = 1.0  # link.json and remoteControl checks, busy feed or not
EVENTS_MAX_BYTES = 256 * 1024  # the relay's cap on one POST /v1/host/events body
EVENTS_MAX = 500  # ...and on the events in it
EVENT_FIELD_MAX = 64  # daemonEventId and type, as the relay takes them
EVENT_RETRY_BASE_S = 1.0  # the backoff after a LinkHost.subscribe or feed failure
EVENT_RETRY_CAP_S = 60.0
EVENT_JOIN_S = 5.0  # how long stop() waits for a post in flight
# Plaintext bytes a too-big pane loses beyond what the size says it must:
# room for the base64 rounding of each device's copy.
PANE_CUT_SLACK = 64
_EVENTS_FRAME = len('{"events":[]}')
# Read by the forwarder, never sent: ``hello`` opens a feed, and ``reset``
# (the feed lost events) is answered with a fresh snapshot instead.
_LOCAL_ONLY_EVENTS = frozenset({"hello", "reset"})
# The types whose meta feeds the relay's session mirror (C4): they carry the
# same routing fields the snapshot already sends in the clear, camelCase. A
# field the event does not carry is left out (the mirror keeps its value); a
# state it carries but the mirror cannot take is sent as null.
_SESSION_ROUTING_EVENTS = frozenset({"session.added", "session.state"})
_SESSION_STATE_FIELDS = (
    ("hook_state", "hookState"),
    ("pane_state", "paneState"),
    ("node_state", "nodeState"),
)


def event_meta(event: dict[str, object]) -> dict[str, object]:
    """The routing-only fields of a daemon event the relay may read: the
    session it is about; for ``session.added`` and ``session.state`` the
    SessionMeta fields it carries for the relay's session mirror (name, live
    and the three states, bounded as in the snapshot); for ``attention``,
    whether it should page and why.
    Never pane text, paths, models or anything else from ``data``."""
    raw = event.get("data")
    data = raw if isinstance(raw, dict) else {}
    meta: dict[str, object] = {}
    session = data.get("session")
    if isinstance(session, str):
        meta["session"] = session
    kind = event.get("type")
    if kind in _SESSION_ROUTING_EVENTS:
        name = data.get("name")
        if isinstance(name, str):
            meta["name"] = utf16_cut(name, SESSION_NAME_MAX)
        if "live" in data:
            meta["live"] = data["live"] is True  # as the snapshot maps it
        for key, wire in _SESSION_STATE_FIELDS:
            if key in data:
                meta[wire] = _state_text(data[key])
    elif kind == "attention":
        if isinstance(data.get("fire"), bool):
            meta["fire"] = data["fire"]
        if isinstance(data.get("hook_state"), str):
            meta["hookState"] = _state_text(data["hook_state"])
    return meta


def pane_tail(text: str, need: int) -> str:
    """``text`` without its shortest prefix whose cost once escaped by
    json.dumps is at least ``need``, kept to whole lines: a tail that starts
    mid-line loses that partial line, unless its only newline is its last
    character (then the partial line stays, rather than nothing)."""
    lo, hi = 0, len(text)
    while lo < hi:  # bisection over character boundaries: no split surrogate pair
        mid = (lo + hi) // 2
        if len(json.dumps(text[:mid])) - 2 >= need:
            hi = mid
        else:
            lo = mid + 1
    tail = text[lo:]
    if lo and text[lo - 1] != "\n":  # the cut landed mid-line
        _, newline, rest = tail.partition("\n")
        if newline and rest:
            tail = rest
    return tail


def _event_field(value: object) -> bool:
    return isinstance(value, str) and 1 <= utf16_len(value) <= EVENT_FIELD_MAX


class EventForwarder:
    """Daemon events to ``POST /v1/host/events``, on its own thread.

    Events arriving within 250 ms share a batch; a batch is split so no POST
    holds over 500 events or 256 KiB, and one event too big on its own is
    dropped with a warning. Each event is sealed once per trusted device (h2d
    key, AAD ``evt:<hostId>:<eventId>``); the relay sees only ``event_meta``.
    A ``reset`` from the daemon (its ring moved on without us) re-PUTs the
    snapshot instead. A batch that fails to post, or arrives while the link
    is not online, is dropped, and a dropped batch is repaired by a snapshot
    re-PUT as soon as the link can post: with the next batch, or on the next
    quiet read (a failed repair backs off). A 429 is waited out inside the
    call, through the runner's sleep; one whose Retry-After is past the
    client's cap drops every batch until it has passed (at most
    RETRY_AFTER_MAX_S), and the repair waits for it too.

    ``subscribe`` (LinkHost.subscribe) is called on the thread: a failing
    subscribe or feed backs off and is tried again, like any LinkHost
    failure, and only the first of a run of failures logs a traceback.
    """

    def __init__(
        self,
        runner: LinkRunner,
        client: RelayClient,
        subscribe: Callable[[], EventFeed],
    ) -> None:
        self._runner = runner
        self._client = client
        self._subscribe = subscribe
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=EVENT_THREAD
        )
        self._log = get_logger("link")
        self._quiet = _LogBudget(self._log)
        self._backoff = Backoff(base=EVENT_RETRY_BASE_S, cap=EVENT_RETRY_CAP_S)
        self._hold_until = 0.0
        self._failing = False
        self._resync = False  # a batch was dropped: re-PUT the snapshot
        self._repair_after = 0.0
        self._repair_backoff = Backoff(base=EVENT_RETRY_BASE_S, cap=EVENT_RETRY_CAP_S)
        self._check_after = 0.0  # the next look at link.json

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Stop and join: within one quiet read (EVENT_IDLE_WAIT_S), or the
        post in flight, up to EVENT_JOIN_S."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(EVENT_IDLE_WAIT_S + EVENT_JOIN_S)
            if self._thread.is_alive():
                self._log.warning("link: the event forwarder is still finishing a post")

    def _run(self) -> None:
        while not self._stop.is_set():
            feed: EventFeed | None = None
            try:
                feed = self._subscribe()
                self._pump(feed)
            except Exception:  # noqa: BLE001  # reason: a LinkHost failure (subscribe, the feed) backs off and retries; it must never end the forwarder, nor the link
                self._failed("link: the daemon's event feed failed")
                if self._stop.wait(self._backoff.next_delay()):
                    break
            finally:
                if feed is not None:
                    with contextlib.suppress(Exception):
                        feed.close()

    def _pump(self, feed: EventFeed) -> None:
        """Read, batch and send until stopped. While the runner backs off
        the feed is not read at all: each read holds a bus subscription,
        which keeps serve sampling every pane only for the batch to be
        dropped. What the daemon published meanwhile is read once the runner
        connects again (or comes back as a ``reset``), and the snapshot is
        repaired after it. A remoteControl switch is re-PUT within
        LINK_CHECK_S, quiet or busy: the relay mirrors it for the app."""
        while not self._stop.is_set():
            self._check_link()
            if self._runner.backing_off():
                self._resync = True
                if self._stop.wait(EVENT_IDLE_WAIT_S):
                    break
                continue
            batch = feed.next_batch(EVENT_IDLE_WAIT_S)
            if not batch:
                if (
                    self._resync
                    and time.monotonic() >= self._repair_after
                    and self._runner.online()
                ):
                    self._send_logged([])  # the repair: just the snapshot
                continue
            deadline = time.monotonic() + EVENT_BATCH_S
            while not self._stop.is_set() and (left := deadline - time.monotonic()) > 0:
                batch.extend(feed.next_batch(left))
            if not self._stop.is_set():
                self._send_logged(batch)

    def _check_link(self) -> None:
        """The runner's look at link.json, at most every LINK_CHECK_S however
        busy the feed is: on a quiet stream nothing else would notice a
        `magent unlink` (a run it ends cuts its own stream), or a device
        `magent link --add-device` saved after the relay announced it. A
        remoteControl switch is checked on the same clock: a feed that never
        has a quiet read must not keep the relay's copy stale; the next post
        (a batch, or the repair on a quiet read) re-PUTs the snapshot."""
        now = time.monotonic()
        if now < self._check_after:
            return
        self._check_after = now + LINK_CHECK_S
        try:
            # Offline, the reconnect PUTs the snapshot for the new trust.
            online = self._runner.online()
            self._runner.watch_link(self._client if online else None)
        except (_Unlinked, _Relinked):
            pass
        except Exception:  # noqa: BLE001  # reason: a snapshot for a changed trust that fails (relay or LinkHost) is repaired like a dropped batch; the forwarder goes on
            self._failed("link: could not send the snapshot for a changed link.json")
            self._dropped(failed=True)
        if not self._resync and self._runner.online():
            self._resync = self._runner.remote_control_changed()

    def _send_logged(self, batch: list[dict[str, object]]) -> None:
        try:
            self.send(batch)
        except Exception:  # noqa: BLE001  # reason: a batch that cannot be sealed or re-snapshotted (a LinkHost failure) is dropped like a failed post; the forwarder goes on
            self._failed("link: dropped an event batch")
            self._dropped(failed=True)
        else:
            self._failing = False
            self._backoff.reset()

    def _failed(self, message: str) -> None:
        """The first failure of a run logs its traceback; the rest one line."""
        if self._failing:
            self._log.warning("%s (again)", message)
        else:
            self._failing = True
            self._log.exception("%s", message)

    def _dropped(self, *, failed: bool = False) -> None:
        """A batch was lost: the next post that can repairs it with a fresh
        snapshot. ``failed``: the relay or the host failed, so the repair
        on a quiet read backs off."""
        self._resync = True
        if failed:
            self._repair_after = time.monotonic() + self._repair_backoff.next_delay()

    def send(self, batch: list[dict[str, object]]) -> None:
        """Seal, split and post one batch; re-snapshot on a reset, or when an
        earlier batch was dropped (``[]`` is the repair alone)."""
        events = [e for e in batch if isinstance(e, dict)]
        if not self._runner.online():
            if events:
                self._dropped()
            return
        if time.monotonic() < self._hold_until:
            self._log.debug("link: rate limited; dropped %d events", len(events))
            self._dropped()
            return
        reset = self._resync or any(e.get("type") == "reset" for e in events)
        sealed = [
            self._sealed(e) for e in events if e.get("type") not in _LOCAL_ONLY_EVENTS
        ]
        try:
            for chunk in self._chunks([e for e in sealed if e is not None]):
                self._client.call("POST", "/v1/host/events", {"events": chunk})
            if reset:
                self._runner.put_snapshot(self._client)
                self._resync = False
                self._repair_backoff.reset()
        except RelayError as exc:
            if exc.status == 429:
                wait = min(exc.retry_after or 0.0, RETRY_AFTER_MAX_S)
                self._hold_until = time.monotonic() + wait
            self._log.info("link: dropped an event batch: %s", exc)
            self._dropped(failed=True)

    def _sealed(self, event: dict[str, object]) -> dict[str, object] | None:
        event_id, kind = event.get("id"), event.get("type")
        if not (_event_field(event_id) and _event_field(kind)):
            self._log.warning("link: dropped an event the relay would refuse")
            return None
        item = self._seal(event)
        if kind == "session.pane":
            item = self._pane_that_fits(event, item)
        return item

    def _seal(self, event: dict[str, object]) -> dict[str, object]:
        plain = json.dumps(event, separators=(",", ":")).encode("utf-8")
        aad = f"{AAD_EVT}{self._runner.host_id}:{event['id']}"
        return {
            "daemonEventId": event["id"],
            "type": event["type"],
            "meta": event_meta(event),
            "sealed": self._runner.seal_for_devices(plain, aad),
        }

    def _pane_that_fits(
        self, event: dict[str, object], item: dict[str, object]
    ) -> dict[str, object]:
        """A ``session.pane`` too big for one post once sealed for every
        device keeps the tail of its text, whole lines only, rather than
        being dropped. The cut is sized in what the text costs sealed, not
        in characters: a plaintext byte costs about 4/3 per device, and
        json.dumps escapes every non-ASCII character to six bytes (twelve
        for a surrogate pair: six per UTF-16 code unit), so a pane of box
        lines and glyphs costs several times its length. The shortest
        prefix whose escaped cost covers the overage is dropped, found by
        bisection over character boundaries, so a surrogate pair is never
        split. A tail that starts mid-line loses that partial line; one whose
        only newline is its last character keeps it rather than going out
        empty. One that still does not fit after rounding is dropped by
        ``_chunks``, as any oversize event. The cut is logged at most once
        per LOG_QUIET_S: a busy pane is cut on every capture."""
        data, sealed = event.get("data"), item.get("sealed")
        text = data.get("text") if isinstance(data, dict) else None
        size = _EVENTS_FRAME + len(json.dumps(item, separators=(",", ":"))) + 1
        if (
            size <= EVENTS_MAX_BYTES
            or not isinstance(data, dict)
            or not isinstance(text, str)
            or not isinstance(sealed, dict)
            or not sealed
        ):
            return item
        need = -(-(size - EVENTS_MAX_BYTES) * 3 // (4 * len(sealed))) + PANE_CUT_SLACK
        tail = pane_tail(text, need)
        self._quiet.log(
            logging.INFO,
            "pane-cut",
            "link: a %d-device pane was cut to its last %d UTF-16 units to fit one post",
            len(sealed),
            utf16_len(tail),
        )
        return self._seal({**event, "data": {**data, "text": tail}})

    def _chunks(self, events: list[dict[str, object]]) -> list[list[dict[str, object]]]:
        chunks: list[list[dict[str, object]]] = []
        current: list[dict[str, object]] = []
        size = _EVENTS_FRAME
        for item in events:
            item_size = len(json.dumps(item, separators=(",", ":")).encode("utf-8")) + 1
            if _EVENTS_FRAME + item_size > EVENTS_MAX_BYTES:
                self._log.warning(
                    "link: dropped event %s (%s): %d bytes is over the %d-byte batch cap",
                    item["daemonEventId"],
                    item["type"],
                    item_size,
                    EVENTS_MAX_BYTES,
                )
                continue
            if current and (
                size + item_size > EVENTS_MAX_BYTES or len(current) >= EVENTS_MAX
            ):
                chunks.append(current)
                current, size = [], _EVENTS_FRAME
            current.append(item)
            size += item_size
        if current:
            chunks.append(current)
        return chunks


# --- Upload pulls (spec 4.5) --------------------------------------------------

UPLOAD_SLACK_BYTES = 65536  # ciphertext framing allowed over the daemon's upload cap
UPLOAD_QUEUE_WAIT_S = 0.5  # how long the idle puller blocks (bounds the stop latency)
UPLOAD_JOIN_S = 5.0  # how long stop() waits for a pull in flight
# The wait between resends of upload states the relay did not take, while
# the runner stays connected (a reconnect resends at once).
UPLOAD_RESEND_BASE_S = 1.0
UPLOAD_RESEND_CAP_S = 60.0
# UTF-8 bytes kept of the device's file name, as the daemon's own saved names
# (uploads.saved_name): the stem is cut, an extension of at most
# _SUFFIX_MAX_BYTES kept, so the agent still knows what the file is and the
# incoming path stays under Windows' 260-character MAX_PATH.
UPLOAD_NAME_MAX = 150
_SUFFIX_MAX_BYTES = 20
# The relay issues upload ids as it does command ids (C3): UUIDs, in lower
# case. One names a folder here, so nothing else gets near the file system,
# and no two ids that differ in case share a folder on Windows.
_UPLOAD_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.ASCII
)
# The relay's order of upload states: a result only ever moves to a later one.
_UPLOAD_RANK = {"pulled": 1, "inject_pending": 2, "injected": 3, "inject_failed": 3}
_PASTE_STATES = {"injected": "injected", "pending": "inject_pending"}
_NAME_FORBIDDEN = frozenset('<>:"|?*')
# Names Windows opens as a device in any folder, an extension or not.
_DEVICE_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"COM{n}" for n in (*range(1, 10), "\u00b9", "\u00b2", "\u00b3")),
        *(f"LPT{n}" for n in (*range(1, 10), "\u00b9", "\u00b2", "\u00b3")),
    }
)


def _pull_token() -> str:
    """Names one pull's incoming folder, beside the upload id."""
    return secrets.token_hex(4)


def safe_name(name: object) -> str:
    """The base name of the device's file name, safe to create here: no
    folder part, no character a file system refuses, no leading space, no
    trailing dot or space, a leading dot kept behind "_" (a dotfile, as the
    daemon's own saved names keep it), at most UPLOAD_NAME_MAX UTF-8 bytes
    (the stem cut, a short extension kept), never a device name."""
    text = name if isinstance(name, str) else ""
    base = text.replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(c for c in base if c.isprintable() and c not in _NAME_FORBIDDEN)
    base = base.rstrip(" .").lstrip(" ")
    if not base:
        return "upload.bin"
    if base.startswith("."):
        base = f"_{base}"
    dot = base.rfind(".")
    suffix = base[dot:] if dot > 0 else ""
    if len(suffix.encode("utf-8")) > _SUFFIX_MAX_BYTES:
        suffix = ""
    stem = base[: len(base) - len(suffix)]
    budget = UPLOAD_NAME_MAX - len(suffix.encode("utf-8"))
    if len(stem.encode("utf-8")) > budget:
        # A cut can land on a space or a dot: neither may end a name.
        cut = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
        stem = cut.rstrip(" .")
    base = stem + suffix
    if base.split(".", 1)[0].rstrip(" ").upper() in _DEVICE_NAMES:
        return f"_{base}"
    return base


def upload_state(envelope: dict[str, object], inject: bool) -> str:
    """The relay's word for what ``stage_upload`` made of a pulled file:
    ``pulled`` when no paste was asked for and the file was saved."""
    data = envelope.get("data") if envelope.get("ok") is True else None
    paste = data.get("paste") if isinstance(data, dict) else None
    if not isinstance(paste, str):
        return "inject_failed"
    if paste in _PASTE_STATES:
        return _PASTE_STATES[paste]
    return "inject_failed" if inject else "pulled"


class _UploadRefused(Exception):
    """An upload this host will not take: reported ``inject_failed``, and
    never remembered (the ring and the result cache hold verified ids only)."""


@dataclass(frozen=True)
class _Verified:
    """An ``upload.ready`` whose key and name opened under a trusted
    device's d2h key."""

    upload_id: str
    device_id: str
    session: str
    inject: bool
    url: str
    cap: int
    content_key: bytes
    name: str


@dataclass(frozen=True)
class _UploadJob:
    """One piece of the puller's work: ``pull`` an upload (``ready`` set),
    ``report`` one already taken, or ``resend`` the undelivered states."""

    kind: str
    upload_id: str = ""
    ready: dict[str, object] | None = None


class UploadPuller:
    """``upload.ready`` -> verify, download, decrypt, stage, report; one at a
    time, on its own thread, so a 100 MB pull never stalls the commands.

    An offer is skipped while the same upload is queued or being pulled, and
    an upload whose id is in the seen ring is only re-reported (its last
    state; ``pulled`` when a restart lost it), never fetched or pasted
    again. Otherwise the device must be trusted, ``sizeBytes`` at most the
    daemon's upload cap plus 64 KiB of framing (or nothing is fetched), and
    the content key (AAD ``ukey:<id>``) and meta (AAD ``umeta:<id>``) must
    open under that device's d2h key. The object is streamed from its
    presigned URL into ``~/.magent/uploads/.incoming/<uploadId>.<token>/``
    (one folder per pull) under the same cap and decrypted chunk by chunk (a
    tampered or cut-short object fails whole). Only then, unless the link
    has stopped meanwhile, does the id enter the seen ring, in one step
    with the seen check; it is reported ``pulled``, handed to
    ``LinkHost.stage_upload``, and its outcome reported: ``injected``,
    ``inject_pending``, ``inject_failed`` (a paste was asked for and did not
    happen) or nothing more (none was asked for). Reported states only move
    forward. A state the relay could not take for now (no reply, a 5xx or a
    429) is kept, the latest per upload, and sent again: at once when the
    runner connects again, and meanwhile while the link stays online, after
    a backoff (1 s doubling to 60 s). A state it refused for good (another
    4xx) is not. A replay's re-report runs on this thread too, never the
    runner's, so a 429 waited out there cannot hold up the commands.

    A refusal is reported ``inject_failed`` and never remembered, so junk
    under a real id can neither flush the ring nor block the real upload. A
    download that fails on the way is reported nothing: the relay sends
    ``upload.ready`` again, with a fresh URL, on the next connect; so is one
    that a stop cut short. A URL the download refuses, or a file that cannot
    be written here, is a refusal. The incoming folder is always removed.
    The presigned URL is never logged.
    """

    def __init__(self, runner: LinkRunner, client: RelayClient, host: LinkHost) -> None:
        self._runner = runner
        self._client = client
        self._host = host
        self._lock = threading.Lock()
        self._inflight: set[str] = set()  # queued or being pulled or re-reported
        self._states: collections.OrderedDict[str, str] = collections.OrderedDict()
        # Verified uploads whose latest state the relay has not taken yet.
        self._undelivered: collections.OrderedDict[str, str] = collections.OrderedDict()
        self._resend_queued = False
        self._resend_at = 0.0
        self._resend_backoff = Backoff(
            base=UPLOAD_RESEND_BASE_S, cap=UPLOAD_RESEND_CAP_S
        )
        self._queue: queue.Queue[_UploadJob | None] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=UPLOAD_THREAD
        )
        self._log = get_logger("link")
        self._quiet = _LogBudget(self._log)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Stop and join: at once when idle, else once the pull in flight
        ends, waiting up to UPLOAD_JOIN_S."""
        self._stop.set()
        self._queue.put(None)  # wakes an idle puller at once
        if self._thread.is_alive():
            self._thread.join(UPLOAD_JOIN_S)
            if self._thread.is_alive():
                self._log.warning("link: the upload puller is still finishing a pull")

    def offer(self, ready: dict[str, object]) -> None:
        """Take one ``upload.ready``; returns at once. An upload already
        taken is queued for a re-report, any other for a pull."""
        upload_id = ready.get("uploadId")
        if not isinstance(upload_id, str) or not _UPLOAD_ID.fullmatch(upload_id):
            self._quiet.log(
                logging.WARNING,
                "invalid-id",
                "link: ignoring an upload.ready without a valid id",
            )
            return
        with self._lock:
            if upload_id in self._inflight:
                return
            self._inflight.add(upload_id)
            seen = self._runner.has_seen(upload_id)
            self._queue.put(
                _UploadJob("report", upload_id)
                if seen
                else _UploadJob("pull", upload_id, ready)
            )

    def connected(self) -> None:
        """The runner (re)connected: send the states the relay did not take
        now, not after the backoff."""
        with self._lock:
            if not self._undelivered or self._resend_queued:
                return
            self._resend_queued = True
            self._queue.put(_UploadJob("resend"))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=UPLOAD_QUEUE_WAIT_S)
            except queue.Empty:
                job = _UploadJob("resend") if self._resend_due() else None
            if job is not None and not self._stop.is_set():
                self._work(job)

    def _work(self, job: _UploadJob) -> None:
        try:
            if job.kind == "resend":
                self._resend()
            elif job.ready is not None:
                self._pull(job.upload_id, job.ready)
            else:
                self._report(job.upload_id)
        except Exception:  # noqa: BLE001  # reason: a LinkHost or disk failure ends this pull unreported (the relay resends it on the next connect), never the puller
            self._log.exception("link: upload %s crashed", job.upload_id or job.kind)
        finally:
            if job.upload_id:
                with self._lock:
                    self._inflight.discard(job.upload_id)

    def _report(self, upload_id: str) -> None:
        """A replayed ``upload.ready`` of an upload already taken: its last
        state again (``pulled`` when a restart lost it), never a second
        fetch or paste."""
        with self._lock:
            state = self._states.get(upload_id, "pulled")
        self._deliver(upload_id, state)

    def _resend_due(self) -> bool:
        with self._lock:
            waiting = bool(self._undelivered) and not self._resend_queued
        return waiting and time.monotonic() >= self._resend_at and self._runner.online()

    def _resend(self) -> None:
        """One round over the states the relay did not take, oldest first.
        It stops at the first one the relay still cannot take (the next
        round waits out the backoff that set)."""
        with self._lock:
            self._resend_queued = False
            pending = list(self._undelivered.items())
        for upload_id, state in pending:
            if self._stop.is_set():
                return
            if not self._deliver(upload_id, state):
                with self._lock:
                    if upload_id in self._undelivered:
                        return

    def _pull(self, upload_id: str, ready: dict[str, object]) -> None:
        """One upload, start to finish, in a folder of this pull's own: a
        puller from an earlier run of the link that is still finishing the
        same upload can never truncate or remove it. The folder is gone
        before the final state is reported."""
        folder = linkfiles.INCOMING_DIR / f"{upload_id}.{_pull_token()}"
        try:
            outcome = self._take(upload_id, ready, folder)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        if outcome is None:
            return
        state, verified = outcome
        if verified:
            self._advance(upload_id, state)
        else:
            self._post(upload_id, state)

    def _take(
        self, upload_id: str, ready: dict[str, object], folder: Path
    ) -> tuple[str, bool] | None:
        """The state to report, and whether it is a verified upload's (kept,
        and moving forward only) or a refusal's (posted, never kept); None
        when a failed download should wait for the relay's resend."""
        try:
            verified = self._verify(upload_id, ready)
            target = self._fetch(verified, folder)
        except _UploadRefused as refused:
            self._quiet.log(
                logging.WARNING,
                "refused",
                "link: refused upload %s: %s",
                upload_id,
                refused,
            )
            return "inject_failed", False
        if target is None:
            return None
        return self._stage(verified, target)

    def _verify(self, upload_id: str, ready: dict[str, object]) -> _Verified:
        device_id = str(ready.get("deviceId"))
        peer = self._runner.peer(device_id)
        if peer is None:
            raise _UploadRefused("it came from a device this host does not trust")
        size, url, session = (
            ready.get("sizeBytes"),
            ready.get("getUrl"),
            ready.get("session"),
        )
        wrapped, sealed_meta = ready.get("wrappedKey"), ready.get("sealedMeta")
        if not (
            isinstance(size, int)
            and not isinstance(size, bool)
            and size >= 0
            and isinstance(url, str)
            and isinstance(wrapped, str)
            and isinstance(sealed_meta, str)
            and isinstance(session, str)
            and session
        ):
            raise _UploadRefused("the upload.ready is malformed")
        cap = self._host.upload_max_bytes() + UPLOAD_SLACK_BYTES
        if size > cap:
            raise _UploadRefused(f"{size} bytes is over this host's {cap}-byte cap")
        try:
            content_key = linkcrypto.open_sealed(
                peer.keys.d2h, wrapped, AAD_UKEY + upload_id
            )
            meta = _object(
                linkcrypto.open_sealed(
                    peer.keys.d2h, sealed_meta, AAD_UMETA + upload_id
                ).decode("utf-8")
            )
        except (SealError, UnicodeDecodeError) as exc:
            raise _UploadRefused("its key or name did not decrypt") from exc
        if len(content_key) != 32 or meta is None:
            raise _UploadRefused("its key or name is malformed")
        return _Verified(
            upload_id=upload_id,
            device_id=device_id,
            session=session,
            inject=ready.get("inject") is True,
            url=url,
            cap=cap,
            content_key=content_key,
            name=safe_name(meta.get("name")),
        )

    def _fetch(self, ready: _Verified, folder: Path) -> Path | None:
        """The decrypted file, or None when the download failed on the way
        (no result: the relay resends it)."""
        # safe_name never starts with a dot, so the two never collide.
        blob, target = folder / ".blob", folder / ready.name
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with blob.open("wb") as fh:
                self._client.get_bytes(ready.url, self._sink(fh), max_bytes=ready.cap)
        except RelayError as exc:
            if exc.status == 413:
                raise _UploadRefused(
                    f"the object is over this host's {ready.cap}-byte cap"
                ) from exc
            if exc.code == "invalid_response":
                # Refused by policy, not lost on the way: a retry would only
                # be refused again.
                raise _UploadRefused(f"its download: {exc.message}") from exc
            self._log.info(
                "link: upload %s download failed (%s); waiting for a resend",
                ready.upload_id,
                exc,
            )
            return None
        except OSError as exc:
            raise _UploadRefused("it could not be written here") from exc
        try:
            with blob.open("rb") as src, target.open("wb") as dst:
                pieces = iter(lambda: src.read(_READ_CHUNK), b"")
                for chunk in linkcrypto.iter_decrypt_upload(
                    ready.content_key, ready.upload_id, pieces
                ):
                    dst.write(chunk)
        except SealError as exc:
            raise _UploadRefused(
                "the object did not decrypt (tampered or cut short)"
            ) from exc
        except OSError as exc:
            # A full disk, or a path past MAX_PATH: never remembered, so a
            # resend that can be written still goes through.
            raise _UploadRefused("it could not be written here") from exc
        with contextlib.suppress(OSError):
            # Space back now; a .blob held a moment (a scanner on Windows) is
            # no reason to refuse a file that decrypted: the folder goes anyway.
            blob.unlink()
        return target

    def _sink(self, fh: BinaryIO) -> Callable[[bytes], None]:
        def sink(data: bytes) -> None:
            if self._stop.is_set():
                # Abandoned, never remembered: resent on the next run.
                raise RelayError(0, "network", "stopping")
            fh.write(data)

        return sink

    def _stage(self, ready: _Verified, target: Path) -> tuple[str, bool] | None:
        """Remember, report ``pulled``, stage: the final state (see _take).
        None, and nothing remembered, when the link stopped meanwhile: the
        relay sends it again on the next run."""
        upload_id = ready.upload_id
        if self._stop.is_set():
            return None
        fresh = self._runner.remember(upload_id)
        if fresh is None:
            # Another delivery got here first: reported, never pasted again.
            with self._lock:
                return self._states.get(upload_id, "pulled"), False
        if not fresh:
            # Unsaved, a restart would not know it was pasted: so it is not.
            self._log.warning(
                "link: upload %s not staged: the link state could not be saved",
                upload_id,
            )
            return "inject_failed", True
        self._advance(upload_id, "pulled")
        try:
            envelope = self._host.stage_upload(
                ready.device_id, ready.session, target, ready.inject
            )
        except Exception:  # noqa: BLE001  # reason: a LinkHost failure fails this upload (reported inject_failed), never the puller
            self._log.exception("link: staging upload %s failed", upload_id)
            state = "inject_failed"
        else:
            state = upload_state(envelope, ready.inject)
        return state, True

    def _advance(self, upload_id: str, state: str) -> None:
        if self._record(upload_id, state):
            self._deliver(upload_id, state)

    def _record(self, upload_id: str, state: str) -> bool:
        """Keep ``state`` as the upload's latest; False, and no change, when
        it would not move it forward."""
        with self._lock:
            known = self._states.get(upload_id)
            if known is not None and _UPLOAD_RANK[state] <= _UPLOAD_RANK[known]:
                return False
            self._states[upload_id] = state
            self._states.move_to_end(upload_id)
            while len(self._states) > RESULT_CACHE_MAX:
                self._states.popitem(last=False)
            return True

    def _deliver(self, upload_id: str, state: str) -> bool:
        """Post a taken upload's state; False when the relay did not take it.
        One it could not take for now is kept to send again (see the class),
        unless a later state of that upload is kept already; a state that
        reached the relay, or that it refused for good, clears what was kept
        up to its rank. Each miss pushes the next resend back (the backoff);
        once nothing is kept, the backoff starts over."""
        error = self._post(upload_id, state)
        rank = _UPLOAD_RANK[state]
        with self._lock:
            kept = self._undelivered.get(upload_id)
            if error is not None and _transient(error):
                if kept is None or _UPLOAD_RANK[kept] <= rank:
                    self._undelivered[upload_id] = state
                    self._undelivered.move_to_end(upload_id)
                    while len(self._undelivered) > RESULT_CACHE_MAX:
                        self._undelivered.popitem(last=False)
                self._resend_at = time.monotonic() + self._resend_backoff.next_delay()
                return False
            if kept is not None and _UPLOAD_RANK[kept] <= rank:
                del self._undelivered[upload_id]
            if not self._undelivered:
                self._resend_backoff.reset()
        return error is None

    def _post(self, upload_id: str, state: str) -> RelayError | None:
        """POST one upload result; the error when the relay did not take it."""
        result = {"type": "upload", "uploadId": upload_id, "state": state}
        try:
            self._client.call("POST", "/v1/host/results", {"results": [result]})
        except RelayError as exc:
            self._log.info(
                "link: upload %s result %s not delivered: %s", upload_id, state, exc
            )
            return exc
        return None


# --- The serve thread's supervisor and the config switch ------------------------

LINK_THREAD = "magent-relay-link"  # serve's thread running supervise
LINK_LOCK = "link"  # ~/.magent/link.lock: held by the one serve running the link
SUPERVISE_BACKOFF_BASE_S = 5.0
SUPERVISE_BACKOFF_CAP_S = 300.0


def _link_stamp(path: Path | None) -> tuple[int, int, int] | None:
    """link.json's mtime, size and file index, None when it is absent: a
    re-pair (an atomic replace, so a new file) or an unlink changes it."""
    try:
        info = (path or linkfiles.LINK_PATH).stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size, info.st_ino


def supervise(
    host_factory: Callable[[], LinkHost],
    stop: threading.Event,
    *,
    runner_factory: Callable[[LinkHost], LinkRunner] = LinkRunner,
    sleep: Callable[[float], bool] | None = None,
    link_path: Path | None = None,
    clock: Callable[[], float] = time.monotonic,
    serve: dict[str, object] | None = None,
) -> None:
    """``magent serve``'s link thread: run LinkRunner until ``stop``.

    MAGENT_LINK=0 logs once and returns (a broken MAGENT_* environment fails
    open: the runner still dials only the relay link.json names).

    One serve on a machine runs the link: serve is per port, and two runners
    on one host token would each run a command (typed twice into a session)
    and paste an upload twice. The loop runs under the machine-wide
    ``LINK_LOCK``; a serve that finds it held says so once and stands by,
    trying again every IDLE_RECHECK_S, until the holder exits. A lock that
    cannot be taken at all stands by the same way: never a second runner.
    While it holds the lock the serve names itself in link-serve.json (its
    pid plus ``serve``: port and config file), which is how `magent link`,
    `status` and `doctor` tell which serve runs the link; and before its
    first runner starts it clears the per-pull folders a serve that died
    mid-pull left behind. Pullers run under the lock, but a stop waits for
    one only UPLOAD_JOIN_S: a pull still running in a serve that is shutting
    down can lose its folder to that sweep, and the relay sends that upload
    again.

    A crash, of the host or the runner, is logged and the runner restarted
    with backoff (5 s doubling to 5 min; a run that ended on its own, or
    lasted BACKOFF_RESET_AFTER_S, starts it over). ``relinked`` and
    ``unlinked`` start a new runner at once (unlinked, it idles until
    `magent link`). After ``revoked`` or ``too_old`` the thread waits for
    link.json to change (`magent unlink`, `magent link`) instead of
    hammering a relay that already said no; a link.json that changed while
    that run was going (paired again, then the old pairing revoked) restarts
    it at once.

    No join is owed on the way out: the runner sees ``stop`` within one
    stream read (STREAM_READ_TIMEOUT_S) and its forwarder within about 6 s;
    after a revoke a forwarder may linger one Retry-After plus one request
    (about 90 s) before its daemon thread ends.
    """
    log = get_logger("link")
    wait = sleep or stop.wait
    if not link_enabled():
        log.info("link: off (MAGENT_LINK=0); this serve will not dial the relay")
        return
    told = False
    while not stop.is_set():
        with contextlib.ExitStack() as held:
            try:
                held.enter_context(lockfile.persistent_lock(LINK_LOCK, wait_s=0.0))
            except lockfile.LockHeld:
                if not told:
                    told = True
                    log.info(
                        "link: another magent serve on this machine runs the link;"
                        " this one stands by"
                    )
            except OSError as exc:
                if not told:
                    told = True
                    log.warning(
                        "link: could not take the link lock (%s); this serve stands by",
                        exc,
                    )
            else:
                if told:
                    log.info("link: the link is free; this serve runs it now")
                _take_over(serve, log)
                try:
                    _supervise_held(
                        host_factory, stop, runner_factory, wait, link_path, clock, log
                    )
                finally:
                    # Best effort: a record left behind means nothing once
                    # the lock is free, which is what every reader checks.
                    with contextlib.suppress(OSError):
                        linkfiles.clear_serve(os.getpid())
                return
        if wait(IDLE_RECHECK_S):
            return


def _supervise_held(
    host_factory: Callable[[], LinkHost],
    stop: threading.Event,
    runner_factory: Callable[[LinkHost], LinkRunner],
    wait: Callable[[float], bool],
    link_path: Path | None,
    clock: Callable[[], float],
    log: logging.Logger,
) -> None:
    """supervise's loop, run while this serve holds the link lock."""
    backoff = Backoff(base=SUPERVISE_BACKOFF_BASE_S, cap=SUPERVISE_BACKOFF_CAP_S)
    while not stop.is_set():
        began = _link_stamp(link_path)
        started = clock()
        try:
            outcome = runner_factory(host_factory()).run(stop)
        except Exception:
            # A crashed runner, or a host that cannot be built yet: logged and
            # restarted with backoff, never the end of the thread for serve's life.
            log.exception("link: the link thread crashed; restarting it")
            if clock() - started >= BACKOFF_RESET_AFTER_S:
                backoff.reset()  # a long healthy run: two crashes days apart start over
            if wait(backoff.next_delay()):
                return
            continue
        if outcome == "stopped":
            return
        backoff.reset()
        if outcome in ("relinked", "unlinked"):
            continue
        log.info("link: %s; waiting for `magent link` to pair this machine", outcome)
        while _link_stamp(link_path) == began:
            if wait(IDLE_RECHECK_S):
                return


def set_remote_control(config_path: Path, value: bool) -> None:
    """Write ``settings.link.remoteControl`` through ``config_io`` (lock,
    validate, backup, atomic replace; MD011). Raises ``lockfile.LockHeld``
    when another writer holds the config, ``config_io.ConfigWriteError``
    when the result would not load."""
    from magent import config_io  # the config writer: only `magent link` needs it

    with config_io.locked():
        data = config_io.load_raw(config_path)
        settings = data.get("settings")
        if not isinstance(settings, dict):
            settings = {}
            data["settings"] = settings
        block = settings.get("link")
        if not isinstance(block, dict):
            block = {}
            settings["link"] = block
        block["remoteControl"] = value
        config_io.save(config_path, data)


# --- Who runs the link, and what status, doctor and /api/v1/meta say (C8) ------

# One pull's folder in linkfiles.INCOMING_DIR: <uploadId>.<_pull_token()>.
_PULL_FOLDER = re.compile(_UPLOAD_ID.pattern + r"\.[0-9a-f]{8}", re.ASCII)


def _take_over(serve: dict[str, object] | None, log: logging.Logger) -> None:
    """What a serve does on taking the link lock, before its first runner:
    name itself in link-serve.json, then clear left-over pulls."""
    try:
        linkfiles.save_serve({"pid": os.getpid(), **(serve or {})})
    except OSError as exc:
        log.warning("link: could not write %s (%s)", linkfiles.LINK_SERVE_PATH, exc)
        # A record a dead serve left would name its port and config as ours.
        with contextlib.suppress(OSError):
            linkfiles.clear_serve()
    _sweep_incoming(log)


def _sweep_incoming(log: logging.Logger) -> int:
    """Remove the per-pull folders (``<uploadId>.<token>``) in
    linkfiles.INCOMING_DIR; how many went. A puller removes its own folder
    when the pull ends, so one is left only by a process that died mid-pull,
    and it holds a decrypted file. Called only while this process holds
    LINK_LOCK and runs no runner, so no puller of this process owns one. A
    serve that is shutting down can still be finishing a pull (its stop
    waits for the puller only UPLOAD_JOIN_S); that pull fails and the relay
    sends the upload again. Anything else in the folder, and anything that
    is not a plain folder, is left alone."""
    try:
        entries = list(linkfiles.INCOMING_DIR.iterdir())
    except OSError:
        return 0
    removed = 0
    for entry in entries:
        if not _PULL_FOLDER.fullmatch(entry.name):
            continue
        if entry.is_symlink() or not entry.is_dir():
            continue
        shutil.rmtree(entry, ignore_errors=True)
        if not entry.exists():
            removed += 1
    if removed:
        log.info("link: removed %d unfinished upload folder(s) left by a stop", removed)
    return removed


def sweep_incoming_if_free() -> int | None:
    """`magent unlink`'s sweep of left-over pulls: only while no serve runs
    the link (the lock is taken for the sweep). None when a serve holds it,
    or the lock cannot be taken: its puller may own a folder. A serve that
    is shutting down has let go of the lock and may still finish a pull;
    see _sweep_incoming."""
    try:
        with lockfile.persistent_lock(LINK_LOCK, wait_s=0.0):
            return _sweep_incoming(get_logger("link"))
    except OSError:  # LockHeld included
        return None


@dataclass(frozen=True)
class LinkServe:
    """Who runs the link on this machine. ``held``: True when a serve holds
    LINK_LOCK (``pid``, ``port`` and ``config`` from its link-serve.json when
    it is readable), False when none does, None when the lock could not be
    probed."""

    held: bool | None
    pid: int | None = None
    port: int | None = None
    config: str | None = None


def _int_field(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def link_serve() -> LinkServe:
    """Probe LINK_LOCK. Taking it for a moment is the probe: free means no
    serve runs the link (its thread holds the lock for as long as it runs,
    and a process that dies lets go of it). A machine that never ran the link
    has no lock file, and the probe creates none. The probe can make a serve
    that is starting at that very moment stand by for one IDLE_RECHECK_S."""
    if not lockfile.lock_path(LINK_LOCK).exists():
        return LinkServe(held=False)
    try:
        with lockfile.persistent_lock(LINK_LOCK, wait_s=0.0):
            return LinkServe(held=False)
    except lockfile.LockHeld:
        pass
    except OSError:
        return LinkServe(held=None)
    record = linkfiles.load_serve() or {}
    config = record.get("config")
    return LinkServe(
        held=True,
        pid=_int_field(record.get("pid")),
        port=_int_field(record.get("port")),
        config=config if isinstance(config, str) and config else None,
    )


NOT_SERVING = "magent serve is not running"
NEVER_CONNECTED_HINT = (
    "it has not connected since it was paired; if that lasts, see "
    "~/.magent/logs/link.log (a relay URL or host token that cannot work "
    "looks like this)"
)


@dataclass(frozen=True)
class LinkStatus:
    """``state`` is one of the C8 words: unlinked, connecting, online,
    offline, revoked, disabled. ``detail`` is the human line, ``hint`` a
    second line when there is something to do (or to look at)."""

    state: str
    retry_in_s: int | None
    detail: str
    hint: str | None = None

    def as_json(self) -> dict[str, object]:
        return {"state": self.state, "retry_in_s": self.retry_in_s}


def describe_link(
    *,
    serving: Callable[[], bool | str],
    now: float | None = None,
    link_path: Path | None = None,
    state_path: Path | None = None,
) -> LinkStatus:
    """The link's state from link.json + link-state.json. ``serving`` says
    whether a ``magent serve`` runs the link: True, False (none is running),
    or the reason none runs it although one is up. It is only asked when the
    answer depends on it. A state file that names another pairing reads as
    fresh, as the runner reads it."""
    if not link_enabled():
        return LinkStatus("disabled", None, "disabled (MAGENT_LINK=0)")
    try:
        linked = linkfiles.load_link(link_path)
        if linked is not None:
            _check_host_token(linked)
    except linkfiles.LinkFileError:
        return LinkStatus(
            "unlinked",
            None,
            "unlinked (link.json is unreadable: run `magent unlink`, then `magent link`)",
        )
    if linked is None:
        return LinkStatus("unlinked", None, "unlinked")
    state = linkfiles.load_state(state_path)
    if state.host_id is not None and state.host_id != linked.host_id:
        state = LinkState()
    if state.state == "revoked":
        return LinkStatus(
            "revoked", None, "revoked (run `magent unlink`, then `magent link`)"
        )
    if state.state == "too_old":
        return LinkStatus(
            "offline", None, "offline (the relay needs a newer magent)", UPGRADE_HINT
        )
    served = serving()
    if served is not True:
        reason = served if isinstance(served, str) and served else NOT_SERVING
        return LinkStatus("offline", None, f"offline ({reason})")
    if state.state == "online":
        return LinkStatus("online", None, "online")
    if state.state == "offline":
        hint = NEVER_CONNECTED_HINT if state.last_connected is None else None
        if state.retry_at is None:
            return LinkStatus("offline", None, "offline", hint)
        clock = time.time() if now is None else now
        retry = max(0, math.ceil(state.retry_at - clock))
        return LinkStatus("offline", retry, f"offline (retry in {retry}s)", hint)
    return LinkStatus("connecting", None, "connecting")
