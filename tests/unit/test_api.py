"""api: the /api/v1 routes, envelope and origin guard, without a socket.

Every success body is validated against the golden schema
(docs/api/v1.schema.json), so a route and its declared wire type cannot drift.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import jsonschema
import pytest

from magent import api, control, events, fleetview, tailnet
from magent.wire import WireError

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "docs" / "api" / "v1.schema.json").read_text(
        encoding="utf-8"
    )
)
PORT = 8080
# A fake MagicDNS name. DECOY is the repo's canary marker; in upper case it
# also keeps the name outside the gitleaks MagicDNS rule, which matches
# lower-case names only. allowed_hosts stores names lower-cased.
MAGICDNS = "box.tailDECOY.ts.net"


def _validate(data: object, name: str) -> None:
    jsonschema.Draft202012Validator(
        {"$ref": f"#/$defs/{name}", "$defs": SCHEMA["$defs"]}
    ).validate(data)


def assert_wire(resp: object, name: str) -> dict[str, object]:
    assert isinstance(resp, api.ApiResponse)
    assert resp.status == 200, resp.body
    assert resp.body["ok"] is True
    _validate(resp.body["data"], name)
    data = resp.body["data"]
    assert isinstance(data, dict)
    return data


def assert_refused(
    resp: object, status: int, code: str, reason: str | None = None
) -> dict[str, object]:
    assert isinstance(resp, api.ApiResponse)
    assert resp.status == status, resp.body
    assert resp.body["ok"] is False
    error = resp.body["error"]
    _validate(error, "Error")
    assert isinstance(error, dict)
    assert error["code"] == code
    if reason is not None:
        assert error["details"]["reason"] == reason
    return error


def make_req(
    method: str = "GET",
    path: str = "/api/v1/meta",
    *,
    host: str | None = f"127.0.0.1:{PORT}",
    peer: str = "127.0.0.1",
    bind: str = "127.0.0.1",
    headers: dict[str, str | None] | None = None,
    body: bytes = b"",
    query: dict[str, list[str]] | None = None,
) -> api.ApiRequest:
    merged: dict[str, str | None] = {"host": host}
    if method in {"POST", "PATCH", "DELETE"}:
        merged["x-magent-client"] = "pytest"
        merged["content-type"] = "application/json"
    merged.update({k.lower(): v for k, v in (headers or {}).items()})
    return api.ApiRequest(
        method=method,
        path=path,
        query=query or {},
        headers={k: v for k, v in merged.items() if v is not None},
        body=api.Body.of(body),
        peer=peer,
        bind=bind,
        port=PORT,
    )


def json_req(method: str, path: str, payload: object, **kw: object) -> api.ApiRequest:
    return make_req(method, path, body=json.dumps(payload).encode(), **kw)


def row(session: str = "api", **over: object) -> fleetview.SessionRow:
    base: dict[str, object] = {
        "session": session,
        "name": session,
        "path": f"work/{session}",
        "cwd": f"/home/u/work/{session}",
        "group": "WORK",
        "tool": None,
        "enabled": True,
        "node": None,
        "live": True,
        "hook_state": "working",
        "hook_state_ts": 100.0,
        "hook_state_age_s": 2.0,
        "hook_state_stale": False,
        "pane_state": "busy",
        "pane_state_ts": 101.0,
        "node_state": None,
        "model": "Opus 4.7",
        "effort": "high",
        "session_id": "abc",
    }
    base.update(over)
    return fleetview.SessionRow(**base)


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "magent.config.json"
    path.write_text(
        json.dumps(
            {
                "version": 4,
                "projects": [
                    {"path": "work/api", "group": "WORK"},
                    {"path": "work/web", "title": "Site", "enabled": False},
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def bus():
    return events.EventBus("deadbeef")


@pytest.fixture
def ctx(cfg, bus, tmp_path):
    return api.ApiContext(
        config_path=str(cfg),
        bus=bus,
        allowed_hosts=frozenset(
            {"localhost", "127.0.0.1", "::1", MAGICDNS.lower(), "100.64.0.7"}
        ),
        upload_dir=tmp_path / "uploads",
        upload_max_bytes=1000,
        request_limit=2000,
        health=lambda: api.Health("magent-upload", PORT, 4321, 5.0, 2, 0.5),
        upload_sessions=lambda: {"api"},
        inject_fn=lambda _session, _text: (True, False),
        heartbeat_s=0.05,
    )


@pytest.fixture
def fixed_rows(monkeypatch):
    """Two fixed rows behind both fleetview seams: ``rows`` (the list) and
    ``row_for`` (the exact-id lookup). ``seen`` records each list build's
    ``include_pane``."""
    seen: list[bool] = []
    fixed = [row("api"), row("Site", enabled=False, live=False, pane_state="dead")]

    def _rows(_config_path, *, include_pane=True, **_kw):
        seen.append(include_pane)
        return list(fixed)

    def _row_for(_config_path, session, **_kw):
        return next((r for r in fixed if r.session == session), None)

    monkeypatch.setattr(fleetview, "rows", _rows)
    monkeypatch.setattr(fleetview, "row_for", _row_for)
    return seen


# --- The origin guard ---------------------------------------------------------


class TestOriginGuard:
    @pytest.mark.parametrize(
        ("host", "bind"),
        [
            (f"127.0.0.1:{PORT}", "127.0.0.1"),
            (f"localhost:{PORT}", "127.0.0.1"),
            (f"[::1]:{PORT}", "::1"),
            (f"{MAGICDNS}:{PORT}", "100.64.0.7"),
            (f"100.64.0.7:{PORT}", "100.64.0.7"),
            # `serve --host 0.0.0.0`: the address the request arrived on.
            (f"192.168.1.5:{PORT}", "192.168.1.5"),
        ],
    )
    def test_answers_its_own_names_on_its_own_port(self, ctx, host, bind):
        assert_wire(api.handle(make_req(host=host, bind=bind), ctx), "Meta")

    @pytest.mark.parametrize(
        ("host", "bind"),
        [
            (f"evil.example:{PORT}", "127.0.0.1"),
            ("127.0.0.1:9999", "127.0.0.1"),
            ("127.0.0.1", "127.0.0.1"),
            (f"192.168.1.5:{PORT}", "127.0.0.1"),
            ("127.0.0.1:80x", "127.0.0.1"),
            (None, "127.0.0.1"),
        ],
    )
    def test_any_other_host_is_refused(self, ctx, host, bind):
        assert_refused(
            api.handle(make_req(host=host, bind=bind), ctx),
            403,
            "forbidden",
            "bad_host",
        )

    @pytest.mark.parametrize(
        "origin",
        [
            None,
            f"http://127.0.0.1:{PORT}",
            f"http://{MAGICDNS}:{PORT}",
            "tauri://localhost",
            "http://tauri.localhost",
            "https://tauri.localhost",
        ],
    )
    def test_allowed_origins(self, ctx, origin):
        resp = api.handle(make_req(headers={"Origin": origin}), ctx)
        assert_wire(resp, "Meta")

    @pytest.mark.parametrize(
        "origin",
        [
            "http://evil.example",
            f"https://127.0.0.1:{PORT}",
            "null",
            "http://127.0.0.1:9999",
        ],
    )
    def test_foreign_origins_are_refused(self, ctx, origin):
        resp = api.handle(make_req(headers={"Origin": origin}), ctx)
        assert_refused(resp, 403, "forbidden", "bad_origin")

    @pytest.mark.parametrize("site", [None, "same-origin", "none"])
    def test_same_origin_fetch_metadata_passes(self, ctx, site):
        resp = api.handle(make_req(headers={"Sec-Fetch-Site": site}), ctx)
        assert_wire(resp, "Meta")

    @pytest.mark.parametrize("site", ["cross-site", "same-site"])
    def test_cross_site_fetch_metadata_is_refused(self, ctx, site):
        resp = api.handle(make_req(headers={"Sec-Fetch-Site": site}), ctx)
        assert_refused(resp, 403, "forbidden", "cross_site")

    def test_the_desktop_webview_is_cross_site_by_construction(self, ctx):
        headers: dict[str, str | None] = {
            "Origin": "tauri://localhost",
            "Sec-Fetch-Site": "cross-site",
        }
        assert_wire(api.handle(make_req(headers=headers), ctx), "Meta")

    def test_the_guard_also_judges_legacy_paths(self, ctx):
        refusal = api.guard(
            make_req(path="/api/flash", headers={"Sec-Fetch-Site": "cross-site"}), ctx
        )
        assert isinstance(refusal, WireError)
        assert refusal.details == {"reason": "cross_site"}
        assert api.guard(make_req(path="/api/flash"), ctx) is None

    def test_options_is_405_never_cors(self, ctx):
        resp = api.handle(make_req("OPTIONS", "/api/v1/sessions"), ctx)
        assert_refused(resp, 405, "method_not_allowed")

    def test_a_known_path_with_the_wrong_method_is_405(self, ctx):
        resp = api.handle(make_req("GET", "/api/v1/sessions/api/send"), ctx)
        error = assert_refused(resp, 405, "method_not_allowed")
        assert error["details"] == {"allow": ["POST"]}

    @pytest.mark.parametrize("path", ["/api/v1", "/api/v1/", "/api/v1/nope"])
    def test_an_unknown_path_is_404(self, ctx, path):
        assert_refused(api.handle(make_req(path=path), ctx), 404, "not_found")


class TestAllowedHosts:
    def test_loopback_tailnet_and_an_explicit_host(self, monkeypatch):
        monkeypatch.setattr(tailnet, "ip4", lambda: "100.64.0.7")
        monkeypatch.setattr(tailnet, "magicdns_host", lambda: "Box.tailDECOY.ts.net")
        assert api.allowed_hosts("MyHost") == {
            "localhost",
            "127.0.0.1",
            "::1",
            "100.64.0.7",
            MAGICDNS.lower(),
            "myhost",
        }

    def test_a_wildcard_bind_adds_no_name(self, monkeypatch):
        monkeypatch.setattr(tailnet, "ip4", lambda: None)
        monkeypatch.setattr(tailnet, "magicdns_host", lambda: None)
        assert api.allowed_hosts("0.0.0.0") == api.LOOPBACK_NAMES


# --- Who may write ------------------------------------------------------------


@pytest.fixture
def fake_interrupt(monkeypatch):
    calls: list[str] = []

    def _interrupt(_config_path, session):
        calls.append(session)
        return control.InterruptResult(session, "Escape", "idle")

    monkeypatch.setattr(control, "interrupt", _interrupt)
    return calls


INTERRUPT = "/api/v1/sessions/api/interrupt"


class TestWritePolicy:
    def test_loopback_may_write(self, ctx, fake_interrupt):
        assert_wire(api.handle(json_req("POST", INTERRUPT, {}), ctx), "InterruptResult")
        assert fake_interrupt == ["api"]

    def test_a_write_needs_the_client_header(self, ctx, fake_interrupt):
        resp = api.handle(
            json_req("POST", INTERRUPT, {}, headers={"X-Magent-Client": None}), ctx
        )
        assert_refused(resp, 403, "forbidden", "missing_client")
        assert fake_interrupt == []

    def test_a_json_write_needs_a_json_content_type(self, ctx, fake_interrupt):
        resp = api.handle(
            json_req("POST", INTERRUPT, {}, headers={"Content-Type": "text/plain"}), ctx
        )
        assert_refused(resp, 400, "invalid_request", "content_type")

    def test_a_tailnet_caller_is_read_only(self, ctx, fake_interrupt):
        resp = api.handle(
            json_req(
                "POST",
                INTERRUPT,
                {},
                host=f"100.64.0.7:{PORT}",
                peer="100.64.0.9",
                bind="100.64.0.7",
            ),
            ctx,
        )
        assert_refused(resp, 403, "forbidden", "loopback_only")
        assert fake_interrupt == []

    def test_a_loopback_peer_with_a_tailnet_host_is_not_loopback(
        self, ctx, fake_interrupt
    ):
        resp = api.handle(
            json_req("POST", INTERRUPT, {}, host=f"{MAGICDNS}:{PORT}"), ctx
        )
        assert_refused(resp, 403, "forbidden", "loopback_only")

    def test_a_linked_caller_needs_remote_control(self, ctx, fake_interrupt):
        off = api.handle(json_req("POST", INTERRUPT, {}), ctx, api.Caller("linked"))
        assert_refused(off, 403, "forbidden", "remote_control_off")
        on = api.handle(
            json_req("POST", INTERRUPT, {}),
            ctx,
            api.Caller("linked", remote_control=True),
        )
        assert_wire(on, "InterruptResult")

    def test_reads_answer_a_tailnet_caller(self, ctx):
        resp = api.handle(
            make_req(host=f"100.64.0.7:{PORT}", peer="100.64.0.9", bind="100.64.0.7"),
            ctx,
        )
        assert assert_wire(resp, "Meta")["write_allowed"] is False


# --- Read routes --------------------------------------------------------------


class TestMetaHealthStatus:
    def test_meta(self, ctx):
        data = assert_wire(api.handle(make_req(), ctx), "Meta")
        assert data["api"] == "v1"
        assert data["epoch"] == "deadbeef"
        assert data["write_allowed"] is True
        assert data["limits"] == {"upload_max_bytes": 1000, "pane_lines_max": 2000}
        assert "choose" in data["caps"]
        assert data["link"] == {"state": "off"}

    def test_health_mirrors_the_legacy_route(self, ctx):
        data = assert_wire(api.handle(make_req(path="/api/v1/health"), ctx), "Health")
        assert data["pid"] == 4321
        assert data["session_count"] == 2

    def test_status_without_a_provider_is_unavailable(self, ctx):
        resp = api.handle(make_req(path="/api/v1/status"), ctx)
        assert_refused(resp, 503, "unavailable")

    def test_status_is_the_status_json_payload_minus_ok(self, ctx):
        wired = api.ApiContext(
            **{**ctx.__dict__, "status_provider": lambda: {"ok": True, "upload": "up"}}
        )
        data = assert_wire(api.handle(make_req(path="/api/v1/status"), wired), "Status")
        assert data == {"upload": "up"}


class TestSessions:
    def test_lists_every_row(self, ctx, fixed_rows):
        data = assert_wire(
            api.handle(make_req(path="/api/v1/sessions"), ctx), "SessionList"
        )
        assert [r["session"] for r in data["sessions"]] == ["api", "Site"]
        assert fixed_rows == [False]

    def test_fresh_reads_the_panes(self, ctx, fixed_rows):
        api.handle(make_req(path="/api/v1/sessions", query={"fresh": ["1"]}), ctx)
        assert fixed_rows == [True]

    def test_a_bad_flag_is_invalid(self, ctx, fixed_rows):
        resp = api.handle(
            make_req(path="/api/v1/sessions", query={"fresh": ["x"]}), ctx
        )
        assert_refused(resp, 400, "invalid_request")

    def test_one_row_by_exact_socket_id(self, ctx, fixed_rows):
        data = assert_wire(
            api.handle(make_req(path="/api/v1/sessions/Site"), ctx), "SessionRow"
        )
        assert data["pane_state"] == "dead"

    def test_no_fuzzy_match(self, ctx, fixed_rows):
        resp = api.handle(make_req(path="/api/v1/sessions/site"), ctx)
        assert_refused(resp, 404, "not_found")

    def test_one_row_is_a_bounded_lookup_not_a_scan(self, ctx, fixed_rows):
        """``GET /sessions/{session}`` goes through ``fleetview.row_for``
        (one probe, one pane), never through a ``rows`` sweep of the fleet."""
        assert_wire(
            api.handle(make_req(path="/api/v1/sessions/api"), ctx), "SessionRow"
        )
        assert fixed_rows == []

    def test_a_bad_config_is_unavailable(self, ctx, monkeypatch):
        def _boom(*_a, **_kw):
            raise ValueError("projects must be a list")

        monkeypatch.setattr(fleetview, "rows", _boom)
        monkeypatch.setattr(fleetview, "row_for", _boom)
        resp = api.handle(make_req(path="/api/v1/sessions"), ctx)
        assert_refused(resp, 503, "unavailable")
        resp = api.handle(make_req(path="/api/v1/sessions/api"), ctx)
        assert_refused(resp, 503, "unavailable")


class TestFleetSource:
    """serve's one typed config + engine for every request thread."""

    @pytest.fixture
    def seams(self, monkeypatch):
        calls = {"load": 0, "engine": 0, "rows": []}
        typed = object()
        engine = object()

        def _load(_path):
            calls["load"] += 1
            return typed

        def _engine(cfg):
            assert cfg is typed
            calls["engine"] += 1
            return engine

        def _rows(config_path, **kw):
            calls["rows"].append((config_path, kw))
            return [row("api")]

        def _row_for(config_path, session, **kw):
            calls["row_for"].append((config_path, session, kw))
            return row("api") if session == "api" else None

        calls["row_for"] = []
        monkeypatch.setattr(fleetview, "load_typed", _load)
        monkeypatch.setattr(fleetview, "engine_from_config", _engine)
        monkeypatch.setattr(fleetview, "rows", _rows)
        monkeypatch.setattr(fleetview, "row_for", _row_for)
        return calls, typed, engine

    def test_loads_once_and_hands_cfg_and_engine_to_every_build(self, seams):
        calls, typed, engine = seams
        source = api.FleetSource("cfg.json")
        source.rows(include_pane=False)
        source.rows(include_pane=True)
        assert source.row("api", include_pane=True) is not None
        assert source.row("ghost", include_pane=False) is None
        assert (calls["load"], calls["engine"]) == (1, 1)
        assert [kw["include_pane"] for _p, kw in calls["rows"]] == [False, True]
        assert [(s, kw["include_pane"]) for _p, s, kw in calls["row_for"]] == [
            ("api", True),
            ("ghost", False),
        ]
        builds = [(p, kw) for p, kw in calls["rows"]]
        builds += [(p, kw) for p, _s, kw in calls["row_for"]]
        for path, kw in builds:
            assert path == "cfg.json"
            assert kw["cfg"] is typed
            assert kw["engine"] is engine
            assert kw["probe_timeout_s"] == events.PROBE_TIMEOUT_S

    def test_a_config_that_does_not_load_is_not_kept(self, seams, monkeypatch):
        calls, _typed, _engine = seams
        real_load = fleetview.load_typed
        state = {"broken": True}

        def _load(path):
            if state["broken"]:
                raise ValueError("projects must be a list")
            return real_load(path)

        monkeypatch.setattr(fleetview, "load_typed", _load)
        source = api.FleetSource("cfg.json")
        with pytest.raises(ValueError):
            source.rows(include_pane=False)
        state["broken"] = False
        assert [r.session for r in source.rows(include_pane=False)] == ["api"]
        assert calls["load"] == 1  # loaded once, on the call that worked

    def test_the_routes_use_the_context_source(self, ctx, seams):
        calls, _typed, _engine = seams
        fleet_ctx = dataclasses.replace(ctx, fleet=api.FleetSource(ctx.config_path))
        assert_wire(
            api.handle(make_req(path="/api/v1/sessions"), fleet_ctx), "SessionList"
        )
        assert_wire(
            api.handle(make_req(path="/api/v1/sessions/api"), fleet_ctx), "SessionRow"
        )
        assert calls["load"] == 1
        assert (len(calls["rows"]), len(calls["row_for"])) == (1, 1)

    def test_a_project_write_invalidates_the_kept_config(self, ctx, seams):
        """A config change made THROUGH the API (a node project added, say)
        must reach the next ``GET`` without a restart: the write drops the
        source's kept config and engine, and the next build reloads them."""
        calls, _typed, _engine = seams
        source = api.FleetSource(ctx.config_path)
        fleet_ctx = dataclasses.replace(ctx, fleet=source)
        assert_wire(
            api.handle(make_req(path="/api/v1/sessions"), fleet_ctx), "SessionList"
        )
        assert calls["load"] == 1
        assert_wire(
            api.handle(
                json_req("POST", "/api/v1/projects", {"path": "work/cli"}), fleet_ctx
            ),
            "Project",
        )
        assert_wire(
            api.handle(make_req(path="/api/v1/sessions"), fleet_ctx), "SessionList"
        )
        assert (calls["load"], calls["engine"]) == (2, 2)

    def test_a_bad_config_through_the_source_is_unavailable(self, ctx, monkeypatch):
        def _boom(_path):
            raise ValueError("projects must be a list")

        monkeypatch.setattr(fleetview, "load_typed", _boom)
        fleet_ctx = dataclasses.replace(ctx, fleet=api.FleetSource(ctx.config_path))
        resp = api.handle(make_req(path="/api/v1/sessions"), fleet_ctx)
        assert_refused(resp, 503, "unavailable")


