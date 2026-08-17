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

# CreateProcess flag: the new process is NOT assigned to its parent's job
# object. Windows OpenSSH puts everything a session runs into a job marked
# kill-on-close, so without this a process spawned over SSH dies with the
# connection -- including a psmux SERVER, and with it the agent it hosts.
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


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
