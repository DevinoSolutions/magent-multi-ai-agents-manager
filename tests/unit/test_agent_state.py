"""Agent-state store tests — schema contract, round-trip, retention, and
normalization.

The schema contract (``TestSchemaContract``) pins the on-disk record shape so
that changes to ``write_state`` are detected by the gate before they can
silently break writers/readers out of lockstep (the in-repo writer is
``state_hook.py``; external writers may exist). If the contract test fails
after a deliberate schema change:

1. Bump ``RECORD_VERSION`` in ``agent_state.py``.
2. Update ``EXPECTED_KEYS`` and the assertions below.
3. Update any external writer in lockstep. A state that ONLY magent writes
   (like ``parked``) is additive: v1 records stay valid, v1 writers need no
   change.
"""

from __future__ import annotations

import json
import logging
import pathlib
import time
from typing import TYPE_CHECKING

import pytest

from magent import agent_state
from tests.unit._worker import on_a_worker_thread

if TYPE_CHECKING:
    from pathlib import Path

# Past json.loads' nesting depth on every supported Python, on any thread.
_DEPTH = 200_000


def _write_deep(cwd: str) -> Path:
    """A real record for ``cwd`` plus one value nested past the parser's depth:
    ``json.loads`` raises RecursionError on it, not ValueError."""
    agent_state.write_state(cwd, "done", session_id="x")
    p = agent_state._path_for(cwd)
    text = p.read_text(encoding="utf-8").rstrip()
    p.write_text(
        text[:-1] + ', "x": ' + "[" * _DEPTH + "]" * _DEPTH + "}", encoding="utf-8"
    )
    return p


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(agent_state, "_swept_this_process", False)
    monkeypatch.setattr(agent_state, "_warned_files", set())


EXPECTED_KEYS = {"state", "ts", "cwd", "session_id"}
VALID_STATES = {"working", "done", "needs-input", "error", "idle", "parked"}


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


class TestParkedStateContract:
    def test_record_version_is_2(self):
        assert agent_state.RECORD_VERSION == 2

    def test_a_parked_record_has_the_v1_keys_and_types(self):
        agent_state.write_state("/projects/foo", "parked", "sid-1")
        d = json.loads(
            next(agent_state.STATE_DIR.glob("*.json")).read_text(encoding="utf-8")
        )
        assert set(d.keys()) == EXPECTED_KEYS
        assert d["state"] == "parked"
        assert isinstance(d["ts"], float)
        assert isinstance(d["cwd"], str) and d["cwd"]
        assert d["session_id"] == "sid-1"

    def test_a_v1_record_still_reads(self, monkeypatch):
        # A record in the exact bytes a v1 writer produces, placed by the same
        # keying write_state uses, reads back unchanged. Disable the TTL sweep
        # so the injected ts is never aged out (monkeypatch, so the flag is
        # restored for the next test rather than left flipped process-wide).
        monkeypatch.setattr(agent_state, "_swept_this_process", True)
        cwd = "/projects/foo"
        agent_state.STATE_DIR.mkdir(parents=True, exist_ok=True)
        agent_state._path_for(cwd).write_text(
            '{"state": "done", "ts": 1790000000.25, '
            '"cwd": "/projects/foo", "session_id": "sid-1"}',
            encoding="utf-8",
        )
        rec = agent_state.state_for(cwd)
        assert rec == {
            "state": "done",
            "ts": 1790000000.25,
            "cwd": "/projects/foo",
            "session_id": "sid-1",
        }
        assert any(r["state"] == "done" for r in agent_state.all_states())

    def test_a_v1_write_replaces_parked(self):
        agent_state.write_state("/projects/foo", "parked", "old")
        agent_state.write_state("/projects/foo", "idle", "new-sid")
        rec = agent_state.state_for("/projects/foo")
        assert rec is not None
        assert rec["state"] == "idle" and rec["session_id"] == "new-sid"


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
            "cwd": "/home/demo/magent/api",
            "session_id": "s",
        }
        _put(mirror, "a.json", rec)
        assert agent_state.read_store(mirror) == [rec]

    def test_a_mirror_and_the_local_store_never_mix(self, tmp_path):
        """The cross-check: each store answers with its own records only."""
        mirror = _mirror(tmp_path)
        agent_state.write_state("/home/demo/local", "working", "local-sid")
        remote = {
            "state": "needs-input",
            "ts": time.time(),
            "cwd": "/home/demo/remote",
            "session_id": "node-sid",
        }
        _put(mirror, "r.json", remote)
        assert agent_state.read_store(mirror) == [remote]
        assert [r["session_id"] for r in agent_state.all_states()] == ["local-sid"]

    def test_a_missing_directory_is_an_empty_store(self, tmp_path):
        # The local store beside it is NOT empty, so reading the wrong
        # directory cannot pass as "empty".
        agent_state.write_state("/home/demo/local", "working", "local-sid")
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

    # Past the bound, whatever json.loads would do with it on this stack:
    # raise (either class), or -- one level past it, in a record otherwise
    # good -- parse it whole.
    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("[" * 200_000, id="200k-open"),
            pytest.param(
                '{"state": "done", "ts": 1.0, "cwd": "/w/a", "session_id": "d", '
                '"junk": ' + "[" * 64 + "]" * 64 + "}",
                id="65-deep-in-a-good-record",
            ),
        ],
    )
    def test_a_record_nested_too_deep_to_parse_is_skipped_not_raised(
        self, tmp_path, caplog, text
    ):
        """A mirror's files are the node's (a pull member may be 64 MiB). A
        record nested past the bound goes down the bad-record path like any
        torn file, in both reads, the same on every stack, and never takes
        the valid record beside it along."""
        mirror = _mirror(tmp_path)
        (mirror / "a.json").write_text(text, encoding="utf-8")
        good = {"state": "done", "ts": 1.0, "cwd": "/w/b", "session_id": "s"}
        _put(mirror, "b.json", good)
        with caplog.at_level(logging.WARNING, logger="magent.attention"):
            assert agent_state.read_store(mirror) == [good]
            assert agent_state.read_store(mirror, strict=True) == [good]
        named = [
            r.getMessage()
            for r in caplog.records
            if str(mirror / "a.json") in r.getMessage()
        ]
        assert len(named) == 1
        assert named[0].endswith("unreadable (nested deeper than 64 levels)")

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


