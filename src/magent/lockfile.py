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

``persistent_lock`` is the WAITING variant: bounded by a deadline, and its
file is a sidecar that is never deleted, so every waiter locks the very inode
the holder has.
"""

from __future__ import annotations

import contextlib
import errno
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator
    from typing import IO


class LockHeld(OSError):
    """Another process already holds the lock."""


def lock_path(name: str) -> Path:
    """``~/.magent/<name>.lock``, resolved per call (a redirected HOME moves it)."""
    return Path.home() / ".magent" / f"{name}.lock"


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
    path = lock_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)

    fh = _take_exclusive(name, path)
    try:
        yield
    finally:
        if sys.platform == "win32":
            # An open file cannot be deleted here: let go, then delete.
            fh.close()
            with contextlib.suppress(OSError):
                path.unlink()
        else:
            # Delete BEFORE letting go. A contender that locks this inode after
            # the close then finds the path no longer names it and retries;
            # deleted after, the delete could strand a contender whose check
            # had already passed.
            with contextlib.suppress(OSError):
                path.unlink()
            fh.close()


def _take_exclusive(name: str, path: Path) -> IO[str]:
    """Open ``path`` and lock it without waiting: the open, locked handle.
    LockHeld when another holder has it. On POSIX a lock on a file the path
    no longer names (its holder deleted it after this open) is let go and
    taken again, up to ``_ACQUIRE_ATTEMPTS`` times."""
    for _ in range(_ACQUIRE_ATTEMPTS):
        fh = open(path, "w", encoding="utf-8")  # noqa: SIM115  # reason: the fd must stay open for the lock duration; a with-block would release too early
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
            if _still_named(path, fh):
                return fh
        except BaseException:
            fh.close()
            raise
        # A contender never deletes: the path may be another holder's now.
        fh.close()
    raise LockHeld(f"{name} lock changed hands under every attempt to take it")


def _still_named(path: Path, fh: IO[str]) -> bool:
    """True when ``path`` still names the file ``fh`` has open. A path that
    is gone (ENOENT) names nothing: False."""
    try:
        return os.stat(path).st_ino == os.fstat(fh.fileno()).st_ino
    except FileNotFoundError:
        return False


def _try_lock(fd: int) -> bool:
    """One non-blocking attempt at an exclusive lock on ``fd``: False when
    another holder has it; any other OSError propagates."""
    if sys.platform == "win32":
        import msvcrt

        # msvcrt locks from the current position: always byte 0.
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EDEADLK):
                return False
            raise
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(fd: int) -> None:
    # Closing the fd releases the lock too; unlocking first releases it NOW
    # (Windows documents the on-close release as eventual).
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def persistent_lock(
    name: str, *, wait_s: float, poll_s: float = 0.02
) -> Generator[None]:
    """Hold an exclusive lock on ``~/.magent/<name>.lock``, waiting up to
    ``wait_s`` (polled every ``poll_s``) for another holder to let go; raises
    ``LockHeld`` when it stays taken. Unlike ``exclusive_lock`` the file is a
    persistent sidecar and is NEVER deleted: a waiter that opened it can never
    end up locking a file the holder is about to unlink.

    The lock is per open file, not per process: a second acquisition in the
    same process waits like any other contender. Callers serialize their own
    threads first (a ``threading.Lock``) so they do not poll each other."""
    deadline = time.monotonic() + wait_s
    path = lock_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise LockHeld(f"{path} is held by another process")
            time.sleep(poll_s)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
