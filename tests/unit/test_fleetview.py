"""fleetview: one SessionRow per configured session, two state vocabularies.

Driven against the real on-disk fake psmux (tests/unit/_fake_psmux.py) and a
real temp config, like the fleet command tests, so the config -> liveness ->
pane read -> hook join path is exercised end to end.
"""

from __future__ import annotations

import dataclasses
import json
import time

import pytest

from magent import agent_state, attention, config, fleetview, psmux
from tests.unit._fake_psmux import make_fake_psmux

MID = "·"

# The spec 3.4 field list, in wire order. A rename or a reorder is an API
# break that magent-app and the relay both see, so it fails here first.
SPEC_FIELDS = (
    "session",
    "name",
    "path",
    "cwd",
    "group",
    "tool",
    "enabled",
    "node",
    "live",
    "hook_state",
    "hook_state_ts",
    "hook_state_age_s",
    "hook_state_stale",
    "pane_state",
    "pane_state_ts",
    "node_state",
    "model",
    "effort",
    "session_id",
)


@pytest.fixture(autouse=True)
def _patient_capture(monkeypatch):
    # The fake psmux is a Python shim; on a loaded box its start alone can
    # overrun the product's 3s capture budget.
    monkeypatch.setattr(psmux, "CAPTURE_PANE_TIMEOUT_S", 60.0)


def _fleet(tmp_config, tmp_path, monkeypatch, *, live, pane=""):
    """Two projects whose folders exist (so ``resolved`` is set), a fake
    psmux on the seam, and the config path."""
    for title in ("caramel", "upup"):
        (tmp_path / title).mkdir(exist_ok=True)
    fake = make_fake_psmux(tmp_path, pane=pane, live=live)
    monkeypatch.setattr("magent.psmux.find_psmux", lambda: fake.path)
    cfg = tmp_config(
        {
            "version": 4,
            "projects": [
                {"path": str(tmp_path / "caramel"), "group": "WORK"},
                {"path": str(tmp_path / "upup"), "tool": "codex"},
            ],
        }
    )
    return fake, cfg


def _refuse_typed_load(monkeypatch):
    """Make the typed loader fail loudly: the test proves it is not reached."""

    def _boom(_path):
        raise AssertionError("load_config called")

    monkeypatch.setattr(config, "load_config", _boom)


def _probe_targets(fake) -> list[str]:
    return [c[c.index("-t") + 1] for c in fake.calls() if "has-session" in c]


def _capture_targets(fake) -> list[str]:
    return [c[c.index("-t") + 1] for c in fake.calls() if "capture-pane" in c]


class TestSessionRowShape:
    def test_fields_are_the_spec_list_in_wire_order(self):
        assert tuple(f.name for f in dataclasses.fields(fleetview.SessionRow)) == (
            SPEC_FIELDS
        )

    def test_rows_are_frozen(self, tmp_config, tmp_path, monkeypatch):
        _fake, cfg = _fleet(tmp_config, tmp_path, monkeypatch, live=["caramel"])
        row = fleetview.rows(cfg, include_pane=False)[0]
        with pytest.raises(dataclasses.FrozenInstanceError):
            row.live = False

    def test_to_dict_is_json_serialisable_with_every_field(
        self, tmp_config, tmp_path, monkeypatch
    ):
        _fake, cfg = _fleet(tmp_config, tmp_path, monkeypatch, live=["caramel"])
        wire = fleetview.rows(cfg, include_pane=False)[0].to_dict()
        assert tuple(wire) == SPEC_FIELDS
        assert json.loads(json.dumps(wire)) == wire


