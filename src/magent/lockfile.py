"""Cross-platform exclusive lockfile — prevents TOCTOU races on daemon startup.

Provides a context manager that acquires an exclusive, non-blocking lock on a
file under ``~/.magent/``.  On Windows the lock uses ``msvcrt.locking``; on
Unix, ``fcntl.flock``.  The lock is advisory (same as flock), which is fine:
callers are cooperating magent processes, not adversaries.

The file is created if absent.  The lock is released (and the file closed) on
context-manager exit.  Only the HOLDER deletes the file, best-effort (on Windows
a concurrent opener may hold the path, so an OSError on unlink is swallowed).  A
contender that failed to acquire never deletes it: on POSIX the holder's flock
lives on that inode, and unlinking it would let the next contender create a
fresh file and "acquire" it while the holder still runs.

The same hazard runs the other way on POSIX: a contender that opened the file
before the holder deleted it can lock it once the holder lets go, holding a
lock on a file no path names while a third taker creates a fresh one.  So the
POSIX holder deletes the file BEFORE letting go, and a taker checks after
locking that the path still names the file it locked -- if not, it lets go and
takes the lock again.  Windows cannot delete an open file, so neither applies
there and its order (let go, then delete) is unchanged.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator
    from typing import IO


class LockHeld(OSError):
    """Another process already holds the lock."""


# How many times ``exclusive_lock`` takes the lock again after finding it had
# locked a file its holder already let go of (POSIX only). Each retry means a
# holder left mid-attempt; three in a row is a lock changing hands, and saying
# LockHeld beats spinning.
_ACQUIRE_ATTEMPTS = 3


@contextlib.contextmanager
def exclusive_lock(name: str) -> Generator[None]:
    """Acquire an exclusive lock on ``~/.magent/<name>.lock``.

    Raises ``LockHeld`` immediately if another process holds it (non-blocking).
    """
    lock_path = Path.home() / ".magent" / f"{name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    fh = _take_exclusive(name, lock_path)
    try:
        yield
    finally:
        if sys.platform == "win32":
            # An open file cannot be deleted here: let go, then delete.
            fh.close()
            with contextlib.suppress(OSError):
                lock_path.unlink()
        else:
            # Delete BEFORE letting go. A contender that locks this inode after
            # the close then finds the path no longer names it and retries;
            # deleted after, the delete could strand a contender whose check
            # had already passed.
            with contextlib.suppress(OSError):
                lock_path.unlink()
            fh.close()


def _take_exclusive(name: str, lock_path: Path) -> IO[str]:
    """Open ``lock_path`` and lock it without waiting: the open, locked handle.
    LockHeld when another holder has it. On POSIX a lock on a file the path
    no longer names (its holder deleted it after this open) is let go and
    taken again, up to ``_ACQUIRE_ATTEMPTS`` times."""
    for _ in range(_ACQUIRE_ATTEMPTS):
        fh = open(lock_path, "w", encoding="utf-8")  # noqa: SIM115  # reason: the fd must stay open for the lock duration; a with-block would release too early
        try:
            if sys.platform == "win32":
                import msvcrt

                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise LockHeld(f"{name} lock is held by another process") from exc
                return fh
            import fcntl

            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise LockHeld(f"{name} lock is held by another process") from exc
            if _still_named(lock_path, fh):
                return fh
        except BaseException:
            fh.close()
            raise
        # A contender never deletes: the path may be another holder's now.
        fh.close()
    raise LockHeld(f"{name} lock changed hands under every attempt to take it")


def _still_named(lock_path: Path, fh: IO[str]) -> bool:
    """True when ``lock_path`` still names the file ``fh`` has open (same
    device and inode). A path that is gone (ENOENT) names nothing: False."""
    try:
        return os.path.samestat(os.stat(lock_path), os.fstat(fh.fileno()))
    except FileNotFoundError:
        return False