class TestPane:
    def test_reads_the_requested_lines(self, ctx, monkeypatch):
        seen = []

        def _read(_config_path, session, lines=200):
            seen.append((session, lines))
            return control.PaneResult("hello", False, 123.0)

        monkeypatch.setattr(control, "read_pane", _read)
        resp = api.handle(
            make_req(path="/api/v1/sessions/api/pane", query={"lines": ["50"]}), ctx
        )
        assert assert_wire(resp, "PaneResult")["text"] == "hello"
        assert seen == [("api", 50)]

    @pytest.mark.parametrize("lines", ["0", "2001", "abc"])
    def test_out_of_range_lines_are_invalid(self, ctx, lines):
        resp = api.handle(
            make_req(path="/api/v1/sessions/api/pane", query={"lines": [lines]}), ctx
        )
        assert_refused(resp, 400, "invalid_request")


# --- Projects -----------------------------------------------------------------


class TestProjects:
    def test_lists_projects(self, ctx):
        data = assert_wire(
            api.handle(make_req(path="/api/v1/projects"), ctx), "ProjectList"
        )
        assert [p["name"] for p in data["projects"]] == ["api", "Site"]

    def test_add_saves_and_announces(self, ctx, bus, cfg):
        resp = api.handle(
            json_req("POST", "/api/v1/projects", {"path": "work/cli", "group": "WORK"}),
            ctx,
        )
        assert assert_wire(resp, "Project")["name"] == "cli"
        assert json.loads(cfg.read_text())["projects"][-1]["path"] == "work/cli"
        [event] = bus.since("deadbeef:0").events
        assert (event.type, event.data) == (
            "project.changed",
            {"name": "cli", "change": "added"},
        )

    def test_a_duplicate_session_is_a_conflict(self, ctx):
        resp = api.handle(json_req("POST", "/api/v1/projects", {"path": "x/api"}), ctx)
        assert_refused(resp, 409, "conflict", "duplicate_session")

    def test_patch_disables(self, ctx, bus):
        resp = api.handle(
            json_req("PATCH", "/api/v1/projects/api", {"enabled": False}), ctx
        )
        assert assert_wire(resp, "Project")["enabled"] is False
        assert bus.since("deadbeef:0").events[0].data["change"] == "disabled"

    def test_patch_needs_enabled(self, ctx):
        resp = api.handle(json_req("PATCH", "/api/v1/projects/api", {}), ctx)
        assert_refused(resp, 400, "invalid_request")

    def test_delete_removes_and_announces(self, ctx, bus):
        resp = api.handle(make_req("DELETE", "/api/v1/projects/Site"), ctx)
        assert assert_wire(resp, "RemoveResult") == {"removed": ["Site"], "stopped": []}
        assert bus.since("deadbeef:0").events[0].data == {
            "name": "Site",
            "change": "removed",
        }

    def test_delete_unknown_is_404(self, ctx):
        resp = api.handle(make_req("DELETE", "/api/v1/projects/ghost"), ctx)
        assert_refused(resp, 404, "not_found")


