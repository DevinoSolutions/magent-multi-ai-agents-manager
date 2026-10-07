"""Pid-file leaf: the one owner of how a daemon's pid is recorded, believed, and
acted on.

Four daemons (upload server, Alt+V listener, attention daemon, node sync) each
kept a private copy of "read the pid, decide whether to believe it, write it,
clear it, kill it", and the copies drifted: one never asked whether the pid was
alive, one skipped the boot check, and all four ended the process with a bare
``taskkill /PID`` that trusts the number. Windows reuses pids quickly and a
crash leaves the file behind, so the number can name a stranger by the time
anything acts on it.

Stdlib + ``procs`` only, so every daemon module (including the win32-only
``hotkey``) and the cli shells can import it.
"""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING

from magent.procs import (
    pid_alive,
    pid_gone,
    predates_boot,
    terminate_pid,
)

if TYPE_CHECKING:
    from pathlib import Path

    from magent.procs import KillOutcome


def recorded(path: Path) -> int | None:
    """The pid the file records, read-only: None when absent or unreadable, and
    None when the file predates the last boot (a restart ends every process
    without letting it remove its file, and the number is free for any process
    now). Believes nothing about the process -- see ``read``."""
    try:
        pid = int(path.read_text().strip())
        written = path.stat().st_mtime
    except (OSError, ValueError):
        return None
    return None if predates_boot(written) else pid


def read(path: Path) -> int | None:
    """The pid of the live process the file records, or None. Clears a file
    that records nothing.

    Three verdicts, and every liveness and kill decision reads through them:

    - Written before the last boot: stale whatever its pid is doing now.
      Ignored and cleared.
    - Alive but not openable (a Session-0 copy an ssh login started): not ours
      to use, and not stale either -- the file is the only record that names it
      for `status`/`doctor`. Ignored and KEPT.
    - Gone (no process and no session): cleared.
    """
    try:
        pid = int(path.read_text().strip())
        written = path.stat().st_mtime
    except (OSError, ValueError):
        return None
    if predates_boot(written):
        clear_stale(path)
        return None
    if pid_alive(pid):
        return pid
    if pid_gone(pid):
        clear_stale(path)
    return None


def write(path: Path) -> None:
    """Record this process's pid, atomically (a reader never sees a torn or
    empty file). Raises OSError: whether a daemon can run without its record is
    the caller's call."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(str(os.getpid()))
    try:
        os.replace(tmp, path)
    except PermissionError:
        # Windows refuses a replace while a reader has the target open; a
        # plain write is no worse than the pre-atomic behaviour.
        path.write_text(str(os.getpid()))
        with contextlib.suppress(OSError):
            tmp.unlink()


def clear(path: Path) -> None:
    """Remove the file iff it still records THIS process (a successor that
    already replaced it keeps its own record)."""
    with contextlib.suppress(OSError):
        if path.read_text().strip() == str(os.getpid()):
            path.unlink()


def clear_stale(path: Path) -> None:
    """Remove the file whoever it records: its process is known not to be there."""
    with contextlib.suppress(OSError):
        path.unlink()


def terminate(path: Path) -> tuple[int | None, KillOutcome]:
    """End the process the file records, if it is provably that process.

    ``(pid, outcome)``. The file's mtime is the moment the record was made --
    the process writes it after it starts -- so a process that started later is
    a stranger that reused the number (``procs.terminate_pid``). A file that
    records no live process answers ``(None, "gone")`` without touching
    anything. The file itself is left for the caller, which knows what else
    goes with it (heartbeat, manifest); ``mismatch`` and ``gone`` mean it is
    stale and ``clear_stale`` is right, ``unverifiable`` and ``failed`` mean
    the process may still be there and it must stay.
    """
    pid = read(path)
    if pid is None:
        return None, "gone"
    try:
        written = path.stat().st_mtime
    except OSError:
        return pid, "unverifiable"
    return pid, terminate_pid(pid, started_before=written)
