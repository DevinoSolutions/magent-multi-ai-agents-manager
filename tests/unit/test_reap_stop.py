"""reap._stop: a real 3-deep chain killed deepest-first, the pane shell kept,
the guards that refuse the whole stop, the confirm poll's root_alive, and (on
a fake process table) the clock that keeps a pid reused after a snapshot out
of the kill list.

Every process a test here can kill is one it spawned itself: the real-process
tests hand _stop the live table cut down to their own pids, their kills go
through a guard that fails the test on any process it did not register (by
identity, so a reused pid is a stranger), and the fake-table tests never
reach a real process at all. (The guards -- _stop's default snapshot fails
the test, and a kill of an unregistered process fails it before the OS --
live in tests/conftest.py, under every test.)"""

from __future__ import annotations

import subprocess
import sys
import time
from typing import TYPE_CHECKING

import pytest

from magent import procs, reap
from magent.procs import ProcessIdentity
from magent.sessions import AGENT_TOOLS
from tests.conftest import FakePlatform
from tests.unit.test_reap_park import _sig as _park_sig

if TYPE_CHECKING:
    from collections.abc import Callable

_BASE_PY = getattr(sys, "_base_executable", None) or sys.executable


def _own_table(*pids: int):
    """The live table cut down to the pids this test spawned, so _stop can see
    -- and so kill -- nothing else."""
    own = set(pids)
    return lambda: [row for row in procs.snapshot_processes() or [] if row[1] in own]


def _sig(*, agent_pid, agent_image, agent_created, pane_pid):
    # Only the fields _stop reads: the tree's root (pane) + the agent identity.
    return reap.Signals(
        psmux_session="demo",
        session_id="sid",
        tool="claude",
        cmd="claude",
        tree=(("pwsh.exe", pane_pid, 1), ("python.exe", agent_pid, pane_pid)),
        agent_pid=agent_pid,
        agent_created=agent_created,
        agent_image=agent_image,
        agent_start=0.0,
        cwd="/x",
        in_scope=True,
        shares_cwd=False,
        tree_known=True,
        root_is_shell=True,
        same_logon_session=True,
        agent_unreadable=False,
        agent_count=1,
        image_is_agent=True,
        cwd_matches=True,
        claude_status="idle",
        claude_status_ts=0.0,
        record_unreadable=False,
        record_present=True,
        record_state="done",
        record_session_id="sid",
        record_ts=0.0,
        transcript_present=True,
        transcript_mtime=0.0,
        pane_state="idle",
        draft="",
        now=0.0,
        threshold_s=1.0,
    )


@pytest.mark.skipif(
    sys.platform != "win32", reason="TerminateProcess/identity are win32"
)
def test_the_agent_subtree_is_killed_and_the_pane_shell_is_kept(own_pids):
    """pane shell stand-in -> agent -> grandchild, each the next one's real
    parent, as in a psmux pane: the grandchild then the agent die, and the
    shell -- the agent's parent, so never in its tree -- lives."""
    # sys.executable inside a -I -S base process is that base itself, so no
    # launcher layer is ever inserted between the links.
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
    # through the identity-guarded kill, never at a bare pid that may have been
    # freed and reused by then.
    links: list[tuple[int, ProcessIdentity]] = []
    try:
        assert shell.stdout is not None
        agent_pid = int(shell.stdout.readline().strip())
        agent_ident = procs.process_identity(agent_pid)
        assert agent_ident is not None
        links.append((agent_pid, agent_ident))
        own_pids.add(agent_pid, agent_ident)
        grand_pid = int(shell.stdout.readline().strip())
        grand_ident = procs.process_identity(grand_pid)
        assert grand_ident is not None
        links.append((grand_pid, grand_ident))
        own_pids.add(grand_pid, grand_ident)

        sig = _sig(
            agent_pid=agent_pid,
            agent_image=agent_ident.image,
            agent_created=agent_ident.created,
            pane_pid=shell.pid,
        )
        result = reap._stop(sig, snapshot=_own_table(shell.pid, agent_pid, grand_pid))
        assert result.aborted is None
        assert result.stopped == [grand_pid, agent_pid]  # deepest first
        assert result.freed > 0
        # The confirm poll saw both identities die (the exit code is set before
        # the process object is signalled, so identity is the reading to trust).
        assert result.survived == []
        assert result.root_alive is False
        assert procs.process_identity(grand_pid) != grand_ident
        assert shell.poll() is None  # the pane shell survives
    finally:
        for pid, ident in reversed(links):
            procs.terminate_verified(pid, ident)  # no-op once dead or reused
        shell.kill()
        shell.wait()
        if shell.stdout is not None:
            shell.stdout.close()


