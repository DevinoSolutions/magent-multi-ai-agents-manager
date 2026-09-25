"""Agent-state store tests — schema contract, round-trip, retention, and
normalization.

The schema contract (``TestSchemaContract``) pins the on-disk record shape so
that changes to ``write_state`` are detected by the gate before they can
silently break writers/readers out of lockstep (the in-repo writer is
``state_hook.py``; external writers may exist). If the contract test fails
after a deliberate schema change:

1. Bump ``RECORD_VERSION`` in ``agent_state.py``.
2. Update ``EXPECTED_KEYS`` and the assertions below.
3. Update any external writer in lockstep.
"""

from __future__ import annotations

import json
import logging
import pathlib
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
    """Pin the on-disk record shape. Writers (state_hook.py in-repo, any
    external adapter) produce records with exactly these keys and value
    types."""

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


def _mirror(tmp_path):
    """A node mirror laid out like ``~/.magent/nodes/<nick>/state``. The
    autouse fixture makes ``STATE_DIR`` == ``tmp_path``, so this directory is
    deliberately NOT the local store: a ``read_store`` that ignored ``root``
    and read ``STATE_DIR`` would return the wrong records here."""
    mirror = tmp_path / "nodes" / "box" / "state"
    mirror.mkdir(parents=True)
    return mirror


def _put(root, name, rec):
    path = root / name
    path.write_text(json.dumps(rec), encoding="utf-8")
    return path


class TestReadStore:
    def test_any_directory_reads_as_a_store(self, tmp_path):
        mirror = _mirror(tmp_path)
        rec = {
            "state": "done",
            "ts": 1.0,
            "cwd": "/home/amin/magent/api",
            "session_id": "s",
        }
        _put(mirror, "a.json", rec)
        assert agent_state.read_store(mirror) == [rec]

    def test_a_mirror_and_the_local_store_never_mix(self, tmp_path):
        """The cross-check: each store answers with its own records only."""
        mirror = _mirror(tmp_path)
        agent_state.write_state("/home/amin/local", "working", "local-sid")
        remote = {
            "state": "needs-input",
            "ts": time.time(),
            "cwd": "/home/amin/remote",
            "session_id": "node-sid",
        }
        _put(mirror, "r.json", remote)
        assert agent_state.read_store(mirror) == [remote]
        assert [r["session_id"] for r in agent_state.all_states()] == ["local-sid"]

    def test_a_missing_directory_is_an_empty_store(self, tmp_path):
        # The local store beside it is NOT empty, so reading the wrong
        # directory cannot pass as "empty".
        agent_state.write_state("/home/amin/local", "working", "local-sid")
        assert agent_state.read_store(tmp_path / "nodes" / "gone" / "state") == []

    def test_an_unreadable_directory_is_an_empty_store_logged_once(
        self, tmp_path, monkeypatch, caplog
    ):
        mirror = _mirror(tmp_path)
        real_glob = pathlib.Path.glob

        def glob(self, pattern):
            if self == mirror:
                raise PermissionError("denied")
            return real_glob(self, pattern)

        monkeypatch.setattr(pathlib.Path, "glob", glob)
        with caplog.at_level(logging.WARNING, logger="magent.attention"):
            assert agent_state.read_store(mirror) == []
            assert agent_state.read_store(mirror) == []
        named = [r for r in caplog.records if str(mirror) in r.getMessage()]
        assert len(named) == 1
        assert "unreadable" in named[0].getMessage()

    def test_a_strict_read_of_a_path_that_is_not_a_directory_raises(self, tmp_path):
        # Path.glob swallows this itself (a file globs to []), so strict must
        # probe the directory rather than trust the glob to raise.
        not_a_dir = tmp_path / "state"
        not_a_dir.write_text("x", encoding="utf-8")
        assert agent_state.read_store(not_a_dir) == []
        with pytest.raises(NotADirectoryError):
            agent_state.read_store(not_a_dir, strict=True)

    def test_a_strict_read_reraises_a_denied_listing(self, tmp_path, monkeypatch):
        mirror = _mirror(tmp_path)
        real_glob = pathlib.Path.glob

        def glob(self, pattern):
            if self == mirror:
                raise PermissionError("denied")
            return real_glob(self, pattern)

        monkeypatch.setattr(pathlib.Path, "glob", glob)
        with pytest.raises(PermissionError):
            agent_state.read_store(mirror, strict=True)

    def test_a_strict_read_of_a_missing_directory_is_still_an_empty_store(
        self, tmp_path
    ):
        # A node whose mirror has not been pulled yet is empty, not broken.
        gone = tmp_path / "nodes" / "gone" / "state"
        assert agent_state.read_store(gone, strict=True) == []

    def test_a_strict_read_still_skips_one_torn_file(self, tmp_path):
        mirror = _mirror(tmp_path)
        good = {"state": "done", "ts": 1.0, "cwd": "/w/a", "session_id": "s"}
        _put(mirror, "a.json", good)
        (mirror / "b.json").write_text("{torn", encoding="utf-8")
        assert agent_state.read_store(mirror, strict=True) == [good]

    def test_unusable_files_are_skipped(self, tmp_path, caplog):
        mirror = _mirror(tmp_path)
        (mirror / "a.json").write_text("{torn", encoding="utf-8")
        (mirror / "b.json").write_text("[1, 2]", encoding="utf-8")
        (mirror / "c.tmp").write_text("{}", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="magent.attention"):
            assert agent_state.read_store(mirror) == []
        # The warning names the MIRROR's file, not a bare file name that a
        # local record with the same cwd hash would share.
        messages = [r.getMessage() for r in caplog.records]
        assert any(str(mirror / "a.json") in m for m in messages)
        assert any(str(mirror / "b.json") in m for m in messages)

    def test_reading_a_store_never_sweeps_but_all_states_still_does(self, tmp_path):
        """A node mirror is the node's to age: a record swept here would come
        straight back on the next pull. This PC's own store keeps its sweep,
        and that sweep never reaches into a mirror."""
        mirror = _mirror(tmp_path)
        remote_ancient = {
            "state": "done",
            "ts": 1.0,
            "cwd": "/w/remote-old",
            "session_id": "node-sid",
        }
        remote_path = _put(mirror, "r.json", remote_ancient)
        local_ancient = {
            "state": "done",
            "ts": 1.0,
            "cwd": "/w/old",
            "session_id": None,
        }
        local_path = agent_state._path_for("/w/old")
        local_path.write_text(json.dumps(local_ancient), encoding="utf-8")

        assert agent_state.read_store(mirror) == [remote_ancient]
        assert remote_path.exists()
        assert agent_state.read_store(agent_state.STATE_DIR) == [local_ancient]
        assert local_path.exists()

        assert agent_state.all_states() == []
        assert not local_path.exists()
        assert remote_path.exists()
        assert agent_state.read_store(mirror) == [remote_ancient]
