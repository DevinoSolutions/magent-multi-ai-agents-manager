"""Raw-dict config I/O: the round-tripping read and the ONE safe write.

The config editor and ``projects.py`` read the config as a raw dict (so keys
the typed loader does not model survive a read-modify-write) and write it back
through ``save``: validated by a round trip through ``load_config`` on a
throwaway copy, the current file copied to ``~/.magent/backups`` (newest 20
per config file kept), then written to a sibling temp file, fsynced and
swapped in with one ``os.replace`` -- so a crash mid-write leaves the old
config, never half of the new one. Lint rule MD011 keeps every save call in
the modules allowed to make one.

``locked`` is the read-modify-write guard: threads of one process queue on a
``threading.Lock`` first, then the file lock ``~/.magent/config.lock`` is
taken with a bounded wait, so two API requests (or a cli and the daemon)
racing over the config serialise instead of one being refused.

Stdlib + ``config`` + ``lockfile`` only; never prints or exits (the cli
module of the same name wraps these for the editor's exit-on-error style).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from magent.config import load_config
from magent.lockfile import LockHeld, persistent_lock

if TYPE_CHECKING:
    from collections.abc import Iterator

BACKUP_KEEP = 20
LOCK_NAME = "config"
# How long one writer waits for another process's config lock before giving
# up with ``LockHeld``. Far longer than one validate + backup + write; a
# writer still waiting after this is behind a hung process and says so.
LOCK_WAIT_S = 2.0

# Threads of ONE process (serve's request handlers) queue here first, so two
# concurrent API writes wait on a cheap lock instead of polling the file lock
# against each other -- the same shape as ``nodes.map_lock``.
_WRITE_LOCK = threading.Lock()

# A reader holding the config on Windows makes the writer's ``os.replace``
# fail with ``PermissionError`` for as long as it reads (every ``load_config``
# is such a reader). Bounded: ~0.5 s outlasts any one ``read_text``; a file
# held for good still surfaces as the error.
REPLACE_RETRIES = 20
REPLACE_SLEEP_S = 0.025


class ConfigWriteError(ValueError):
    """The new content would not load as a config; nothing was written."""


def backups_dir() -> Path:
    """``~/.magent/backups``, resolved per call (a redirected HOME moves it)."""
    return Path.home() / ".magent" / "backups"


def load_raw(path: Path) -> dict[str, object]:
    """The config file as a raw dict. ``FileNotFoundError`` when absent,
    ``ValueError`` when it is not JSON, ``TypeError`` when it is JSON but
    not an object."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"Config at {path} is not a JSON object")
    return data


def validate_text(text: str) -> str | None:
    """``None`` when TEXT would load as a valid config, else the reason it
    would not. Runs against a throwaway copy: ``load_config`` reads a path.

    Known limit: ``load_config`` prints its non-fatal warnings (an unknown
    key, an old schema version) to stderr, so a validation inside ``serve``
    writes them to the daemon's stderr rather than to the API caller."""
    with tempfile.TemporaryDirectory(prefix="magent-cfgcheck-") as td:
        probe = Path(td) / "config.json"
        probe.write_text(text, encoding="utf-8")
        try:
            load_config(str(probe))
        except (ValueError, OSError) as e:
            return str(e)
    return None


def backup_prefix(path: Path) -> str:
    """``config-<hash8>-``: the backup file prefix for one config file, so
    the backups of an alternate ``--config`` file and the default one keep
    separate retention counts instead of evicting each other."""
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"config-{digest}-"


def backup(path: Path, *, keep: int = BACKUP_KEEP) -> Path | None:
    """Copy the current config to ``backups/<prefix><UTC stamp>Z.json`` and
    drop all but the newest ``keep`` of that config's backups. None when
    there is no file to back up yet. The stamp is read from one clock, so
    two backups in one second still sort in the order they were made."""
    if not path.exists():
        return None
    folder = backups_dir()
    folder.mkdir(parents=True, exist_ok=True)
    prefix = backup_prefix(path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    dest = folder / f"{prefix}{stamp}Z.json"
    dest.write_bytes(path.read_bytes())
    for old in sorted(folder.glob(f"{prefix}*.json"))[:-keep]:
        with contextlib.suppress(OSError):
            old.unlink()
    return dest


def replace_retrying(
    src: Path,
    dst: Path,
    *,
    retries: int = REPLACE_RETRIES,
    sleep_s: float = REPLACE_SLEEP_S,
) -> None:
    """``os.replace(src, dst)``, retried ``retries`` times ``sleep_s`` apart
    while a reader holds ``dst`` (``PermissionError``, Windows); any other
    error, and a refusal that outlasts every retry, propagate."""
    for attempt in range(retries + 1):
        try:
            os.replace(src, dst)
        except PermissionError:
            if attempt == retries:
                raise
            time.sleep(sleep_s)
        else:
            return


def write_atomic(path: Path, data: dict[str, object]) -> None:
    """Write DATA over PATH via a sibling temp file, fsync and one
    ``os.replace`` (retried while a reader holds the file). Sibling, not
    tempdir: ``os.replace`` is only atomic within one filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2))
            fh.flush()
            os.fsync(fh.fileno())
        replace_retrying(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def save(
    path: Path,
    data: dict[str, object],
    *,
    validate: bool = True,
    keep_backup: bool = True,
) -> None:
    """THE config write: validate (``ConfigWriteError`` and nothing written
    if it would not load), back up the current file, write atomically. An
    ``OSError`` from the backup or the write propagates; the config is then
    still the old file (the swap is the last step and all-or-nothing)."""
    if validate:
        why = validate_text(json.dumps(data))
        if why is not None:
            raise ConfigWriteError(why)
    if keep_backup:
        backup(path)
    write_atomic(path, data)


@contextlib.contextmanager
def locked(wait_s: float | None = None) -> Iterator[None]:
    """Hold the config exclusively across threads AND processes for one
    read-modify-write, waiting up to ``wait_s`` (default ``LOCK_WAIT_S``) in
    all for it. Raises ``lockfile.LockHeld`` when it stays taken. The file
    lock is ``~/.magent/config.lock``, a persistent sidecar that is never
    deleted (``lockfile.persistent_lock``), so a waiter can never lock a file
    the holder is about to unlink.

    NOT reentrant: a nested ``locked()`` on the same thread waits ``wait_s``
    on the thread lock and then raises ``LockHeld``. Never nest it; do one
    read-modify-write per hold."""
    if wait_s is None:
        wait_s = LOCK_WAIT_S
    deadline = time.monotonic() + wait_s
    if not _WRITE_LOCK.acquire(timeout=max(wait_s, 0.0)):
        raise LockHeld("the config is held by another writer in this process")
    try:
        remaining = max(deadline - time.monotonic(), 0.0)
        with persistent_lock(LOCK_NAME, wait_s=remaining):
            yield
    finally:
        _WRITE_LOCK.release()
