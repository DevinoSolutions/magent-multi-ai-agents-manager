"""The ``/api/v1`` surface as a pure function: ``handle(request, context)``.

``upload_server`` turns each HTTP request under ``PREFIX`` into an
``ApiRequest`` and writes back what ``handle`` returns: an ``ApiResponse``
(one JSON envelope) or an ``ApiStream`` (SSE frames). Everything a route needs
from the running server arrives in ``ApiContext``, so every route is testable
without a socket. The origin guard (``guard``) lives here too, and
``upload_server`` applies it to the legacy routes as well (spec 3.6).

Rows for ``GET /sessions``, ``GET /sessions/{session}`` and the stream's
``hello`` come through ONE ``FleetSource`` per serve (``ApiContext.fleet``):
its typed config and attention engine are loaded on the first build and kept
until the server restarts, so a request pays neither a typed load nor a
throwaway engine. The poller (``events.EventPoller``) freezes its own copy
the same way, on its own first tick, so after a config edit the stream and a
``GET`` may disagree on node rows (built from the typed config) until the
next restart; the local rows read the raw file every build and agree at once.
The poller's engine is never handed to a request thread.

Envelope and error codes: ``magent.wire``. Route table: spec 3.3. A leaf over
``control``, ``events``, ``fleetview``, ``projects``, ``uploads`` and
``wire``; never imports the cli package or the HTTP handler (LS-A-001).
"""

from __future__ import annotations

import functools
import io
import ipaddress
import json
import socket
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import unquote

from magent import control, events, fleetview, projects, uploads, wire
from magent.log import get_logger, log_safe
from magent.sessions import upload_limit_text
from magent.wire import WireError

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

    from magent.attention import AttentionEngine
    from magent.config import MagentConfig

PREFIX = "/api/v1"
API_VERSION = "v1"
# What this server can do, for a client deciding which controls to show.
CAPS = (
    "sessions",
    "pane",
    "events",
    "uploads",
    "send",
    "choose",
    "interrupt",
    "model",
    "start",
    "stop",
    "projects",
)
JSON_BODY_MAX = 64 * 1024
POLL_WAIT_MAX_S = 25.0
CLIENT_HEADER = "x-magent-client"
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
# The desktop app's webview origins (Tauri 2 on each OS).
TAURI_ORIGINS = frozenset(
    {"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"}
)
_FETCH_SITES_OK = frozenset({"same-origin", "none"})
_WRITE_METHODS = frozenset({"POST", "PATCH", "DELETE"})
_WILDCARDS = frozenset({"0.0.0.0", "::"})

CallerKind = Literal["loopback", "remote", "linked"]


# --- Requests -----------------------------------------------------------------


class Body:
    """A request body as a counted stream: the HTTP shell drains whatever a
    refused request left unread, so an early 4xx never ends in a TCP RST."""

    def __init__(self, read1: Callable[[int], bytes], length: int | None) -> None:
        self._read1 = read1
        self.length = length
        self.consumed = 0

    @classmethod
    def of(cls, data: bytes) -> Body:
        return cls(io.BytesIO(data).read1, len(data))

    def read1(self, n: int) -> bytes:
        chunk = self._read1(n)
        self.consumed += len(chunk)
        return chunk

    @property
    def unread(self) -> bool:
        return (self.length or 0) > self.consumed


@dataclass(frozen=True)
class ApiRequest:
    """One HTTP request, socket-free. ``headers`` keys are lower-case;
    ``port`` is the port the server is bound to and ``bind`` the local
    address the request arrived on."""

    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: Body
    peer: str
    bind: str
    port: int

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


@dataclass(frozen=True)
class Caller:
    """Who is asking. ``linked`` callers arrive through ``link.py`` (P2),
    which builds this itself; the HTTP shell only ever sees the other two."""

    kind: CallerKind
    remote_control: bool = False


# --- Context ------------------------------------------------------------------


@dataclass(frozen=True)
class Health:
    """``/health`` and ``/api/v1/health``: one shape, two routes."""

    service: str
    port: int | None
    pid: int | None
    uptime_s: float
    session_count: int | None
    sessions_age_s: float | None


