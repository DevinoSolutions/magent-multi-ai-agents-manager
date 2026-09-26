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

from magent.procs import (
    ABOVE_NORMAL_PRIORITY_CLASS,
    CREATE_BREAKAWAY_FROM_JOB,
    NO_CONSOLE_SESSION,
    REGISTRATION_TIMEOUT_S,
    active_console_session_id,
    await_registration,
    count_processes,
    current_session_id,
    pid_alive,
    pids_by_image_name,
    raise_priority_above_normal,
    session_id_of,
    spawn_unjobbed,
)


class TestCountProcesses:
    """Enrichment for doctor's psmux-wedge finding: the wedge left psmux.exe
    processes that ignored ``taskkill /F``, so a count corroborates it. It must
    stay cheap (a Toolhelp snapshot, no subprocess) and must never pass off
    "could not look" as zero."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Toolhelp is win32-only")
    def test_it_counts_a_real_running_process(self):
        # Our own interpreter is running, by definition.
        found = count_processes(os.path.basename(sys.executable))
        assert found is not None
        assert found >= 1

    @pytest.mark.skipif(sys.platform != "win32", reason="Toolhelp is win32-only")
    def test_it_is_case_insensitive_like_windows(self):
        # Deliberately NOT `count(UPPER) == count(lower)`: two snapshots of a
        # live machine are two different machines (this box churns
        # python.exe constantly). Both spellings must FIND our interpreter --
        # that is the property; the exact population is not.
        exe = os.path.basename(sys.executable)
        assert count_processes(exe.upper()) >= 1
        assert count_processes(exe.lower()) >= 1

    @pytest.mark.skipif(sys.platform != "win32", reason="Toolhelp is win32-only")
    def test_a_name_nothing_runs_is_zero_not_none(self):
        assert count_processes("magent-definitely-not-running.exe") == 0

    @pytest.mark.skipif(sys.platform != "win32", reason="Toolhelp is win32-only")
    def test_it_costs_no_subprocess(self, monkeypatch):
        # A PowerShell/CIM query would make the diagnostic slower than the
        # machine it is diagnosing.
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: pytest.fail("spawned a subprocess")
        )
        monkeypatch.setattr(
            subprocess, "Popen", lambda *a, **k: pytest.fail("spawned a subprocess")
        )
        count_processes("psmux.exe")

    @pytest.mark.skipif(sys.platform == "win32", reason="the None branch is POSIX")
    def test_off_windows_it_admits_it_cannot_look(self):
        # None, never 0: a caller that rendered "0 psmux.exe resident" on Linux
        # would be inventing a fact.
        assert count_processes("psmux.exe") is None


class TestPidsByImageName:
    """The enumerator half of the psmux priority sweep. READ-ONLY here by
    design: it is asked about this interpreter's own image name, so the
    assertion never depends on -- and never touches -- anything else running on
    the machine."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Toolhelp is win32-only")
    def test_it_finds_our_own_process(self):
        assert os.getpid() in pids_by_image_name({os.path.basename(sys.executable)})

    @pytest.mark.skipif(sys.platform != "win32", reason="Toolhelp is win32-only")
    def test_it_matches_case_insensitively_like_windows(self):
        exe = os.path.basename(sys.executable)
        assert os.getpid() in pids_by_image_name({exe.upper()})
        assert os.getpid() in pids_by_image_name({exe.lower()})

    @pytest.mark.skipif(sys.platform != "win32", reason="Toolhelp is win32-only")
    def test_a_name_nothing_runs_finds_nothing(self):
        assert pids_by_image_name({"magent-definitely-not-running.exe"}) == []

    @pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch")
    def test_off_windows_it_is_empty(self):
        assert pids_by_image_name({"psmux.exe"}) == []