class TestARecordNestedPastTheParsersDepth:
    """``json.loads`` raises RecursionError, not ValueError, on a value nested
    past its depth. Every reader treats that file exactly like a corrupt one
    -- skipped, never a raise -- and ``read_record`` names it unreadable, never
    absent. Each read runs on a worker thread, where serve's reaper and the
    attention daemon read."""

    def test_read_record_says_unreadable(self):
        _write_deep("/deep")
        read = on_a_worker_thread(lambda: agent_state.read_record("/deep"))
        assert read == (None, True)

    def test_state_for_reads_none(self):
        _write_deep("/deep")
        assert on_a_worker_thread(lambda: agent_state.state_for("/deep")) is None

    def test_all_states_skips_it_names_it_and_keeps_the_rest(self, caplog):
        agent_state.write_state("/good", "working")
        p = _write_deep("/deep")
        with caplog.at_level("WARNING", logger="magent.attention"):
            states = on_a_worker_thread(agent_state.all_states)
        assert [s["state"] for s in states] == ["working"]
        assert [r.getMessage() for r in caplog.records if p.name in r.getMessage()]

    def test_sweep_stale_leaves_it_in_place(self):
        p = _write_deep("/deep")
        later = time.time() + 10 * agent_state.STATE_TTL_S
        assert on_a_worker_thread(lambda: agent_state.sweep_stale(now=later)) == 0
        assert p.exists()


class TestReadRecord:
    """The raw read the reaper acts on, as ``(record, unreadable)``: a record,
    NO file (absent), or a file that is there but unusable (unreadable) --
    unknown, which is not absent."""

    def test_a_usable_record(self):
        agent_state.write_state("/a", "done", session_id="x")
        record, unreadable = agent_state.read_record("/a")
        assert unreadable is False
        assert record is not None
        assert record["state"] == "done"

    def test_no_file_is_absent_not_unreadable(self):
        assert agent_state.read_record("/nowhere") == (None, False)

    @pytest.mark.parametrize(
        "content",
        [b"not json", b"[1, 2, 3]", b'"a string"', b"", b"\xff\xfe{"],
        ids=["not-json", "array", "string", "empty", "not-utf8"],
    )
    def test_a_file_that_is_not_a_record_is_unreadable(self, content):
        p = agent_state._path_for("/a")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        assert agent_state.read_record("/a") == (None, True)
        assert agent_state.state_for("/a") is None

    def test_a_path_that_cannot_be_read_is_unreadable(self):
        # A directory where the record file belongs: an OSError that is not
        # "no such file" -- something is there, and what it says is unknown.
        agent_state._path_for("/a").mkdir(parents=True)
        assert agent_state.read_record("/a") == (None, True)
        assert agent_state.state_for("/a") is None
