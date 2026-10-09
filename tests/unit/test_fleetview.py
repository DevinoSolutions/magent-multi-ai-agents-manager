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

from magent import agent_state, cli, fleetview, psmux
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

    def test_no_psmux_means_nothing_is_live(self, tmp_config, tmp_path, monkeypatch):
        _fake, cfg = _fleet(tmp_config, tmp_path, monkeypatch, live=["caramel"])
        monkeypatch.setattr("magent.psmux.find_psmux", lambda: None)

        assert [r.pane_state for r in fleetview.rows(cfg)] == ["dead", "dead"]

    def test_no_config_file_is_no_rows(self, tmp_path):
        assert fleetview.rows(str(tmp_path / "absent.json")) == []

    def test_row_for_matches_the_socket_id_exactly(
        self, tmp_config, tmp_path, monkeypatch
    ):
        _fake, cfg = _fleet(tmp_config, tmp_path, monkeypatch, live=["caramel"])

        assert fleetview.row_for(cfg, "caramel", include_pane=False) is not None
        assert fleetview.row_for(cfg, "cara", include_pane=False) is None


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

    def test_hooks_false_never_loads_the_typed_config(
        self, tmp_config, tmp_path, monkeypatch
    ):
        _fake, cfg = _fleet(tmp_config, tmp_path, monkeypatch, live=["caramel"])

        def _boom(_path):
            raise AssertionError("typed load on the legacy path")

        monkeypatch.setattr(fleetview, "load_typed", _boom)

        rows = fleetview.rows(cfg, include_pane=False, hooks=False)

        assert [r.hook_state for r in rows] == [None, None]


class TestLegacyParity:
    """`sessions --json` is produced by ``to_legacy``: the array the command
    printed before the lift and the rows' legacy form must be equal."""

    def test_to_legacy_equals_the_sessions_json_array(
        self, runner, tmp_config, tmp_path, monkeypatch
    ):
        _fake, cfg = _fleet(
            tmp_config,
            tmp_path,
            monkeypatch,
            live=["caramel"],
            pane=f"x\nOpus 5 {MID} max",
        )

        result = runner.invoke(cli.main, ["--config", cfg, "sessions", "--json"])
        legacy = [r.to_legacy() for r in fleetview.rows(cfg, hooks=False)]

        assert result.exit_code == 0
        assert json.loads(result.stdout) == legacy