class FleetSource:
    """The request threads' typed config and attention engine: loaded on the
    first row build and kept for the life of the serve, so a request pays
    neither a typed load (nor its stderr warning) nor a throwaway engine. A
    ``settings`` edit reaches the API after a restart, as it reaches the
    poller. This is never the poller's engine, which is that thread's alone;
    the request threads share this one, so a row build through it holds
    ``_lock`` (``AttentionEngine.poll`` keeps per-node state). A config that
    does not load is not kept: that request answers ``unavailable`` and the
    next one tries again. Each liveness probe is bounded by
    ``events.PROBE_TIMEOUT_S``, so a wedged socket cannot hold the lock."""

    def __init__(self, config_path: str | None) -> None:
        self.config_path = config_path
        self._lock = threading.Lock()
        self._loaded = False
        self._cfg: MagentConfig | None = None
        self._engine: AttentionEngine | None = None

    def _load(self) -> None:
        """Load the typed config and build the engine once; under ``_lock``."""
        if self._loaded:
            return
        cfg = fleetview.load_typed(self.config_path)
        self._engine = fleetview.engine_from_config(cfg) if cfg is not None else None
        self._cfg = cfg
        self._loaded = True

    def rows(self, *, include_pane: bool) -> list[fleetview.SessionRow]:
        with self._lock:
            self._load()
            return fleetview.rows(
                self.config_path,
                include_pane=include_pane,
                cfg=self._cfg,
                engine=self._engine,
                probe_timeout_s=events.PROBE_TIMEOUT_S,
            )

    def invalidate(self) -> None:
        """Forget the kept config and engine: the next build reloads them.
        Called after this process changed the config (``/projects`` writes),
        so a project added through the API, a node project included, is on
        the next ``GET`` instead of waiting for a restart. An edit made
        elsewhere still waits for the restart, as it does for the poller."""
        with self._lock:
            self._loaded = False
            self._cfg = None
            self._engine = None

    def row(self, session: str, *, include_pane: bool) -> fleetview.SessionRow | None:
        """One session's row by exact socket id (``fleetview.row_for``: one
        probe, one pane read), or None."""
        with self._lock:
            self._load()
            return fleetview.row_for(
                self.config_path,
                session,
                include_pane=include_pane,
                cfg=self._cfg,
                engine=self._engine,
                probe_timeout_s=events.PROBE_TIMEOUT_S,
            )


@dataclass(frozen=True)
class ApiContext:
    """What the running server lends every route."""

    config_path: str | None
    bus: events.EventBus
    allowed_hosts: frozenset[str]
    upload_dir: Path
    upload_max_bytes: int
    request_limit: int
    health: Callable[[], Health]
    upload_sessions: Callable[[], set[str]]
    status_provider: Callable[[], dict[str, object]] | None = None
    inject_fn: Callable[[str, str], tuple[bool, bool]] | None = None
    heartbeat_s: float = events.HEARTBEAT_S
    # serve's one FleetSource; None (a test, the CLI) builds rows per call.
    fleet: FleetSource | None = None


def allowed_hosts(
    explicit: str | None, *, tailnet_names: bool = True
) -> frozenset[str]:
    """The Host names a BROWSER may use for this server besides the address
    a request arrived on: loopback, this machine's hostname, an explicit
    ``--host`` and, with ``tailnet_names``, the tailnet IPv4 and the
    MagicDNS name, full and short (``tailscale`` is a subprocess: serve
    reads them once, off its startup path)."""
    from magent import tailnet

    names = set(LOOPBACK_NAMES)
    names.add(socket.gethostname())
    if tailnet_names:
        dns = tailnet.magicdns_host()
        names.update(n for n in (tailnet.ip4(), dns, dns and dns.split(".")[0]) if n)
    if explicit and explicit not in _WILDCARDS:
        names.add(explicit.strip("[]"))
    return frozenset(n.lower() for n in names)


# --- Responses ----------------------------------------------------------------


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: dict[str, object]
    close: bool = False

    @classmethod
    def ok(cls, data: object) -> ApiResponse:
        return cls(200, wire.ok(data))

    @classmethod
    def refuse(cls, exc: WireError, *, close: bool = False) -> ApiResponse:
        return cls(wire.STATUS[exc.code], wire.from_exc(exc), close)

    def encode(self) -> bytes:
        return json.dumps(self.body).encode()


