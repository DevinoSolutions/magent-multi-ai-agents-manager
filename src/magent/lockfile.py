"""Cross-platform exclusive lockfile — prevents TOCTOU races on daemon startup.

Provides a context manager that acquires an exclusive, non-blocking lock on a
file under ``~/.magent/``.  On Windows the lock uses ``msvcrt.locking``; on
Unix, ``fcntl.flock``.  The lock is advisory (same as flock), which is fine:
callers are cooperating magent processes, not adversaries.

The file is created if absent.  The lock is released (and the file closed) on
context-manager exit; deletion is best-effort — on Windows a concurrent opener
may hold the path, so an OSError on unlink is swallowed.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator


class LockHeld(OSError):
    """Another process already holds the lock."""


@contextlib.contextmanager
def exclusive_lock(name: str) -> Generator[None]:
    """Acquire an exclusive lock on ``~/.magent/<name>.lock``.

    Raises ``LockHeld`` immediately if another process holds it (non-blocking).
    """
    lock_path = Path.home() / ".magent" / f"{name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    fh = open(lock_path, "w", encoding="utf-8")  # noqa: SIM115  # reason: the fd must stay open for the lock duration; a with-block would release too early
    try:
        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise LockHeld(f"{name} lock is held by another process") from exc
        else:
            import fcntl

            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise LockHeld(f"{name} lock is held by another process") from exc
        yield
    finally:
        fh.close()
        with contextlib.suppress(OSError):
            lock_path.unlink()
