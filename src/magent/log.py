"""Shared observability: rotating file logging + cross-platform liveness heartbeats.

Every consumer gets its own log file, named by concern, under
``~/.magent/logs/`` -- runtime state, not config (see cli.py's
_config_dir for the config/*.json home). Logging is best-effort: setup
failures fall back to a NullHandler rather than raising, since the daemons
that call get_logger() run detached with no console to report a crash to.

A log NAME is not owned by one process. ``hotkey.log`` is written by the Alt+V
listener, by ``magent serve`` (its listener supervisor) and by any foreground
``magent up``/``attach``; ``launch.log`` by the foreground CLI, by ``serve``
(every psmux warning) and by the listener; ``attention.log`` by the attention
daemon, by ``magent watch`` and by anything that reads a bad agent-state record;
``platform.log`` by every process that imports a platform backend at all. The
stdlib's ``RotatingFileHandler`` is a single-process design, so the handler here
is a shared-file subclass -- see ``_SharedRotatingFileHandler``.

Heartbeats live here (not in hotkey.py) so cross-platform callers -- `status`,
Linux CI -- can check liveness without importing the Windows-only hotkey
module.
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import threading

# The shared-log interlock's primitive, bound ONCE at import: msvcrt on Windows,
# flock elsewhere -- the same per-OS locking-API split lockfile.py makes.
#
# Bound at import, not per call, because the OS does not change while a process
# runs but ``sys.platform`` DOES: tests monkeypatch it to drive the win32
# branches of platform-specific code (`test_upload_server.py`'s taskkill path is
# one), and a logger that re-read it per record would try to `import msvcrt` on
# Linux and take down the very call it was asked to observe. It did, on the
# first CI run of this change. Binding here also makes the hot path one name
# lookup instead of a comparison plus an import.
if sys.platform == "win32":
    import msvcrt

    def _lock_exclusive(fd: int) -> None:
        """Take the exclusive lock, or raise OSError immediately.

        ``msvcrt.locking`` locks a byte range from the CURRENT offset, so the
        seek is load-bearing, not decoration.
        """
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_exclusive(fd: int) -> None:
        """Take the exclusive lock, or raise OSError immediately."""
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


LOG_DIR = Path.home() / ".magent" / "logs"
HEARTBEAT_DIR = Path.home() / ".magent"

HEARTBEAT_INTERVAL = 10  # seconds between heartbeat writes
HEARTBEAT_MAX_AGE = 30  # 3x the interval, tolerant of scheduler jitter

_MAX_BYTES = 1_000_000
_BACKUP_COUNT = 3
_FORMAT = "%(asctime)s %(levelname)s %(process)d %(name)s %(message)s"

# How long a writer waits for the cross-process interlock before giving up and
# writing anyway. Generous on purpose: the critical section is one open/write/
# close, so a wait this long means something pathological (a paused process
# under a debugger), not ordinary contention. Both OS locks are released by the
# kernel when the holder dies, so a crashed writer cannot wedge the others.
_LOCK_TIMEOUT_S = 5.0
_LOCK_POLL_S = 0.002

_CONFIGURED_ATTR = "_magent_log_configured"


def _configured_level() -> int:
    """The level from ``MAGENT_LOG_LEVEL``, or INFO when unset.

    Read via the ``magent.env`` accessor only (``os.environ`` is banned
    outside env.py, TID251); imported in-body to keep this leaf import-light and
    cycle-free. A bad value fails fast at CLI entry (app.py exits 1 before any
    logging happens), so an unset level (``None``) is simply the honest default
    of INFO -- not a swallowed error. But if ``get_env()`` still raises here --
    a detached daemon whose inherited env went bad after startup -- logging must
    never crash the process it observes, so that case also falls back to INFO.
    """
    from pydantic import ValidationError

    from magent.env import get_env

    try:
        level = get_env().log_level
    except ValidationError:
        return logging.INFO
    if level is None:
        return logging.INFO
    value = getattr(logging, level)
    return value if isinstance(value, int) else logging.INFO


class _SharedRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A ``RotatingFileHandler`` that several magent PROCESSES may point at the
    same file.

    The stdlib handler keeps the log open for the process's lifetime and rotates
    by renaming it. Both halves break when the file is shared, and both were
    measured (``tests/e2e/test_log_multiprocess.py``, 4 real writers x 200
    records, Windows):

    * ``doRollover`` renames the live file. A second process holding it open
      makes that ``os.rename`` fail with ``PermissionError: [WinError 32]``;
      the handler has already dropped its stream, so the record goes to
      ``handleError`` and the NEXT record retries the same doomed rename -- and
      the reopen then fails too (``[Errno 13] Permission denied``, the rename
      left the path delete-pending). **272 of 800 records were lost** and the
      backup chain came out with holes (``.3`` and ``.8`` missing, clobbered by
      overlapping cascades). On POSIX the rename succeeds instead, and the other
      process keeps writing into the file it renamed away -- the same records,
      lost more quietly.
    * Windows has no atomic append: the CRT implements ``open(path, "a")`` as
      seek-to-end then write, with nothing holding the file in between, so two
      overlapping writers resolve the same offset and one lands on top of the
      other (the same finding ``test_altv_flash.py`` records for its shim).

    So this handler holds no file across records. Every emit takes a
    cross-process exclusive lock on a sidecar, opens the log, rotates it if it
    has crossed ``maxBytes``, writes, and closes -- all inside the lock. Because
    nobody holds the log outside the critical section, the rename can never be
    blocked, two writers can never rotate the same file twice, and no two writes
    can resolve the same offset.

    The sidecar is ``<name>.lock``, deliberately NOT ``<name>.log.lock``: the
    soak tier (and anyone at a shell) enumerates rotated files with
    ``glob("<name>.log*")``, and a lock file must not read as a log file.

    Cost is one lock + one open/close per record. These logs are lifecycle
    events at a few records per second, not a request stream, so that buys
    correctness at a price nothing here can feel.
    """

    def __init__(
        self, filename: Path, *, max_bytes: int, backup_count: int, encoding: str
    ) -> None:
        super().__init__(
            filename,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding=encoding,
            # Load-bearing: the stream must be opened per record inside the
            # lock, never left open across records, or the rename this class
            # exists to protect is blocked again.
            delay=True,
        )
        self._lock_path = Path(self.baseFilename).with_suffix(".lock")
        self._lock_fd: int | None = None
        self._warned_unlocked = False

    # --- the cross-process critical section ---------------------------------

    def _acquire(self) -> bool:
        """Hold the interlock, or report honestly that we could not."""
        if self._lock_fd is None:
            try:
                self._lock_fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            except OSError:
                return False  # narrated by _warn_unlocked, never silent
        deadline = time.monotonic() + _LOCK_TIMEOUT_S
        while True:
            try:
                _lock_exclusive(self._lock_fd)
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(_LOCK_POLL_S)
            else:
                return True

    def _release(self) -> None:
        if self._lock_fd is not None:
            with contextlib.suppress(OSError):
                _unlock(self._lock_fd)

    def _warn_unlocked(self, record: logging.LogRecord) -> None:
        """Say, once per process, that writes are no longer interlocked.

        Degrading to an unlocked write keeps the RECORD (losing it is the defect
        this class exists to remove) but reopens the interleaving window, so it
        must not be silent. The notice goes into the log file itself on this
        very write: a detached daemon has no console, and reaching for
        ``get_logger`` from inside a handler would recurse.
        """
        if self._warned_unlocked:
            return
        self._warned_unlocked = True
        logging.FileHandler.emit(
            self,
            logging.LogRecord(
                record.name,
                logging.WARNING,
                record.pathname,
                record.lineno,
                "log interlock unavailable after %.0fs (%s); records from this "
                "process may now interleave with other magent processes",
                (_LOCK_TIMEOUT_S, self._lock_path),
                None,
            ),
        )

    def _rollover_if_full(self) -> None:
        """Rotate when the file has crossed ``maxBytes``.

        Deliberately measures the file, not a cached stream offset: another
        process may have rotated or grown it since our last record. A file is
        therefore rotated on the record AFTER it crosses the threshold, so it
        can exceed ``maxBytes`` by at most one line -- a bound that, unlike the
        stdlib's "would this record fit" arithmetic, cannot be thrown off by a
        record whose formatting raises.
        """
        if self.maxBytes <= 0:
            return
        try:
            size = os.path.getsize(self.baseFilename)
        except OSError:
            return  # absent (or just rotated away): _open() recreates it
        if size < self.maxBytes:
            return
        # self.stream is None here -- every emit closes it -- so doRollover has
        # nothing to close, nobody else holds the file, and delay=True leaves it
        # closed for the write below to reopen.
        self.doRollover()

    def _close_stream(self) -> None:
        stream = self.stream
        self.stream = None
        if stream is None:
            return
        # StreamHandler.emit already flushed the record to the OS, so a failure
        # here cannot mean a lost line and must not be reported as one.
        with contextlib.suppress(OSError):
            stream.close()

    def emit(self, record: logging.LogRecord) -> None:
        locked = self._acquire()
        try:
            if not locked:
                self._warn_unlocked(record)
            try:
                self._rollover_if_full()
            except OSError:
                # Rotation is bounded growth; the record is the point. Report
                # the rotation failure through the stdlib's own channel and
                # still write the line -- unlike the stdlib, which drops it.
                self.handleError(record)
            logging.FileHandler.emit(self, record)
        finally:
            self._close_stream()
            if locked:
                self._release()

    def close(self) -> None:
        try:
            super().close()
        finally:
            fd, self._lock_fd = self._lock_fd, None
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)


