"""Unit tests for the procs leaf (P1-09) — the one owner of pid liveness and
of the job-object breakaway every long-lived spawn depends on.

Runs against real processes (our own pid, a just-exited child), so the same
assertions exercise the win32 OpenProcess branch on Windows and the
os.kill(pid, 0) branch on POSIX.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from magent.procs import CREATE_BREAKAWAY_FROM_JOB, pid_alive, spawn_unjobbed


class TestPidAlive:
    def test_own_process_is_alive(self):
        assert pid_alive(os.getpid()) is True

    def test_exited_child_is_dead(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        assert pid_alive(p.pid) is False

    def test_none_is_dead(self):
        assert pid_alive(None) is False

    def test_zero_is_dead(self):
        assert pid_alive(0) is False

    def test_negative_is_dead(self):
        # On POSIX, os.kill(-n, 0) would probe a process GROUP -- the guard
        # keeps a corrupt pid file from ever reporting such a group as a
        # live process.
        assert pid_alive(-5) is False


class TestSpawnUnjobbed:
    """The primitive that decides whether a psmux session -- and the agent
    inside it -- can outlive the SSH connection that created it."""

    def test_it_really_spawns_and_the_child_really_runs(self):
        proc = spawn_unjobbed([sys.executable, "-c", "raise SystemExit(3)"])
        assert proc.wait() == 3

    def test_it_forwards_the_kwargs_a_caller_needs(self):
        # The real call site passes env= and DEVNULL pipes; a helper that
        # swallowed them would silently change what psmux sees.
        proc = spawn_unjobbed(
            [sys.executable, "-c", "import os,sys; sys.exit(int(os.environ['MDRC']))"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "MDRC": "7"},
        )
        assert proc.wait() == 7

    @pytest.mark.skipif(sys.platform != "win32", reason="job objects are win32-only")
    def test_it_asks_to_break_out_of_the_job_on_windows(self, monkeypatch):
        seen: dict[str, object] = {}

        class _Fake:
            def __init__(self, args, **kwargs):
                seen.update(args=args, kwargs=kwargs)

        monkeypatch.setattr(subprocess, "Popen", _Fake)
        spawn_unjobbed(["x"], creationflags=0x08000000)
        flags = seen["kwargs"]["creationflags"]
        assert flags & CREATE_BREAKAWAY_FROM_JOB
        # The caller's own flags survive: spawn_detached's detached-console
        # half must not be dropped by the job half.
        assert flags & 0x08000000

    @pytest.mark.skipif(sys.platform != "win32", reason="job objects are win32-only")
    def test_a_job_that_forbids_breakaway_degrades_instead_of_raising(
        self, monkeypatch
    ):
        # CreateProcess FAILS OUTRIGHT when the parent job forbids breakaway.
        # There is no way to spawn out of such a job, so the only correct
        # answer is today's behavior -- never an exception out of a bring-up.
        attempts: list[int] = []

        class _Fake:
            def __init__(self, args, **kwargs):
                attempts.append(kwargs["creationflags"])
                if kwargs["creationflags"] & CREATE_BREAKAWAY_FROM_JOB:
                    raise OSError("access denied")

        monkeypatch.setattr(subprocess, "Popen", _Fake)
        spawn_unjobbed(["x"], creationflags=0x8)
        assert attempts == [0x8 | CREATE_BREAKAWAY_FROM_JOB, 0x8]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX has no job objects")
    def test_posix_takes_a_plain_popen(self, monkeypatch):
        # No creationflags kwarg exists off Windows -- passing one is a
        # TypeError, so the branch must be structural, not cosmetic.
        seen: dict[str, object] = {}

        class _Fake:
            def __init__(self, args, **kwargs):
                seen.update(args=args, kwargs=kwargs)

        monkeypatch.setattr(subprocess, "Popen", _Fake)
        spawn_unjobbed(["x"])
        assert seen == {
            "args": ["x"],
            "kwargs": {"stdout": None, "stderr": None, "env": None},
        }