@dataclass(frozen=True)
class ApiStream:
    """An SSE response: the shell writes and flushes each frame, and closes
    the generator when the client goes (which frees its subscription)."""

    frames: Generator[bytes, None, None]
    content_type: str = "text/event-stream"


# --- Wire types (the golden schema is generated from these) -------------------


@dataclass(frozen=True)
class Limits:
    upload_max_bytes: int
    pane_lines_max: int


@dataclass(frozen=True)
class LinkInfo:
    state: str


@dataclass(frozen=True)
class Meta:
    api: str
    version: str
    epoch: str
    caps: list[str]
    write_allowed: bool
    limits: Limits
    link: LinkInfo


@dataclass(frozen=True)
class SessionList:
    sessions: list[fleetview.SessionRow]
    snapshot_ts: float


@dataclass(frozen=True)
class ProjectList:
    projects: list[projects.Project]


@dataclass(frozen=True)
class ErrorBody:
    code: wire.ErrorCode
    message: str
    details: dict[str, object] | None = None


# Name -> type of every ``data`` payload (and the error body). The route
# table names its success type from here; scripts/gen_api_schema.py writes
# one $defs entry per name.
WIRE_TYPES: dict[str, object] = {
    "Meta": Meta,
    "Health": Health,
    "Status": dict[str, object],
    "SessionList": SessionList,
    "SessionRow": fleetview.SessionRow,
    "PaneResult": control.PaneResult,
    "ProjectList": ProjectList,
    "Project": projects.Project,
    "RemoveResult": projects.RemoveResult,
    "Poll": events.Since,
    "Event": events.Event,
    "UploadResult": uploads.UploadResult,
    "SendResult": control.SendResult,
    "ChooseResult": control.ChooseResult,
    "InterruptResult": control.InterruptResult,
    "ModelResult": control.ModelResult,
    "StartResult": control.StartResult,
    "StopResult": control.StopResult,
    "Error": ErrorBody,
}


# --- The origin guard (spec 3.6) ----------------------------------------------


def split_host(value: str) -> tuple[str, int | None] | None:
    """``host[:port]`` as (lower-case host without brackets, port or None);
    None when it does not parse."""
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return None
        host, rest = value[1:end], value[end + 1 :]
    elif value.count(":") > 1:
        return None  # an IPv6 literal must be bracketed in a Host header
    else:
        host, sep, port_text = value.partition(":")
        rest = sep + port_text
    if not host:
        return None
    if not rest:
        return host, None
    if not rest.startswith(":") or not rest[1:].isdigit():
        return None
    return host, int(rest[1:])


def _host_allowed(value: str, req: ApiRequest, ctx: ApiContext) -> bool:
    parsed = split_host(value)
    if parsed is None:
        return False
    host, port = parsed
    names = ctx.allowed_hosts | {req.bind.lower()}
    return host in names and (port or 80) == req.port


def _from_browser(req: ApiRequest) -> bool:
    """An Origin, any fetch-metadata header, or a browser User-Agent (which
    page script cannot change)."""
    if req.header("origin") is not None:
        return True
    if any(name.startswith("sec-fetch-") for name in req.headers):
        return True
    return (req.header("user-agent") or "").startswith("Mozilla/")


def guard(req: ApiRequest, ctx: ApiContext) -> WireError | None:
    """The refusal for a request this server must not answer, or None.
    Host allowlist (DNS rebinding), Origin, then fetch metadata (the
    ``<img src=/api/flash>`` CSRF hole). Applies to every route.

    The Host allowlist judges browsers only: DNS rebinding needs a browser,
    and a non-browser client can send any Host it likes anyway. That keeps
    the remote Alt+V listener working, which posts to the ssh host name the
    user typed for ``magent attach`` (an alias, a short name)."""
    host = req.header("host")
    if _from_browser(req) and (host is None or not _host_allowed(host, req, ctx)):
        return WireError(
            "forbidden",
            "Host is not one this server answers to",
            {"reason": "bad_host"},
        )
    origin = req.header("origin")
    tauri = origin in TAURI_ORIGINS
    if origin is not None and not tauri:
        scheme = "http://"
        if not origin.lower().startswith(scheme) or not _host_allowed(
            origin[len(scheme) :], req, ctx
        ):
            return WireError(
                "forbidden", "cross-origin request refused", {"reason": "bad_origin"}
            )
    site = req.header("sec-fetch-site")
    # A Tauri webview's fetch is cross-site by construction; its Origin was
    # already checked above.
    if site is not None and not tauri and site.lower() not in _FETCH_SITES_OK:
        return WireError(
            "forbidden", "cross-site request refused", {"reason": "cross_site"}
        )
    return None