@pytest.mark.skipif(sys.platform != "win32", reason="identity is win32")
def test_a_wrong_root_identity_aborts_and_kills_nothing():
    child = subprocess.Popen(
        [_BASE_PY, "-I", "-S", "-c", "import time; time.sleep(40)"]
    )
    try:
        ident = procs.process_identity(child.pid)
        assert ident is not None
        sig = _sig(
            agent_pid=child.pid,
            agent_image=ident.image,
            agent_created=ident.created + 999,  # wrong create time
            pane_pid=1,
        )
        result = reap._stop(sig, snapshot=_own_table(child.pid))
        assert result.aborted == "root-identity"
        assert child.poll() is None  # untouched
    finally:
        child.kill()
        child.wait()


@pytest.mark.skipif(sys.platform != "win32", reason="identity is win32")
def test_the_pane_pid_being_in_the_tree_aborts():
    child = subprocess.Popen(
        [_BASE_PY, "-I", "-S", "-c", "import time; time.sleep(40)"]
    )
    try:
        ident = procs.process_identity(child.pid)
        assert ident is not None
        # pane_pid == the agent pid: the pane would be in the kill list -> refuse
        sig = _sig(
            agent_pid=child.pid,
            agent_image=ident.image,
            agent_created=ident.created,
            pane_pid=child.pid,
        )
        result = reap._stop(sig, snapshot=_own_table(child.pid))
        assert result.aborted == "pane-in-tree"
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()


def test_off_windows_aborts_because_the_snapshot_is_unreadable():
    if sys.platform == "win32":
        pytest.skip("this asserts the POSIX snapshot-None early abort")
    sig = _sig(agent_pid=1, agent_image="python", agent_created=1, pane_pid=2)
    # The one explicit live table in the suite: None off win32, and any kill it
    # could ever lead to is refused by the own-pids guard (nothing registered).
    result = reap._stop(sig, snapshot=procs.snapshot_processes)
    assert result.aborted == "snapshot-unreadable"
    assert result.root_alive is True


# --- real processes, own pids only: what the process table can hand _stop ------


def _spawn(spawned: list) -> subprocess.Popen:
    """A sleeper this test owns, returned once its identity reads. Cleanup goes
    through the Popen handle, never a bare pid."""
    proc = subprocess.Popen([_BASE_PY, "-I", "-S", "-c", "import time; time.sleep(60)"])
    spawned.append(proc)
    deadline = time.monotonic() + 5
    while procs.process_identity(proc.pid) is None and time.monotonic() < deadline:
        time.sleep(0.01)
    return proc


def _reap_all(spawned: list) -> None:
    for proc in spawned:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def _real_sig(agent: subprocess.Popen, shell: subprocess.Popen) -> reap.Signals:
    ident = procs.process_identity(agent.pid)
    assert ident is not None
    return _sig(
        agent_pid=agent.pid,
        agent_image=ident.image,
        agent_created=ident.created,
        pane_pid=shell.pid,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="identity + TerminateProcess")
@pytest.mark.parametrize("which_pass", ["main", "straggler"])
def test_a_process_created_after_the_snapshot_is_never_killed(which_pass, own_pids):
    """What a pid reuse leaves: the snapshot says 'child of the agent' for a
    pid that, by the time _stop reads identities, names a process created
    AFTER that snapshot. The snapshot below spawns that newcomer inside its own
    call, after the clock read that bounds it -- main pass or straggler pass."""
    spawned: list[subprocess.Popen] = []
    try:
        shell, agent = _spawn(spawned), _spawn(spawned)
        sig = _real_sig(agent, shell)
        own_pids.add(agent.pid, ProcessIdentity(sig.agent_image, sig.agent_created))
        root_row = (sig.agent_image, agent.pid, shell.pid)
        box: dict[str, subprocess.Popen] = {}
        calls = [0]

        def snapshot() -> list[tuple[str, int, int]]:
            calls[0] += 1
            first = calls[0] == 1
            if (which_pass == "main") == first:
                time.sleep(0.05)
                box["new"] = _spawn(spawned)
                row = (sig.agent_image, box["new"].pid, agent.pid)
                return [root_row, row] if first else [row]
            return [root_row] if first else []

        result = reap._stop(sig, snapshot=snapshot)
        newcomer = box["new"]
        assert result.aborted is None
        assert agent.wait(timeout=reap.KILL_SETTLE_S) == 1  # the agent itself died
        assert newcomer.pid not in result.stopped
        assert newcomer.poll() is None  # the newcomer lives
        assert shell.poll() is None
    finally:
        _reap_all(spawned)