class TestLocalRows:
    def test_live_row_carries_pane_state_and_footer(
        self, tmp_config, tmp_path, monkeypatch
    ):
        _fake, cfg = _fleet(
            tmp_config,
            tmp_path,
            monkeypatch,
            live=["caramel"],
            pane=f"working on it\nFable 5.1 {MID} high",
        )

        caramel, upup = fleetview.rows(cfg)

        assert caramel.session == "caramel"
        assert caramel.live is True
        assert caramel.pane_state == "idle"
        assert caramel.pane_state_ts is not None
        assert (caramel.model, caramel.effort) == ("Fable 5.1", "high")
        assert caramel.group == "WORK"
        assert caramel.tool == "claude"
        assert caramel.node is None
        assert caramel.node_state is None
        assert upup.live is False
        assert upup.pane_state == "dead"
        assert upup.tool == "codex"

    def test_include_pane_false_reads_no_pane(self, tmp_config, tmp_path, monkeypatch):
        fake, cfg = _fleet(tmp_config, tmp_path, monkeypatch, live=["caramel"])

        caramel, _upup = fleetview.rows(cfg, include_pane=False)

        assert caramel.pane_state is None
        assert not [c for c in fake.calls() if "capture-pane" in c]

    def test_a_live_pane_that_does_not_answer_reads_timeout(
        self, tmp_config, tmp_path, monkeypatch
    ):
        fake, cfg = _fleet(
            tmp_config, tmp_path, monkeypatch, live=["caramel"], pane="x"
        )
        fake.set_capture_delay(1.5)
        monkeypatch.setattr(psmux, "CAPTURE_PANE_TIMEOUT_S", 0.3)

        caramel = fleetview.rows(cfg, hooks=False)[0]

        assert caramel.live is True
        assert caramel.pane_state == "timeout"
        assert (caramel.model, caramel.effort) == (None, None)

    def test_no_psmux_means_nothing_is_live(self, tmp_config, tmp_path, monkeypatch):
        _fake, cfg = _fleet(tmp_config, tmp_path, monkeypatch, live=["caramel"])
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: None)

        assert [r.pane_state for r in fleetview.rows(cfg)] == ["dead", "dead"]

    def test_no_config_file_is_no_rows(self, tmp_path):
        assert fleetview.rows(str(tmp_path / "absent.json")) == []

    def test_the_probe_timeout_reaches_the_liveness_sweep(
        self, tmp_config, tmp_path, monkeypatch
    ):
        _fake, cfg = _fleet(tmp_config, tmp_path, monkeypatch, live=["caramel"])
        seen: list[dict[str, object]] = []

        def _live(names, psmux=None, *, timeout=None, retries=1):
            seen.append({"names": list(names), "timeout": timeout})
            return ["caramel"]

        monkeypatch.setattr(psmux, "live_sessions", _live)

        rows = fleetview.rows(cfg, include_pane=False, probe_timeout_s=5.0)

        assert [r.live for r in rows] == [True, False]
        assert seen == [{"names": ["caramel", "upup"], "timeout": 5.0}]

    def test_the_default_probe_is_unbounded_like_status(
        self, tmp_config, tmp_path, monkeypatch
    ):
        _fake, cfg = _fleet(tmp_config, tmp_path, monkeypatch, live=["caramel"])
        seen: list[tuple[float | None, int]] = []

        def _live(names, psmux=None, *, timeout=None, retries=1):
            seen.append((timeout, retries))
            return []

        monkeypatch.setattr(psmux, "live_sessions", _live)

        fleetview.rows(cfg, include_pane=False)

        assert seen == [(None, 1)]

    def test_probe_retries_reach_the_liveness_sweep(
        self, tmp_config, tmp_path, monkeypatch
    ):
        _fake, cfg = _fleet(tmp_config, tmp_path, monkeypatch, live=["caramel"])
        seen: list[int] = []

        def _live(names, psmux=None, *, timeout=None, retries=1):
            seen.append(retries)
            return []

        monkeypatch.setattr(psmux, "live_sessions", _live)

        fleetview.rows(cfg, include_pane=False, probe_retries=0)
        fleetview.row_for(cfg, "caramel", include_pane=False, probe_retries=3)

        assert seen == [0, 3]

    def test_the_raw_config_is_parsed_once_per_build(
        self, tmp_config, tmp_path, monkeypatch
    ):
        _fake, cfg = _fleet(tmp_config, tmp_path, monkeypatch, live=["caramel"])
        real_loads = json.loads
        parses: list[int] = []

        def _counting_loads(text, *a, **kw):
            parses.append(1)
            return real_loads(text, *a, **kw)

        monkeypatch.setattr(json, "loads", _counting_loads)

        rows = fleetview.rows(cfg, include_pane=False, hooks=False)

        assert [r.session for r in rows] == ["caramel", "upup"]
        assert len(parses) == 1


