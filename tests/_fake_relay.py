"""An in-process fake of the magent relay's HOST side (contracts C3 and C5).

A threaded http.server on loopback port 0 that the real RelayClient and
LinkRunner talk to. It mirrors the relay's wire contract (``/v1/host/*``
and ``/v1/stream/host``) closely enough that a daemon bug cannot hide behind
it:

- every reply is the C1 envelope; 426 for a missing or wrong X-Magent-Link,
  401 for an unknown or revoked host token, 405 (with Allow) for a wrong
  method, 400 ``invalid_request`` for a body or query the real route refuses;
- the stream replays as the relay does: on every (re)connect the trusted
  devices (always, even an empty list), then each queued command with
  ``seq > since`` that has no result yet, then each upload with no upload
  result yet; frames are ``event:``, ``id:``, ``data:`` in that order;
- a host holds at most 2 streams; a third is 429 with Retry-After 5 (the
  relay's per-host cap);
- an ack (alone or piggybacked on results) past the highest queued command
  seq is 400 ``ack_ahead`` and refuses the whole results batch;
- pair/poll hands out the host token exactly once, and a claimed pairing
  stays claimed; pair/start with a host token checks it (401) and the keys
  (409 ``key_mismatch``).

Tests script it: queue commands and uploads, change the devices, revoke the
host, cut every open stream, fail the next stream opens, rate-limit the next
calls to a rate-limited route, or play a relay too new for this daemon.
Long-polls and heartbeats are off by default so tests stay fast.

Every token here carries DECOY (gitleaks allowlists it under tests/).
"""

from __future__ import annotations

import json
import re
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable

HOST_TOKEN = "mlh_DECOY_fake_relay_host_token_00000000000000"
POLL_TOKEN = "mlp_DECOY_fake_relay_poll_token_00000000000000"
PAIRING_ID = "pair_fake_1"
# A pairing code: 8 characters of a 32-letter alphabet with no 0, O, 1 or I,
# sent as XXXX-XXXX. People type it back with or without the dash, in any case.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIRING_CODE = "ABCD-EFGH"
HOST_ID = "hst_fake_1"
PAIRED_AT = "2026-10-08T22:00:00.000Z"
EXPIRES_AT = "2026-10-08T22:10:00.000Z"
EVENTS_MAX_BYTES = 256 * 1024
EVENTS_MAX = 500
RESULTS_MAX = 100
SESSIONS_MAX = 1000
COMMANDS_BATCH = 100
HOST_STREAM_LIMIT = 2
STREAM_LIMIT_RETRY_S = 5
LONG_POLL_CAP_S = 25
_WAIT_S = 0.05
# How long teardown waits for the serve thread, then for the open streams.
_STOP_S = 5.0
# Every server-side socket read and write gives up after this long, so a
# client that stops reading (or never sends the body it announced) frees its
# handler thread instead of holding it, and its stream slot, forever.
_SOCKET_TIMEOUT_S = 10.0
# ASCII digits only, the whole value: "5\n" and full-width digits are refused.
_DECIMAL = re.compile(r"\d{1,15}", re.ASCII)
# A raw 32-byte key as the relay takes it: 43 base64url characters.
_KEY32 = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)
# Base64url without padding (C4), the only binary encoding on the wire.
_B64U = re.compile(r"[A-Za-z0-9_-]*", re.ASCII)
# A sealed blob: nonce and tag at least, a pane tail at most.
SEALED_MIN, SEALED_MAX = 38, 1_400_000
# A canonical RFC 9562 UUID (commandId, uploadId), the nil and max UUIDs
# included, as the relay accepts them.
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    r"|00000000-0000-0000-0000-000000000000"
    r"|ffffffff-ffff-ffff-ffff-ffffffffffff",
    re.ASCII | re.IGNORECASE,
)
# A psmux session id: one path segment of the C3 charset, never "." or "..".
_SESSION_ID = re.compile(r"(?!\.{1,2}$)[A-Za-z0-9._-]{1,128}", re.ASCII)
# Contract C1: the envelope error code each scripted failure status carries.
_CODES = {
    400: "invalid_request",
    429: "rate_limited",
    500: "internal",
    503: "unavailable",
    504: "timeout",
}
# The relay moves an upload only to a later state; equal ranks do not move.
_UPLOAD_RANK = {"pulled": 2, "inject_pending": 3, "injected": 4, "inject_failed": 4}
# path -> {method: handler}. A known path with another method is 405.
_ROUTES = {
    "/v1/host/pair/start": {"POST": "_pair_start"},
    "/v1/host/pair/poll": {"GET": "_pair_poll"},
    "/v1/stream/host": {"GET": "_stream"},
    "/v1/host/commands": {"GET": "_commands"},
    "/v1/host/ack": {"POST": "_ack"},
    "/v1/host/results": {"POST": "_results"},
    "/v1/host/events": {"POST": "_events"},
    "/v1/host/snapshot": {"PUT": "_snapshot"},
    "/v1/host/devices": {"GET": "_devices"},
    "/v1/host": {"DELETE": "_unlink"},
}
# The pairing routes authenticate themselves (no host token, or an optional one).
_OPEN_ROUTES = frozenset({"/v1/host/pair/start", "/v1/host/pair/poll"})