@pytest.mark.skipif(sys.platform != "win32", reason="identity + TerminateProcess")
def test_a_real_process_older_than_the_agent_listed_under_it_lives(own_pids):
    """Stale-ppid adoption: Toolhelp keeps a dead parent's pid, so a process
    OLDER than the agent can be listed under it. It is never ours to kill."""
    spawned: list[subprocess.Popen] = []
    try:
        shell = _spawn(spawned)
        stranger = _spawn(spawned)  # created BEFORE the agent
        time.sleep(0.05)
        agent = _spawn(spawned)
        sig = _real_sig(agent, shell)
        own_pids.add(agent.pid, ProcessIdentity(sig.agent_image, sig.agent_created))
        rows = [
            (sig.agent_image, agent.pid, shell.pid),
            (sig.agent_image, stranger.pid, agent.pid),  # the stale-ppid row
        ]
        result = reap._stop(sig, snapshot=lambda: list(rows))
        assert result.aborted is None
        assert agent.wait(timeout=reap.KILL_SETTLE_S) == 1
        assert stranger.pid not in result.stopped
        assert stranger.poll() is None
    finally:
        _reap_all(spawned)


# --- a fake process table: the windows no real test can open, on every OS -----

_PANE, _AGENT, _CHILD, _REUSED, _GRAND = 10, 20, 30, 31, 35
_STRAGGLER, _LATE, _ELDER, _ELDER_KID = 40, 41, 42, 43
_FIRST_READ, _SECOND_READ = 1_000, 2_000  # the clock before each snapshot
_ROOT = ProcessIdentity("claude.exe", 500)


def _default_idents() -> dict[int, ProcessIdentity]:
    return {
        _AGENT: _ROOT,
        _CHILD: ProcessIdentity("node.exe", 600),
        _GRAND: ProcessIdentity("node.exe", 650),
        # Listed as the agent's child; that process exited and the pid was
        # reused by one created after the first read.
        _REUSED: ProcessIdentity("other.exe", _FIRST_READ + 1),
        # Spawned by _CHILD between the snapshots (so after the first read): a
        # real straggler.
        _STRAGGLER: ProcessIdentity("node.exe", _FIRST_READ + 500),
        # A reuse of a straggler's pid after the second read.
        _LATE: ProcessIdentity("other.exe", _SECOND_READ + 1),
        # Older than _CHILD yet lists its pid as parent: its real parent was an
        # earlier process at that pid.
        _ELDER: ProcessIdentity("svc.exe", 100),
    }


def _default_snapshots() -> list[list[tuple[str, int, int]]]:
    return [
        [
            ("pwsh.exe", _PANE, 1),
            ("claude.exe", _AGENT, _PANE),
            ("node.exe", _CHILD, _AGENT),
            ("node.exe", _REUSED, _AGENT),
            ("node.exe", _GRAND, _CHILD),
        ],
        [
            ("pwsh.exe", _PANE, 1),
            ("other.exe", _REUSED, 999),
            ("node.exe", _STRAGGLER, _CHILD),
            ("node.exe", _LATE, _CHILD),
            ("svc.exe", _ELDER, _CHILD),
        ],
    ]


class _FakeProcesses:
    """What _stop sees of the OS: identities by pid, the snapshots it takes in
    order, and the clock. Records every kill, and the order the clock, the
    snapshots and the kills were reached in. A kill removes the identity, so
    the confirm poll reads the process dead. Defaults to the agent's tree with
    one listed pid reused after the first read, then a real straggler, a reuse
    of a straggler's pid after the second read, and an elder.

    ``clock`` holds the read that bounds each snapshot; a snapshot spends it.
    A read taken between the snapshots -- before a main-pass kill -- answers
    the next snapshot's read: a kill happens before that read, never later.
    (What a kill read bounds is pinned on _TimedWorld, where every call has its
    own time.)"""

    def __init__(
        self,
        idents: dict[int, ProcessIdentity] | None = None,
        snapshots: list[list[tuple[str, int, int]]] | None = None,
    ) -> None:
        self.idents = _default_idents() if idents is None else dict(idents)
        self.snapshots = _default_snapshots() if snapshots is None else snapshots
        self.clock: list[int | None] = [_FIRST_READ, _SECOND_READ]
        self.reads: list[str] = []
        self.killed: list[int] = []

    def snapshot(self) -> list[tuple[str, int, int]] | None:
        self.reads.append("snapshot")
        if self.clock:
            self.clock.pop(0)
        return self.snapshots.pop(0) if self.snapshots else []

    def bound(self) -> int | None:
        self.reads.append("clock")
        return self.clock[0] if self.clock else None

    def process_identity(self, pid: int) -> ProcessIdentity | None:
        return self.idents.get(pid)

    def terminate_verified(self, pid: int, expected: ProcessIdentity) -> int | None:
        self.reads.append("kill")
        if self.idents.get(pid) != expected:
            return None
        self.killed.append(pid)
        del self.idents[pid]
        return 100


def _install(monkeypatch: pytest.MonkeyPatch, table: _FakeProcesses) -> _FakeProcesses:
    monkeypatch.setattr(procs, "process_identity", table.process_identity)
    monkeypatch.setattr(procs, "terminate_verified", table.terminate_verified)
    return table


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeProcesses:
    return _install(monkeypatch, _FakeProcesses())