class TestRowFor:
    def test_matches_the_socket_id_exactly(self, tmp_config, tmp_path, monkeypatch):
        _fake, cfg = _fleet(tmp_config, tmp_path, monkeypatch, live=["caramel"])

        assert fleetview.row_for(cfg, "caramel", include_pane=False) is not None
        assert fleetview.row_for(cfg, "cara", include_pane=False) is None

    def test_reads_one_pane_and_probes_one_session(
        self, tmp_config, tmp_path, monkeypatch
    ):
        fake, cfg = _fleet(
            tmp_config,
            tmp_path,
            monkeypatch,
            live=["caramel", "upup"],
            pane=f"x\nFable 5.1 {MID} high",
        )

        row = fleetview.row_for(cfg, "upup")

        assert row is not None
        assert (row.session, row.live, row.pane_state) == ("upup", True, "idle")
        assert _capture_targets(fake) == ["upup"]
        assert _probe_targets(fake) == ["upup"]

    def test_bounds_the_liveness_probe(self, tmp_config, tmp_path, monkeypatch):
        _fake, cfg = _fleet(tmp_config, tmp_path, monkeypatch, live=["caramel"])
        seen: list[tuple[list[str], float | None]] = []

        def _live(names, psmux=None, *, timeout=None, retries=1):
            seen.append((list(names), timeout))
            return list(names)

        monkeypatch.setattr(psmux, "live_sessions", _live)

        row = fleetview.row_for(cfg, "caramel", include_pane=False, probe_timeout_s=2.5)

        assert row is not None and row.live is True
        assert seen == [(["caramel"], 2.5)]

    def test_hooks_false_never_loads_the_typed_config(
        self, tmp_config, tmp_path, monkeypatch
    ):
        _fake, cfg = _fleet(tmp_config, tmp_path, monkeypatch, live=["caramel"])
        _refuse_typed_load(monkeypatch)

        row = fleetview.row_for(cfg, "caramel", include_pane=False, hooks=False)

        assert row is not None and row.hook_state is None


class TestConfigLoads:
    """The typed config (``load_config``: validation + a stderr warning per
    load) is read only when something needs it: never on the legacy path, and
    never when the caller already holds the engine or the config."""

    def test_hooks_false_never_loads_the_typed_config(
        self, tmp_config, tmp_path, monkeypatch
    ):
        _fake, cfg = _fleet(tmp_config, tmp_path, monkeypatch, live=["caramel"])

        def _boom(_path):
            raise AssertionError("typed load on the legacy path")

        monkeypatch.setattr(fleetview, "load_typed", _boom)

        rows = fleetview.rows(cfg, include_pane=False, hooks=False)

        assert [r.hook_state for r in rows] == [None, None]

    def test_a_supplied_engine_skips_the_typed_load(
        self, tmp_config, tmp_path, monkeypatch
    ):
        _fake, cfg = _fleet(tmp_config, tmp_path, monkeypatch, live=["caramel"])
        agent_state.write_state(str(tmp_path / "caramel"), agent_state.DONE)
        typed = fleetview.load_typed(cfg)
        assert typed is not None
        engine = fleetview.engine_from_config(typed)
        _refuse_typed_load(monkeypatch)

        caramel, _upup = fleetview.rows(cfg, include_pane=False, engine=engine)

        assert caramel.hook_state == "done"

    def test_a_supplied_config_skips_the_typed_load(
        self, tmp_config, tmp_path, monkeypatch
    ):
        _fake, cfg = _fleet(tmp_config, tmp_path, monkeypatch, live=["caramel"])
        agent_state.write_state(str(tmp_path / "caramel"), agent_state.DONE)
        typed = fleetview.load_typed(cfg)
        _refuse_typed_load(monkeypatch)

        caramel, _upup = fleetview.rows(cfg, include_pane=False, cfg=typed)

        assert caramel.hook_state == "done"

    def test_engine_from_config_has_no_staleness_override(self):
        import inspect

        params = inspect.signature(fleetview.engine_from_config).parameters
        assert "staleness" not in params

    def test_an_invalid_config_is_a_value_error_when_it_is_needed(
        self, tmp_config, tmp_path, monkeypatch
    ):
        fake = make_fake_psmux(tmp_path, live=[])
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: fake.path)
        cfg = tmp_config({"version": 4, "projects": {}})

        with pytest.raises(ValueError):
            fleetview.rows(cfg, include_pane=False)
        # The legacy path never validates: an unreadable project list is
        # simply no sessions, as `sessions --json` has always answered.
        assert fleetview.rows(cfg, include_pane=False, hooks=False) == []


