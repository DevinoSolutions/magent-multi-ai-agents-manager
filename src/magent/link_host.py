"""The running daemon as magent link sees it: ``link.LinkHost`` over the
same ``api.handle`` the local ``/api/v1`` uses (spec 4.1).

A paired device's command arrives as (method, path, body, device) and
becomes an ``api.ApiRequest`` from a ``linked`` caller: write verbs answer
only while ``settings.link.remoteControl`` is on. The snapshot, the event
feed, the upload cap and staged uploads go through the same routes
(``/meta``, ``/sessions``, ``/events/poll``, ``/uploads``), so the link never
reaches into serve's internals: everything it needs from P1 is
``api.handle``, ``api.ApiRequest``, ``api.Body``, ``api.Caller``,
``api.ApiStream`` and ``api.PREFIX``.

A device reaches only the routes the relay tunnels (contract C3: the reads)
and the write verbs, which P1's remote-control gate then judges; anything
else, a dot segment or an encoded character included, is refused before it
is routed, whatever the relay let through. Its command always carries
``Content-Type: application/json``, so it can never reach the staged-upload
form (``link_staged``): only ``stage_upload`` sends that, for a file
``link.py`` decrypted itself.

Thread-safe: the runner, the event forwarder and the upload puller each call
in from their own thread. ``snapshot``, ``remote_control`` and
``upload_max_bytes`` never raise.
"""

from __future__ import annotations

import json
import platform
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote, urlsplit

from magent import api, wire
from magent.config import load_config
from magent.link import EVENT_FIELD_MAX, utf16_len
from magent.link_staged import CONTENT_TYPE as STAGED_CONTENT_TYPE
from magent.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

CLIENT_NAME = "magent-relay"  # X-Magent-Client: <this>/<deviceId> names the device
LINK_PEER = "magent-relay"  # ApiRequest.peer/bind for a linked request (no socket)
FEED_ERROR_PAUSE_S = 1.0

# One path segment the relay tunnels in place of a session id or a project
# name: C3's session-id charset, never "." or "..". No "%" can appear, so no
# encoded dot or slash ever reaches the router.
_SEGMENT = re.compile(r"(?!\.{1,2}\Z)[A-Za-z0-9._-]{1,128}", re.ASCII)
_ANY = "*"  # a _SEGMENT in a route shape
# Contract C3: the reads the relay tunnels in P2, by shape under api.PREFIX.
_READS: tuple[tuple[str, ...], ...] = (
    ("meta",),
    ("health",),
    ("status",),
    ("sessions",),
    ("sessions", _ANY),
    ("sessions", _ANY, "pane"),
    ("projects",),
    ("events", "poll"),
)
# P1's write verbs. They reach P1's gate, which answers a linked caller only
# while remote control is on (P2's relay tunnels none of them).
_WRITES: dict[str, tuple[tuple[str, ...], ...]] = {
    "POST": (
        ("sessions", "start"),
        ("sessions", "stop"),
        ("sessions", _ANY, "send"),
        ("sessions", _ANY, "choose"),
        ("sessions", _ANY, "interrupt"),
        ("sessions", _ANY, "model"),
        ("projects",),
    ),
    "PATCH": (("projects", _ANY),),
    "DELETE": (("projects", _ANY),),
}


def _fits(parts: tuple[str, ...], shape: tuple[str, ...]) -> bool:
    return len(parts) == len(shape) and all(
        bool(_SEGMENT.fullmatch(part)) if want == _ANY else part == want
        for part, want in zip(parts, shape, strict=True)
    )


def tunnelled(method: str, path: str) -> bool:
    """May a device's command reach ``api.handle`` at all? ``path`` is the
    path alone (no query): one of the C3 reads, or a write verb."""
    if not path.startswith(api.PREFIX + "/"):
        return False
    parts = tuple(path[len(api.PREFIX) + 1 :].split("/"))
    shapes = _READS if method == "GET" else _WRITES.get(method, ())
    return any(_fits(parts, shape) for shape in shapes)


def _data(envelope: dict[str, object]) -> dict[str, object]:
    data = envelope.get("data") if envelope.get("ok") is True else None
    return {str(k): v for k, v in data.items()} if isinstance(data, dict) else {}


def _rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [{str(k): v for k, v in r.items()} for r in value if isinstance(r, dict)]