def get_logger(name: str) -> logging.Logger:
    """Return the ``magent.<name>`` logger, attaching a rotating file
    handler under LOG_DIR on first use. Idempotent -- repeat calls return the
    same logger without stacking handlers. Never raises: if LOG_DIR can't be
    created (read-only home, permissions), the logger falls back to a
    NullHandler and stays otherwise usable.

    Safe to call for the same ``name`` from several magent processes at once --
    see ``_SharedRotatingFileHandler``.
    """
    logger = logging.getLogger(f"magent.{name}")
    if getattr(logger, _CONFIGURED_ATTR, False):
        return logger

    handler: logging.Handler
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = _SharedRotatingFileHandler(
            LOG_DIR / f"{name}.log",
            max_bytes=_MAX_BYTES,
            backup_count=_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(_FORMAT))
    except OSError:
        handler = logging.NullHandler()

    logger.addHandler(handler)
    logger.setLevel(_configured_level())
    setattr(logger, _CONFIGURED_ATTR, True)
    return logger


def reset_logging() -> None:
    """Test seam: strip handlers + the configured-sentinel from every
    ``magent.*`` logger so the next get_logger() call re-attaches under
    whatever LOG_DIR is current (tests monkeypatch it to a tmp_path)."""
    manager = logging.Logger.manager
    for logger_name, logger in list(manager.loggerDict.items()):
        if logger_name != "magent" and not logger_name.startswith("magent."):
            continue
        if isinstance(logger, logging.PlaceHolder):
            continue
        for h in list(logger.handlers):
            logger.removeHandler(h)
            h.close()
        if hasattr(logger, _CONFIGURED_ATTR):
            delattr(logger, _CONFIGURED_ATTR)


