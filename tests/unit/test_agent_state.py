"""Agent-state store tests — schema contract, round-trip, retention, and
normalization.

The schema contract (``TestSchemaContract``) pins the on-disk record shape so
that changes to ``write_state`` are detected by the gate before they can
silently break out-of-repo writers (Claude Code hooks via ``state-sink.mjs``,
Codex notify). If the contract test fails after a deliberate schema change:

1. Bump ``RECORD_VERSION`` in ``agent_state.py``.
2. Update ``EXPECTED_KEYS`` and the assertions below.
3. Update every external writer in lockstep — ``state-sink.mjs`` in the Claude
   Code hook repo, and any Codex notify adapter.
"""

from __future__ import annotations

import json
import time

import pytest

from magent import agent_state


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(agent_state, "_swept_this_process", False)
    monkeypatch.setattr(agent_state, "_warned_files", set())


EXPECTED_KEYS = {"state", "ts", "cwd", "session_id"}
VALID_STATES = {"working", "done", "needs-input", "error", "idle"}


class TestSchemaContract:
    """Pin the on-disk record shape. External writers (state-sink.mjs, Codex
    notify) produce records with exactly these keys and value types."""

    def test_record_keys_are_exact(self):
        agent_state.write_state("/projects/foo", "working", session_id="abc")
        records = list(agent_state.STATE_DIR.glob("*.json"))
        assert len(records) == 1
        d = json.loads(records[0].read_text(encoding="utf-8"))
        assert set(d.keys()) == EXPECTED_KEYS

    def test_record_value_types(self):
        agent_state.write_state("/projects/foo", "done", session_id="s1")
        d = json.loads(
            next(agent_state.STATE_DIR.glob("*.json")).read_text(encoding="utf-8")
        )
        assert isinstance(d["state"], str) and d["state"] in VALID_STATES
        assert isinstance(d["ts"], float)
        assert isinstance(d["cwd"], str) and len(d["cwd"]) > 0
        assert isinstance(d["session_id"], str)

    def test_session_id_nullable(self):
        agent_state.write_state("/projects/bar", "idle")
        d = json.loads(
            next(agent_state.STATE_DIR.glob("*.json")).read_text(encoding="utf-8")
        )
        assert d["session_id"] is None

    def test_ts_is_epoch_seconds(self):
        before = time.time()
        agent_state.write_state("/projects/baz", "working")
        after = time.time()
        d = json.loads(
            next(agent_state.STATE_DIR.glob("*.json")).read_text(encoding="utf-8")
        )
        assert before <= d["ts"] <= after

    def test_valid_states_match_module_constants(self):
        assert VALID_STATES == agent_state._VALID


class TestRoundTrip:
    def test_write_then_read(self):
        agent_state.write_state("/a/b", "done", session_id="x")
        rec = agent_state.state_for("/a/b")
        assert rec is not None
        assert rec["state"] == "done"
        assert rec["session_id"] == "x"

    def test_overwrite_replaces(self):
        agent_state.write_state("/a/b", "working")
        agent_state.write_state("/a/b", "error")
        rec = agent_state.state_for("/a/b")
        assert rec is not None
        assert rec["state"] == "error"

    def test_clear_removes(self):
        agent_state.write_state("/a/b", "done")
        agent_state.clear_state("/a/b")
        assert agent_state.state_for("/a/b") is None

    def test_invalid_state_ignored(self):
        agent_state.write_state("/a/b", "bogus")
        assert agent_state.state_for("/a/b") is None

    def test_empty_cwd_ignored(self):
        agent_state.write_state("", "done")
        assert list(agent_state.STATE_DIR.glob("*.json")) == []


class TestNormalization:
    def test_backslash_to_forward(self):
        assert (
            agent_state.norm_cwd("C:\\Users\\foo") == "c:/users/foo"
            or agent_state.norm_cwd("C:\\Users\\foo") == "C:/Users/foo"
        )

    def test_trailing_slash_stripped(self):
        n = agent_state.norm_cwd("/projects/foo/")
        assert not n.endswith("/")

    def test_same_key_for_same_path(self):
        agent_state.write_state("/projects/foo", "working")
        agent_state.write_state("/projects/foo/", "done")
        assert len(list(agent_state.STATE_DIR.glob("*.json"))) == 1

    def test_different_paths_different_keys(self):
        agent_state.write_state("/a", "working")
        agent_state.write_state("/b", "working")
        assert len(list(agent_state.STATE_DIR.glob("*.json"))) == 2


class TestRetention:
    def test_sweep_removes_old_records(self):
        now = 1_000_000.0
        agent_state.write_state("/old", "done")
        p = next(agent_state.STATE_DIR.glob("*.json"))
        d = json.loads(p.read_text(encoding="utf-8"))
        d["ts"] = now - agent_state.STATE_TTL_S - 1
        p.write_text(json.dumps(d), encoding="utf-8")

        removed = agent_state.sweep_stale(now=now)
        assert removed == 1
        assert list(agent_state.STATE_DIR.glob("*.json")) == []

    def test_sweep_keeps_fresh_records(self):
        agent_state.write_state("/fresh", "working")
        removed = agent_state.sweep_stale(now=time.time())
        assert removed == 0
        assert len(list(agent_state.STATE_DIR.glob("*.json"))) == 1

    def test_maybe_sweep_once_per_process(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            agent_state, "sweep_stale", lambda **kw: calls.append(1) or 0
        )
        monkeypatch.setattr(agent_state, "_swept_this_process", False)
        agent_state.maybe_sweep_stale()
        agent_state.maybe_sweep_stale()
        assert len(calls) == 1


class TestAllStates:
    def test_corrupt_file_skipped(self):
        agent_state.write_state("/good", "done")
        bad = agent_state.STATE_DIR / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        states = agent_state.all_states()
        assert len(states) == 1
        assert states[0]["state"] == "done"

    def test_non_object_file_skipped(self):
        agent_state.write_state("/good", "working")
        bad = agent_state.STATE_DIR / "arr.json"
        bad.write_text("[1,2,3]", encoding="utf-8")
        states = agent_state.all_states()
        assert len(states) == 1

    def test_state_for_max_age(self, monkeypatch):
        agent_state.write_state("/a", "done")
        p = next(agent_state.STATE_DIR.glob("*.json"))
        d = json.loads(p.read_text(encoding="utf-8"))
        d["ts"] = time.time() - 3600
        p.write_text(json.dumps(d), encoding="utf-8")
        assert agent_state.state_for("/a", max_age=60) is None
        assert agent_state.state_for("/a", max_age=7200) is not None
