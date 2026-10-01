"""Unit tests for the procs leaf (P1-09) — the one owner of pid liveness and
of the job-object breakaway every long-lived spawn depends on.

Runs against real processes (our own pid, a just-exited child), so the same
assertions exercise the win32 OpenProcess branch on Windows and the
os.kill(pid, 0) branch on POSIX.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

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
    process_tree,
    raise_priority_above_normal,
    session_id_of,
    snapshot_processes,
    spawn_unjobbed,
)

# The BASE interpreter: a venv python on Windows is a launcher that re-execs
# the base interpreter as its child, so a pid from `sys.executable` would name
# the launcher, not the process that holds (or lacks) the console.
_BASE_PY = getattr(sys, "_base_executable", None) or sys.executable
# -I -S: isolated, no site, so no venv/user layer forks underneath either.
_SLEEP = [_BASE_PY, "-I", "-S", "-c", "import time; time.sleep(40)"]


class TestProcessTree:
    """The subtree walk behind "is an agent still under this pane". Pure over a
    snapshot, so the shapes a live Toolhelp list takes are pinned here without
    reading one; the one real-process pin below uses a child it spawned."""

    SNAPSHOT = (
        ("psmux.exe", 1, 0),
        ("pwsh.exe", 10, 1),
        ("cmd.exe", 11, 10),
        ("claude.exe", 12, 11),
        ("bash.exe", 13, 12),
        ("pwsh.exe", 20, 1),  # a sibling pane: not ours
        ("node.exe", 21, 20),
    )

    def test_it_walks_every_depth_under_the_root(self):
        tree = process_tree(10, self.SNAPSHOT)
        assert tree is not None
        assert [pid for _image, pid, _ppid in tree] == [10, 11, 12, 13]

    def test_the_root_comes_first(self):
        # idle_sessions asks "is the pane's own process a shell" of tree[0].
        tree = process_tree(12, self.SNAPSHOT)
        assert tree is not None
        assert tree[0] == ("claude.exe", 12, 11)

    def test_siblings_and_ancestors_are_not_the_subtree(self):
        tree = process_tree(20, self.SNAPSHOT)
        assert tree is not None
        assert {pid for _image, pid, _ppid in tree} == {20, 21}

    def test_a_root_missing_from_the_snapshot_is_unknown_not_empty(self):
        # "The pane process is gone" and "nothing runs under it" are different
        # claims; only the second may make a pane idle.
        assert process_tree(99, self.SNAPSHOT) is None

    def test_a_parent_cycle_terminates(self):
        # Windows recycles pids and never rewrites a parent pid, so a stale
        # parent link can close a loop; the walk must still end.
        cyclic = [("a.exe", 1, 2), ("b.exe", 2, 1), ("c.exe", 3, 2)]
        tree = process_tree(1, cyclic)
        assert tree is not None
        assert sorted(pid for _image, pid, _ppid in tree) == [1, 2, 3]

    @pytest.mark.skipif(sys.platform != "win32", reason="Toolhelp is win32-only")
    def test_the_real_snapshot_carries_parent_pids(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            snapshot = snapshot_processes()
            assert snapshot is not None
            tree = process_tree(os.getpid(), snapshot)
            assert tree is not None
            assert child.pid in {
                pid for _image, pid, ppid in tree if ppid == os.getpid()
            }
        finally:
            child.kill()
            child.wait()


class _FakeToolhelp:
    """kernel32's Toolhelp walk, as ``snapshot_processes`` drives it: each
    Process32*W call fills the next entry, and the call after the last one
    fails with ``end_error``.

    The error lands where ctypes really keeps it: in the private copy that
    ``ctypes.get_last_error`` reads, updated only for functions of a library
    loaded with ``use_last_error=True``. Python code between two foreign calls
    may clobber the thread's own last error, so that copy is the only one worth
    reading."""

    def __init__(self, entries, end_error, *, use_last_error):
        self._pending = list(entries)
        self._end_error = end_error
        self._use_last_error = use_last_error
        self.last_error = 0
        self.closed = False

    def CreateToolhelp32Snapshot(self, flags, pid):
        return 0x1234

    def Process32FirstW(self, snapshot, ref):
        return self._next_into(ref)

    def Process32NextW(self, snapshot, ref):
        return self._next_into(ref)

    def CloseHandle(self, handle):
        self.closed = True
        return 1

    def _next_into(self, ref):
        if not self._pending:
            if self._use_last_error:
                self.last_error = self._end_error
            return 0
        entry = ref._obj
        entry.szExeFile, entry.th32ProcessID, entry.th32ParentProcessID = (
            self._pending.pop(0)
        )
        return 1


@pytest.mark.skipif(sys.platform != "win32", reason="Toolhelp is win32-only")
class TestTheSnapshotWalk:
    """The Toolhelp walk now feeds a SAFETY verdict: ``idle_sessions`` reads
    "not in the snapshot" as "nothing runs under this pane", and a yes there
    types into the pane. So only Windows' own end-of-list error may end the
    walk; a list cut short by any other failure is a failed snapshot."""

    ENTRIES = (("pwsh.exe", 10, 1), ("cmd.exe", 11, 10), ("claude.exe", 12, 11))

    def _kernel32(self, monkeypatch, entries, end_error):
        """Serve every way the walk could reach kernel32 -- ``windll`` (no
        private last-error copy) and ``WinDLL(..., use_last_error=True)`` --
        from one fake walk, and return that walk."""
        import ctypes

        libraries: list[_FakeToolhelp] = []

        def _load(name, use_last_error=False, **_kw):
            library = _FakeToolhelp(entries, end_error, use_last_error=use_last_error)
            libraries.append(library)
            return library

        monkeypatch.setattr(ctypes, "WinDLL", _load)
        monkeypatch.setattr(
            ctypes, "windll", type("_Loader", (), {"kernel32": _load("kernel32")})
        )
        monkeypatch.setattr(ctypes, "get_last_error", lambda: libraries[-1].last_error)
        return libraries

    def test_a_walk_that_reaches_the_end_returns_every_entry(self, monkeypatch):
        libraries = self._kernel32(
            monkeypatch,
            self.ENTRIES,
            end_error=18,  # ERROR_NO_MORE_FILES
        )
        assert snapshot_processes() == list(self.ENTRIES)
        assert any(library.closed for library in libraries)

    def test_a_walk_that_fails_partway_is_unknown_not_short(self, monkeypatch):
        # ERROR_GEN_FAILURE after two entries: the agent's entry never came,
        # and a short list would say nothing runs under the pane.
        libraries = self._kernel32(monkeypatch, self.ENTRIES[:2], end_error=31)
        assert snapshot_processes() is None
        assert any(library.closed for library in libraries)


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


class TestConsoleClients:
    """procs.console_clients against real processes on a real hidden console."""

    @pytest.mark.skipif(sys.platform != "win32", reason="consoles are a win32 concept")
    def test_own_console_with_a_child_reads_both_pids(self):
        from magent.procs import console_clients

        # A hidden console of our own with one child sharing it. The holder is
        # spawned CREATE_NO_WINDOW off the BASE executable with -I -S: Windows
        # gives it a fresh windowless console (no launcher shim re-execs first),
        # the suite's own console is untouched, and the grandchild inherits it.
        script = (
            "import subprocess,sys,time;"
            "p=subprocess.Popen([sys.executable,'-I','-S','-c','import time;time.sleep(20)']);"
            "open(sys.argv[1],'w').write(str(p.pid));"
            "time.sleep(20)"
        )
        d = Path(tempfile.mkdtemp(prefix="magent-con-test-"))
        pidfile = d / "child.pid"
        holder = subprocess.Popen(
            [_BASE_PY, "-I", "-S", "-c", script, str(pidfile)],
            creationflags=0x08000000,  # CREATE_NO_WINDOW: a console, no window
        )
        child_pid: int | None = None
        try:
            for _ in range(100):
                if pidfile.exists() and pidfile.stat().st_size:
                    break
                time.sleep(0.1)
            child_pid = int(pidfile.read_text().strip())
            clients = console_clients([holder.pid])
            got = clients[holder.pid]
            assert got is not None
            assert got == {holder.pid, child_pid}
        finally:
            if child_pid is not None:
                # The grandchild outlives the holder's kill by its full sleep.
                with contextlib.suppress(OSError):
                    os.kill(child_pid, signal.SIGTERM)
            holder.kill()
            holder.wait()
            shutil.rmtree(d, ignore_errors=True)

    @pytest.mark.skipif(sys.platform != "win32", reason="consoles are a win32 concept")
    def test_a_detached_process_reads_none(self):
        from magent.procs import console_clients

        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            creationflags=0x00000008,  # DETACHED_PROCESS -> no console
        )
        try:
            assert console_clients([child.pid])[child.pid] is None
        finally:
            child.kill()
            child.wait()

    @pytest.mark.skipif(sys.platform != "win32", reason="consoles are a win32 concept")
    def test_an_exited_pid_reads_none(self):
        from magent.procs import console_clients

        child = subprocess.Popen([_BASE_PY, "-I", "-S", "-c", "pass"])
        child.wait()
        assert console_clients([child.pid])[child.pid] is None

    @pytest.mark.skipif(sys.platform != "win32", reason="consoles are a win32 concept")
    def test_a_tiny_timeout_reads_none_for_every_pid(self):
        from magent.procs import console_clients

        clients = console_clients([os.getpid()], timeout=0.001)
        assert clients == {os.getpid(): None}

    @pytest.mark.skipif(sys.platform != "win32", reason="consoles are a win32 concept")
    def test_a_temp_dir_that_cannot_be_made_reads_none_not_a_raise(self, monkeypatch):
        from magent.procs import console_clients

        def _full(*_a, **_k):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(tempfile, "mkdtemp", _full)
        assert console_clients([os.getpid(), 4]) == {os.getpid(): None, 4: None}

    @pytest.mark.skipif(sys.platform != "win32", reason="consoles are a win32 concept")
    def test_a_helper_that_cannot_start_reads_none_not_a_raise(
        self, monkeypatch, tmp_path
    ):
        from magent import procs

        missing = str(tmp_path / "no-such-python.exe")
        monkeypatch.setattr(procs, "_helper_python", lambda: missing)
        assert procs.console_clients([os.getpid()]) == {os.getpid(): None}

    @pytest.mark.skipif(sys.platform != "win32", reason="consoles are a win32 concept")
    def test_a_hung_helper_is_killed_at_the_deadline(self, monkeypatch):
        from magent import procs

        # A helper that never answers: the call must come back near its timeout
        # and the helper must be dead -- not waited out for its whole sleep.
        spawned: list[subprocess.Popen] = []
        real_popen = subprocess.Popen

        def _spy(*a, **kw):
            proc = real_popen(*a, **kw)
            spawned.append(proc)
            return proc

        monkeypatch.setattr(procs, "_CONSOLE_HELPER", "import time; time.sleep(30)")
        monkeypatch.setattr(subprocess, "Popen", _spy)
        try:
            t0 = time.monotonic()
            got = procs.console_clients([os.getpid()], timeout=0.5)
            elapsed = time.monotonic() - t0
            assert got == {os.getpid(): None}
            assert elapsed < 3.0
            assert len(spawned) == 1
            assert spawned[0].poll() is not None
            assert not pid_alive(spawned[0].pid)
        finally:
            for p in spawned:
                p.kill()
                p.wait()

    @pytest.mark.skipif(sys.platform != "win32", reason="consoles are a win32 concept")
    def test_the_answer_dir_is_removed(self, monkeypatch, tmp_path):
        from magent.procs import console_clients

        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        console_clients([os.getpid()])
        assert not list(tmp_path.glob("magent-con-*"))

    def test_off_windows_every_pid_is_none(self):
        from magent.procs import console_clients

        if sys.platform == "win32":
            pytest.skip("this asserts the POSIX early return")
        assert console_clients([1, 2]) == {1: None, 2: None}

    def test_no_pids_is_empty(self):
        from magent.procs import console_clients

        assert console_clients([]) == {}


class TestFiletimeToEpoch:
    def test_off_by_the_1601_epoch_offset(self):
        from magent.procs import filetime_to_epoch

        # 1601-01-01 is 0; the unix epoch is 11644473600 seconds later.
        assert filetime_to_epoch(0) == -11_644_473_600.0
        assert filetime_to_epoch(11_644_473_600 * 10_000_000) == 0.0


class TestPreciseFiletime:
    @pytest.mark.skipif(sys.platform != "win32", reason="FILETIME identity is win32")
    def test_it_is_the_wall_clock_on_the_filetime_scale(self):
        from magent.procs import filetime_to_epoch, precise_filetime

        now = precise_filetime()
        assert now is not None
        assert abs(filetime_to_epoch(now) - time.time()) < 2.0

    @pytest.mark.skipif(sys.platform != "win32", reason="FILETIME identity is win32")
    def test_it_brackets_a_process_created_between_two_reads(self):
        """reap._stop compares these two clocks: a process created before a
        read must read as earlier (else it is spared as a newcomer), and one
        created after must read as later (a pid reused after the snapshot)."""
        from magent.procs import precise_filetime, process_identity

        before = precise_filetime()
        child = subprocess.Popen(_SLEEP)
        after = precise_filetime()
        try:
            ident = process_identity(child.pid)
            assert ident is not None
            assert before is not None and after is not None
            assert before < ident.created < after
        finally:
            child.kill()
            child.wait()

    def test_off_windows_there_is_no_clock_to_bound_a_snapshot(self):
        from magent.procs import precise_filetime

        if sys.platform == "win32":
            pytest.skip("this asserts the POSIX early return")
        assert precise_filetime() is None


class TestProcessIdentity:
    @pytest.mark.skipif(sys.platform != "win32", reason="FILETIME identity is win32")
    def test_reads_a_child_we_spawned(self):
        from magent.procs import process_identity

        child = subprocess.Popen(_SLEEP)
        try:
            ident = process_identity(child.pid)
            assert ident is not None
            assert ident.image.lower() in {"python.exe", "python"}
            assert ident.created > 0
        finally:
            child.kill()
            child.wait()

    @pytest.mark.skipif(sys.platform != "win32", reason="FILETIME identity is win32")
    def test_an_exited_pid_is_none(self):
        from magent.procs import process_identity

        child = subprocess.Popen([_BASE_PY, "-I", "-S", "-c", "pass"])
        child.wait()
        assert process_identity(child.pid) is None

    def test_off_windows_is_none(self):
        from magent.procs import process_identity

        if sys.platform == "win32":
            pytest.skip("this asserts the POSIX early return")
        assert process_identity(os.getpid()) is None


class TestTerminateVerified:
    @pytest.mark.skipif(sys.platform != "win32", reason="TerminateProcess is win32")
    def test_the_right_identity_is_killed_and_bytes_returned(self, own_pids):
        from magent.procs import process_identity, terminate_verified

        child = subprocess.Popen(_SLEEP)
        try:
            ident = process_identity(child.pid)
            assert ident is not None
            own_pids.add(child.pid, ident)
            freed = terminate_verified(child.pid, ident)
            assert freed is not None and freed > 0
            for _ in range(50):
                if child.poll() is not None:
                    break
                time.sleep(0.1)
            assert child.poll() is not None
        finally:
            child.kill()
            child.wait()

    @pytest.mark.skipif(sys.platform != "win32", reason="TerminateProcess is win32")
    def test_a_wrong_create_time_kills_nothing(self, own_pids):
        from magent.procs import ProcessIdentity, process_identity, terminate_verified

        child = subprocess.Popen(_SLEEP)
        try:
            ident = process_identity(child.pid)
            assert ident is not None
            wrong = ProcessIdentity(image=ident.image, created=ident.created + 999)
            # Registered as the identity the call names, so it reaches the
            # primitive whose check is under test; no process has that one.
            own_pids.add(child.pid, wrong)
            assert terminate_verified(child.pid, wrong) is None
            assert child.poll() is None  # still alive
        finally:
            child.kill()
            child.wait()

    @pytest.mark.skipif(sys.platform != "win32", reason="TerminateProcess is win32")
    def test_an_exited_pid_returns_none(self, own_pids):
        from magent.procs import ProcessIdentity, terminate_verified

        child = subprocess.Popen([_BASE_PY, "-I", "-S", "-c", "pass"])
        pid = child.pid
        gone = ProcessIdentity(image="python.exe", created=1)
        own_pids.add(pid, gone)
        child.wait()
        assert terminate_verified(pid, gone) is None

    @pytest.mark.skipif(sys.platform != "win32", reason="TerminateProcess is win32")
    def test_the_agent_subtree_dies_and_its_parent_shell_lives(self, own_pids):
        """shell stand-in -> agent -> grandchild, each the next one's real
        parent. Killing ``process_tree(agent)`` takes the agent and the
        grandchild and never the shell. (The deepest-first ORDER is reap._stop's
        and is pinned there; this pins that each link is killable and that the
        tree stops at the agent.)"""
        from magent.procs import (
            ProcessIdentity,
            process_identity,
            process_tree,
            snapshot_processes,
            terminate_verified,
        )

        # sys.executable inside a -I -S base process is that base itself, so
        # no launcher layer is ever inserted between the links.
        agent_code = (
            "import subprocess,sys,time;"
            "g=subprocess.Popen([sys.executable,'-I','-S','-c','import time;time.sleep(40)']);"
            "print(g.pid,flush=True);"
            "time.sleep(40)"
        )
        shell_code = (
            "import subprocess,sys,time;"
            "a=subprocess.Popen([sys.executable,'-I','-S','-c',sys.argv[1]],"
            "stdout=subprocess.PIPE,text=True);"
            "print(a.pid,flush=True);"
            "print(a.stdout.readline().strip(),flush=True);"
            "time.sleep(40)"
        )
        shell = subprocess.Popen(
            [_BASE_PY, "-I", "-S", "-c", shell_code, agent_code],
            stdout=subprocess.PIPE,
            text=True,
        )
        # Identities are captured the moment each pid is known: cleanup goes
        # through the identity-guarded kill, never at a bare pid that may have
        # been freed and reused by then.
        links: list[tuple[int, ProcessIdentity]] = []
        try:
            assert shell.stdout is not None
            agent_pid = int(shell.stdout.readline().strip())
            agent_ident = process_identity(agent_pid)
            assert agent_ident is not None
            links.append((agent_pid, agent_ident))
            own_pids.add(agent_pid, agent_ident)
            grand_pid = int(shell.stdout.readline().strip())
            grand_ident = process_identity(grand_pid)
            assert grand_ident is not None
            links.append((grand_pid, grand_ident))
            own_pids.add(grand_pid, grand_ident)

            tree = process_tree(agent_pid, snapshot_processes() or [])
            assert {pid for _img, pid, _ppid in tree} == {agent_pid, grand_pid}
            for _img, pid, _ppid in reversed(tree):
                ident = process_identity(pid)
                assert ident is not None
                assert terminate_verified(pid, ident) is not None

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and any(
                process_identity(pid) == ident for pid, ident in links
            ):
                time.sleep(0.1)
            assert process_identity(grand_pid) != grand_ident  # the grandchild died
            assert process_identity(agent_pid) != agent_ident  # the agent died
            assert shell.poll() is None  # its parent shell is untouched
        finally:
            for pid, ident in reversed(links):
                terminate_verified(pid, ident)  # no-op once dead or reused
            shell.kill()
            shell.wait()
            if shell.stdout is not None:
                shell.stdout.close()

    def test_off_windows_returns_none(self, own_pids):
        from magent.procs import ProcessIdentity, terminate_verified

        if sys.platform == "win32":
            pytest.skip("this asserts the POSIX early return")
        # Registered so the call reaches the primitive; the early return (and,
        # past it, the image mismatch) means it never opens this process.
        nobody = ProcessIdentity(image="x", created=1)
        own_pids.add(os.getpid(), nobody)
        assert terminate_verified(os.getpid(), nobody) is None


class _FakeKernel32:
    """kernel32 as terminate_verified sees it, answered from fields: each read
    can fail (returns 0), and every TerminateProcess is recorded. A live-process
    identity by default -- image ``claude.exe``, created ``5000``."""

    def __init__(
        self,
        *,
        open_ok: bool = True,
        exit_code: int | None = 259,  # STILL_ACTIVE; None = the read fails
        image: str | None = "C:\\Users\\alice\\.local\\bin\\claude.exe",
        created: int | None = 5000,
        terminate_ok: bool = True,
    ) -> None:
        self.open_ok = open_ok
        self.exit_code = exit_code
        self.image = image
        self.created = created
        self.terminate_ok = terminate_ok
        self.terminated: list[int] = []

        def _memory_info(handle, counters, cb):
            return 0  # the byte count is not what these pins are about

        # A plain function, so _private_bytes can set .argtypes on it.
        self.K32GetProcessMemoryInfo = _memory_info

    def OpenProcess(self, rights, inherit, pid):
        return 42 if self.open_ok else 0

    def CloseHandle(self, handle):
        return 1

    def GetExitCodeProcess(self, handle, code_ref):
        if self.exit_code is None:
            return 0
        code_ref._obj.value = self.exit_code
        return 1

    def QueryFullProcessImageNameW(self, handle, flags, buf, size_ref):
        if self.image is None:
            return 0
        buf.value = self.image
        return 1

    def GetProcessTimes(self, handle, creation, exit_, kernel, user):
        if self.created is None:
            return 0
        creation._obj.dwLowDateTime = self.created & 0xFFFFFFFF
        creation._obj.dwHighDateTime = self.created >> 32
        return 1

    def TerminateProcess(self, handle, code):
        self.terminated.append(handle)
        return 1 if self.terminate_ok else 0


@pytest.mark.skipif(sys.platform != "win32", reason="TerminateProcess is win32")
class TestTerminateVerifiedGuards:
    """Unknown never kills: every read that fails, and every identity that
    differs in image OR creation time, refuses before TerminateProcess. Driven
    through a fake kernel32, because a real exited pid fails at the image read
    and would hide which guard did the refusing."""

    EXPECTED = ("claude.exe", 5000)

    @pytest.fixture(autouse=True)
    def _the_fake_pid_is_ours(self, own_pids):
        from magent.procs import ProcessIdentity

        # Answered by the fake kernel32, not the OS.
        own_pids.add(4242, ProcessIdentity(*self.EXPECTED))

    def _run(self, own_pids, **fake_kwargs):
        from magent import procs

        # The fake OS sits where the real one does: behind the kernel32 the
        # conftest guard hands out, so a kill here passes both guards.
        fake = _FakeKernel32(**fake_kwargs)
        own_pids.real_kernel32 = lambda: fake
        image, created = self.EXPECTED
        result = procs.terminate_verified(
            4242, procs.ProcessIdentity(image=image, created=created)
        )
        return result, fake

    def test_the_matching_identity_is_killed(self, own_pids):
        # The control: without it every "never called" below would be vacuous.
        result, fake = self._run(own_pids)
        assert result == 0
        assert fake.terminated == [42]

    @pytest.mark.parametrize(
        "fake_kwargs",
        [
            pytest.param({"open_ok": False}, id="unopenable"),
            pytest.param({"exit_code": None}, id="exit-code-unreadable"),
            pytest.param({"exit_code": 0}, id="already-exited"),
            pytest.param({"image": None}, id="image-unreadable"),
            pytest.param({"created": None}, id="times-unreadable"),
            pytest.param({"image": "C:\\bin\\node.exe"}, id="same-time-other-image"),
            pytest.param({"created": 5001}, id="same-image-other-time"),
        ],
    )
    def test_anything_short_of_the_same_identity_kills_nothing(
        self, own_pids, fake_kwargs
    ):
        result, fake = self._run(own_pids, **fake_kwargs)
        assert result is None
        assert fake.terminated == []

    def test_a_failed_terminate_reports_nothing_killed(self, own_pids):
        result, fake = self._run(own_pids, terminate_ok=False)
        assert result is None
        assert fake.terminated == [42]


class TestNoDirectConsoleApiOutsideTheHelper:
    """AttachConsole/FreeConsole/AllocConsole must appear ONLY inside the
    helper source string -- magent must never swap ITS OWN process's console."""

    def test_no_src_module_touches_the_console_swap_api(self):
        import ast
        from pathlib import Path

        import magent

        banned = {"AttachConsole", "FreeConsole", "AllocConsole"}
        root = Path(magent.__file__).parent
        modules = sorted(root.rglob("*.py"))
        assert any(p.name == "procs.py" for p in modules)
        for path in modules:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                name = (
                    node.attr
                    if isinstance(node, ast.Attribute)
                    else node.id
                    if isinstance(node, ast.Name)
                    else None
                )
                if name in banned:
                    raise AssertionError(
                        f"{name} is referenced in {path.relative_to(root)}; "
                        "console swaps belong only inside procs._CONSOLE_HELPER's "
                        "source string"
                    )