class TestHookJoin:
    def test_hook_state_and_session_id_come_from_the_store(
        self, tmp_config, tmp_path, monkeypatch
    ):
        _fake, cfg = _fleet(tmp_config, tmp_path, monkeypatch, live=["caramel"])
        agent_state.write_state(
            str(tmp_path / "caramel"), agent_state.NEEDS_INPUT, session_id="s-123"
        )

        caramel, upup = fleetview.rows(cfg, include_pane=False)

        assert caramel.hook_state == "needs-input"
        assert caramel.session_id == "s-123"
        assert caramel.hook_state_stale is False
        assert caramel.hook_state_age_s is not None
        assert upup.hook_state is None

    def test_a_stale_working_record_reads_idle_and_says_so(
        self, tmp_config, tmp_path, monkeypatch
    ):
        _fake, cfg = _fleet(tmp_config, tmp_path, monkeypatch, live=["caramel"])
        written_at = time.time() - 2 * 3600  # past the 1800s working window
        with monkeypatch.context() as m:
            m.setattr(agent_state.time, "time", lambda: written_at)
            agent_state.write_state(str(tmp_path / "caramel"), agent_state.WORKING)

        caramel = fleetview.rows(cfg, include_pane=False)[0]

        assert caramel.hook_state == "idle"
        assert caramel.hook_state_stale is True

    def test_the_record_is_read_once_through_the_engine(
        self, tmp_config, tmp_path, monkeypatch
    ):
        # The engine's view carries everything a row needs (raw state for
        # the stale flag, the session id): a second owner re-reading the
        # record would race the first and double the store reads per tick.
        _fake, cfg = _fleet(tmp_config, tmp_path, monkeypatch, live=["caramel"])
        agent_state.write_state(
            str(tmp_path / "caramel"), agent_state.DONE, session_id="s-9"
        )

        def _no_reread(_cwd):
            raise AssertionError("record re-read outside the engine")

        monkeypatch.setattr(agent_state, "read_record", _no_reread)

        caramel = fleetview.rows(cfg, include_pane=False)[0]

        assert (caramel.hook_state, caramel.session_id) == ("done", "s-9")


class TestSessionView:
    def test_carries_the_raw_state_and_session_id(self):
        now = 10_000.0
        engine = attention.AttentionEngine(now=lambda: now, staleness={"working": 5})
        record = {
            "state": "working",
            "ts": now - 60,
            "cwd": "/w/api",
            "session_id": "s",
        }

        view = engine._view(record, now)

        assert view is not None
        assert (view.state, view.raw_state, view.session_id) == ("idle", "working", "s")

    def test_a_missing_or_blank_session_id_is_none(self):
        engine = attention.AttentionEngine(now=lambda: 1.0)
        blank = {"state": "done", "ts": 1.0, "cwd": "/w/a", "session_id": ""}
        absent = {"state": "done", "ts": 1.0, "cwd": "/w/b"}

        views = [engine._view(blank, 1.0), engine._view(absent, 1.0)]

        assert [v.session_id for v in views if v is not None] == [None, None]


