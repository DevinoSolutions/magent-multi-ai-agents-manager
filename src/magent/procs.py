"""Process-lifetime leaf: the one owner of "is this pid alive?" and of "spawn
this so a dying SSH connection cannot take it with it".

Before this module, cli/background.py and hotkey.py each carried a private
``_pid_alive`` (P1-09) -- the hotkey copy existed only because hotkey.py
raises ImportError off-Windows, so nothing importable-from-anywhere owned the
check. Like paths.py / titles.py / tailnet.py this is a true leaf:
stdlib-only, no dependency on any magent module, importable by cli
commands, subsystems, and the win32-only hotkey module alike.

``spawn_unjobbed`` lives here for that leaf-ness specifically. The
job-object-breakaway recipe was born inside ``launch.spawn_detached``, but
``platform/windows.py`` -- which owns the ONE spawn that gives a psmux session
its server, and therefore the one spawn whose job membership decides whether a
user's agents outlive their SSH connection -- cannot import ``launch`` (launch
imports platform; the reverse would cycle). Two copies of a Windows process
primitive is exactly how one of them silently rots, so the primitive moved down
here and both callers reach it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    import ctypes  # for the ctypes.CDLL annotation on the win32 helpers below
    from collections.abc import Callable, Iterable

# CreateProcess flag: the new process is NOT assigned to its parent's job
# object. Windows OpenSSH puts everything a session runs into a job marked
# kill-on-close, so without this a process spawned over SSH dies with the
# connection -- including a psmux SERVER, and with it the agent it hosts.
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

# Toolhelp constants for the process snapshot: snapshot the process list, and
# the sentinel CreateToolhelp32Snapshot returns when it cannot.
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = -1
# The one error that means the walk reached the end of the snapshot. Any other
# failure of Process32NextW is a walk that stopped early.
_ERROR_NO_MORE_FILES = 18

# OpenProcess rights for ``raise_priority_above_normal``: the minimum pair that
# lets a same-user, NON-ELEVATED caller read a priority class and set it.
# PROCESS_SET_INFORMATION is the write half; PROCESS_QUERY_LIMITED_INFORMATION
# (not the full PROCESS_QUERY_INFORMATION) is the read half that a normal user
# is granted against their own processes.
PROCESS_SET_INFORMATION = 0x0200
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# The priority classes this module cares about. ABOVE_NORMAL is the only value
# ever SET; the frozenset is the only set of values it may be set FROM.
ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
NORMAL_PRIORITY_CLASS = 0x00000020
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
IDLE_PRIORITY_CLASS = 0x00000040

# A raise, never a change. HIGH (0x80) and REALTIME (0x100) are ABSENT on
# purpose: somebody -- a user, another tool, the process itself -- put a
# process there deliberately, and a sweep that ran every 30 seconds and quietly
# demoted it would be a background process fighting a foreground decision.
# GetPriorityClass answers 0 when it fails, which is in no set here, so a failed
# read can never be mistaken for a boostable NORMAL.
_RAISABLE_FROM = frozenset(
    {NORMAL_PRIORITY_CLASS, BELOW_NORMAL_PRIORITY_CLASS, IDLE_PRIORITY_CLASS}
)

# CreateProcess flag: the child gets NO console of its own, so the console
# helper can AttachConsole to each pane in turn without a console to detach
# from first. How long the whole probe may take before it is killed and every
# pid answers None.
DETACHED_PROCESS = 0x00000008
CONSOLE_PROBE_TIMEOUT_S = 5.0

# OpenProcess rights and exit-code sentinel for the identity/kill primitives.
PROCESS_TERMINATE = 0x0001
PROCESS_VM_READ = 0x0010
STILL_ACTIVE = 259  # GetExitCodeProcess for a process that has not exited

# FILETIME is 100 ns ticks since 1601-01-01; the unix epoch is this many
# seconds later. QueryFullProcessImageNameW's buffer size, in wide chars.
_FILETIME_EPOCH_OFFSET_S = 11_644_473_600
_IMAGE_BUFFER_CHARS = 32_768


def pid_alive(pid: int | None) -> bool:
    """Portable best-effort liveness check for a pid (None/0/negative: dead)."""
    if not pid or pid < 0:
        return False
    if sys.platform == "win32":
        import ctypes  # win-only: ctypes.windll doesn't exist off Windows

        k = ctypes.windll.kernel32
        handle = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            k.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    else:
        return True


def _btime_from_proc_stat(text: str) -> float | None:
    """The ``btime`` line of Linux's ``/proc/stat`` (epoch seconds), or None."""
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key == "btime":
            try:
                return float(value.strip())
            except ValueError:
                return None
    return None


