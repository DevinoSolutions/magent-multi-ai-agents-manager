"""Claude's idle probe: read_session_files (the stale-file rule, the field
contract, .key never opened, name never surfaced) and last_activity."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from magent.sessions import claude, live
from magent.sessions.claude import (
    encode_claude_project_path,
    last_activity,
    read_session_files,
)

FIXTURE = Path(__file__).parent / "fixtures" / "claude_session"


@pytest.fixture(autouse=True)
def _fresh_warn_latch(monkeypatch):
    # "Logged once" is process state: every test starts with nothing reported.
    monkeypatch.setattr(claude, "_warned_files", set())


def _valid(pid: int = 123) -> dict[str, object]:
    return {
        "pid": pid,
        "sessionId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "cwd": "/projects/demo",
        "procStart": "1",
        "kind": "interactive",
        "status": "idle",
        "statusUpdatedAt": 1790000000000,
    }


def _nested_past_the_parsers_depth(depth: int = 100_000) -> str:
    # Every field valid, plus one value nested deeper than json.loads recurses:
    # it raises RecursionError, not ValueError.
    return json.dumps(_valid())[:-1] + ', "x": ' + "[" * depth + "]" * depth + "}"


_EMPTY = live.SessionScan({}, frozenset())


def _only_unusable(*pids: int) -> live.SessionScan:
    """No live session, and these pids' files there but unusable."""
    return live.SessionScan({}, frozenset(pids))


def _unusable_warnings(caplog, name: str) -> int:
    return sum(
        "unusable claude session file" in r.getMessage() and name in r.getMessage()
        for r in caplog.records
    )


class TestABadFileIsUnusableNeverARaise:
    """One bad session file is never a live session, is reported by its pid as
    UNUSABLE (an agent nobody can read: unknown, never absent), and is logged
    once per episode -- a raise would stop every sweep for the whole fleet
    while the file exists."""

    def test_a_write_caught_mid_character(self, tmp_path, caplog):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "123.json").write_bytes(b'{"pid": 123, "name": "caf\xc3')
        with caplog.at_level("WARNING", logger="magent.reap"):
            assert read_session_files(tmp_path) == _only_unusable(123)
            assert read_session_files(tmp_path) == _only_unusable(123)
        assert _unusable_warnings(caplog, "123.json") == 1

    def test_a_procstart_past_the_int_digit_limit(self, tmp_path, caplog):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "123.json").write_text(
            json.dumps({**_valid(), "procStart": "1" * 5000}), encoding="utf-8"
        )
        with caplog.at_level("WARNING", logger="magent.reap"):
            assert read_session_files(tmp_path) == _only_unusable(123)
            assert read_session_files(tmp_path) == _only_unusable(123)
        assert _unusable_warnings(caplog, "123.json") == 1

    def test_a_status_time_past_any_float(self, tmp_path, caplog):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "123.json").write_text(
            json.dumps({**_valid(), "statusUpdatedAt": 10**400}), encoding="utf-8"
        )
        with caplog.at_level("WARNING", logger="magent.reap"):
            assert read_session_files(tmp_path) == _only_unusable(123)
            assert read_session_files(tmp_path) == _only_unusable(123)
        assert _unusable_warnings(caplog, "123.json") == 1

    def test_json_nested_past_the_parsers_depth(self, tmp_path, caplog):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "123.json").write_text(
            _nested_past_the_parsers_depth(), encoding="utf-8"
        )
        with caplog.at_level("WARNING", logger="magent.reap"):
            assert read_session_files(tmp_path) == _only_unusable(123)
            assert read_session_files(tmp_path) == _only_unusable(123)
        assert _unusable_warnings(caplog, "123.json") == 1

    def test_a_file_that_parses_again_ends_the_episode(self, tmp_path, caplog):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        path = sessions / "123.json"
        with caplog.at_level("WARNING", logger="magent.reap"):
            path.write_bytes(b"{")
            assert read_session_files(tmp_path) == _only_unusable(123)
            # Parses (then dropped by the stale-file rule: procStart "1" is no
            # live process's creation time) -- the file is usable again.
            path.write_text(json.dumps(_valid()), encoding="utf-8")
            assert read_session_files(tmp_path) == _EMPTY
            path.write_bytes(b"{")
            assert read_session_files(tmp_path) == _only_unusable(123)
        assert _unusable_warnings(caplog, "123.json") == 2

    def test_a_file_that_cannot_be_read(self, tmp_path):
        (tmp_path / "sessions" / "123.json").mkdir(parents=True)  # a dir
        assert read_session_files(tmp_path) == _only_unusable(123)

    @pytest.mark.parametrize("name", ["notes.json", "١٢.json", "12a.json"])
    def test_a_name_that_is_not_a_pid_names_no_process(self, tmp_path, caplog, name):
        # Logged, but reported under no pid: nothing is in a tree under it.
        # (Arabic-Indic digits are digits to str.isdigit, but no pid's name.)
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / name).write_bytes(b"{")
        with caplog.at_level("WARNING", logger="magent.reap"):
            assert read_session_files(tmp_path) == _EMPTY
        assert _unusable_warnings(caplog, name) == 1

    def test_only_the_unusable_files_are_reported(self, tmp_path):
        # A usable-but-stale file (a dead pid) is dropped silently, never
        # unusable; each bad file is reported by its own pid.
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "123.json").write_bytes(b"{")
        (sessions / "124.json").write_text(json.dumps(_valid(124)), encoding="utf-8")
        (sessions / "125.json").write_text(
            json.dumps({**_valid(125), "kind": 5}), encoding="utf-8"
        )
        (sessions / "notes.json").write_bytes(b"{")
        assert read_session_files(tmp_path) == _only_unusable(123, 125)

    def test_a_file_gone_before_its_read_is_absent_not_unusable(
        self, tmp_path, monkeypatch
    ):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        (sessions / "123.json").write_bytes(b"{")
        real = Path.read_bytes

        def _gone(self):
            if self.name == "123.json":
                raise FileNotFoundError(2, "gone")
            return real(self)

        monkeypatch.setattr(Path, "read_bytes", _gone)
        assert read_session_files(tmp_path) == _EMPTY