# --- Control routes -----------------------------------------------------------


CONTROL_CASES = [
    (
        "/api/v1/sessions/api/send",
        {"text": "hi", "wait_idle": True},
        "send",
        control.SendResult("api", True, "busy"),
        "SendResult",
    ),
    (
        "/api/v1/sessions/api/choose",
        {"option": 2},
        "choose",
        control.ChooseResult("api", 2, True, "busy"),
        "ChooseResult",
    ),
    (
        "/api/v1/sessions/api/model",
        {"model": "opus", "effort": "high"},
        "set_model",
        control.ModelResult("api", "Opus 4.7", "high", True),
        "ModelResult",
    ),
    (
        "/api/v1/sessions/start",
        {"sessions": ["api", "x"]},
        "start",
        control.StartResult(["api"], [], [control.StartFailure("x", "unknown")]),
        "StartResult",
    ),
    (
        "/api/v1/sessions/stop",
        {"sessions": ["api"]},
        "stop",
        control.StopResult(["api"], []),
        "StopResult",
    ),
]


class TestControlRoutes:
    @pytest.mark.parametrize(("path", "payload", "fn", "result", "name"), CONTROL_CASES)
    def test_each_verb_returns_its_result(
        self, ctx, monkeypatch, path, payload, fn, result, name
    ):
        calls = []

        def _fake(*args, **kwargs):
            calls.append((args[1:], kwargs))
            return result

        monkeypatch.setattr(control, fn, _fake)
        assert_wire(api.handle(json_req("POST", path, payload), ctx), name)
        assert len(calls) == 1

    def test_send_passes_its_options(self, ctx, monkeypatch):
        seen = {}

        def _send(_config_path, session, text, **kwargs):
            seen.update(session=session, text=text, **kwargs)
            return control.SendResult(session, True, "busy")

        monkeypatch.setattr(control, "send", _send)
        api.handle(
            json_req(
                "POST",
                "/api/v1/sessions/api/send",
                {"text": "hi", "wait_idle": True, "timeout_s": 5},
            ),
            ctx,
        )
        assert seen == {
            "session": "api",
            "text": "hi",
            "wait_idle": True,
            "timeout_s": 5.0,
        }

    def test_a_refusal_keeps_its_code_and_details(self, ctx, monkeypatch):
        def _choose(*_a):
            raise control.ControlError(
                "conflict",
                "not in a dialog",
                {"reason": "not_in_dialog", "pane_state": "idle"},
            )

        monkeypatch.setattr(control, "choose", _choose)
        resp = api.handle(
            json_req("POST", "/api/v1/sessions/api/choose", {"option": 1}), ctx
        )
        error = assert_refused(resp, 409, "conflict", "not_in_dialog")
        assert error["details"]["pane_state"] == "idle"

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            ("/api/v1/sessions/api/send", b"{}"),
            ("/api/v1/sessions/api/send", b'{"text": 5}'),
            ("/api/v1/sessions/api/choose", b'{"option": "2"}'),
            ("/api/v1/sessions/api/choose", b'{"option": true}'),
            ("/api/v1/sessions/api/model", b'{"effort": "high"}'),
            ("/api/v1/sessions/stop", b'{"sessions": []}'),
            ("/api/v1/sessions/stop", b'{"sessions": [1]}'),
            ("/api/v1/sessions/api/interrupt", b"[1]"),
            ("/api/v1/sessions/api/interrupt", b"not json"),
        ],
    )
    def test_a_bad_body_is_invalid(self, ctx, path, body):
        assert_refused(
            api.handle(make_req("POST", path, body=body), ctx), 400, "invalid_request"
        )

    def test_an_oversized_body_is_413_and_unread(self, ctx):
        req = make_req("POST", INTERRUPT, body=b" " * (api.JSON_BODY_MAX + 1))
        assert_refused(api.handle(req, ctx), 413, "payload_too_large")
        assert req.body.unread

    def test_a_crash_is_a_logged_500(self, ctx, monkeypatch, caplog):
        def _boom(*_a, **_kw):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(control, "interrupt", _boom)
        resp = api.handle(json_req("POST", INTERRUPT, {}), ctx)
        error = assert_refused(resp, 500, "internal")
        assert "kaboom" in caplog.text
        # The traceback stays in the log: the envelope carries no repr(exc).
        assert error["message"] == "internal error"
        assert "details" not in error
        assert isinstance(resp, api.ApiResponse)
        assert "kaboom" not in json.dumps(resp.body)