def _frame(event: str, data: object, ident: str | None) -> bytes:
    """One SSE frame, fields in the relay's order: event, id, data."""
    head = f"event: {event}\n" + (f"id: {ident}\n" if ident is not None else "")
    return f"{head}data: {json.dumps(data)}\n\n".encode()


def normalize_code(text: str) -> str | None:
    """A pairing code as typed (dash and whitespace optional, any case), as its
    8 bare characters; None when it is not one."""
    code = re.sub(r"[\s-]", "", text).upper()
    if len(code) != 8 or any(ch not in CODE_ALPHABET for ch in code):
        return None
    return code


def _seq(value: object) -> int | None:
    """A wire seq (a non-negative JSON integer, not a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _units(text: str) -> int:
    """A string's length as the relay counts it: UTF-16 code units."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _text(value: object, low: int, high: int) -> bool:
    return isinstance(value, str) and low <= _units(value) <= high


class _Refused(Exception):
    """A C1 error reply, raised by a route and answered by _route."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: dict[str, object] | None = None,
        retry_after: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.headers = dict(headers or {})
        if retry_after is not None:
            self.headers["Retry-After"] = str(retry_after)


def _invalid(
    message: str = "fake relay: the body is not valid", **details: object
) -> _Refused:
    return _Refused(400, "invalid_request", message, details=details or None)


class FakeRelay:
    """Context manager: the server runs between __enter__ and __exit__.

    ``devices`` is the host's trusted device list (TrustedDevice dicts); set
    it to match the test's link.json: every stream open sends it, and the
    daemon reads revocations from it. ``host_keys`` is the host's
    (kexPub, signPub) for the add-device key check; None skips that check
    until a new-host claim records the keys of the last pair/start.
    ``long_poll_s`` caps the /commands and pair/poll long-polls (the relay
    waits up to 25 s; 0 answers at once). ``heartbeat_s`` sends a ``: ping``
    comment after that much stream silence (the relay: 15 s; None: off).
    """

    def __init__(
        self,
        *,
        devices: list[dict[str, object]] | None = None,
        host_keys: tuple[str, str] | None = None,
        long_poll_s: float = 0.0,
        heartbeat_s: float | None = None,
    ) -> None:
        self.host_token = HOST_TOKEN
        self.poll_token = POLL_TOKEN
        self.host_keys = host_keys
        self.long_poll_s = long_poll_s
        self.heartbeat_s = heartbeat_s
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.results: list[dict[str, object]] = []
        self.events: list[dict[str, object]] = []
        self.snapshots: list[dict[str, object]] = []
        self.acks: list[int] = []
        self.deleted = False
        self.objects: dict[str, bytes] = {}
        self.pair_starts: list[dict[str, object]] = []
        # What pair/start answers as expiresAt. A fixed past instant by default;
        # a test that waits on the code's real expiry sets a future one.
        self.expires_at = EXPIRES_AT
        self._cond = threading.Condition()
        self._commands: list[dict[str, object]] = []
        self._uploads: list[dict[str, object]] = []
        self._upload_ranks: dict[str, int] = {}
        self._devices: list[dict[str, object]] = list(devices or [])
        self._devices_version = 0
        self._acked = 0
        self._revoked = False
        self._too_old = False
        self._drop_gen = 0
        self._fail: list[tuple[int, int | None]] = []
        self._limited: list[int] = []
        self._pairing: dict[str, object] | None = None
        self._pairing_claimed = False
        self._pairing_expired = False
        self._token_taken = False
        self._polls = 0
        self._stage_gen = 0
        self._open_streams = 0
        self._closing = False
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(self))
        self._server.daemon_threads = True
        host, port = self._server.server_address[:2]
        self.address = (str(host), int(port))
        self.url = f"http://{host}:{port}"
        self._thread: threading.Thread | None = None

    def __enter__(self) -> FakeRelay:
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": _WAIT_S},
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, *_: object) -> None:
        # The teardown checks stay quiet while another failure propagates, so
        # they never mask it.
        loud = exc_type is None
        with self._cond:
            self._closing = True
            self._cond.notify_all()
        # BaseServer.shutdown() has no timeout of its own: it runs on a helper
        # thread so a serve loop that never returns cannot hang the test.
        stopper = threading.Thread(target=self._server.shutdown, daemon=True)
        stopper.start()
        stopper.join(_STOP_S)
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(_STOP_S)
            if loud and (stopper.is_alive() or self._thread.is_alive()):
                raise AssertionError(
                    f"fake relay: the serve thread did not stop in {_STOP_S}s"
                )
        deadline = time.monotonic() + _STOP_S
        with self._cond:
            while self._open_streams and time.monotonic() < deadline:
                self._cond.wait(_WAIT_S)
            if loud and self._open_streams:
                raise AssertionError(
                    f"fake relay: {self._open_streams} stream(s) still open after"
                    f" {_STOP_S}s"
                )

    @property
    def serve_thread(self) -> threading.Thread | None:
        return self._thread

    @property
    def open_streams(self) -> int:
        with self._cond:
            return self._open_streams

    @property
    def acked_seq(self) -> int:
        with self._cond:
            return self._acked

    @property
    def devices(self) -> list[dict[str, object]]:
        with self._cond:
            return list(self._devices)

    # -- scripting -------------------------------------------------------------

    def queue_command(self, command: dict[str, object]) -> None:
        """Queue a HostCommand (C4). Its ``id`` must be a UUID, as the relay's
        are: a test that queues anything else fails here, where it is written."""
        _require_uuid("command id", command.get("id"))
        with self._cond:
            self._commands.append(command)
            self._cond.notify_all()

    def queue_upload_ready(self, ready: dict[str, object]) -> None:
        """Queue an UploadReady (C4); its ``uploadId`` must be a UUID."""
        _require_uuid("uploadId", ready.get("uploadId"))
        with self._cond:
            self._uploads.append(ready)
            self._cond.notify_all()

    def set_devices(self, devices: list[dict[str, object]]) -> None:
        """Replace the trusted devices; open streams get a ``devices`` frame."""
        with self._cond:
            self._devices = list(devices)
            self._devices_version += 1
            self._cond.notify_all()

    def revoke(self) -> None:
        with self._cond:
            self._revoked = True
            self._cond.notify_all()

    def drop_streams(self) -> None:
        with self._cond:
            self._drop_gen += 1
            self._cond.notify_all()

    def fail_next_connects(
        self, n: int, status: int = 503, *, retry_after: int | None = None
    ) -> None:
        """The next ``n`` stream opens answer ``status``; a 429 carries
        Retry-After (``retry_after`` seconds, default 5 as the relay's per-host cap)."""
        if status == 429 and retry_after is None:
            retry_after = STREAM_LIMIT_RETRY_S
        with self._cond:
            self._fail.extend([(status, retry_after)] * n)

    def rate_limit_next_calls(self, n: int, retry_after: int = 30) -> None:
        """The next ``n`` calls to a route the relay rate-limits (pair/start,
        events, snapshot) answer 429 ``rate_limited`` with Retry-After, and
        none of them acts. The other host routes are not limited by the relay
        and are not touched; the stream's limit is its 2-connection cap."""
        with self._cond:
            self._limited.extend([retry_after] * n)

    def set_protocol_too_old(self) -> None:
        with self._cond:
            self._too_old = True

    def stage_pairing(
        self,
        *,
        claimed_devices: list[dict[str, object]],
        new_host: bool = True,
        after_polls: int = 1,
    ) -> None:
        """A device claims the pairing; poll number ``after_polls`` (counted
        from here) is the first to see it. The claim adds the devices to the
        trusted list, and a new-host claim hands out the host token once."""
        with self._cond:
            self._pairing = {
                "devices": claimed_devices,
                "new_host": new_host,
                "after": after_polls,
            }
            self._pairing_claimed = False
            self._pairing_expired = False
            self._token_taken = False
            self._polls = 0
            self._stage_gen += 1
            self._cond.notify_all()

    def expire_pairing(self) -> None:
        """An unclaimed pairing answers ``expired`` from now on; a claimed one
        stays claimed (at the relay a claim that landed is still a claim)."""
        with self._cond:
            self._pairing_expired = True
            self._cond.notify_all()

    def wait_for(self, predicate: Callable[[], bool], timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError(f"fake relay: condition not met within {timeout}s")

    # -- state the handler reads (call with self._cond held) -------------------

    def _command_seq(self) -> int:
        return max((int(str(c.get("seq", 0))) for c in self._commands), default=0)

    def _resolved_commands(self) -> set[str]:
        return {
            str(r.get("commandId")) for r in self.results if r.get("type") == "command"
        }

    def _resolved_uploads(self) -> set[str]:
        return {
            str(r.get("uploadId")) for r in self.results if r.get("type") == "upload"
        }

    def _pending(
        self, since: int
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        done_c, done_u = self._resolved_commands(), self._resolved_uploads()
        commands = sorted(
            (
                c
                for c in self._commands
                if int(str(c.get("seq", 0))) > since and str(c.get("id")) not in done_c
            ),
            key=lambda c: int(str(c.get("seq", 0))),
        )
        uploads = [u for u in self._uploads if str(u.get("uploadId")) not in done_u]
        return commands, uploads

    def _assert_ackable(self, seq: int) -> None:
        if seq > self._command_seq():
            raise _invalid("fake relay: ack past the last command", reason="ack_ahead")

    def _poll_outcome(self) -> dict[str, object] | None:
        if self._pairing_claimed:
            return self._claimed()
        if self._pairing_expired:
            return {"status": "expired"}
        staged = self._pairing
        if staged is None or self._polls < int(str(staged["after"])):
            return None
        self._land_claim(staged)
        return self._claimed()

    def _land_claim(self, staged: dict[str, object]) -> None:
        self._pairing_claimed = True
        if staged["new_host"]:
            self._revoked = False
            if self.pair_starts:
                start = self.pair_starts[-1]
                self.host_keys = (str(start["hostKexPub"]), str(start["hostSignPub"]))
        known = {str(d.get("deviceId")) for d in self._devices}
        devices = staged["devices"]
        for paired in devices if isinstance(devices, list) else []:
            if str(paired.get("deviceId")) in known:
                continue
            self._devices.append(
                {
                    "deviceId": paired.get("deviceId"),
                    "name": paired.get("name", ""),
                    "kexPub": paired.get("kexPub", ""),
                    "signPub": paired.get("signPub", ""),
                    "pairedAt": PAIRED_AT,
                    "revokedAt": None,
                }
            )
        self._devices_version += 1
        self._cond.notify_all()

    def _claimed(self) -> dict[str, object]:
        staged = self._pairing or {}
        data: dict[str, object] = {"status": "claimed", "hostId": HOST_ID}
        if staged.get("new_host") and not self._token_taken:
            # The relay hands the token to exactly one poll, the first after the claim.
            self._token_taken = True
            data["hostToken"] = self.host_token
        data["devices"] = staged.get("devices", [])
        return data


class _Handler(BaseHTTPRequestHandler):
    """The fake relay's routes; ``relay`` is bound per server by _handler_for."""

    relay: FakeRelay
    protocol_version = "HTTP/1.0"
    timeout = _SOCKET_TIMEOUT_S
    _raw = b""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002  # reason: BaseHTTPRequestHandler's own signature
        return

    # -- plumbing ----------------------------------------------------------

    def _reply(
        self, status: int, payload: object, headers: dict[str, str] | None = None
    ) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, data: object) -> None:
        self._reply(200, {"ok": True, "data": data})

    def _refuse(self, err: _Refused) -> None:
        error: dict[str, object] = {"code": err.code, "message": err.message}
        if err.details is not None:
            error["details"] = err.details
        self._reply(err.status, {"ok": False, "error": error}, err.headers)

    def _drain(self) -> None:
        """Read the whole request body up front. A reply sent while body bytes
        are still unread makes Windows reset the connection, and the client
        then sees a network error instead of the 4xx."""
        length = self.headers.get("Content-Length")
        if length is None or not length.strip().isdigit():
            if length is not None:
                raise _invalid("fake relay: bad Content-Length")
            return
        self._raw = self.rfile.read(int(length)) if int(length) else b""

    def _json(self, max_bytes: int = 65_536) -> dict[str, object]:
        """The drained body as a JSON object; empty reads as {}, as at the relay."""
        if len(self._raw) > max_bytes:
            raise _Refused(
                413,
                "payload_too_large",
                "fake relay: body over the cap",
                details={"maxBytes": max_bytes},
            )
        try:
            body = json.loads(self._raw) if self._raw else {}
        except ValueError:
            raise _invalid("fake relay: the body is not JSON") from None
        if not isinstance(body, dict):
            raise _invalid()
        return body

    def _protocol_ok(self) -> None:
        with self.relay._cond:
            too_old = self.relay._too_old
        if too_old or self.headers.get("X-Magent-Link") != "1":
            raise _Refused(
                426,
                "upgrade_required",
                "fake relay: protocol too old",
                details={"protocol": "1"},
            )

    def _host_ok(self) -> None:
        self._protocol_ok()
        with self.relay._cond:
            revoked = self.relay._revoked
        if revoked or self.headers.get("Authorization") != (
            f"Bearer {self.relay.host_token}"
        ):
            raise _Refused(
                401, "unauthorized", "fake relay: host token revoked or unknown"
            )

    def _rate_limited(self) -> None:
        with self.relay._cond:
            retry_after = self.relay._limited.pop(0) if self.relay._limited else None
        if retry_after is not None:
            raise _Refused(
                429,
                "rate_limited",
                "fake relay: rate limited",
                retry_after=retry_after,
            )

    def _route(self) -> None:
        with self.relay._cond:
            self.relay.requests.append(
                (self.command, self.path, dict(self.headers.items()))
            )
        try:
            self._dispatch()
        except _Refused as err:
            self._refuse(err)
        except OSError:
            # The client went away or stalled past the socket timeout.
            return

    def _dispatch(self) -> None:
        parts = urlsplit(self.path)
        self._drain()
        if parts.path.startswith("/blob/") and self.command == "GET":
            self._blob(parts.path.removeprefix("/blob/"))
            return
        methods = _ROUTES.get(parts.path)
        if methods is None:
            raise _Refused(404, "not_found", f"fake relay: no route {parts.path}")
        name = methods.get(self.command)
        if name is None:
            raise _Refused(
                405,
                "method_not_allowed",
                "fake relay: method not allowed",
                headers={"Allow": ", ".join(methods)},
            )
        if parts.path not in _OPEN_ROUTES:
            self._host_ok()
        getattr(self, name)(parse_qs(parts.query, keep_blank_values=True))

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _route

    # -- routes ------------------------------------------------------------

    def _blob(self, key: str) -> None:
        data = self.relay.objects.get(key)
        if data is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _pair_start(self, query: dict[str, list[str]]) -> None:
        # Real order: protocol, the per-IP limit, the body, then the optional
        # host token and its keys (add-device).
        self._protocol_ok()
        self._rate_limited()
        body = self._json()
        keys = (body.get("hostKexPub"), body.get("hostSignPub"))
        if not (
            all(isinstance(k, str) and _KEY32.fullmatch(k) for k in keys)
            and isinstance(body.get("name"), str)
            and 1 <= _units(str(body.get("name")).strip()) <= 100
            and _text(body.get("platform"), 1, 32)
            and _text(body.get("daemonVersion"), 1, 32)
        ):
            raise _invalid()
        if "Authorization" in self.headers:
            self._host_ok()
            if self.relay.host_keys is not None and keys != self.relay.host_keys:
                raise _Refused(
                    409,
                    "conflict",
                    "fake relay: keys differ from the host's",
                    details={"reason": "key_mismatch"},
                )
        with self.relay._cond:
            self.relay.pair_starts.append(body)
        self._ok(
            {
                "pairingId": PAIRING_ID,
                "code": PAIRING_CODE,
                "pollToken": self.relay.poll_token,
                "expiresAt": self.relay.expires_at,
                "appUrl": self.relay.url,
            }
        )

    def _pair_poll(self, query: dict[str, list[str]]) -> None:
        self._protocol_ok()
        pairing_id = query.get("pairingId", [""])[0]
        if not pairing_id:
            raise _invalid("fake relay: pairingId missing")
        if len(pairing_id) > 64:
            raise _invalid("fake relay: pairingId too long")
        bearer = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if not bearer.startswith("mlp_"):
            raise _Refused(401, "unauthorized", "fake relay: no poll token")
        if pairing_id != PAIRING_ID or bearer != self.relay.poll_token:
            raise _Refused(
                401, "unauthorized", "fake relay: unknown pairing or poll token"
            )
        relay = self.relay
        deadline = time.monotonic() + min(LONG_POLL_CAP_S, relay.long_poll_s)
        with relay._cond:
            relay._polls += 1
            gen = relay._stage_gen
            while True:
                if relay._stage_gen != gen:
                    # Staged while this poll waited: it counts after the stage.
                    gen = relay._stage_gen
                    relay._polls += 1
                outcome = relay._poll_outcome()
                remaining = deadline - time.monotonic()
                if outcome is not None or relay._closing or remaining <= 0:
                    break
                relay._cond.wait(min(remaining, _WAIT_S))
        self._ok(outcome or {"status": "pending"})

    def _write(self, chunk: bytes) -> None:
        self.wfile.write(chunk)
        self.wfile.flush()

    def _peer_gone(self) -> bool:
        """The client hung up (an EOF waiting on the socket)."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def _stream(self, query: dict[str, list[str]]) -> None:
        # As at the relay: a bad since falls back to Last-Event-ID, then 0.
        raw = [query.get("since", [""])[0], self.headers.get("Last-Event-ID") or ""]
        since = next((int(v) for v in raw if _DECIMAL.fullmatch(v)), 0)
        relay = self.relay
        with relay._cond:
            failing = relay._fail.pop(0) if relay._fail else None
            full = failing is None and relay._open_streams >= HOST_STREAM_LIMIT
            if failing is None and not full:
                relay._open_streams += 1
            gen = relay._drop_gen
        if failing is not None:
            status, retry_after = failing
            code = _CODES.get(status, "unavailable")
            raise _Refused(
                status, code, "fake relay: scripted failure", retry_after=retry_after
            )
        if full:
            raise _Refused(
                429,
                "rate_limited",
                "fake relay: too many streams for this host",
                details={
                    "reason": "host_connection_limit",
                    "retryAfterSeconds": STREAM_LIMIT_RETRY_S,
                },
                retry_after=STREAM_LIMIT_RETRY_S,
            )
        try:
            self._serve_stream(since, gen)
        except OSError:
            return
        finally:
            with relay._cond:
                relay._open_streams -= 1
                relay._cond.notify_all()

    def _serve_stream(self, since: int, gen: int) -> None:
        relay = self.relay
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        sent_commands: set[str] = set()
        sent_uploads: set[str] = set()
        devices_seen = -1
        self._write(b": ok\n\n")
        last_write = time.monotonic()
        while True:
            frames: list[bytes] = []
            revoked = False
            with relay._cond:
                if relay._closing or relay._drop_gen != gen:
                    return
                revoked = relay._revoked
                if revoked:
                    frames.append(_frame("revoked", {}, None))
                else:
                    # The relay's connect order: devices, commands, uploads.
                    if devices_seen != relay._devices_version:
                        devices_seen = relay._devices_version
                        frames.append(
                            _frame("devices", {"devices": relay._devices}, None)
                        )
                    commands, uploads = relay._pending(since)
                    for c in commands:
                        if str(c["id"]) not in sent_commands:
                            sent_commands.add(str(c["id"]))
                            frames.append(_frame("command", c, str(c["seq"])))
                    for u in uploads:
                        if str(u["uploadId"]) not in sent_uploads:
                            sent_uploads.add(str(u["uploadId"]))
                            frames.append(_frame("upload.ready", u, None))
                if not frames:
                    relay._cond.wait(_WAIT_S)
            if not frames:
                if self._peer_gone():
                    return
                beat = relay.heartbeat_s
                if beat is not None and time.monotonic() - last_write >= beat:
                    self._write(b": ping\n\n")
                    last_write = time.monotonic()
                continue
            for frame in frames:
                self._write(frame)
            last_write = time.monotonic()
            if revoked:
                return

    def _commands(self, query: dict[str, list[str]]) -> None:
        relay = self.relay
        values = []
        for key in ("since", "wait"):
            raw = query.get(key, [""])[0]
            if raw and not _DECIMAL.fullmatch(raw):
                raise _invalid("fake relay: since/wait not a non-negative integer")
            values.append(int(raw) if raw else None)
        with relay._cond:
            since = relay._acked if values[0] is None else values[0]
        wait = min(LONG_POLL_CAP_S if values[1] is None else values[1], LONG_POLL_CAP_S)
        deadline = time.monotonic() + min(wait, relay.long_poll_s)
        with relay._cond:
            while True:
                commands, uploads = relay._pending(since)
                remaining = deadline - time.monotonic()
                # Ready uploads are news: the relay answers the poll at once.
                if commands or uploads or relay._closing or remaining <= 0:
                    break
                relay._cond.wait(min(remaining, _WAIT_S))
        self._ok({"commands": commands[:COMMANDS_BATCH], "uploads": uploads})

    def _ack(self, query: dict[str, list[str]]) -> None:
        seq = _seq(self._json().get("seq"))
        if seq is None:
            raise _invalid(path="seq")
        with self.relay._cond:
            self.relay._assert_ackable(seq)
            self.relay._acked = max(self.relay._acked, seq)
            self.relay.acks.append(seq)
            self.relay._cond.notify_all()
        self._ok({"seq": seq})

    def _results(self, query: dict[str, list[str]]) -> None:
        body = self._json(8 * 1024 * 1024)
        results = body.get("results")
        ack = body.get("ack")
        if not isinstance(results, list) or len(results) > RESULTS_MAX:
            raise _invalid(path="results")
        if not all(_valid_result(r) for r in results):
            raise _invalid(path="results")
        if ack is not None and _seq(ack) is None:
            raise _invalid(path="ack")
        relay = self.relay
        with relay._cond:
            # A batch whose ack is ahead is refused whole, before any write.
            if ack is not None:
                relay._assert_ackable(int(ack))
            accepted = 0
            queued = {str(c.get("id")) for c in relay._commands}
            done = relay._resolved_commands()
            uploads = {str(u.get("uploadId")) for u in relay._uploads}
            for r in results:
                if r["type"] == "command":
                    if r["commandId"] in queued and r["commandId"] not in done:
                        done.add(r["commandId"])
                        accepted += 1
                    continue
                rank = _UPLOAD_RANK[r["state"]]
                if r["uploadId"] in uploads and rank > relay._upload_ranks.get(
                    r["uploadId"], 1
                ):
                    relay._upload_ranks[r["uploadId"]] = rank
                    accepted += 1
            relay.results.extend(results)
            if ack is not None:
                relay._acked = max(relay._acked, int(ack))
                relay.acks.append(int(ack))
            relay._cond.notify_all()
        self._ok({"accepted": accepted})

    def _events(self, query: dict[str, list[str]]) -> None:
        events = self._json(EVENTS_MAX_BYTES).get("events")
        if not isinstance(events, list) or not 1 <= len(events) <= EVENTS_MAX:
            raise _invalid(path="events")
        if not all(_valid_event(e) for e in events):
            raise _invalid(path="events")
        self._rate_limited()
        with self.relay._cond:
            self.relay.events.extend(events)
            last = len(self.relay.events)
        self._ok({"accepted": len(events), "lastSeq": last})

    def _snapshot(self, query: dict[str, list[str]]) -> None:
        # The relay charges the limit before it reads the body.
        self._rate_limited()
        snapshot = self._json(4 * 1024 * 1024)
        sessions = snapshot.get("sessions")
        daemon = snapshot.get("daemon")
        if not (
            isinstance(daemon, dict)
            and all(
                _text(daemon.get(k), 1, 32) for k in ("version", "platform", "epoch")
            )
            and isinstance(snapshot.get("remoteControl"), bool)
            and _seq(snapshot.get("uploadMaxBytes")) not in (None, 0)
            and isinstance(sessions, list)
            and len(sessions) <= SESSIONS_MAX
            and all(_valid_session(s) for s in sessions)
            and _valid_sealed(snapshot.get("sealed"))
        ):
            raise _invalid()
        with self.relay._cond:
            self.relay.snapshots.append(snapshot)
        # The relay keeps one row per session id.
        self._ok({"sessions": len({str(s["session"]) for s in sessions})})

    def _devices(self, query: dict[str, list[str]]) -> None:
        self._ok({"devices": self.relay.devices})

    def _unlink(self, query: dict[str, list[str]]) -> None:
        with self.relay._cond:
            self.relay.deleted = True
            self.relay._revoked = True
            self.relay._cond.notify_all()
        self._ok({"revoked": True})


def _valid_result(result: object) -> bool:
    """One host result, the fields the relay acts on."""
    if not isinstance(result, dict):
        return False
    if result.get("type") == "command":
        status = result.get("status")
        return (
            _uuid(result.get("commandId"))
            and isinstance(status, int)
            and not isinstance(status, bool)
            and 100 <= status <= 599
            and _sealed(result.get("sealed"))
        )
    return (
        result.get("type") == "upload"
        and _uuid(result.get("uploadId"))
        and result.get("state") in _UPLOAD_RANK
    )


def _valid_event(event: object) -> bool:
    """One host event, as the relay validates it."""
    return (
        isinstance(event, dict)
        and _text(event.get("daemonEventId"), 1, 64)
        and _text(event.get("type"), 1, 64)
        and isinstance(event.get("meta"), dict)
        and _valid_sealed(event.get("sealed"))
    )


def _uuid(value: object) -> bool:
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


def _require_uuid(what: str, value: object) -> None:
    if not _uuid(value):
        raise ValueError(f"fake relay: {what} {value!r} is not a UUID")


def _sealed(value: object) -> bool:
    """One sealed blob: base64url, SEALED_MIN to SEALED_MAX characters."""
    return (
        _text(value, SEALED_MIN, SEALED_MAX) and _B64U.fullmatch(str(value)) is not None
    )


def _valid_sealed(value: object) -> bool:
    """A per-device map of sealed blobs."""
    return isinstance(value, dict) and all(
        isinstance(k, str) and _sealed(v) for k, v in value.items()
    )


def _state(value: object) -> bool:
    return value is None or _text(value, 0, 32)


def _valid_session(row: object) -> bool:
    """One session row of a snapshot (C4): every key present, camelCase."""
    return (
        isinstance(row, dict)
        and isinstance(row.get("session"), str)
        and _SESSION_ID.fullmatch(str(row.get("session"))) is not None
        and _text(row.get("name"), 0, 200)
        and isinstance(row.get("live"), bool)
        and all(
            key in row and _state(row[key])
            for key in ("hookState", "paneState", "nodeState")
        )
    )


def _handler_for(relay: FakeRelay) -> type[BaseHTTPRequestHandler]:
    return type("FakeRelayHandler", (_Handler,), {"relay": relay})