def _probe_boot_time() -> float | None:
    """The per-OS read behind ``boot_time``; may raise."""
    if sys.platform == "win32":
        import ctypes  # win-only: ctypes.windll doesn't exist off Windows

        k = ctypes.windll.kernel32
        k.GetTickCount64.restype = ctypes.c_ulonglong
        return time.time() - k.GetTickCount64() / 1000.0
    if sys.platform == "darwin":
        import ctypes
        import ctypes.util

        class _Timeval(ctypes.Structure):
            _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_int32)]

        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        tv = _Timeval()
        size = ctypes.c_size_t(ctypes.sizeof(tv))
        if libc.sysctlbyname(
            b"kern.boottime", ctypes.byref(tv), ctypes.byref(size), None, 0
        ):
            return None
        return float(tv.tv_sec) + tv.tv_usec / 1e6
    with open("/proc/stat", encoding="ascii", errors="replace") as fh:
        return _btime_from_proc_stat(fh.read())


def boot_time() -> float | None:
    """When this machine last booted, in epoch seconds, or None when unknown.

    The fact that separates two things every daemon surface used to call one:
    a daemon that died while the machine was up (a crash -- something to look
    into) and a daemon whose last sign of life predates the boot (nothing
    crashed; the machine went down under it). Windows reads the uptime counter
    (``GetTickCount64``, which keeps counting through sleep), Linux the
    ``btime`` line of ``/proc/stat``, macOS ``kern.boottime``.

    Never raises: None is "we could not tell", and every caller treats it as
    exactly today's behaviour -- a status line must not die of a probe.

    One honest gap, on Windows: Fast Startup makes "Shut down" a hibernation
    of the kernel, so the uptime counter does not reset and a daemon that was
    running before a shut-down-then-power-on still reads as newer than the
    boot. A Restart (which is what the incident was) always resets it.
    """
    try:
        return _probe_boot_time()
    except (OSError, AttributeError, ValueError):
        return None


# How far before the boot a timestamp must be to count as "before the boot".
# The boot time is derived, not recorded -- Windows computes it as now minus the
# uptime, so a wall-clock correction after the boot moves it, and Linux's btime
# is rounded -- and the two ways of being wrong are not the same size. A pid
# file a LIVE listener or daemon wrote just after the boot, read as pre-boot,
# is discarded and a supervisor starts a second one beside it. A heartbeat from
# just before a very fast restart, read as newer than the boot, only gets the
# wording it had before boot_time existed.
BOOT_CLOCK_SLACK_S = 30.0


def predates_boot(timestamp: float) -> bool:
    """True when ``timestamp`` (epoch seconds) is older than the last boot by
    more than ``BOOT_CLOCK_SLACK_S``.

    False when it is not -- and False when the boot time is unknown, so an
    unknown boot never re-labels anything.
    """
    boot = boot_time()
    return boot is not None and timestamp < boot - BOOT_CLOCK_SLACK_S