# --- Liveness heartbeats -----------------------------------------------------
# A daemon (currently: the hotkey listener) touches its heartbeat file on an
# interval; status reads its mtime to tell "running" from "wedged". Freshness
# is judged by mtime, not file contents, so a torn write can't corrupt the
# check.


def _heartbeat_path(name: str) -> Path:
    return HEARTBEAT_DIR / f"{name}.heartbeat"


def write_heartbeat(name: str) -> None:
    """Best-effort liveness pulse. Never raises."""
    try:
        HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
        _heartbeat_path(name).write_text(str(time.time()))
    except OSError:
        pass


def heartbeat_age(name: str) -> float | None:
    """Seconds since the last heartbeat, or None if it was never written."""
    try:
        mtime = _heartbeat_path(name).stat().st_mtime
    except OSError:
        return None
    return time.time() - mtime


def heartbeat_fresh(name: str, max_age: float = HEARTBEAT_MAX_AGE) -> bool:
    age = heartbeat_age(name)
    return age is not None and age <= max_age


def clear_heartbeat(name: str) -> None:
    """Remove a heartbeat file -- the mark of a *clean* daemon shutdown, so
    `status` reads 'off' (never-started) rather than 'crashed' (a heartbeat
    left behind by a process that died without cleaning up). Best-effort:
    never raises."""
    with contextlib.suppress(OSError):
        _heartbeat_path(name).unlink()


def run_heartbeat(name: str, stop_event: threading.Event) -> None:
    """Pulse ``name``'s heartbeat every HEARTBEAT_INTERVAL until ``stop_event``
    is set. Meant to run on a dedicated daemon thread so the liveness signal is
    decoupled from the caller's work cadence: a slow poll loop -- or a user who
    widens that loop's interval past HEARTBEAT_MAX_AGE -- can't make `status`
    read a false 'stale'. Mirrors hotkey.py's heartbeat thread."""
    while not stop_event.is_set():
        write_heartbeat(name)
        stop_event.wait(HEARTBEAT_INTERVAL)