# --- Uploads ------------------------------------------------------------------


BOUNDARY = "----magentApiTest"


def multipart(*parts: tuple[str, str | None, bytes]) -> bytes:
    out = b""
    for name, filename, data in parts:
        disp = f'form-data; name="{name}"'
        if filename is not None:
            disp += f'; filename="{filename}"'
        out += (
            f"--{BOUNDARY}\r\nContent-Disposition: {disp}\r\n\r\n".encode()
            + data
            + b"\r\n"
        )
    return out + f"--{BOUNDARY}--\r\n".encode()


def upload_req(body: bytes, **kw: object) -> api.ApiRequest:
    headers = {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}
    headers.update(kw.pop("headers", {}))
    return make_req("POST", "/api/v1/uploads", body=body, headers=headers, **kw)


class TestUploads:
    @pytest.fixture(autouse=True)
    def _psmux(self, monkeypatch):
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: "psmux")

    def test_saves_pastes_and_announces(self, ctx, bus, tmp_path):
        body = multipart(("session", None, b"api"), ("file", "a.png", b"PNG"))
        data = assert_wire(api.handle(upload_req(body), ctx), "UploadResult")
        assert data["paste"] == "injected"
        assert Path(data["path"]).read_bytes() == b"PNG"
        [event] = bus.since("deadbeef:0").events
        assert event.type == "upload"
        assert event.data == {
            "upload_id": data["upload_id"],
            "session": "api",
            "paste": "injected",
        }

    def test_any_caller_may_upload(self, ctx):
        body = multipart(("session", None, b"api"), ("file", "a.png", b"PNG"))
        req = upload_req(
            body, host=f"100.64.0.7:{PORT}", peer="100.64.0.9", bind="100.64.0.7"
        )
        assert_wire(api.handle(req, ctx), "UploadResult")

    def test_an_upload_still_needs_the_client_header(self, ctx):
        body = multipart(("session", None, b"api"), ("file", "a.png", b"PNG"))
        resp = api.handle(upload_req(body, headers={"X-Magent-Client": None}), ctx)
        assert_refused(resp, 403, "forbidden", "missing_client")

    def test_an_unknown_session_is_404(self, ctx):
        body = multipart(("session", None, b"ghost"), ("file", "a.png", b"PNG"))
        assert_refused(
            api.handle(upload_req(body), ctx), 404, "not_found", "unknown_session"
        )

    def test_no_session_is_invalid(self, ctx):
        body = multipart(("file", "a.png", b"PNG"))
        assert_refused(api.handle(upload_req(body), ctx), 400, "invalid_request")

    def test_too_large_is_refused_before_a_byte_is_read(self, ctx):
        req = upload_req(b"x" * 2001)
        assert_refused(api.handle(req, ctx), 413, "payload_too_large")
        assert req.body.consumed == 0

    def test_files_over_the_cap_are_413(self, ctx):
        body = multipart(("session", None, b"api"), ("file", "a.bin", b"x" * 1001))
        assert_refused(api.handle(upload_req(body), ctx), 413, "payload_too_large")

    def test_a_short_body_is_incomplete_and_closes(self, ctx):
        body = multipart(("session", None, b"api"), ("file", "a.png", b"PNG"))
        req = upload_req(body)
        short = api.ApiRequest(
            **{
                **req.__dict__,
                "body": api.Body(api.Body.of(body[:40]).read1, len(body)),
            }
        )
        resp = api.handle(short, ctx)
        error = assert_refused(resp, 400, "invalid_request", "incomplete")
        assert isinstance(resp, api.ApiResponse)
        assert resp.close is True
        assert error["details"]["received"] == 40
        assert error["details"]["declared"] == len(body)