def test_session_id_re_fullmatch():
    assert live.SESSION_ID_RE.fullmatch("11111111-2222-3333-4444-555555555555")
    assert live.SESSION_ID_RE.fullmatch("a")
    assert not live.SESSION_ID_RE.fullmatch("")
    assert not live.SESSION_ID_RE.fullmatch("has space")
    assert not live.SESSION_ID_RE.fullmatch("x" * 200)


def test_a_dead_pid_is_dropped_by_the_stale_file_rule():
    # The committed fixture's pid 4242 is not this test's live process, so the
    # reader drops it (identity check fails) and never surfaces `name`.
    result = read_session_files(FIXTURE)
    assert result is not None
    assert 4242 not in result.sessions
    assert result.unusable == frozenset()  # stale is not unusable


def test_a_missing_sessions_dir_is_empty_not_none(tmp_path):
    # A fresh machine with no claude sessions: normal empty state, not a
    # read failure. {} (not None) so the reaper does not treat it as unknown.
    assert read_session_files(tmp_path / "does-not-exist") == _EMPTY


def test_a_file_where_the_sessions_dir_should_be_is_none(tmp_path):
    # There, but unlistable: iterdir raises NotADirectoryError (an OSError that
    # is not FileNotFoundError) on both POSIX and Windows -> None.
    (tmp_path / "sessions").write_text("not a dir", encoding="utf-8")
    assert read_session_files(tmp_path) is None


def test_off_windows_every_file_is_dropped(tmp_path):
    # process_identity is None off Windows, so the stale-file rule drops every
    # file: a readable dir of valid files still yields {} (never None).
    if sys.platform == "win32":
        pytest.skip("this asserts the POSIX identity-is-None drop")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "777.json").write_text(
        json.dumps(
            {
                "pid": 777,
                "sessionId": "a",
                "cwd": "/x",
                "procStart": "1",
                "kind": "interactive",
                "status": "idle",
                "statusUpdatedAt": 1,
            }
        ),
        encoding="utf-8",
    )
    assert read_session_files(tmp_path) == _EMPTY


_MISSING = object()