def _settle_clock() -> tuple[Callable[[], float], Callable[[float], None]]:
    """A monotonic clock only a sleep advances. The confirm poll reaches its
    deadline after KILL_SETTLE_S of sleeps, so a process that never dies ends
    the stop with a survivor -- a failed assertion -- instead of spinning the
    test forever."""
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    return (lambda: clock[0]), sleep


def _stop_on(table: _FakeProcesses) -> reap.StopResult:
    sig = _sig(
        agent_pid=_AGENT,
        agent_image=_ROOT.image,
        agent_created=_ROOT.created,
        pane_pid=_PANE,
    )
    monotonic, sleep = _settle_clock()
    return reap._stop(
        sig,
        monotonic=monotonic,
        sleep=sleep,
        snapshot=table.snapshot,
        bound=table.bound,
    )


class TestTheSnapshotClock:
    def test_the_tree_dies_deepest_first_then_the_straggler(self, fake):
        result = _stop_on(fake)
        assert result.aborted is None
        assert result.stopped == [_GRAND, _CHILD, _AGENT, _STRAGGLER]
        assert fake.killed == result.stopped
        assert result.freed == 400
        assert result.survived == []
        assert result.root_alive is False

    def test_a_pid_reused_after_the_snapshot_is_never_killed(self, fake):
        _stop_on(fake)
        assert _REUSED not in fake.killed
        assert _REUSED in fake.idents  # the newcomer runs on

    def test_a_straggler_created_after_the_second_read_is_never_killed(self, fake):
        _stop_on(fake)
        assert _STRAGGLER in fake.killed  # created between the two snapshots
        assert _LATE not in fake.killed
        assert _LATE in fake.idents

    def test_an_elder_listing_a_killed_pid_as_parent_is_never_killed(self, fake):
        _stop_on(fake)
        assert _ELDER not in fake.killed
        assert _ELDER in fake.idents

    def test_the_clock_is_read_before_each_snapshot_and_each_main_pass_kill(self, fake):
        _stop_on(fake)
        assert fake.reads == [
            "clock",
            "snapshot",
            *["clock", "kill"] * 3,  # _GRAND, _CHILD, _AGENT
            "clock",
            "snapshot",
            "kill",  # _STRAGGLER: nothing under it is ever looked at
        ]

    def test_a_process_created_at_the_read_itself_is_not_the_snapshots(self, fake):
        # Only a creation time strictly before the read proves the process
        # existed when the snapshot was taken.
        fake.idents[_REUSED] = ProcessIdentity("node.exe", _FIRST_READ)
        _stop_on(fake)
        assert _REUSED not in fake.killed

    def test_no_clock_refuses_the_stop(self, fake):
        fake.clock = [None]
        result = _stop_on(fake)
        assert result.aborted == "clock-unreadable"
        assert fake.killed == []
        assert result.root_alive is True

    def test_no_second_clock_kills_no_straggler(self, fake):
        fake.clock = [_FIRST_READ, None]
        result = _stop_on(fake)
        assert result.aborted is None
        assert fake.killed == [_GRAND, _CHILD, _AGENT]
        assert _STRAGGLER in fake.idents
        # No bound, no look: the kills' own reads come back None too.
        assert fake.reads == ["clock", "snapshot", *["clock", "kill"] * 3, "clock"]


def _straggler_table(*stragglers: tuple[int, int, int]) -> _FakeProcesses:
    """The agent and one child, then a second snapshot listing each
    ``(pid, parent, created)``."""
    idents = {_AGENT: _ROOT, _CHILD: ProcessIdentity("node.exe", 600)}
    idents |= {pid: ProcessIdentity("node.exe", c) for pid, _pp, c in stragglers}
    return _FakeProcesses(
        idents=idents,
        snapshots=[
            [("claude.exe", _AGENT, _PANE), ("node.exe", _CHILD, _AGENT)],
            [("node.exe", pid, parent) for pid, parent, _c in stragglers],
        ],
    )