def caller_for(req: ApiRequest) -> Caller:
    """``loopback`` when both the peer address and the Host name are
    loopback (a proxy on this machine forwarding a tailnet request keeps its
    tailnet Host, and stays ``remote``)."""
    parsed = split_host(req.header("host") or "")
    try:
        peer_loopback = ipaddress.ip_address(req.peer).is_loopback
    except ValueError:
        peer_loopback = False
    if peer_loopback and parsed is not None and parsed[0] in LOOPBACK_NAMES:
        return Caller("loopback")
    return Caller("remote")


def write_refusal(caller: Caller) -> WireError | None:
    """Write verbs answer the loopback caller, and a linked caller whose
    link has remote control on; the plain tailnet bind stays read-only."""
    if caller.kind == "loopback":
        return None
    if caller.kind == "linked":
        if caller.remote_control:
            return None
        return WireError(
            "forbidden",
            "remote control is off for this link",
            {"reason": "remote_control_off"},
        )
    return WireError(
        "forbidden",
        "write verbs answer only on this machine's loopback address",
        {"reason": "loopback_only"},
    )


# --- Routing ------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    req: ApiRequest
    ctx: ApiContext
    params: dict[str, str]
    caller: Caller


@dataclass(frozen=True)
class Route:
    method: str
    pattern: str
    fn: Callable[[Call], ApiResponse | ApiStream]
    data: str
    write: bool = False
    json_body: bool = False

    def match(self, path: str) -> dict[str, str] | None:
        want = self.pattern.strip("/").split("/")
        got = path.strip("/").split("/")
        if len(want) != len(got):
            return None
        params: dict[str, str] = {}
        for w, g in zip(want, got, strict=True):
            if w.startswith("{"):
                if not g:
                    return None
                params[w[1:-1]] = unquote(g)
            elif w != g:
                return None
        return params


def handle(
    req: ApiRequest, ctx: ApiContext, caller: Caller | None = None
) -> ApiResponse | ApiStream:
    """Answer one ``/api/v1`` request. Never raises: a refusal is its error
    envelope, a crash is a logged 500 ``internal``."""
    log = get_logger("upload")
    try:
        answer = _dispatch(req, ctx, caller or caller_for(req))
    except WireError as exc:
        answer = ApiResponse.refuse(exc)
    except Exception:
        log.exception("api: %s %s crashed", req.method, log_safe(req.path))
        answer = ApiResponse.refuse(WireError("internal", "internal error"))
    if req.method in _WRITE_METHODS and isinstance(answer, ApiResponse):
        log.info(
            "api %s %s client=%s -> %d",
            req.method,
            log_safe(req.path),
            log_safe(req.header(CLIENT_HEADER) or "-"),
            answer.status,
        )
    return answer


def _dispatch(
    req: ApiRequest, ctx: ApiContext, caller: Caller
) -> ApiResponse | ApiStream:
    refusal = guard(req, ctx)
    if refusal is not None:
        raise refusal
    if req.method == "OPTIONS":
        raise WireError("method_not_allowed", "OPTIONS is not served (no CORS)")
    if not req.path.startswith(PREFIX + "/"):
        raise WireError("not_found", f"no route {req.path}")
    route, params, allowed = _route(req.method, req.path[len(PREFIX) :])
    if route is None:
        if allowed:
            raise WireError(
                "method_not_allowed",
                f"{req.method} is not allowed on {req.path}",
                {"allow": allowed},
            )
        raise WireError("not_found", f"no route {req.path}")
    if req.method in _WRITE_METHODS:
        refusal = _write_headers(req, route)
        if refusal is not None:
            raise refusal
    if route.write:
        refusal = write_refusal(caller)
        if refusal is not None:
            raise refusal
    return route.fn(Call(req, ctx, params, caller))