class TestRaisePriorityAboveNormal:
    """The setter half. The ONLY test in the suite that changes a real
    process's priority class, and it does so against a child it spawned itself
    and kills in the same test -- never a pid it merely found. (The developer
    box this feature was written on runs a 169-process psmux fleet; a test that
    swept it would be re-prioritising production.)"""

    @pytest.mark.skipif(sys.platform != "win32", reason="priority classes are win32")
    def test_it_really_raises_a_child_we_spawned(self):
        import ctypes

        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            assert raise_priority_above_normal(child.pid) is True

            k = ctypes.windll.kernel32
            handle = k.OpenProcess(0x1000, False, child.pid)  # QUERY_LIMITED
            try:
                assert k.GetPriorityClass(handle) == ABOVE_NORMAL_PRIORITY_CLASS
            finally:
                k.CloseHandle(handle)

            # Idempotent: a second call finds it already above normal and
            # reports no change, which is what keeps a 30s sweep silent.
            assert raise_priority_above_normal(child.pid) is False
        finally:
            child.kill()
            child.wait()

    @pytest.mark.skipif(sys.platform != "win32", reason="priority classes are win32")
    def test_a_pid_we_cannot_open_is_false_not_an_exception(self):
        # PID 4 is the System process: OpenProcess refuses a non-elevated
        # caller outright, so nothing is read and nothing is written. This is
        # the shape of every per-pid failure the sweep meets on a live box (a
        # pid that exited between the snapshot and the open, another user's
        # process, a protected one) and none of them may raise -- an exception
        # here would abort the sweep partway through the fleet.
        assert raise_priority_above_normal(4) is False

    @pytest.mark.skipif(sys.platform != "win32", reason="priority classes are win32")
    def test_an_exited_child_whose_handle_we_still_hold_is_harmless(self):
        # Measured, and deliberately NOT asserted as False: while a Popen keeps
        # the process HANDLE open, Windows keeps the process object alive, so
        # OpenProcess/SetPriorityClass both succeed against a pid whose program
        # has exited. That is a no-op on a corpse, and it cannot reach the real
        # sweep anyway -- pids_by_image_name only ever yields processes the
        # Toolhelp snapshot listed as running. What matters is that it does not
        # raise.
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        assert raise_priority_above_normal(child.pid) in (True, False)

    @pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch")
    def test_off_windows_it_reports_no_change(self):
        assert raise_priority_above_normal(os.getpid()) is False


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


class TestLogonSessionIds:
    """The two probes behind the Session-0 desktop hand-off. Both answer None
    rather than raising, because every caller treats "we could not tell" as
    "interactive" -- a probe that failed must never be able to stop a normal
    desktop launch."""

    @pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch")
    def test_off_windows_there_are_no_logon_sessions(self):
        # Not a degradation: POSIX has no session isolation and tmux over ssh
        # is the ordinary way to work there, so the question does not arise.
        assert current_session_id() is None
        assert session_id_of(os.getpid()) is None

    @pytest.mark.skipif(sys.platform != "win32", reason="win32 logon sessions")
    def test_this_process_reports_an_integer_session(self):
        session = current_session_id()
        assert isinstance(session, int)
        assert session >= 0

    @pytest.mark.skipif(sys.platform != "win32", reason="win32 logon sessions")
    def test_our_own_pid_agrees_with_our_own_session(self):
        # session_id_of takes no process HANDLE, so it must answer for a pid
        # the same way the handle-free "current" call answers for us.
        assert session_id_of(os.getpid()) == current_session_id()

    @pytest.mark.skipif(sys.platform != "win32", reason="win32 logon sessions")
    def test_a_pid_that_cannot_exist_is_unknowable_not_session_zero(self):
        # The trap this guards: a failed ProcessIdToSessionId leaves its DWORD
        # out-parameter at 0, and 0 is the SERVICES session -- so reading that
        # value without checking the call's RETURN would report every pid it
        # could not resolve as the exact thing the diagnostics hunt for.
        #
        # A number above the pid space rather than a just-exited child: a
        # terminated process whose handle is still open is still resolvable, so
        # "I killed it" is not the same claim as "there is no such pid".
        assert session_id_of(0xFFFFFFF0) is None

    def test_a_bogus_pid_is_unknowable_everywhere(self):
        assert session_id_of(0) is None
        assert session_id_of(-1) is None


class TestActiveConsoleSession:
    """Read to answer "is there a desktop to hand work to at all?" -- and
    deliberately never to PICK a session. The id can name an RDP session that
    is not the desktop the user is looking at, so anything built on "find the
    interactive session" is wrong on some real machine."""

    @pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch")
    def test_off_windows_there_is_no_console_session(self):
        assert active_console_session_id() is None

    @pytest.mark.skipif(sys.platform != "win32", reason="win32 console sessions")
    def test_a_logged_on_box_reports_a_usable_session(self):
        session = active_console_session_id()
        assert isinstance(session, int)
        # This suite runs from a logged-on desktop or a CI runner; either way
        # the call answers. Whether the value is USABLE is the platform
        # probe's judgement, not this module's -- see NO_CONSOLE_SESSION.
        assert session >= 0

    def test_the_two_non_answers_are_named_not_guessed(self):
        # 0 is the services session; 0xFFFFFFFF is "nothing attached". Both are
        # "no desktop", and neither may be mistaken for a session id.
        assert NO_CONSOLE_SESSION == (0, 0xFFFFFFFF)