class TestTheSessionFileContract:
    """``_parse_session_file`` over every field: the valid base parses, and
    each missing field, wrong type, or pid that is not the file's name makes
    the file unusable."""

    def test_the_valid_base_parses_with_the_r6_clock_in_seconds(self):
        parsed = claude._parse_session_file("123", json.dumps(_valid()))
        assert parsed is not None
        assert parsed.pid == 123
        assert parsed.proc_start == 1
        # statusUpdatedAt is epoch MILLISECONDS; R6 compares seconds.
        assert parsed.status_ts == 1_790_000_000.0

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("pid", _MISSING),
            ("pid", "123"),
            ("pid", True),
            ("pid", 124),  # not the file's name
            ("sessionId", _MISSING),
            ("sessionId", 5),
            ("sessionId", ""),
            ("sessionId", "has space"),
            ("cwd", _MISSING),
            ("cwd", 5),
            ("cwd", ""),
            ("procStart", _MISSING),
            ("procStart", 1),
            ("procStart", "12a"),
            ("procStart", "-1"),
            ("procStart", "١٢"),  # digits, but not ASCII ones
            ("kind", _MISSING),
            ("kind", 5),
            ("status", _MISSING),
            ("status", 5),
            ("statusUpdatedAt", _MISSING),
            ("statusUpdatedAt", "1790000000000"),
            ("statusUpdatedAt", True),
            ("statusUpdatedAt", 1.5),
            pytest.param("statusUpdatedAt", 10**400, id="statusUpdatedAt-overflow"),
        ],
    )
    def test_a_missing_or_mistyped_field_is_unusable(self, field, value):
        raw = _valid()
        if value is _MISSING:
            del raw[field]
        else:
            raw[field] = value
        assert claude._parse_session_file("123", json.dumps(raw)) is None

    @pytest.mark.parametrize("text", ["[]", "null", "{", ""])
    def test_a_file_that_is_not_one_json_object_is_unusable(self, text):
        assert claude._parse_session_file("123", text) is None

    def test_json_nested_past_the_parsers_depth_is_unusable(self):
        assert (
            claude._parse_session_file("123", _nested_past_the_parsers_depth()) is None
        )

    def test_json_nested_one_past_the_bound_is_unusable_on_any_python(self):
        # The refusal is magent's own depth bound, not the parser's recursion
        # limit: json.loads on Python 3.14.7 reads 100_000 levels without a
        # RecursionError, and every interpreter reads this one. Valid in every
        # other field, so only the nesting can make it unusable.
        from magent.json_depth import MAX_JSON_DEPTH

        too_deep = _nested_past_the_parsers_depth(MAX_JSON_DEPTH)
        json.loads(too_deep)  # the control: the parser itself has no objection
        assert claude._parse_session_file("123", too_deep) is None


def test_a_key_file_is_never_opened(tmp_path, monkeypatch):
    # Spied at every Path read, not inferred from a directory that would raise
    # when read -- the reader swallows OSError per file, which would hide it.
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "123.json").write_text(json.dumps(_valid()), encoding="utf-8")
    (sessions / "123.key").write_text("secret", encoding="utf-8")
    opened: list[str] = []
    for method in ("read_bytes", "read_text", "open"):
        real = getattr(Path, method)

        def _spy(self, *args, _real=real, **kwargs):
            opened.append(self.name)
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(Path, method, _spy)
    read_session_files(tmp_path)
    assert "123.json" in opened  # the control: the spy sees the reads
    assert not [name for name in opened if name.endswith(".key")]


@pytest.fixture
def live_child():
    """A sleeping child of this test and its identity. Base interpreter,
    isolated: a venv python is a launcher whose pid dies after it re-execs,
    which would fail the stale-file identity re-read."""
    from magent.procs import process_identity

    base = getattr(sys, "_base_executable", None) or sys.executable
    child = subprocess.Popen([base, "-I", "-S", "-c", "import time; time.sleep(30)"])
    try:
        ident = process_identity(child.pid)
        assert ident is not None
        yield child, ident
    finally:
        child.kill()  # through the Popen handle: never a bare pid
        child.wait()


def _write_session(tmp_path: Path, pid: int, **fields: object) -> Path:
    sessions = tmp_path / "sessions"
    sessions.mkdir(exist_ok=True)
    path = sessions / f"{pid}.json"
    path.write_text(json.dumps({**_valid(pid), **fields}), encoding="utf-8")
    return path