class _TimedWorld:
    """A fake OS on one clock that ticks on every call _stop makes into it, so
    each read and each kill has its own time, in the order _stop makes them --
    however many reads it takes. The agent has one child, killed first. The
    second snapshot lists what ``stragglers(world)`` returns, so a straggler's
    creation time can be set against the kills: ``read_before[pid]`` is the
    last clock read before that pid's kill, ``death[pid]`` the kill itself.

    ``unreadable`` names reads (1-based, in call order) that come back None.
    ``step_back`` winds the clock back by that much after the agent's kill, the
    last one of the main pass: a system clock stepped backwards."""

    def __init__(
        self,
        stragglers: Callable[[_TimedWorld], list[tuple[int, int, int]]],
        *,
        unreadable: frozenset[int] = frozenset(),
        step_back: int = 0,
    ) -> None:
        self.now = 100
        self.idents = {
            _AGENT: ProcessIdentity("claude.exe", 5),
            _CHILD: ProcessIdentity("node.exe", 6),
        }
        self.stragglers = stragglers
        self.unreadable = unreadable
        self.step_back = step_back
        self.nreads = 0
        self.last_read: int | None = None
        self.read_before: dict[int, int | None] = {}
        self.death: dict[int, int] = {}
        self.killed: list[int] = []
        self.snaps = 0

    def _tick(self) -> int:
        self.now += 10
        return self.now

    def bound(self) -> int | None:
        self.nreads += 1
        t = self._tick()
        self.last_read = None if self.nreads in self.unreadable else t
        return self.last_read

    def snapshot(self) -> list[tuple[str, int, int]]:
        self._tick()
        self.snaps += 1
        if self.snaps == 1:
            return [("claude.exe", _AGENT, _PANE), ("node.exe", _CHILD, _AGENT)]
        rows = []
        for pid, parent, created in self.stragglers(self):
            self.idents[pid] = ProcessIdentity("node.exe", created)
            rows.append(("node.exe", pid, parent))
        return rows

    def process_identity(self, pid: int) -> ProcessIdentity | None:
        self._tick()
        return self.idents.get(pid)

    def terminate_verified(self, pid: int, expected: ProcessIdentity) -> int | None:
        self.read_before[pid] = self.last_read
        t = self._tick()
        if self.idents.get(pid) != expected:
            return None
        self.killed.append(pid)
        self.death[pid] = t
        del self.idents[pid]
        if pid == _AGENT:
            self.now -= self.step_back
        return 100

    def stop(self, monkeypatch: pytest.MonkeyPatch) -> reap.StopResult:
        monkeypatch.setattr(procs, "process_identity", self.process_identity)
        monkeypatch.setattr(procs, "terminate_verified", self.terminate_verified)
        sig = _sig(
            agent_pid=_AGENT, agent_image="claude.exe", agent_created=5, pane_pid=_PANE
        )
        monotonic, sleep = _settle_clock()
        return reap._stop(
            sig,
            monotonic=monotonic,
            sleep=sleep,
            snapshot=self.snapshot,
            bound=self.bound,
        )


class TestTheStragglerBound:
    """A straggler is one a parent we killed made: created after that parent,
    before the read taken just before its kill -- once killed, its pid is free,
    and a process that reuses it lists its own children under it -- and before
    the second snapshot's read."""

    def test_a_child_of_a_process_that_reused_a_killed_pid_is_never_killed(
        self, monkeypatch
    ):
        # After _CHILD dies its pid names a stranger, whose child 60 is listed
        # under _CHILD. 61 is the genuine straggler, made by _CHILD before it.
        world = _TimedWorld(
            lambda w: [
                (60, _CHILD, w.death[_CHILD] + 1),
                (61, _CHILD, (w.read_before[_CHILD] or 0) - 1),
            ]
        )
        world.stop(monkeypatch)
        assert 61 in world.killed
        assert 60 not in world.killed
        assert 60 in world.idents

    def test_a_straggler_created_at_its_parents_kill_read_is_not_killed(
        self, monkeypatch
    ):
        world = _TimedWorld(lambda w: [(60, _CHILD, w.read_before[_CHILD] or 0)])
        world.stop(monkeypatch)
        assert 60 not in world.killed

    def test_a_straggler_made_during_its_parents_terminate_is_neither_killed_nor_counted(
        self, monkeypatch
    ):
        # Created after the read taken just before its parent's kill, and
        # before that kill landed: the named residual. It is not killed -- and
        # not counted among the survivors either.
        world = _TimedWorld(lambda w: [(60, _CHILD, (w.read_before[_CHILD] or 0) + 1)])
        result = world.stop(monkeypatch)
        assert (
            (world.read_before[_CHILD] or 0)
            < world.idents[60].created
            < world.death[_CHILD]
        )
        assert 60 not in world.killed
        assert 60 not in result.survived

    def test_no_kill_read_means_no_straggler_under_that_parent(self, monkeypatch):
        # Read 2 is the one just before _CHILD's kill; the agent's (read 3)
        # and the second snapshot's (read 4) are fine.
        world = _TimedWorld(
            lambda w: [(60, _CHILD, 7), (62, _AGENT, 7)], unreadable=frozenset({2})
        )
        world.stop(monkeypatch)
        assert world.read_before[_CHILD] is None
        assert 60 not in world.killed
        assert 62 in world.killed

    def test_a_clock_stepped_back_after_the_kills_still_bounds_by_the_second_read(
        self, monkeypatch
    ):
        # The second read comes out EARLIER than the kill reads -- yet still
        # after the parent was made, so only the second read excludes 60: the
        # lower of the two bounds holds.
        world = _TimedWorld(
            lambda w: [(60, _CHILD, (w.last_read or 0) + 1)], step_back=100
        )
        world.stop(monkeypatch)
        assert 6 < (world.last_read or 0) + 1 < (world.read_before[_CHILD] or 0)
        assert 60 not in world.killed

    def test_a_straggler_created_at_the_second_read_is_not_killed(self, monkeypatch):
        table = _install(monkeypatch, _straggler_table((60, _CHILD, _SECOND_READ)))
        _stop_on(table)
        assert 60 not in table.killed

    def test_a_straggler_created_with_its_parent_is_not_killed(self, monkeypatch):
        table = _install(monkeypatch, _straggler_table((60, _CHILD, 600)))
        _stop_on(table)
        assert 60 not in table.killed

    def test_a_straggler_between_its_parent_and_the_second_read_is_killed(
        self, monkeypatch
    ):
        # The control: without it every "not killed" above would be vacuous.
        table = _install(monkeypatch, _straggler_table((60, _CHILD, 601)))
        _stop_on(table)
        assert table.killed == [_CHILD, _AGENT, 60]