class _Child:
    """A detached child that records every attempt to end it."""

    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.ended: list[str] = []

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.ended.append("kill")

    def terminate(self) -> None:
        self.ended.append("terminate")

    def send_signal(self, sig: int) -> None:
        self.ended.append(f"signal {sig}")


class _Clock:
    """A fake clock whose ``sleep`` advances it; nothing really sleeps. A wait
    that never closes FAILS here instead of hanging the suite."""

    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        assert self.now < 120.0, "the registration wait never closed"

    def __call__(self) -> float:
        return self.now


class TestAwaitRegistration:
    """The launcher-side wait for a detached child's pid file. The old fixed 2s
    window reported "failed to start" over children that registered at
    4.7-11s on a loaded desktop and then kept running."""

    def test_a_child_that_registers_after_five_seconds_is_accepted(self):
        clock = _Clock()

        def read_pid() -> int | None:
            return 4242 if clock.now >= 5.0 else None

        pid = await_registration(_Child(), read_pid, sleep=clock.sleep, clock=clock)

        assert pid == 4242

    def test_the_default_window_outlasts_the_slowest_measured_start(self):
        assert REGISTRATION_TIMEOUT_S >= 11.5  # slowest measured: 11.45s

    def test_a_child_that_exited_is_not_waited_out(self):
        clock = _Clock()

        pid = await_registration(
            _Child(returncode=1), lambda: None, sleep=clock.sleep, clock=clock
        )

        assert pid is None
        assert clock.now < 1.0

    def test_the_wait_is_bounded(self):
        clock = _Clock()

        pid = await_registration(
            _Child(), lambda: None, 20.0, sleep=clock.sleep, clock=clock
        )

        assert pid is None
        assert 20.0 <= clock.now < 20.5

    def test_the_old_pid_never_counts_as_the_new_registration(self):
        # A restart whose kill did not take leaves the old pid in the file.
        clock = _Clock()

        pid = await_registration(
            _Child(), lambda: 1234, 3.0, not_pid=1234, sleep=clock.sleep, clock=clock
        )

        assert pid is None

    def test_a_pid_already_there_is_returned_after_one_poll(self):
        clock = _Clock()

        pid = await_registration(_Child(), lambda: 99, sleep=clock.sleep, clock=clock)

        assert pid == 99
        assert clock.now == pytest.approx(0.1)

    def test_a_child_that_exited_zero_is_not_waited_out(self):
        # `magent hotkey` exits 0 when another listener already runs: a clean
        # exit is still an exit, not 20 seconds of waiting on a corpse.
        clock = _Clock()

        pid = await_registration(
            _Child(returncode=0), lambda: None, sleep=clock.sleep, clock=clock
        )

        assert pid is None
        assert clock.now < 1.0


class TestTheWaitNeverEndsTheChild:
    """A timeout means "not registered YET", never "dead": on a loaded box the
    child may be a second from coming up, so the wait must not end it."""

    def test_a_child_that_never_registers_is_left_running(self):
        clock = _Clock()
        child = _Child()

        pid = await_registration(
            child, lambda: None, 3.0, sleep=clock.sleep, clock=clock
        )

        assert pid is None
        assert child.ended == []

    def test_a_restart_whose_kill_did_not_take_leaves_the_new_child_alone(self):
        clock = _Clock()
        child = _Child()

        pid = await_registration(
            child, lambda: 1234, 3.0, not_pid=1234, sleep=clock.sleep, clock=clock
        )

        assert pid is None
        assert child.ended == []


class TestTheDefaultWindowIsBounded:
    """Long enough for the slowest measured start, and still a bound: a child
    that hangs alive without registering must not stall serve's supervisor
    thread or a `--go` launch forever."""

    def test_the_default_window_is_a_bound_not_a_hang(self):
        assert REGISTRATION_TIMEOUT_S <= 30.0

    def test_the_default_window_is_what_a_caller_without_one_gets(self):
        clock = _Clock()

        pid = await_registration(_Child(), lambda: None, sleep=clock.sleep, clock=clock)

        assert pid is None
        assert REGISTRATION_TIMEOUT_S <= clock.now < REGISTRATION_TIMEOUT_S + 0.5