class TestBootTime:
    """When this machine last booted -- the fact that tells "a daemon died
    while the machine was up" (a crash) from "a daemon that has not run since
    the last restart" (nothing crashed; the machine went down under it). The
    user-facing surfaces used to call both CRASHED."""

    def test_it_is_known_on_every_os_the_suite_runs_on(self):
        # Windows (GetTickCount64), Linux (/proc/stat btime) and macOS
        # (kern.boottime) all answer, so None here is a broken probe, not an
        # unknown platform.
        from magent.procs import boot_time

        boot = boot_time()
        assert boot is not None
        assert 0 < time.time() - boot < 20 * 365 * 24 * 3600

    def test_the_running_test_process_started_after_it(self):
        # The one ordering every OS guarantees: nothing alive now was written
        # before the boot. A file this process creates must not predate it.
        from magent.procs import predates_boot

        assert predates_boot(time.time()) is False

    def test_linux_btime_is_read_from_proc_stat(self):
        from magent.procs import _btime_from_proc_stat

        text = (
            "cpu  10 0 5 100 0 0 0 0 0 0\n"
            "intr 1 2 3\n"
            "ctxt 12345\n"
            "btime 1727654400\n"
            "processes 99\n"
        )
        assert _btime_from_proc_stat(text) == 1727654400.0

    def test_a_proc_stat_without_btime_is_unknown(self):
        from magent.procs import _btime_from_proc_stat

        assert _btime_from_proc_stat("cpu 1 2 3\n") is None
        assert _btime_from_proc_stat("btime not-a-number\n") is None
        assert _btime_from_proc_stat("") is None

    def test_a_timestamp_before_the_boot_predates_it(self, monkeypatch):
        from magent import procs

        monkeypatch.setattr(procs, "boot_time", lambda: 1000.0)

        assert procs.predates_boot(1000.0 - procs.BOOT_CLOCK_SLACK_S - 1) is True
        assert procs.predates_boot(1000.0) is False
        assert procs.predates_boot(5000.0) is False

    def test_a_timestamp_just_before_the_boot_is_given_the_benefit_of_the_doubt(
        self, monkeypatch
    ):
        # The boot time is derived, not recorded: Windows computes it as "now
        # minus uptime", so a clock correction after the boot moves it, and
        # Linux's btime is rounded. A pid file a LIVE listener wrote seconds
        # after the boot must never read as pre-boot -- that verdict discards it
        # and a supervisor starts a second listener beside the first (two
        # keyboard hooks, every Alt+V pasted twice). Erring the other way only
        # costs a very fast restart the old wording.
        from magent import procs

        monkeypatch.setattr(procs, "boot_time", lambda: 1000.0)

        assert procs.BOOT_CLOCK_SLACK_S >= 10
        assert procs.predates_boot(1000.0 - procs.BOOT_CLOCK_SLACK_S + 1) is False

    def test_an_unknown_boot_time_never_claims_a_timestamp_predates_it(
        self, monkeypatch
    ):
        # The fallback is today's behaviour: with no boot time, nothing is
        # re-labelled, so every caller keeps reading exactly what it read before.
        from magent import procs

        monkeypatch.setattr(procs, "boot_time", lambda: None)

        assert procs.predates_boot(0.0) is False

    def test_a_failing_probe_is_unknown_not_an_exception(self, monkeypatch):
        # A status line must never die of a boot-time probe.
        from magent import procs

        def _boom() -> float | None:
            raise OSError("probe failed")

        monkeypatch.setattr(procs, "_probe_boot_time", _boom)

        assert procs.boot_time() is None
