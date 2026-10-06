"""The pid-file leaf and the verified kill behind it, against REAL processes.

Every pid here belongs to a sleeping child this module spawned, so the same
assertions drive the win32 handle-verified branch (through the conftest kill
guard, with the child registered as ours) and the POSIX start-time + SIGTERM
branch. What is pinned: a live pid whose process predates its record is ended;
a pid whose process started AFTER the record (a recycled number) is never
touched; a record of a dead process is cleared without a kill.
"""

from __future__ import annotations

import contextlib
import gc
import os
import subprocess
import sys
import time

import pytest

from magent import pidfile, procs

_BASE_PY = getattr(sys, "_base_executable", None) or sys.executable
_SLEEP = [_BASE_PY, "-I", "-S", "-c", "import time; time.sleep(60)"]


@pytest.fixture
def child(own_pids):
    proc = subprocess.Popen(
        _SLEEP, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    ident = None
    deadline = time.monotonic() + 5
    while ident is None and time.monotonic() < deadline:
        ident = procs.process_identity(proc.pid) if sys.platform == "win32" else True
        time.sleep(0.02)
    if sys.platform == "win32":
        own_pids.add(proc.pid, ident)
    try:
        yield proc
    finally:
        with contextlib.suppress(OSError):
            proc.kill()
        proc.wait(timeout=10)


def _record(tmp_path, pid, *, mtime=None):
    path = tmp_path / "x.pid"
    path.write_text(str(pid))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _wait_gone(proc):
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        return False
    return True


class TestTerminate:
    def test_a_live_matching_pid_is_terminated(self, tmp_path, child):
        path = _record(tmp_path, child.pid, mtime=time.time() + 1)

        pid, outcome = pidfile.terminate(path)

        assert (pid, outcome) == (child.pid, "terminated")
        assert _wait_gone(child)

    def test_a_recycled_pid_is_not_touched(self, tmp_path, child):
        # The record is older than the process now wearing the number: the
        # recorded process died and the OS handed its pid to a stranger.
        path = _record(tmp_path, child.pid, mtime=time.time() - 3600)

        pid, outcome = pidfile.terminate(path)

        assert (pid, outcome) == (child.pid, "mismatch")
        assert child.poll() is None
        assert path.exists()  # clearing is the caller's call, not the kill's

    def test_an_unreadable_start_time_is_not_touched(
        self, tmp_path, child, monkeypatch
    ):
        monkeypatch.setattr(procs, "process_started_at", lambda pid: None)
        path = _record(tmp_path, child.pid, mtime=time.time() + 1)

        assert pidfile.terminate(path) == (child.pid, "unverifiable")
        assert child.poll() is None

    def test_a_dead_pid_is_cleared_without_a_kill(self, tmp_path, monkeypatch):
        proc = subprocess.Popen([_BASE_PY, "-I", "-S", "-c", "pass"])
        proc.wait(timeout=10)
        pid = proc.pid
        # Popen holds a handle that keeps a dead process "existing" on Windows;
        # the pid file's process must be gone for real.
        del proc
        gc.collect()
        path = _record(tmp_path, pid)

        def _no_kill(*a, **k):
            raise AssertionError("a dead pid must not be killed")

        monkeypatch.setattr(procs, "terminate_pid", _no_kill)
        monkeypatch.setattr(pidfile, "terminate_pid", _no_kill)

        assert pidfile.terminate(path) == (None, "gone")
        assert not path.exists()  # read() cleared the record of a gone process

    def test_no_file_is_nothing_to_do(self, tmp_path):
        assert pidfile.terminate(tmp_path / "absent.pid") == (None, "gone")


class TestTerminatePid:
    def test_a_dead_pid_is_gone(self):
        proc = subprocess.Popen([_BASE_PY, "-I", "-S", "-c", "pass"])
        proc.wait(timeout=10)
        assert procs.terminate_pid(proc.pid, started_before=time.time()) == "gone"

    @pytest.mark.parametrize("pid", [0, -5, None])
    def test_a_nonsense_pid_is_gone(self, pid):
        assert procs.terminate_pid(pid, started_before=time.time()) == "gone"  # ty: ignore[invalid-argument-type] -- reason: None is the "no pid" a caller can hold

    def test_the_slack_tolerates_a_coarse_record_clock(self, child):
        started = procs.process_started_at(child.pid)
        assert started is not None
        # A record stamped a hair BEFORE the start (coarse fs timestamps) is
        # still the same process.
        outcome = procs.terminate_pid(child.pid, started_before=started - 1.0)
        assert outcome == "terminated"


class TestReadWriteClear:
    def test_write_is_atomic_and_read_sees_this_process(self, tmp_path):
        path = tmp_path / "sub" / "x.pid"
        pidfile.write(path)
        assert path.read_text() == str(os.getpid())
        assert not list(path.parent.glob("*.tmp"))
        assert pidfile.read(path) == os.getpid()

    def test_clear_removes_only_its_own_record(self, tmp_path):
        mine = tmp_path / "mine.pid"
        pidfile.write(mine)
        pidfile.clear(mine)
        assert not mine.exists()

        theirs = _record(tmp_path, os.getpid() + 1)
        pidfile.clear(theirs)
        assert theirs.exists()

    def test_a_pre_boot_record_is_ignored_and_cleared(self, tmp_path, monkeypatch):
        path = _record(tmp_path, os.getpid(), mtime=1000.0)
        monkeypatch.setattr(procs, "boot_time", lambda: 5000.0)
        assert pidfile.read(path) is None
        assert not path.exists()

    def test_recorded_never_clears_or_checks_liveness(self, tmp_path, monkeypatch):
        path = _record(tmp_path, 4242424)
        assert pidfile.recorded(path) == 4242424
        monkeypatch.setattr(procs, "boot_time", lambda: time.time() + 1000)
        assert pidfile.recorded(path) is None
        assert path.exists()