class TestAdoptedStrangers:
    """Toolhelp never updates a parent pid: a process whose parent exited keeps
    the dead pid, and whatever process reuses that pid 'adopts' it. An entry
    created before its listed parent was not made by it, so it -- and every
    process under it -- is not the agent's."""

    def test_a_stranger_under_a_child_is_pruned_with_its_subtree(self, monkeypatch):
        # claude -> node -> app (OLDER than node) -> kid (app's own child)
        table = _install(
            monkeypatch,
            _FakeProcesses(
                idents={
                    _AGENT: _ROOT,
                    _CHILD: ProcessIdentity("node.exe", 600),
                    50: ProcessIdentity("app.exe", 300),
                    51: ProcessIdentity("kid.exe", 350),
                },
                snapshots=[
                    [
                        ("claude.exe", _AGENT, _PANE),
                        ("node.exe", _CHILD, _AGENT),
                        ("app.exe", 50, _CHILD),
                        ("kid.exe", 51, 50),
                    ],
                    [],
                ],
            ),
        )
        result = _stop_on(table)
        assert result.aborted is None
        assert table.killed == [_CHILD, _AGENT]
        assert {50, 51} <= set(table.idents)
        assert result.survived == []  # strangers are not ours to count

    # Older than the agent, or younger than the agent but older than _CHILD:
    # the link that has to be ordered is the one to the LISTED parent.
    @pytest.mark.parametrize("elder_created", [100, 550])
    def test_an_elder_in_the_first_snapshot_is_never_killed_nor_its_child(
        self, fake, elder_created
    ):
        # The elder lists _CHILD as parent in the FIRST snapshot too. Its kid
        # was made by it after _CHILD and before the read: that one link is
        # ordered, but its chain back to the agent breaks at the elder.
        fake.idents[_ELDER] = ProcessIdentity("svc.exe", elder_created)
        fake.idents[_ELDER_KID] = ProcessIdentity("kid.exe", 700)
        fake.snapshots[0] += [
            ("svc.exe", _ELDER, _CHILD),
            ("kid.exe", _ELDER_KID, _ELDER),
        ]
        result = _stop_on(fake)
        assert result.aborted is None
        assert fake.killed == [_GRAND, _CHILD, _AGENT, _STRAGGLER]
        assert {_ELDER, _ELDER_KID} <= set(fake.idents)
        assert result.survived == []

    def test_a_stranger_directly_under_the_agent_is_pruned(self, monkeypatch):
        table = _install(
            monkeypatch,
            _FakeProcesses(
                idents={_AGENT: _ROOT, 50: ProcessIdentity("app.exe", 499)},
                snapshots=[
                    [("claude.exe", _AGENT, _PANE), ("app.exe", 50, _AGENT)],
                    [],
                ],
            ),
        )
        _stop_on(table)
        assert table.killed == [_AGENT]

    def test_a_child_created_with_its_parent_is_not_proven_ours(self, monkeypatch):
        # created == the parent's: not provably after it, so not provably its
        table = _install(
            monkeypatch,
            _FakeProcesses(
                idents={_AGENT: _ROOT, 50: ProcessIdentity("node.exe", 500)},
                snapshots=[
                    [("claude.exe", _AGENT, _PANE), ("node.exe", 50, _AGENT)],
                    [],
                ],
            ),
        )
        _stop_on(table)
        assert table.killed == [_AGENT]

    def test_a_newcomers_subtree_is_pruned_with_it(self, monkeypatch):
        # _REUSED was reused after the read; whatever lists it as parent goes too
        table = _install(
            monkeypatch,
            _FakeProcesses(
                idents={
                    _AGENT: _ROOT,
                    _REUSED: ProcessIdentity("other.exe", _FIRST_READ + 1),
                    52: ProcessIdentity("kid.exe", 900),
                },
                snapshots=[
                    [
                        ("claude.exe", _AGENT, _PANE),
                        ("other.exe", _REUSED, _AGENT),
                        ("kid.exe", 52, _REUSED),
                    ],
                    [],
                ],
            ),
        )
        _stop_on(table)
        assert table.killed == [_AGENT]