def _route(method: str, sub: str) -> tuple[Route | None, dict[str, str], list[str]]:
    allowed: list[str] = []
    for route in ROUTES:
        params = route.match(sub)
        if params is None:
            continue
        if route.method == method:
            return route, params, []
        allowed.append(route.method)
    return None, {}, sorted(set(allowed))


def _write_headers(req: ApiRequest, route: Route) -> WireError | None:
    if not (req.header(CLIENT_HEADER) or "").strip():
        return WireError(
            "forbidden",
            "writes need an X-Magent-Client header",
            {"reason": "missing_client"},
        )
    if route.json_body:
        ctype = (req.header("content-type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return WireError(
                "invalid_request",
                "this route takes Content-Type: application/json",
                {"reason": "content_type"},
            )
    return None


# --- Request parsing ----------------------------------------------------------


def _bad(message: str) -> WireError:
    return WireError("invalid_request", message)


def _query(req: ApiRequest, key: str) -> str | None:
    values = req.query.get(key)
    return values[0] if values else None


def _flag(req: ApiRequest, key: str) -> bool:
    value = _query(req, key)
    if value is None or value in {"0", "false"}:
        return False
    if value in {"1", "true"}:
        return True
    raise _bad(f"{key} must be 0 or 1")


def _int_query(req: ApiRequest, key: str, default: int) -> int:
    value = _query(req, key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        raise _bad(f"{key} must be an integer") from None


def _float_query(req: ApiRequest, key: str, default: float) -> float:
    value = _query(req, key)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        raise _bad(f"{key} must be a number") from None


def _panes(req: ApiRequest) -> tuple[str, ...]:
    raw = _query(req, "panes") or ""
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _json(req: ApiRequest) -> dict[str, object]:
    """The body as a JSON object; ``{}`` for an empty body."""
    length = req.body.length
    if length is None:
        raise _bad("a JSON body needs a Content-Length")
    if length > JSON_BODY_MAX:
        raise WireError(
            "payload_too_large", f"JSON bodies are capped at {JSON_BODY_MAX} bytes"
        )
    raw = bytearray()
    while len(raw) < length:
        chunk = req.body.read1(length - len(raw))
        if not chunk:
            raise _bad("body incomplete")
        raw += chunk
    try:
        data = json.loads(bytes(raw) or b"{}")
    except ValueError:
        raise _bad("body is not JSON") from None
    if not isinstance(data, dict):
        raise _bad("body must be a JSON object")
    return data


def _str_field(
    body: dict[str, object], key: str, *, required: bool = False
) -> str | None:
    value = body.get(key)
    if value is None:
        if required:
            raise _bad(f"{key} is required")
        return None
    if not isinstance(value, str):
        raise _bad(f"{key} must be a string")
    return value


def _bool_field(body: dict[str, object], key: str, default: bool) -> bool:
    value = body.get(key, default)
    if not isinstance(value, bool):
        raise _bad(f"{key} must be true or false")
    return value


def _number_field(body: dict[str, object], key: str, default: float) -> float:
    value = body.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _bad(f"{key} must be a number")
    return float(value)


def _int_field(body: dict[str, object], key: str) -> int:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _bad(f"{key} must be an integer")
    return value


def _str_list_field(body: dict[str, object], key: str) -> list[str] | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, list):
        raise _bad(f"{key} must be a list of strings")
    names = [v for v in value if isinstance(v, str)]
    if len(names) != len(value):
        raise _bad(f"{key} must be a list of strings")
    return names


# --- Read routes --------------------------------------------------------------


@functools.cache
def _version() -> str:
    from magent import __version__

    return __version__


def _meta(call: Call) -> ApiResponse:
    return ApiResponse.ok(
        Meta(
            api=API_VERSION,
            version=_version(),
            epoch=call.ctx.bus.epoch,
            caps=list(CAPS),
            write_allowed=write_refusal(call.caller) is None,
            limits=Limits(call.ctx.upload_max_bytes, control.PANE_LINES_MAX),
            link=LinkInfo(state="off"),
        )
    )


def _health(call: Call) -> ApiResponse:
    return ApiResponse.ok(call.ctx.health())


def _status(call: Call) -> ApiResponse:
    provider = call.ctx.status_provider
    if provider is None:
        raise WireError("unavailable", "status is served only by `magent serve`")
    payload = dict(provider())
    payload.pop("ok", None)
    return ApiResponse.ok(payload)


def _build_rows(
    config_path: str | None, *, include_pane: bool, fleet: FleetSource | None
) -> list[fleetview.SessionRow]:
    if fleet is not None:
        return fleet.rows(include_pane=include_pane)
    return fleetview.rows(config_path, include_pane=include_pane)


def _build_row(
    config_path: str | None,
    session: str,
    *,
    include_pane: bool,
    fleet: FleetSource | None,
) -> fleetview.SessionRow | None:
    if fleet is not None:
        return fleet.row(session, include_pane=include_pane)
    return fleetview.row_for(config_path, session, include_pane=include_pane)


def _rows(
    config_path: str | None, *, include_pane: bool, fleet: FleetSource | None = None
) -> list[fleetview.SessionRow]:
    try:
        return _build_rows(config_path, include_pane=include_pane, fleet=fleet)
    except (ValueError, FileNotFoundError) as exc:
        raise WireError("unavailable", f"config: {exc}") from exc


def _row(
    config_path: str | None,
    session: str,
    *,
    include_pane: bool,
    fleet: FleetSource | None = None,
) -> fleetview.SessionRow:
    """``session``'s row through the bounded lookup (``fleetview.row_for``),
    never a scan of the fleet; ``not_found`` for a name that is not an exact
    socket id."""
    try:
        row = _build_row(config_path, session, include_pane=include_pane, fleet=fleet)
    except (ValueError, FileNotFoundError) as exc:
        raise WireError("unavailable", f"config: {exc}") from exc
    if row is None:
        raise WireError("not_found", f"no configured session named {session!r}")
    return row


def _sessions(call: Call) -> ApiResponse:
    rows = _rows(
        call.ctx.config_path,
        include_pane=_flag(call.req, "fresh"),
        fleet=call.ctx.fleet,
    )
    return ApiResponse.ok(SessionList(sessions=rows, snapshot_ts=time.time()))


def _session(call: Call) -> ApiResponse:
    return ApiResponse.ok(
        _row(
            call.ctx.config_path,
            call.params["session"],
            include_pane=True,
            fleet=call.ctx.fleet,
        )
    )


def _pane(call: Call) -> ApiResponse:
    lines = _int_query(call.req, "lines", 200)
    return ApiResponse.ok(
        control.read_pane(call.ctx.config_path, call.params["session"], lines)
    )


def _projects(call: Call) -> ApiResponse:
    return ApiResponse.ok(ProjectList(projects.list_projects(call.ctx.config_path)))


# --- Events -------------------------------------------------------------------


def _synthetic(
    type_: events.EventType, event_id: str, data: dict[str, object]
) -> bytes:
    """A frame that is not in the ring (``hello``, ``reset``)."""
    return events.sse_frame(
        events.Event(id=event_id, type=type_, ts=time.time(), data=data)
    )


def _hello_rows(ctx: ApiContext) -> list[dict[str, object]]:
    try:
        rows = _build_rows(ctx.config_path, include_pane=False, fleet=ctx.fleet)
    except (ValueError, FileNotFoundError):
        get_logger("upload").warning("events: hello sent without rows (bad config)")
        return []
    return [r.to_dict() for r in rows]


def sse_frames(
    ctx: ApiContext, last_id: str | None, panes: tuple[str, ...]
) -> Generator[bytes, None, None]:
    """One connection's stream: ``hello``, the replay after ``last_id`` (or
    one ``reset``), then live events with a ``: ping`` every
    ``ctx.heartbeat_s`` of quiet. An open generator counts as one
    subscriber (the poller runs); closing it stops counting. A crash inside
    the stream is logged and ends it (the shell's write loop sees a plain
    end of frames), never raised into the shell: ``handle`` has already
    returned by the time a frame is drawn."""
    bus = ctx.bus
    with bus.subscription(panes):
        try:
            head = bus.last_id()
            yield _synthetic(
                "hello",
                head,
                {"epoch": bus.epoch, "last_id": head, "sessions": _hello_rows(ctx)},
            )
            got = bus.since(last_id) if last_id else events.Since(events=[], next=head)
            while True:
                if got.reset is not None:
                    yield _synthetic("reset", got.next, {"reason": got.reset})
                for event in got.events:
                    yield events.sse_frame(event)
                got = bus.wait(got.next, ctx.heartbeat_s)
                if got.reset is None and not got.events:
                    yield events.SSE_PING
        except Exception:
            log = get_logger("upload")
            log.exception("events: stream crashed")
            return


def _events(call: Call) -> ApiStream:
    last = call.req.header("last-event-id") or _query(call.req, "since")
    return ApiStream(sse_frames(call.ctx, last, _panes(call.req)))


def _poll(call: Call) -> ApiResponse:
    wait = _float_query(call.req, "wait", POLL_WAIT_MAX_S)
    wait = min(max(wait, 0.0), POLL_WAIT_MAX_S)
    with call.ctx.bus.subscription(_panes(call.req)):
        got = call.ctx.bus.wait(_query(call.req, "since"), wait)
    return ApiResponse.ok(got)


# --- Uploads ------------------------------------------------------------------


def _upload(call: Call) -> ApiResponse:
    req, ctx = call.req, call.ctx
    length = req.body.length
    if length is None:
        raise _bad("an upload needs a Content-Length")
    if length > ctx.request_limit:
        raise WireError(
            "payload_too_large",
            f"File too large - {upload_limit_text(ctx.upload_max_bytes)} limit",
        )
    try:
        fields, files = uploads.parse_multipart(
            req.header("content-type") or "",
            str(length),
            req.body.read1,
            limit=ctx.request_limit,
        )
    except uploads.UploadIncomplete as exc:
        # The one UploadError that also closes the connection: the body
        # stopped short, so there is nothing left to drain. Its byte counts
        # ride along for the client, and the one log line says how far it
        # got (a warning, no traceback: a client leaving is not a crash).
        get_logger("upload").warning(
            "upload client went away mid-body on %s after %d of %d bytes: %s",
            log_safe(req.path),
            exc.received,
            exc.declared,
            log_safe(str(exc)),
        )
        return ApiResponse.refuse(
            WireError(
                "invalid_request",
                "Upload incomplete",
                {"reason": "incomplete", **exc.details},
            ),
            close=True,
        )
    session = fields.get("session", "")
    if not session:
        raise _bad("session is required")
    if session not in ctx.upload_sessions():
        raise WireError(
            "not_found",
            f"no live session named {session!r}",
            {"reason": "unknown_session"},
        )
    result = uploads.save(
        files.get("file", []),
        session,
        inject=fields.get("inject", "1") == "1",
        upload_dir=ctx.upload_dir,
        max_bytes=ctx.upload_max_bytes,
        inject_fn=ctx.inject_fn,
    )
    get_logger("upload").info(
        "upload api=v1 project=%s files=%d paste=%s",
        log_safe(session),
        len(result.paths),
        result.paste,
    )
    ctx.bus.publish(
        "upload",
        {"upload_id": result.upload_id, "session": session, "paste": result.paste},
    )
    return ApiResponse.ok(result)


# --- Write verbs --------------------------------------------------------------


def _send(call: Call) -> ApiResponse:
    """A ``wait_idle`` send parks the handler thread for up to
    ``control.SEND_TIMEOUT_MAX_S`` plus the settle sleeps: one connection's
    thread under ``ThreadingHTTPServer``, which the socket timeout does not
    bound (the wait is on the pane, not the socket)."""
    body = _json(call.req)
    return ApiResponse.ok(
        control.send(
            call.ctx.config_path,
            call.params["session"],
            _str_field(body, "text", required=True) or "",
            wait_idle=_bool_field(body, "wait_idle", False),
            timeout_s=_number_field(body, "timeout_s", 30.0),
        )
    )


def _choose(call: Call) -> ApiResponse:
    option = _int_field(_json(call.req), "option")
    return ApiResponse.ok(
        control.choose(call.ctx.config_path, call.params["session"], option)
    )


def _interrupt(call: Call) -> ApiResponse:
    _json(call.req)
    return ApiResponse.ok(
        control.interrupt(call.ctx.config_path, call.params["session"])
    )


def _model(call: Call) -> ApiResponse:
    body = _json(call.req)
    return ApiResponse.ok(
        control.set_model(
            call.ctx.config_path,
            call.params["session"],
            _str_field(body, "model", required=True) or "",
            _str_field(body, "effort"),
        )
    )


def _start(call: Call) -> ApiResponse:
    body = _json(call.req)
    return ApiResponse.ok(
        control.start(
            call.ctx.config_path,
            _str_list_field(body, "sessions"),
            _str_field(body, "group"),
        )
    )


def _stop(call: Call) -> ApiResponse:
    sessions = _str_list_field(_json(call.req), "sessions")
    if not sessions:
        raise _bad("sessions is required")
    return ApiResponse.ok(control.stop(call.ctx.config_path, sessions))


def _changed(ctx: ApiContext, name: str, change: str) -> None:
    """Announce a config change this process made, and drop the kept typed
    config so the next row build sees it."""
    if ctx.fleet is not None:
        ctx.fleet.invalidate()
    ctx.bus.publish("project.changed", {"name": name, "change": change})


def _project_add(call: Call) -> ApiResponse:
    body = _json(call.req)
    project = projects.add(
        call.ctx.config_path,
        _str_field(body, "path", required=True) or "",
        title=_str_field(body, "title"),
        group=_str_field(body, "group"),
        tool=_str_field(body, "tool"),
        node=_str_field(body, "node"),
    )
    _changed(call.ctx, project.name, "added")
    return ApiResponse.ok(project)


def _project_patch(call: Call) -> ApiResponse:
    body = _json(call.req)
    if "enabled" not in body:
        raise _bad("enabled is required")
    project = projects.set_enabled(
        call.ctx.config_path, call.params["name"], _bool_field(body, "enabled", True)
    )
    _changed(call.ctx, project.name, "enabled" if project.enabled else "disabled")
    return ApiResponse.ok(project)


def _project_delete(call: Call) -> ApiResponse:
    result = projects.remove(
        call.ctx.config_path, call.params["name"], stop=_flag(call.req, "stop")
    )
    for name in result.removed:
        _changed(call.ctx, name, "removed")
    return ApiResponse.ok(result)


def _write(
    method: str, pattern: str, fn: Callable[[Call], ApiResponse], data: str
) -> Route:
    """A loopback-only write verb (spec 3.3 "write")."""
    return Route(method, pattern, fn, data, write=True, json_body=method != "DELETE")


ROUTES: tuple[Route, ...] = (
    Route("GET", "/meta", _meta, "Meta"),
    Route("GET", "/health", _health, "Health"),
    Route("GET", "/status", _status, "Status"),
    Route("GET", "/sessions", _sessions, "SessionList"),
    _write("POST", "/sessions/start", _start, "StartResult"),
    _write("POST", "/sessions/stop", _stop, "StopResult"),
    Route("GET", "/sessions/{session}", _session, "SessionRow"),
    Route("GET", "/sessions/{session}/pane", _pane, "PaneResult"),
    _write("POST", "/sessions/{session}/send", _send, "SendResult"),
    _write("POST", "/sessions/{session}/choose", _choose, "ChooseResult"),
    _write("POST", "/sessions/{session}/interrupt", _interrupt, "InterruptResult"),
    _write("POST", "/sessions/{session}/model", _model, "ModelResult"),
    Route("GET", "/projects", _projects, "ProjectList"),
    _write("POST", "/projects", _project_add, "Project"),
    _write("PATCH", "/projects/{name}", _project_patch, "Project"),
    _write("DELETE", "/projects/{name}", _project_delete, "RemoveResult"),
    Route("GET", "/events", _events, "Event"),
    Route("GET", "/events/poll", _poll, "Poll"),
    # Any caller, like the legacy /upload (spec 3.3); still a write METHOD,
    # so it needs X-Magent-Client (spec 3.6 item 4).
    Route("POST", "/uploads", _upload, "UploadResult"),
)