def current_session_id() -> int | None:
    """The Windows logon session this process runs in, or None when unknown.

    Session 0 is the one nobody can see. Since Vista, Windows isolates SERVICES
    into logon session 0 and gives every interactive logon its own session (1,
    2, ...) with the only window station a monitor is ever composited from.
    Windows OpenSSH is a service, so EVERY process an ssh login spawns is born
    in Session 0 -- including, before this module could say so, a whole psmux
    fleet: 82 servers and 42 agents that the desktop's own magent could neither
    see (`status` called them stopped) nor kill, while psmux's shared registry
    under ``~/.psmux`` made their names unusable for the sessions the user was
    actually looking at.

    None is "we could not tell", NOT "session 0": every caller treats an
    unknown answer as interactive, because a probe that fails on some future
    Windows must not be able to stop a normal desktop launch. Off Windows this
    is always None -- POSIX has no logon sessions and tmux over ssh is the
    ordinary way to work there, so the whole question does not arise.
    """
    if sys.platform != "win32":
        return None
    import ctypes  # win-only: ctypes.windll doesn't exist off Windows
    from ctypes import wintypes

    try:
        k = ctypes.windll.kernel32
        sid = wintypes.DWORD()
        ok = k.ProcessIdToSessionId(k.GetCurrentProcessId(), ctypes.byref(sid))
    except (OSError, AttributeError):
        return None
    return int(sid.value) if ok else None


# WTSGetActiveConsoleSessionId's two non-answers. 0 is the isolated services
# session, never composited onto a screen since Vista; 0xFFFFFFFF means no
# session is currently attached to the console at all.
NO_CONSOLE_SESSION = (0, 0xFFFFFFFF)


def active_console_session_id() -> int | None:
    """The logon session attached to the physical console, or None if unknown.

    Read to answer ONE question -- "is there a desktop to hand work to at all?"
    -- and deliberately never to PICK a session. The id this returns can name
    an RDP session that is not the desktop a user is looking at, so anything
    built on "find the interactive session" is wrong on some real machine.
    Task Scheduler's "run only when the user is logged on" trigger does the
    placing; this only decides whether the offer exists.

    Never raises: the caller is a capability probe whose whole job is to
    explain why something cannot happen, so it must not be able to fail for an
    unrelated reason.
    """
    if sys.platform != "win32":
        return None
    import ctypes  # win-only: ctypes.windll doesn't exist off Windows

    try:
        return int(ctypes.windll.kernel32.WTSGetActiveConsoleSessionId())
    except (OSError, AttributeError):
        return None


def session_id_of(pid: int) -> int | None:
    """The Windows logon session ``pid`` runs in, or None when unknowable.

    ``ProcessIdToSessionId`` needs no process HANDLE -- it reads the session
    from the pid alone -- so this answers for processes a normal user could not
    open, which is exactly the population the Session-0 diagnostics ask about
    (a psmux server an sshd service started runs at a higher integrity level
    than the desktop's own shell). A dead or bogus pid answers None.
    """
    if sys.platform != "win32" or not pid or pid < 0:
        return None
    import ctypes  # win-only: ctypes.windll doesn't exist off Windows
    from ctypes import wintypes

    try:
        sid = wintypes.DWORD()
        ok = ctypes.windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(sid))
    except (OSError, AttributeError):
        return None
    return int(sid.value) if ok else None


def pid_gone(pid: int) -> bool:
    """True only when ``pid`` names no process at all -- the one answer that
    licenses deleting a pid file.

    ``pid_alive`` needs a process HANDLE, so it answers False for a live
    process this user cannot open -- and a daemon an ssh login started in
    logon Session 0 is exactly that (Windows OpenSSH hands an admin a full
    token). Deleting its pid file erased the only record of which process that
    daemon is. ``session_id_of`` needs no handle, so a live-but-unopenable pid
    still has a session; off Windows it is always None and this is plain
    ``not pid_alive``.
    """
    return not pid_alive(pid) and session_id_of(pid) is None


def session0_residents(pids: Iterable[int]) -> dict[int, str]:
    """``{pid: image name}`` for those of ``pids`` alive in logon Session 0.

    Asked only while a desktop exists: on a headless host (no console session)
    Session 0 is where daemons are SUPPOSED to live
    (``MAGENT_SESSION0_POLICY=allow``), so there is nothing they are missing
    from. The console id answers that one question and never picks a session
    -- see ``active_console_session_id``.

    The image name rides along because a pid file outlives its process and
    Session 0 is full of services a recycled pid could now name; the caller
    decides which images are its own. Empty when the snapshot cannot be taken:
    a diagnostic must never invent a problem.
    """
    wanted = set(pids)
    if not wanted:
        return {}
    console = active_console_session_id()
    if console is None or console in NO_CONSOLE_SESSION:
        return {}
    entries = snapshot_processes()
    if entries is None:
        return {}
    return {
        pid: name
        for name, pid, _ppid in entries
        if pid in wanted and session_id_of(pid) == 0
    }