class TestUnknownIsNeverGone:
    """A child whose identity will not read may be the agent's and alive: it is
    left out of the kill (it cannot be verified) and counted as a survivor,
    with everything under it. Strangers are proven NOT ours and never counted."""

    def test_an_unreadable_child_and_its_subtree_count_as_survivors(self, monkeypatch):
        table = _install(
            monkeypatch,
            _FakeProcesses(
                idents={_AGENT: _ROOT, _GRAND: ProcessIdentity("node.exe", 650)},
                snapshots=[
                    [
                        ("claude.exe", _AGENT, _PANE),
                        ("node.exe", _CHILD, _AGENT),  # identity unreadable
                        ("node.exe", _GRAND, _CHILD),
                    ],
                    [],
                ],
            ),
        )
        result = _stop_on(table)
        assert result.aborted is None
        assert table.killed == [_AGENT]
        assert result.survived == [_CHILD, _GRAND]
        assert result.root_alive is False  # the park still counts

    def test_an_unreadable_straggler_counts_as_a_survivor(self, monkeypatch):
        table = _install(
            monkeypatch,
            _FakeProcesses(
                idents={_AGENT: _ROOT},
                snapshots=[
                    [("claude.exe", _AGENT, _PANE)],
                    [("node.exe", _STRAGGLER, _AGENT)],  # identity unreadable
                ],
            ),
        )
        result = _stop_on(table)
        assert table.killed == [_AGENT]
        assert result.survived == [_STRAGGLER]

    def test_an_unreadable_child_relisted_as_a_straggler_counts_once(self, monkeypatch):
        table = _install(
            monkeypatch,
            _FakeProcesses(
                idents={_AGENT: _ROOT},
                snapshots=[
                    [("claude.exe", _AGENT, _PANE), ("node.exe", _CHILD, _AGENT)],
                    [("node.exe", _CHILD, _AGENT)],
                ],
            ),
        )
        assert _stop_on(table).survived == [_CHILD]

    def test_a_newcomers_subtree_is_not_counted(self, monkeypatch):
        table = _install(
            monkeypatch,
            _FakeProcesses(
                idents={
                    _AGENT: _ROOT,
                    _REUSED: ProcessIdentity("other.exe", _FIRST_READ + 1),
                },
                snapshots=[
                    [
                        ("claude.exe", _AGENT, _PANE),
                        ("other.exe", _REUSED, _AGENT),
                        ("kid.exe", 52, _REUSED),  # unreadable, under a stranger
                    ],
                    [],
                ],
            ),
        )
        assert _stop_on(table).survived == []


def _only_the_agent(*, image: str = _ROOT.image) -> _FakeProcesses:
    return _FakeProcesses(
        idents={_AGENT: ProcessIdentity(image, _ROOT.created)},
        snapshots=[[("claude.exe", _AGENT, _PANE)], []],
    )


class TestTheGuardsRefuseTheWholeStop:
    @pytest.mark.parametrize(
        "image", ["python.exe", "notclaude.exe.old.1", "claude.exe.older"]
    )
    def test_the_right_creation_time_with_the_wrong_image(self, monkeypatch, image):
        table = _install(monkeypatch, _only_the_agent(image=image))
        result = _stop_on(table)
        assert result.aborted == "root-identity"
        assert table.killed == []

    def test_a_root_the_updater_renamed_aside_since_the_re_read(self, monkeypatch):
        # Same pid, same creation time, the image now read under the name the
        # auto-updater moved it to: the same agent, so the stop goes ahead.
        table = _install(
            monkeypatch, _only_the_agent(image="claude.exe.old.1790669558315")
        )
        result = _stop_on(table)
        assert result.aborted is None
        assert table.killed == [_AGENT]
        assert result.root_alive is False

    @pytest.mark.parametrize("server", ["psmux.exe", "pmux.exe"])
    def test_a_psmux_server_in_the_kill_list(self, monkeypatch, server):
        table = _install(
            monkeypatch,
            _FakeProcesses(
                idents={_AGENT: _ROOT, _CHILD: ProcessIdentity(server, 600)},
                snapshots=[
                    [("claude.exe", _AGENT, _PANE), (server, _CHILD, _AGENT)],
                    [],
                ],
            ),
        )
        result = _stop_on(table)
        assert result.aborted == "psmux-in-tree"
        assert table.killed == []

    def test_an_agent_missing_from_the_snapshot(self, monkeypatch):
        # Its identity still reads, but the snapshot does not list it: the
        # snapshot is what the tree is proven from.
        table = _install(monkeypatch, _only_the_agent())
        table.snapshots = [[("pwsh.exe", _PANE, 1)], []]
        result = _stop_on(table)
        assert result.aborted == "empty-tree"
        assert table.killed == []