# --- Events -------------------------------------------------------------------


class TestEventsPoll:
    def test_returns_what_happened_since(self, ctx, bus):
        bus.publish("session.added", {"session": "api"})
        bus.publish("session.removed", {"session": "api"})
        resp = api.handle(
            make_req(
                path="/api/v1/events/poll",
                query={"since": ["deadbeef:0"], "wait": ["0"]},
            ),
            ctx,
        )
        data = assert_wire(resp, "Poll")
        assert [e["type"] for e in data["events"]] == [
            "session.added",
            "session.removed",
        ]
        assert data["next"] == "deadbeef:2"

    def test_a_foreign_epoch_is_a_reset(self, ctx):
        resp = api.handle(
            make_req(
                path="/api/v1/events/poll",
                query={"since": ["cafe0000:3"], "wait": ["0"]},
            ),
            ctx,
        )
        assert assert_wire(resp, "Poll")["reset"] == "epoch"

    def test_a_bad_wait_is_invalid(self, ctx):
        resp = api.handle(
            make_req(path="/api/v1/events/poll", query={"wait": ["soon"]}), ctx
        )
        assert_refused(resp, 400, "invalid_request")

    def test_the_wait_counts_as_a_subscriber_with_its_panes(
        self, ctx, bus, monkeypatch
    ):
        """``bus.wait`` alone registers nobody, and the poller sleeps through
        an unsubscribed wait: the route must hold a subscription (with the
        panes asked for) for exactly the wait, and release it after."""
        seen: list[tuple[int, list[str]]] = []
        real_wait = bus.wait

        def _wait(last_id, timeout):
            seen.append((bus.subscribers, bus.pane_interest()))
            return real_wait(last_id, timeout)

        monkeypatch.setattr(bus, "wait", _wait)
        resp = api.handle(
            make_req(
                path="/api/v1/events/poll", query={"panes": ["api"], "wait": ["0"]}
            ),
            ctx,
        )
        assert_wire(resp, "Poll")
        assert seen == [(1, ["api"])]
        assert bus.subscribers == 0
        assert bus.pane_interest() == []