def _event_ok(event: dict[str, object]) -> bool:
    """What the forwarder seals and the relay takes: an id of 1-64 UTF-16
    units and a type that is a string."""
    ident, kind = event.get("id"), event.get("type")
    return (
        isinstance(ident, str)
        and 1 <= utf16_len(ident) <= EVENT_FIELD_MAX
        and isinstance(kind, str)
    )


class ApiLinkHost:
    """``link.LinkHost`` for a running ``magent serve``.

    ``context`` builds the server's ``api.ApiContext`` per call (serve's
    ``UploadHandler._api_context``), so the link sees exactly what a local
    request would. ``config_path`` is where ``settings.link.remoteControl``
    is read, re-read whenever the file changes.
    """

    def __init__(
        self, context: Callable[[], api.ApiContext], config_path: str | None
    ) -> None:
        self._context = context
        self._config_path = config_path
        self._log = get_logger("link")
        self._rc_lock = threading.Lock()
        self._rc_cache: tuple[int, bool] | None = None
        self._max_lock = threading.Lock()
        self._max_bytes: int | None = None
        self._rows_lock = threading.Lock()
        self._last_rows: tuple[dict[str, object], ...] = ()
        self._rows_failing = False

    def call(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str = "application/json",
        client: str = CLIENT_NAME,
        remote_control: bool = False,
    ) -> tuple[int, dict[str, object]]:
        """One ``api.handle`` call as a linked caller: (status, envelope)."""
        parts = urlsplit(path)
        req = api.ApiRequest(
            method=method.upper(),
            path=parts.path,
            query=parse_qs(parts.query),
            headers={"x-magent-client": client, "content-type": content_type},
            body=api.Body.of(body or b""),
            peer=LINK_PEER,
            bind=LINK_PEER,
            port=0,
        )
        caller = api.Caller("linked", remote_control=remote_control)
        answer = api.handle(req, self._context(), caller)
        if isinstance(answer, api.ApiStream):
            answer.frames.close()
            return 400, wire.error(
                "invalid_request", "streams are not served over the link"
            )
        return answer.status, answer.body

    # -- link.LinkHost -----------------------------------------------------------

    def handle(
        self, method: str, path: str, body: bytes | None, device_id: str
    ) -> tuple[int, dict[str, object]]:
        parts = urlsplit(path)
        if parts.fragment or not tunnelled(method, parts.path):
            return 400, wire.error(
                "invalid_request",
                "this request is not served over the link",
                {"reason": "not_tunnelled"},
            )
        # Never a content type of the device's choosing: always JSON.
        status, envelope = self.call(
            method,
            path,
            body=body,
            client=f"{CLIENT_NAME}/{device_id}",
            remote_control=self.remote_control(),
        )
        try:
            # The runner seals json.dumps of this answer outside its guard.
            json.dumps(envelope)
        except (TypeError, ValueError):
            self._log.exception("link: %s %s answered no JSON", method, parts.path)
            return 500, wire.error("internal", "internal error")
        return status, envelope

    def stage_upload(
        self, device_id: str, session: str, file_path: Path, inject: bool
    ) -> dict[str, object]:
        """POST the staged form for ``file_path`` exactly as the puller gave
        it (``link_staged`` resolves and checks it); the /api/v1 envelope."""
        self._log.info("link: staging an upload from %s for %s", device_id, session)
        body = {
            "staged_path": str(file_path),
            "name": file_path.name,
            "session": session,
            "inject": inject,
        }
        _status, envelope = self.call(
            "POST",
            api.PREFIX + "/uploads",
            body=json.dumps(body).encode("utf-8"),
            content_type=STAGED_CONTENT_TYPE,
        )
        return envelope

    def snapshot(self) -> tuple[list[dict[str, object]], dict[str, str]]:
        """``/meta`` and ``/sessions`` as the relay snapshot wants them. A
        read that fails keeps the last rows it had (none before the first),
        so a broken config is a stale snapshot, never a crash. Every caller
        gets rows of its own."""
        meta: dict[str, object] = {}
        rows: list[dict[str, object]] | None = None
        try:
            _status, meta_env = self.call("GET", api.PREFIX + "/meta")
            meta = _data(meta_env)
            status, sessions_env = self.call("GET", api.PREFIX + "/sessions")
            if status == 200:
                rows = _rows(_data(sessions_env).get("sessions"))
        except Exception:  # noqa: BLE001  # reason: the snapshot never raises; a failed read keeps the last rows (logged once per streak)
            self._log.debug("link: snapshot read failed", exc_info=True)
        with self._rows_lock:
            if rows is None:
                if not self._rows_failing:
                    self._log.warning(
                        "link: the session list could not be read; "
                        "the relay keeps the last one"
                    )
                self._rows_failing = True
                kept = self._last_rows
            else:
                self._rows_failing = False
                kept = self._last_rows = tuple(dict(r) for r in rows)
            out = [dict(r) for r in kept]
        daemon = {
            "version": str(meta.get("version", "")),
            "platform": platform.system().lower(),
            "epoch": str(meta.get("epoch", "")),
        }
        return out, daemon

    def subscribe(self) -> PollFeed:
        return PollFeed(self)

    def remote_control(self) -> bool:
        """``settings.link.remoteControl``; False whenever it cannot be read.
        Re-read when the file's mtime changes; callers at once share one
        read."""
        if self._config_path is None:
            return False
        with self._rc_lock:
            try:
                stamp = Path(self._config_path).stat().st_mtime_ns
            except OSError:
                return False
            if self._rc_cache is not None and self._rc_cache[0] == stamp:
                return self._rc_cache[1]
            try:
                value = load_config(self._config_path).settings.link.remote_control
            except Exception:  # noqa: BLE001  # reason: a config that does not load (ConfigError, a vanished file, anything) turns remote control OFF; it must never raise into the link
                value = False
            self._rc_cache = (stamp, value)
            return value

    def upload_max_bytes(self) -> int:
        """``/meta``'s ``limits.upload_max_bytes``, read once (it is fixed
        for the life of a serve): an int >= 0, and 0 while it cannot be read
        (tried again on the next call)."""
        with self._max_lock:
            if self._max_bytes is None:
                try:
                    _status, meta_env = self.call("GET", api.PREFIX + "/meta")
                except Exception:  # noqa: BLE001  # reason: the cap never raises into the puller or the snapshot; 0 until /meta answers
                    self._log.debug("link: /meta read failed", exc_info=True)
                    return 0
                limits = _data(meta_env).get("limits")
                value = (
                    limits.get("upload_max_bytes") if isinstance(limits, dict) else None
                )
                if not isinstance(value, int) or isinstance(value, bool):
                    return 0
                self._max_bytes = max(0, value)
            return self._max_bytes