class TestNodeRows:
    """Pool-node rows come from the sync daemon's files (never a dial here):
    the node map for where it runs, ``sessions.json`` for its state, the
    mirrored state store for its hook state."""

    def _node_fleet(self, tmp_config, tmp_path, monkeypatch, *, ts, live_local=()):
        from magent import nodes
        from magent.nodes import NodeMapEntry

        (tmp_path / "caramel").mkdir(exist_ok=True)
        fake = make_fake_psmux(tmp_path, live=list(live_local))
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: fake.path)
        monkeypatch.setattr(nodes, "NODES_DIR", tmp_path / "nodes")
        monkeypatch.setattr(nodes, "NODE_MAP_PATH", tmp_path / "node-map.json")
        nodes.write_json_atomic(
            nodes.sessions_path("second"), {"ts": ts, "sessions": ["api"]}
        )
        nodes.update_node_map(
            "api",
            NodeMapEntry(
                nick="second",
                sid="api",
                placed_ts=1.0,
                attached_existing=False,
                remote_root="~/magent/api",
                target="demo@box-second",
                cwd="/home/demo/magent/api",
            ),
        )
        cfg = tmp_config(
            {
                "version": 4,
                "projects": [
                    {"path": str(tmp_path / "caramel"), "title": "caramel"},
                    {
                        "path": str(tmp_path / "api"),
                        "title": "api",
                        "node": "second",
                        "group": "REMOTE",
                        "tool": "codex",
                    },
                ],
                "settings": {
                    "nodes": {"second": {"host": "box-second", "user": "demo"}}
                },
            }
        )
        return fake, cfg

    def _mirror_record(self, tmp_path, *, state: str, ts: float, session_id: str):
        from magent import nodes

        store = nodes.state_dir("second", "api")
        store.mkdir(parents=True, exist_ok=True)
        (store / "api.json").write_text(
            json.dumps(
                {
                    "state": state,
                    "ts": ts,
                    "cwd": "/home/demo/magent/api",
                    "session_id": session_id,
                }
            ),
            encoding="utf-8",
        )

    def test_a_live_node_row_carries_the_v1_fields(
        self, tmp_config, tmp_path, monkeypatch
    ):
        _fake, cfg = self._node_fleet(tmp_config, tmp_path, monkeypatch, ts=time.time())

        caramel, api = fleetview.rows(cfg, include_pane=False)

        assert caramel.node is None
        assert api.to_dict() == {
            "session": "api",
            "name": "api",
            "path": "~/magent/api",
            "cwd": "/home/demo/magent/api",
            "group": "REMOTE",
            "tool": "codex",
            "enabled": True,
            "node": "second",
            "live": True,
            "hook_state": None,
            "hook_state_ts": None,
            "hook_state_age_s": None,
            "hook_state_stale": False,
            "pane_state": None,
            "pane_state_ts": None,
            "node_state": "live",
            "model": None,
            "effort": None,
            "session_id": None,
        }

    def test_the_tool_falls_back_to_the_default_tool(
        self, tmp_config, tmp_path, monkeypatch
    ):
        _fake, cfg = self._node_fleet(tmp_config, tmp_path, monkeypatch, ts=time.time())
        raw = json.loads((tmp_path / "magent.config.json").read_text("utf-8"))
        del raw["projects"][1]["tool"]
        (tmp_path / "magent.config.json").write_text(json.dumps(raw), "utf-8")

        api = fleetview.rows(cfg, include_pane=False)[1]

        assert api.tool == "claude"

    def test_hook_state_joins_the_mirrored_store_and_flags_staleness(
        self, tmp_config, tmp_path, monkeypatch
    ):
        _fake, cfg = self._node_fleet(tmp_config, tmp_path, monkeypatch, ts=time.time())
        self._mirror_record(
            tmp_path, state="working", ts=time.time() - 2 * 3600, session_id="n-7"
        )

        api = fleetview.rows(cfg, include_pane=False)[1]

        assert api.hook_state == "idle"
        assert api.hook_state_stale is True
        assert api.session_id == "n-7"
        assert api.hook_state_age_s is not None and api.hook_state_age_s > 7000

    def test_a_fresh_mirrored_record_is_not_stale(
        self, tmp_config, tmp_path, monkeypatch
    ):
        _fake, cfg = self._node_fleet(tmp_config, tmp_path, monkeypatch, ts=time.time())
        self._mirror_record(
            tmp_path, state="needs-input", ts=time.time(), session_id="n-8"
        )

        api = fleetview.rows(cfg, include_pane=False)[1]

        assert (api.hook_state, api.hook_state_stale) == ("needs-input", False)

    def test_a_stale_node_is_live_none_in_the_legacy_row(
        self, tmp_config, tmp_path, monkeypatch
    ):
        _fake, cfg = self._node_fleet(tmp_config, tmp_path, monkeypatch, ts=0.0)

        api = fleetview.rows(cfg, include_pane=False, hooks=False)[1]

        assert (api.node_state, api.live) == ("stale", False)
        assert api.to_legacy() == {
            "name": "api",
            "cwd": "/home/demo/magent/api",
            "live": None,
            "state": "stale",
            "model": None,
            "effort": None,
            "node": "second",
        }

    def test_row_for_finds_a_node_session(self, tmp_config, tmp_path, monkeypatch):
        fake, cfg = self._node_fleet(
            tmp_config, tmp_path, monkeypatch, ts=time.time(), live_local=["caramel"]
        )

        row = fleetview.row_for(cfg, "api", include_pane=False)

        assert row is not None
        assert (row.node, row.node_state) == ("second", "live")
        # The local sweep was not asked about a session no local socket has.
        assert _probe_targets(fake) == []

    def test_a_local_target_skips_the_node_side_and_its_typed_load(
        self, tmp_config, tmp_path, monkeypatch
    ):
        # The config names a pool node, but the target is local: answered by
        # the local rows alone, so no typed load (the verbs' path) and no
        # node map read.
        fake, cfg = self._node_fleet(
            tmp_config, tmp_path, monkeypatch, ts=time.time(), live_local=["caramel"]
        )
        _refuse_typed_load(monkeypatch)

        row = fleetview.row_for(cfg, "caramel", include_pane=False, hooks=False)

        assert row is not None and (row.session, row.live) == ("caramel", True)
        assert _probe_targets(fake) == ["caramel"]

    def test_a_supplied_engine_still_loads_the_typed_config_for_node_rows(
        self, tmp_config, tmp_path, monkeypatch
    ):
        _fake, cfg = self._node_fleet(tmp_config, tmp_path, monkeypatch, ts=time.time())
        typed = fleetview.load_typed(cfg)
        assert typed is not None
        engine = fleetview.engine_from_config(typed)
        loads: list[str] = []
        real = config.load_config

        def _counting(path):
            loads.append(path)
            return real(path)

        monkeypatch.setattr(config, "load_config", _counting)

        rows = fleetview.rows(cfg, include_pane=False, engine=engine)

        assert [r.session for r in rows] == ["caramel", "api"]
        assert loads == [cfg]

    def test_a_supplied_config_builds_the_node_rows_from_it(
        self, tmp_config, tmp_path, monkeypatch
    ):
        # The poller's frozen config: node rows come from the object handed
        # in, not from the file, which may since have changed.
        import dataclasses as dc

        _fake, cfg = self._node_fleet(tmp_config, tmp_path, monkeypatch, ts=time.time())
        typed = fleetview.load_typed(cfg)
        assert typed is not None
        api = next(p for p in typed.projects if p.title == "api")
        frozen = dc.replace(
            typed,
            projects=[
                dc.replace(p, group="FROZEN") if p is api else p for p in typed.projects
            ],
        )
        _refuse_typed_load(monkeypatch)

        rows = fleetview.rows(cfg, include_pane=False, cfg=frozen)

        assert [(r.session, r.group) for r in rows] == [
            ("caramel", None),
            ("api", "FROZEN"),
        ]


class TestLegacyParity:
    """``sessions --json`` is ``[r.to_legacy() for r in rows(hooks=False,
    dial_nodes=True)]``. The golden rows (live, dead, timeout, node) are
    pinned against the real command in test_fleet_cmd.py (``TestSessionsJson``
    and ``TestTheFleetCommandsKnowNodeSessions``); this is the local-row
    shape alone."""

    def test_to_legacy_is_the_sessions_json_row(
        self, tmp_config, tmp_path, monkeypatch
    ):
        _fake, cfg = _fleet(
            tmp_config,
            tmp_path,
            monkeypatch,
            live=["caramel"],
            pane=f"x\nOpus 5 {MID} max",
        )

        legacy = [r.to_legacy() for r in fleetview.rows(cfg, hooks=False)]

        assert legacy == [
            {
                "name": "caramel",
                "cwd": str(tmp_path / "caramel"),
                "live": True,
                "state": "idle",
                "model": "Opus 5",
                "effort": "max",
                "node": None,
            },
            {
                "name": "upup",
                "cwd": str(tmp_path / "upup"),
                "live": False,
                "state": "dead",
                "model": None,
                "effort": None,
                "node": None,
            },
        ]
