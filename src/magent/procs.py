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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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


def snapshot_processes() -> list[tuple[str, int]] | None:
    """``(image name, pid)`` for every live process, or None when we could not
    look -- which is NOT the same as "nothing is running" and must never be
    rendered as one. Off Windows: always None.

    THE one process enumeration in the product, deliberately: both callers
    (``count_processes`` for doctor's wedge count, ``pids_by_image_name`` for
    the psmux priority sweep) want the same Toolhelp walk over the same struct,
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

    k = ctypes.windll.kernel32
    snapshot = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return None
    try:
        entry = _ProcessEntry32()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32)
        if not k.Process32FirstW(snapshot, ctypes.byref(entry)):
            return None
        found: list[tuple[str, int]] = []
        while True:
            found.append((entry.szExeFile, int(entry.th32ProcessID)))
            if not k.Process32NextW(snapshot, ctypes.byref(entry)):
                return found
    finally:
        k.CloseHandle(snapshot)


def count_processes(exe_name: str) -> int | None:
    """How many live processes run ``exe_name`` (case-insensitive). None = we
    could not look, which is NOT the same as zero and must never be rendered
    as one (``magent doctor``'s psmux-wedge finding renders this)."""
    entries = snapshot_processes()
    if entries is None:
        return None
    wanted = exe_name.casefold()
    return sum(1 for name, _pid in entries if name.casefold() == wanted)


def pids_by_image_name(names: Iterable[str]) -> list[int]:
    """Live pids whose image name is one of ``names``, matched case-insensitively
    (Windows filenames are). Empty off Windows, and empty when the snapshot
    fails -- a sweep that cannot see anything simply has nothing to do, which
    is not the same claim ``count_processes`` has to make to a human reader.
    """
    wanted = {name.casefold() for name in names}
    return [
        pid for name, pid in snapshot_processes() or () if name.casefold() in wanted
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