class PollFeed:
    """``link.EventFeed`` over ``GET /api/v1/events/poll``: the long-poll a
    local client would use, resumed from the ``next`` id each time. A
    ``reset`` answer (this reader fell behind the ring) becomes one synthetic
    ``reset`` event, which the forwarder answers with a fresh snapshot. It
    never emits ``hello``: that opens an SSE stream, and the poll has none.
    ``next_batch`` returns within about ``timeout`` (the poll's own wait),
    always a list, of dict events whose id the relay takes."""

    def __init__(self, host: ApiLinkHost) -> None:
        self._host = host
        self._lock = threading.Lock()
        self._next: str | None = None
        self._closed = False

    def next_batch(self, timeout: float) -> list[dict[str, object]]:
        wait = max(timeout, 0.0)
        with self._lock:
            if self._closed:
                return []
            since = self._next
        query = f"wait={wait:.3f}"
        if since is not None:
            query += f"&since={quote(since, safe='')}"
        began = time.monotonic()
        status, envelope = self._host.call("GET", f"{api.PREFIX}/events/poll?{query}")
        if status != 200:
            # The poll did not wait: pause the rest of the timeout (at most
            # FEED_ERROR_PAUSE_S), so a failing route is not polled hot.
            left = min(wait, FEED_ERROR_PAUSE_S) - (time.monotonic() - began)
            if left > 0:
                time.sleep(left)
            return []
        data = _data(envelope)
        nxt = data.get("next")
        with self._lock:
            if self._closed:
                return []
            if isinstance(nxt, str):
                self._next = nxt
            head = self._next
        batch = [e for e in _rows(data.get("events")) if _event_ok(e)]
        reset = data.get("reset")
        if isinstance(reset, str) and head:
            batch.append(
                {
                    "id": head,
                    "type": "reset",
                    "ts": time.time(),
                    "data": {"reason": reset},
                }
            )
        return batch

    def close(self) -> None:
        """End the feed; idempotent, from any thread."""
        with self._lock:
            self._closed = True
            self._next = None