def snapshot_processes() -> list[tuple[str, int, int]] | None:
    """``(image name, pid, parent pid)`` for every live process, or None when
    we could not look -- which is NOT the same as "nothing is running" and must
    never be rendered as one. A walk that fails partway is "could not look"
    too, never the shorter list it got as far as. Off Windows: always None.

    THE one process enumeration in the product, deliberately: every caller
    (``count_processes`` for doctor's wedge count, ``pids_by_image_name`` for
    the psmux priority sweep, ``process_tree`` for the idle-pane proof in
    ``psmux.idle_sessions``) wants the same Toolhelp walk over the same struct,
    and a second copy of a Windows process primitive is exactly how one of them
    silently rots -- the lesson ``spawn_unjobbed`` already encodes.

    Toolhelp, not a CIM/PowerShell query: doctor calls this from a machine that
    is already misbehaving, and a diagnostic that costs a PowerShell boot (~1 s,
    and 10 s bounded on the attach path -- see
    ``platform/windows.py::process_cmdlines``) would make the report slower than
    the thing it reports on. One snapshot walk over ~900 processes costs
    single-digit milliseconds and needs no privileges.

    Names only, never command lines: reading another process's command line
    means NtQueryInformationProcess plus a cross-bitness PEB walk, which is a
    lot of fragile surface for a name match. Off Windows there is no cheap
    stdlib-only equivalent, so this answers None rather than shelling out.
    """
    if sys.platform != "win32":
        return None
    import ctypes  # win-only: ctypes.windll doesn't exist off Windows
    from ctypes import wintypes

    class _ProcessEntry32(ctypes.Structure):
        _fields_ = (
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        )

    # use_last_error: ctypes keeps its own copy of the error each call leaves,
    # the only one Python code running between two foreign calls cannot clobber.
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return None
    try:
        entry = _ProcessEntry32()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32)
        if not k.Process32FirstW(snapshot, ctypes.byref(entry)):
            return None
        found: list[tuple[str, int, int]] = []
        while True:
            found.append(
                (
                    entry.szExeFile,
                    int(entry.th32ProcessID),
                    int(entry.th32ParentProcessID),
                )
            )
            if not k.Process32NextW(snapshot, ctypes.byref(entry)):
                # FALSE for any reason but "no more entries" is a walk that
                # stopped early, and a partial list would read "nothing runs
                # here" for every process it never reached -- the one way an
                # unknown could make idle_sessions call a live pane idle.
                if ctypes.get_last_error() != _ERROR_NO_MORE_FILES:
                    return None
                return found
    finally:
        k.CloseHandle(snapshot)


def process_tree(
    root: int, entries: Iterable[tuple[str, int, int]]
) -> list[tuple[str, int, int]] | None:
    """``root`` and every process descended from it, root first, out of one
    ``snapshot_processes`` result -- or None when ``root`` is not in it.

    Pure: it walks the entries it is handed and asks the OS nothing, so ONE
    snapshot answers for every root a caller has (the idle-pane check reads a
    whole fleet's panes off one). None means "that process was not there",
    which a caller must treat as unknown, never as "nothing runs under it".

    Parent pids are only as good as Windows keeps them: a process whose parent
    exited keeps the dead parent's pid, so an orphan is unreachable from here,
    and a reused pid can adopt strangers. The walk tolerates the cycles reuse
    can create; a caller asking "does anything I care about run under this
    root" gets an answer that errs toward yes on reuse and can miss an orphan.
    """
    items = list(entries)
    root_entry = next((e for e in items if e[1] == root), None)
    if root_entry is None:
        return None
    children: dict[int, list[tuple[str, int, int]]] = {}
    for entry in items:
        children.setdefault(entry[2], []).append(entry)
    tree = [root_entry]
    seen = {root}
    frontier = [root]
    while frontier:
        for entry in children.get(frontier.pop(), ()):
            if entry[1] not in seen:
                seen.add(entry[1])
                tree.append(entry)
                frontier.append(entry[1])
    return tree