def frame(raw: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in raw.decode().strip().splitlines():
        key, _, value = line.partition(": ")
        out[key] = value
    return out


class TestSse:
    @pytest.fixture(autouse=True)
    def _rows(self, monkeypatch):
        monkeypatch.setattr(fleetview, "rows", lambda *_a, **_kw: [row("api")])

    def _open(self, ctx, **kw):
        stream = api.handle(make_req(path="/api/v1/events", **kw), ctx)
        assert isinstance(stream, api.ApiStream)
        assert stream.content_type == "text/event-stream"
        return stream.frames

    def test_hello_first_then_live_events(self, ctx, bus):
        frames = self._open(ctx)
        hello = frame(next(frames))
        assert (hello["id"], hello["event"]) == ("deadbeef:0", "hello")
        data = json.loads(hello["data"])
        _validate(data, "Event")
        assert data["data"]["sessions"][0]["session"] == "api"
        bus.publish("session.state", {"session": "api", "hook_state": "done"})
        live = frame(next(frames))
        assert (live["id"], live["event"]) == ("deadbeef:1", "session.state")
        _validate(json.loads(live["data"]), "Event")
        frames.close()

    def test_quiet_is_a_heartbeat(self, ctx):
        frames = self._open(ctx)
        next(frames)
        assert next(frames) == events.SSE_PING
        frames.close()

    def test_last_event_id_replays_the_gap(self, ctx, bus):
        bus.publish("session.added", {"session": "a"})
        bus.publish("session.added", {"session": "b"})
        frames = self._open(ctx, headers={"Last-Event-ID": "deadbeef:1"})
        next(frames)
        replay = frame(next(frames))
        assert replay["id"] == "deadbeef:2"
        frames.close()

    @pytest.mark.parametrize(
        ("since", "reason"), [("cafe0000:9", "epoch"), ("deadbeef:0", "evicted")]
    )
    def test_an_unreplayable_since_gets_one_reset(self, ctx, since, reason):
        """A foreign epoch, or an id that fell out of the ring (here a
        two-slot ring with three events published), is one ``reset`` frame
        naming why, after ``hello``."""
        small = events.EventBus("deadbeef", ring=2)
        for name in ("a", "b", "c"):
            small.publish("session.added", {"session": name})
        frames = self._open(
            dataclasses.replace(ctx, bus=small), query={"since": [since]}
        )
        next(frames)
        reset = frame(next(frames))
        assert reset["event"] == "reset"
        assert json.loads(reset["data"])["data"] == {"reason": reason}
        # The reset is followed by live frames, not a second reset.
        assert next(frames) == events.SSE_PING
        frames.close()

    def test_a_crash_inside_the_stream_is_logged_and_ends_it(
        self, ctx, bus, monkeypatch, caplog
    ):
        """The shell's write loop sees the frames end; nothing is raised
        into it, and the subscription is still released."""

        def _boom(*_a, **_kw):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(bus, "wait", _boom)
        frames = self._open(ctx)
        assert frame(next(frames))["event"] == "hello"
        assert bus.subscribers == 1
        with pytest.raises(StopIteration):
            next(frames)
        assert "stream crashed" in caplog.text
        assert "kaboom" in caplog.text
        assert bus.subscribers == 0

    def test_an_open_stream_is_a_subscriber_and_closing_frees_it(self, ctx, bus):
        frames = self._open(ctx, query={"panes": ["api, Site"]})
        next(frames)
        assert bus.subscribers == 1
        assert bus.pane_interest() == ["Site", "api"]
        frames.close()
        assert bus.subscribers == 0
        assert bus.pane_interest() == []