def test_a_root_that_outlives_the_confirm_poll_reads_alive(monkeypatch):
    table = _install(monkeypatch, _only_the_agent())
    monkeypatch.setattr(procs, "terminate_verified", lambda pid, expected: None)
    clock = [0.0]

    def _sleep(seconds: float) -> None:
        clock[0] += seconds

    result = reap._stop(
        _sig(
            agent_pid=_AGENT,
            agent_image=_ROOT.image,
            agent_created=_ROOT.created,
            pane_pid=_PANE,
        ),
        monotonic=lambda: clock[0],
        sleep=_sleep,
        snapshot=table.snapshot,
        bound=table.bound,
    )
    assert result.aborted is None
    assert result.root_alive is True
    assert result.survived == [_AGENT]
    assert clock[0] >= reap.KILL_SETTLE_S  # it polled out the whole settle


def test_a_root_renamed_aside_at_its_kill_still_reads_alive(monkeypatch):
    # The updater renames the running binary aside just as the kill comes:
    # terminate_verified sees a changed identity and kills nothing, and the
    # confirm poll must still read the SAME process (pid, creation time) as
    # alive -- a dead reading here would park a live agent.
    table = _install(monkeypatch, _only_the_agent())
    renamed = ProcessIdentity("claude.exe.old.1790669558315", _ROOT.created)

    def _renamed_at_the_kill(pid: int, expected: ProcessIdentity) -> int | None:
        table.idents[pid] = renamed
        return None

    monkeypatch.setattr(procs, "terminate_verified", _renamed_at_the_kill)
    result = _stop_on(table)
    assert result.aborted is None
    assert result.root_alive is True
    assert result.survived == [_AGENT]


class TestNoKillHereReachesAForeignPid:
    """What keeps a real-process test on its own processes, pinned by what it
    does: the table _stop is handed holds only that test's pids, and a foreign
    process that gets into it anyway -- a stranger's pid, or a reuse of the
    test's own -- is refused at the kill."""

    def test_the_injected_table_is_only_the_tests_own_pids(self, monkeypatch):
        live = [("pwsh.exe", 8, 4), ("python.exe", 12, 8), ("svc.exe", 16, 12)]
        monkeypatch.setattr(procs, "snapshot_processes", lambda: live)
        assert _own_table(8, 12)() == live[:2]

    @staticmethod
    def _arm(own_pids, monkeypatch) -> _FakeProcesses:
        # The guard every test here runs under, with the fake table standing in
        # for the OS behind it: _AGENT is the test's, and _CHILD -- killed
        # first -- is listed in the table as its child. _stop kills through
        # procs, so the guard must be what procs holds before _stop runs:
        # otherwise these fake pids would reach the real primitive.
        assert procs.terminate_verified == own_pids.terminate_verified
        table = _FakeProcesses(
            idents={_AGENT: _ROOT, _CHILD: ProcessIdentity("node.exe", 600)},
            snapshots=[
                [("claude.exe", _AGENT, _PANE), ("node.exe", _CHILD, _AGENT)],
                [],
            ],
        )
        monkeypatch.setattr(procs, "process_identity", table.process_identity)
        own_pids.kill = table.terminate_verified
        own_pids.add(_AGENT, _ROOT)
        return table

    def test_a_foreign_pid_in_the_injected_table_is_never_killed(
        self, own_pids, monkeypatch
    ):
        table = self._arm(own_pids, monkeypatch)
        with pytest.raises(pytest.fail.Exception, match="did not spawn"):
            _stop_on(table)
        assert table.killed == []

    def test_every_pid_the_test_spawned_may_die(self, own_pids, monkeypatch):
        table = self._arm(own_pids, monkeypatch)
        own_pids.add(_CHILD, ProcessIdentity("node.exe", 600))
        assert _stop_on(table).stopped == [_CHILD, _AGENT]
        assert table.killed == [_CHILD, _AGENT]

    def test_a_pid_the_test_spawned_now_naming_a_stranger_is_refused(
        self, own_pids, monkeypatch
    ):
        # The test spawned a node at _CHILD (created 500); it died, and the pid
        # now names another node (created 600) that the table lists under the
        # agent. _stop verifies the reuser as itself -- only the guard's
        # identity key refuses it.
        table = self._arm(own_pids, monkeypatch)
        own_pids.add(_CHILD, ProcessIdentity("node.exe", 500))
        with pytest.raises(pytest.fail.Exception, match="did not spawn"):
            _stop_on(table)
        assert table.killed == []


def test_park_hands_stop_its_default_snapshot_and_the_conftest_guard_refuses_it():
    # _park is not reap._stop's own caller in this module: the refusal reaches
    # it from tests/conftest.py, because _park passes _stop no table of its
    # own. A _park that handed _stop any table -- the live one or an empty
    # one -- would not stop here.
    monotonic, sleep = _settle_clock()
    with pytest.raises(pytest.fail.Exception, match="live process table"):
        reap._park(
            FakePlatform(),
            _park_sig(),
            tools={"claude": AGENT_TOOLS["claude"]},
            writer=lambda *_a: pytest.fail("a record was written"),
            monotonic=monotonic,
            sleep=sleep,
        )