def count_processes(exe_name: str) -> int | None:
    """How many live processes run ``exe_name`` (case-insensitive). None = we
    could not look, which is NOT the same as zero and must never be rendered
    as one (``magent doctor``'s psmux-wedge finding renders this)."""
    entries = snapshot_processes()
    if entries is None:
        return None
    wanted = exe_name.casefold()
    return sum(1 for name, _pid, _ppid in entries if name.casefold() == wanted)


def pids_by_image_name(names: Iterable[str]) -> list[int]:
    """Live pids whose image name is one of ``names``, matched case-insensitively
    (Windows filenames are). Empty off Windows, and empty when the snapshot
    fails -- a sweep that cannot see anything simply has nothing to do, which
    is not the same claim ``count_processes`` has to make to a human reader.
    """
    wanted = {name.casefold() for name in names}
    return [
        pid
        for name, pid, _ppid in snapshot_processes() or ()
        if name.casefold() in wanted
    ]


def raise_priority_above_normal(pid: int) -> bool:
    """Raise ``pid`` to ABOVE_NORMAL_PRIORITY_CLASS, if and only if it is
    currently at or below NORMAL. True when this call actually changed it.

    Every failure is a False, never an exception: the caller sweeps a live
    process list, so a pid that exited between the snapshot and the OpenProcess
    is the NORMAL case, not an error, and a pid owned by another user (or
    protected) is a permission answer we simply accept. No elevation is needed
    to raise one's own processes to ABOVE_NORMAL -- unlike HIGH/REALTIME, which
    is one of the reasons ABOVE_NORMAL is the target.
    """
    if sys.platform != "win32":
        return False
    import ctypes  # win-only: ctypes.windll doesn't exist off Windows

    k = ctypes.windll.kernel32
    try:
        handle = k.OpenProcess(
            PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        try:
            if k.GetPriorityClass(handle) not in _RAISABLE_FROM:
                return False
            return bool(k.SetPriorityClass(handle, ABOVE_NORMAL_PRIORITY_CLASS))
        finally:
            k.CloseHandle(handle)
    except OSError:
        return False


def boost_above_normal(
    names: Iterable[str],
    *,
    list_pids: Callable[[Iterable[str]], list[int]] = pids_by_image_name,
    raise_priority: Callable[[int], bool] = raise_priority_above_normal,
) -> int:
    """Raise every live process named in ``names`` to ABOVE_NORMAL. Returns how
    many were actually raised (already-boosted ones count zero, which is what
    makes repeat sweeps quiet).

    Idempotent, admin-free, and it NEVER raises: a per-pid failure is skipped
    silently because the alternative -- a sweep that aborts halfway through the
    fleet because one pid died -- boosts an arbitrary prefix of it.

    The two seams are injectable so the policy above (match by name, raise only
    upward, tolerate per-pid failure) can be tested without a single real
    process being touched; the defaults are the real Windows primitives.
    """
    boosted = 0
    for pid in list_pids(names):
        try:
            raised = raise_priority(pid)
        except OSError:
            # A pid that died mid-sweep, or one the OS refuses us. Both are
            # ordinary on a live box; neither is a reason to stop sweeping.
            continue
        if raised:
            boosted += 1
    return boosted


def spawn_unjobbed(
    args: list[str],
    *,
    creationflags: int = 0,
    stdout: int | None = None,
    stderr: int | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.Popen[bytes]:
    """``subprocess.Popen`` the child OUTSIDE the caller's Windows job object.

    Why this exists, in one incident: a laptop on flaky wi-fi runs
    ``magent attach``, which sends ``magent up`` to the host over SSH. That
    bring-up creates psmux sessions; each ``new-session`` client forks the psmux
    SERVER that will host the project's agent for the next eight hours. Windows
    OpenSSH runs every session command inside a job object marked
    kill-on-close, and job membership is inherited by every descendant -- so
    those servers were born inside a job whose lifetime is the WI-FI'S. One
    flap and sshd tore the job down, taking 29 psmux servers and 29 running
    agents with it, while the sessions that had been created locally (no job,
    no owner) sat there untouched. A client disconnect must never be able to
    kill work on the server.

    ``CREATE_BREAKAWAY_FROM_JOB`` is the escape, and it is the ONLY difference
    from a plain ``Popen``: no console flags are added, so a child that today
    inherits the caller's console keeps inheriting it and nothing about its
    stdio, encoding or pty changes. Callers that also want a detached console
    pass their own ``creationflags`` (see ``launch.spawn_detached``).

    The keyword arguments are spelled out rather than forwarded as ``**kwargs``
    on purpose: ``creationflags`` does not exist off Windows, so the two
    branches genuinely differ, and an untyped passthrough would erase
    ``Popen``'s overload resolution (the byte-mode return type this promises)
    for every caller.

    CreateProcess FAILS OUTRIGHT when the parent's job forbids breakaway, so
    the flag can never be set unconditionally -- hence the fallback, which is
    also the normal path: a process that is in no job at all ignores the flag,
    and one in a breakaway-forbidding job gets today's behavior back rather
    than an exception. The fallback is a silent degradation by necessity, not
    by preference: there is no way to spawn out of such a job.
    """
    if sys.platform != "win32":
        return subprocess.Popen(args, stdout=stdout, stderr=stderr, env=env)
    try:
        return subprocess.Popen(
            args,
            creationflags=creationflags | CREATE_BREAKAWAY_FROM_JOB,
            stdout=stdout,
            stderr=stderr,
            env=env,
        )
    except OSError:
        return subprocess.Popen(
            args,
            creationflags=creationflags,
            stdout=stdout,
            stderr=stderr,
            env=env,
        )


# How long a launcher waits for a detached child to register its pid before it
# calls the start a failure. The child pays a full interpreter start plus its
# own setup first (~1-1.5s on an idle box). The old fixed 2s window was measured
# failing on a loaded Windows desktop while the child registered at 4.7-11s: the
# launcher reported "failed to start" over a process that was, in fact, running.
# The poll returns the moment the pid appears, so the idle path pays nothing.
REGISTRATION_TIMEOUT_S = 20.0


def await_registration(
    child: subprocess.Popen[bytes],
    read_pid: Callable[[], int | None],
    timeout_s: float = REGISTRATION_TIMEOUT_S,
    *,
    not_pid: int | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> int | None:
    """The pid a freshly spawned detached ``child`` registered, or None.

    ``read_pid`` is the caller's own pid-file reader (it owns the path and the
    stale-file cleanup); ``not_pid`` is a pid that must NOT count as the new
    registration -- a restart whose kill did not take leaves the old pid in the
    file. Gives up early once ``child`` has exited: a child that died before
    registering is not coming, and waiting out the window would only delay the
    failure. It never kills ``child`` -- one that is merely slow may be about to
    come up, and a launcher must not take down the process it is waiting for.

    ``sleep``/``clock`` are resolved at call time so a test can drive the
    window without sleeping through it.
    """
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + timeout_s
    while True:
        sleep(0.1)
        pid = read_pid()
        if pid and pid != not_pid:
            return pid
        if child.poll() is not None or clock() >= deadline:
            return None


# The console-probe helper, run as its own DETACHED_PROCESS python so it has no
# console of its own to disturb. For each pid it AttachConsoles, lists that
# console's process ids, and FreeConsoles, writing one JSON object {pid: [ids]}
# to argv[1]. A file, not a pipe: once a process swaps consoles its standard
# handles are unreliable (the prototype conlist.py learned this). It never
# touches THIS interpreter's console -- it is a separate process by design.
_CONSOLE_HELPER = r"""
import ctypes, json, sys
from ctypes import wintypes

k = ctypes.WinDLL("kernel32", use_last_error=True)
k.FreeConsole.restype = wintypes.BOOL
k.AttachConsole.argtypes = [wintypes.DWORD]
k.AttachConsole.restype = wintypes.BOOL
k.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
k.GetConsoleProcessList.restype = wintypes.DWORD

out_path = sys.argv[1]
pids = [int(a) for a in sys.argv[2:]]
me = k.GetCurrentProcessId()
# Start from no console at all, whatever we inherited.
k.FreeConsole()
result = {}
for pid in pids:
    clients = None
    if k.AttachConsole(pid):
        try:
            cap = 4096
            buf = (wintypes.DWORD * cap)()
            n = k.GetConsoleProcessList(buf, cap)
            if 0 < n <= cap:
                clients = sorted(int(buf[i]) for i in range(n) if int(buf[i]) != me)
        finally:
            k.FreeConsole()
    result[str(pid)] = clients
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(result, fh)
"""


def _helper_python() -> str:
    """The interpreter to run a detached stdlib helper with -- the base
    executable, so a venv/launcher shim does not re-exec into a console."""
    return getattr(sys, "_base_executable", None) or sys.executable


def _parse_clients(value: object) -> frozenset[int] | None:
    """A helper's per-pid answer -> a frozenset of client pids, or None. Only a
    non-empty list of positive, non-bool ints is trusted."""
    if not isinstance(value, list) or not value:
        return None
    out: set[int] = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            return None
        out.add(item)
    return frozenset(out)


def console_clients(
    pids: Iterable[int], *, timeout: float = CONSOLE_PROBE_TIMEOUT_S
) -> dict[int, frozenset[int] | None]:
    """For each pid, the set of process ids sharing that pid's CONSOLE, or None
    when it could not be read (attach failed, no console, the process is gone).

    ONE detached helper does the whole batch (see ``_CONSOLE_HELPER``); this
    process never AttachConsole/FreeConsole itself. Off Windows, and on any
    spawn/timeout/parse failure, every pid answers None -- the reading that
    keeps a keystroke OUT of a pane whose console we could not establish.
    """
    ordered = list(dict.fromkeys(pids))
    if sys.platform != "win32" or not ordered:
        return dict.fromkeys(ordered, None)
    import json
    import shutil
    import tempfile

    d: str | None = None
    try:
        # Inside the try: a full/unwritable/AV-locked TEMP is a failure like
        # any other, answered None -- never an OSError into idle_sessions.
        d = tempfile.mkdtemp(prefix="magent-con-")
        out_path = os.path.join(d, "clients.json")
        proc = subprocess.Popen(
            [
                _helper_python(),
                "-I",
                "-S",
                "-c",
                _CONSOLE_HELPER,
                out_path,
                *(str(p) for p in ordered),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=DETACHED_PROCESS,
        )
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return dict.fromkeys(ordered, None)
        try:
            with open(out_path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return dict.fromkeys(ordered, None)
        if not isinstance(raw, dict):
            return dict.fromkeys(ordered, None)
        return {pid: _parse_clients(raw.get(str(pid))) for pid in ordered}
    except OSError:
        return dict.fromkeys(ordered, None)
    finally:
        if d is not None:
            shutil.rmtree(d, ignore_errors=True)


class ProcessIdentity(NamedTuple):
    """A pid's identity across a moment: image base name and creation FILETIME.
    Two reads of the same pid that agree on both are the same process; a reused
    pid disagrees on ``created``."""

    image: str
    created: int


def filetime_to_epoch(ft: int) -> float:
    """A Windows FILETIME (100 ns ticks since 1601) as unix epoch seconds."""
    return ft / 1e7 - _FILETIME_EPOCH_OFFSET_S


def precise_filetime() -> int | None:
    """Now, as a FILETIME comparable with ``ProcessIdentity.created``, or None
    off Windows (there is no creation FILETIME to compare it with there).

    GetSystemTimePreciseAsFileTime, so a process created before this call always
    carries a creation time before it, whether the kernel stamped it from the
    tick clock or the precise one (measured: the precise one, 0.6 ms or more
    after a read taken just before the spawn). ``time.time_ns()`` is the tick
    clock before Python 3.13, where a process created earlier in the same tick
    could read as created AFTER it."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    ft = wintypes.FILETIME()
    _kernel32().GetSystemTimePreciseAsFileTime(ctypes.byref(ft))
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def _identity_of_handle(k: ctypes.CDLL, handle: int) -> ProcessIdentity | None:
    """(image, creation FILETIME) read through an already-open handle, or None
    when the process has exited (exit code != STILL_ACTIVE) or a read failed.
    Win32-only; every caller is behind a ``sys.platform`` guard."""
    import ctypes
    from ctypes import wintypes

    code = wintypes.DWORD()
    if not k.GetExitCodeProcess(handle, ctypes.byref(code)):
        return None
    if code.value != STILL_ACTIVE:
        return None
    size = wintypes.DWORD(_IMAGE_BUFFER_CHARS)
    buf = ctypes.create_unicode_buffer(_IMAGE_BUFFER_CHARS)
    if not k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
        return None
    image = buf.value.replace("\\", "/").rsplit("/", 1)[-1]
    creation = wintypes.FILETIME()
    exit_ = wintypes.FILETIME()
    kernel_ = wintypes.FILETIME()
    user_ = wintypes.FILETIME()
    if not k.GetProcessTimes(
        handle,
        ctypes.byref(creation),
        ctypes.byref(exit_),
        ctypes.byref(kernel_),
        ctypes.byref(user_),
    ):
        return None
    created = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    return ProcessIdentity(image=image, created=created)


def _kernel32() -> ctypes.CDLL:
    """A fresh kernel32 handle with the identity/kill argtypes declared. Win32
    only; every caller is behind a ``sys.platform`` guard. The guard here is
    never taken -- it narrows ``ctypes.WinDLL`` for the non-win32 type check
    (the annotation is ``ctypes.CDLL``, the base of ``WinDLL``, which exists
    on every platform)."""
    if sys.platform != "win32":
        raise OSError("kernel32 is win32-only")
    import ctypes
    from ctypes import wintypes

    filetime_p = ctypes.POINTER(wintypes.FILETIME)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    k.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        filetime_p,
        filetime_p,
        filetime_p,
        filetime_p,
    ]
    k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.GetSystemTimePreciseAsFileTime.argtypes = [filetime_p]
    k.GetSystemTimePreciseAsFileTime.restype = None
    return k


def process_identity(pid: int) -> ProcessIdentity | None:
    """A pid's (image, creation FILETIME), or None off Windows / on a dead or
    unopenable pid. Uses PROCESS_QUERY_LIMITED_INFORMATION, which a normal user
    is granted against their own processes."""
    if sys.platform != "win32" or not pid or pid < 0:
        return None
    k = _kernel32()
    handle = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        return _identity_of_handle(k, handle)
    finally:
        k.CloseHandle(handle)


def _private_bytes(k: ctypes.CDLL, handle: int) -> int:
    """The process's private commit bytes (PrivateUsage), or 0 on failure.
    ``K32GetProcessMemoryInfo`` lives in kernel32 (the ``k`` handle already
    open), so no second DLL is loaded. Win32-only; callers are guarded."""
    import ctypes
    from ctypes import wintypes

    class _PMCEX(ctypes.Structure):
        _fields_ = (
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        )

    k.K32GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_PMCEX),
        wintypes.DWORD,
    ]
    counters = _PMCEX()
    counters.cb = ctypes.sizeof(_PMCEX)
    if k.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        return int(counters.PrivateUsage)
    return 0


def terminate_verified(pid: int, expected: ProcessIdentity) -> int | None:
    """Terminate ``pid`` iff it is STILL the process ``expected`` names, reading
    the identity through the same handle used to kill so a reused pid can never
    be hit. Returns the process's private commit bytes (0 if only that read
    failed), or None when nothing was killed. None off Windows."""
    if sys.platform != "win32" or not pid or pid < 0:
        return None
    rights = PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ
    k = _kernel32()
    handle = k.OpenProcess(rights, False, pid)
    if not handle:
        # VM_READ is what GetProcessMemoryInfo wants; without it the identity
        # check + kill still work, only the byte count is lost.
        handle = k.OpenProcess(
            PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return None
    try:
        actual = _identity_of_handle(k, handle)
        if actual is None or actual != expected:
            return None
        freed = _private_bytes(k, handle)
        if not k.TerminateProcess(handle, 1):
            return None
        return freed
    finally:
        k.CloseHandle(handle)