@pytest.mark.skipif(sys.platform != "win32", reason="identity is FILETIME-based")
class TestLiveSessions:
    def test_a_live_child_with_a_matching_procstart_is_every_field(
        self, tmp_path, live_child
    ):
        child, ident = live_child
        _write_session(tmp_path, child.pid, procStart=str(ident.created), name="secret")
        result = read_session_files(tmp_path)
        assert result == live.SessionScan(
            {
                child.pid: live.LiveSession(
                    pid=child.pid,
                    created=ident.created,
                    image=ident.image,
                    session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    cwd="/projects/demo",
                    status="idle",
                    status_ts=1_790_000_000.0,
                    kind="interactive",
                    quiet=True,
                )
            },
            frozenset(),
        )  # and `name` is never carried: LiveSession has no such field

    def test_any_other_procstart_is_stale(self, tmp_path, live_child):
        child, ident = live_child
        _write_session(tmp_path, child.pid, procStart=str(ident.created + 1))
        assert read_session_files(tmp_path) == _EMPTY

    @pytest.mark.parametrize(
        ("status", "quiet"),
        [("idle", True), ("busy", False), ("waiting", False), ("", False)],
    )
    def test_only_idle_is_quiet(self, tmp_path, live_child, status, quiet):
        # An unknown status is the tool saying something we do not model:
        # never read as idle.
        child, ident = live_child
        _write_session(tmp_path, child.pid, procStart=str(ident.created), status=status)
        result = read_session_files(tmp_path)
        assert result is not None
        assert result.sessions[child.pid].quiet is quiet

    def test_a_file_whose_process_has_exited_is_dropped(self, tmp_path, live_child):
        child, ident = live_child
        _write_session(tmp_path, child.pid, procStart=str(ident.created))
        assert child.pid in (read_session_files(tmp_path) or _EMPTY).sessions
        child.kill()
        child.wait()
        assert read_session_files(tmp_path) == _EMPTY


_SID = "sid-1"
_SESSION = live.LiveSession(
    pid=1,
    created=1,
    image="claude",
    session_id=_SID,
    cwd="/projects/demo",
    status="idle",
    status_ts=0.0,
    kind="interactive",
    quiet=True,
)


def _touch(path: Path, mtime: float) -> None:
    path.write_text("{}\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _transcripts(cfg: Path, main: float, subs: dict[str, float] | None) -> Path:
    """The main transcript at mtime ``main``; ``subs`` (name -> mtime) under
    ``<sid>/subagents`` when given -- None leaves that directory absent."""
    proj = cfg / "projects" / encode_claude_project_path(_SESSION.cwd)
    proj.mkdir(parents=True)
    _touch(proj / f"{_SID}.jsonl", main)
    if subs is not None:
        subdir = proj / _SID / "subagents"
        subdir.mkdir(parents=True)
        for name, mtime in subs.items():
            _touch(subdir / name, mtime)
    return proj


def _stat_failing_for(monkeypatch, name: str, exc: OSError) -> None:
    real = os.stat

    def _stat(path, *args, **kwargs):
        if os.fspath(path).endswith(name):
            raise exc
        return real(path, *args, **kwargs)

    monkeypatch.setattr(claude.os, "stat", _stat)


class TestLastActivity:
    def test_no_main_transcript_is_none(self, tmp_path):
        assert last_activity(_SESSION, tmp_path) is None

    def test_the_main_transcript_alone(self, tmp_path):
        # No subagents dir at all: there are none, not an unknown.
        _transcripts(tmp_path, 1000.0, None)
        assert last_activity(_SESSION, tmp_path) == 1000.0

    def test_a_subagent_newer_than_the_main_transcript(self, tmp_path):
        _transcripts(tmp_path, 1000.0, {"a.jsonl": 2000.0})
        assert last_activity(_SESSION, tmp_path) == 2000.0

    def test_a_main_transcript_newer_than_every_subagent(self, tmp_path):
        # Only *.jsonl counts; the newest of everything wins, whatever order
        # the listing comes back in.
        _transcripts(
            tmp_path, 3000.0, {"a.jsonl": 2000.0, "b.jsonl": 1000.0, "c.txt": 9000.0}
        )
        assert last_activity(_SESSION, tmp_path) == 3000.0

    def test_a_vanished_subagent_is_skipped_not_the_rest_of_the_scan(
        self, tmp_path, monkeypatch
    ):
        # a.jsonl is removed between the listing and its stat; b.jsonl, the
        # newest thing in the tree, must still be read.
        _transcripts(tmp_path, 1000.0, {"a.jsonl": 5000.0, "b.jsonl": 9000.0})
        _stat_failing_for(monkeypatch, "a.jsonl", FileNotFoundError(2, "gone"))
        assert last_activity(_SESSION, tmp_path) == 9000.0

    def test_an_unreadable_subagent_is_unknown(self, tmp_path, monkeypatch):
        _transcripts(tmp_path, 1000.0, {"a.jsonl": 5000.0, "b.jsonl": 9000.0})
        _stat_failing_for(monkeypatch, "a.jsonl", PermissionError(13, "denied"))
        assert last_activity(_SESSION, tmp_path) is None

    def test_a_subagents_path_that_cannot_be_listed_is_unknown(self, tmp_path):
        proj = _transcripts(tmp_path, 1000.0, None)
        (proj / _SID).mkdir()
        (proj / _SID / "subagents").write_text("not a dir", encoding="utf-8")
        assert last_activity(_SESSION, tmp_path) is None
