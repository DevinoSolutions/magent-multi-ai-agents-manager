"""Agent-neutral session-state store.

A small file-per-session record of what an agent is doing -- ``working``,
``done`` (your turn), ``needs-input`` (blocked on you), ``error``, ``idle`` --
keyed by the session's working directory. Any agent that can emit lifecycle
events (Claude Code via hooks, Codex via its ``notify`` program, ...) writes
here; the session picker reads here. That keeps status detection out of the
terminal (no scraping) and uniform across agent types.

Deliberately dependency-light (stdlib only) so the hook handler that imports it
adds negligible latency to every turn.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import time
from pathlib import Path

STATE_DIR = Path.home() / ".magent" / "state"

# Canonical state values.
WORKING = "working"  # a turn is in flight
DONE = "done"  # finished -- waiting on the user
NEEDS_INPUT = "needs-input"  # blocked on the user (permission prompt)
ERROR = "error"  # turn ended on an error
IDLE = "idle"  # session open, nothing pending

_VALID = {WORKING, DONE, NEEDS_INPUT, ERROR, IDLE}

# Schema version — bump when the on-disk record shape changes. The in-repo
# writer is state_hook.py (the magent-state-hook console script); any external
# writer should check this before writing.
# Pinned by tests/unit/test_agent_state.py::TestSchemaContract.
RECORD_VERSION = 1

# Default retention — overridable via settings.attention.stateTtlDays.
STATE_TTL_S = 14 * 24 * 60 * 60  # 14 days, in seconds

# Process-lifetime bookkeeping (a fresh interpreter resets both): the store
# grows on the daemon's multi-hour timescale, so one opportunistic sweep per
# process is plenty, and each unusable record is named once — not every poll.
_swept_this_process = False
_warned_files: set[str] = set()


def _norm(path: str) -> str:
    """Canonicalize a path so the writer's cwd and the picker's pane_current_path
    map to the same key: slashes -> '/', drop trailing '/', lowercase on
    Windows. Deliberately string-only -- no realpath -- so any external writer
    (e.g. a JS hook) can produce byte-identical keys without OS calls."""
    s = (path or "").replace("\\", "/").rstrip("/")
    if sys.platform == "win32":
        s = s.lower()
    return s


def _key(path: str) -> str:
    return hashlib.sha1(_norm(path).encode("utf-8")).hexdigest()[:16]


def _path_for(cwd: str) -> Path:
    return STATE_DIR / f"{_key(cwd)}.json"


def write_state(cwd: str, state: str, session_id: str | None = None) -> None:
    if state not in _VALID or not cwd:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path_for(cwd).with_suffix(".tmp")
    payload = json.dumps(
        {
            "state": state,
            "ts": time.time(),
            "cwd": _norm(cwd),
            "session_id": session_id,
        }
    )
    # Write-then-rename so a concurrent reader never sees a half-written file.
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, _path_for(cwd))


def clear_state(cwd: str) -> None:
    if not cwd:
        return
    with contextlib.suppress(OSError):
        _path_for(cwd).unlink()


def sweep_stale(ttl: float = STATE_TTL_S, now: float | None = None) -> int:
    """Delete state records whose ``ts`` is more than ``ttl`` seconds behind
    ``now`` (default: the wall clock), returning the count removed.

    Best-effort: unreadable/foreign files carry no trustworthy timestamp and
    are left in place (``all_states`` surfaces them separately), and a delete
    that loses a race with a fresh write is ignored. ``now`` is injectable so
    tests exercise retention with no real clock."""
    ref = time.time() if now is None else now
    removed = 0
    try:
        paths = sorted(STATE_DIR.glob("*.json"))
    except OSError:
        return 0
    for p in paths:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        ts = d.get("ts", 0)
        ts_num = ts if isinstance(ts, (int, float)) and not isinstance(ts, bool) else 0
        if (ref - ts_num) <= ttl:
            continue
        cwd = d.get("cwd")
        if isinstance(cwd, str) and cwd:
            clear_state(cwd)  # the store's own reclaimer — now with a prod caller
        else:
            with contextlib.suppress(OSError):
                p.unlink()
        removed += 1
    return removed


def maybe_sweep_stale(ttl: float = STATE_TTL_S) -> None:
    """Run :func:`sweep_stale` at most once per process. Called eagerly on
    attention-daemon start and opportunistically from every ``all_states``
    read, so retention holds even for a user who only ever runs
    ``watch``/``status`` and never the daemon. One sweep per interpreter is
    plenty — the store only grows on the daemon's multi-hour timescale."""
    global _swept_this_process  # noqa: PLW0603  # reason: process-once sweep guard (same module-singleton pattern as env._cached_env)
    if _swept_this_process:
        return
    _swept_this_process = True
    sweep_stale(ttl=ttl)


def state_for(cwd: str, max_age: float | None = None) -> dict[str, object] | None:
    """Return the state record for a cwd, or None if absent/expired."""
    p = _path_for(cwd)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    ts = d.get("ts", 0)
    ts_num = ts if isinstance(ts, (int, float)) and not isinstance(ts, bool) else 0
    if max_age is not None and (time.time() - ts_num) > max_age:
        return None
    return d


def norm_cwd(path: str) -> str:
    """Public path canonicalizer — how config project paths map onto the
    ``cwd`` field stored in state records (attention engine, watch)."""
    return _norm(path)


def _warn_bad_record(path: Path, why: str) -> None:
    """Log ONE warning per offending state file per process (P6-09). Records
    arrive from hook subprocesses (state_hook.py today, possibly external
    writers tomorrow); if a writer regresses to malformed JSON or a changed
    schema, the affected sessions silently vanish from watch/attention. Naming
    the file makes that visible without spamming the log every poll."""
    key = str(path)
    if key in _warned_files:
        return
    _warned_files.add(key)
    # Lazy import so the WRITE path (the hook handler that imports this module
    # every turn) stays stdlib-only and import-cheap; only the read side logs.
    from magent.log import get_logger

    get_logger("attention").warning(
        # The FULL path: a bad local record and a bad node-mirror record for
        # the same cwd hash share a file name and must stay distinguishable.
        "skipping unusable agent-state record %s: %s",
        path,
        why,
    )


def read_store(root: Path, *, strict: bool = False) -> list[dict[str, object]]:
    """Every readable record in the store at ``root`` -- this PC's, or a node
    session's mirror under ``~/.magent/nodes``. Corrupt, non-object and
    too deeply nested (``nodes.MAX_JSON_DEPTH``) files are skipped and named
    once (``_warn_bad_record``). Never sweeps: a mirror
    is the node's to age, and a record deleted here would come back on the
    next pull. A missing or unreadable directory is an empty store (unreadable
    is logged once). Records are unfiltered (no TTL) and ordered by filename,
    not by ts.

    ``strict=True`` is for a caller that can do better than "empty" with a
    broken store (the attention engine holds a node's last good records).
    It re-raises a DIRECTORY-level ``OSError`` other than
    ``FileNotFoundError`` -- ``NotADirectoryError``, ``PermissionError``, or
    any other error listing ``root`` -- from an ``os.scandir`` probe or the
    glob, and logs nothing for it. A missing directory is still ``[]`` (a
    mirror not pulled yet is empty, not broken) and a torn or unparseable
    FILE is still skipped (a partial pull is normal). The probe is needed
    because ``Path.glob`` swallows these errors itself: a file globs to []."""
    records: list[dict[str, object]] = []
    try:
        if strict:
            with os.scandir(root):
                pass
        paths = sorted(root.glob("*.json"))
    except OSError as exc:
        if strict:
            if isinstance(exc, FileNotFoundError):
                return records
            raise
        key = str(root)
        if key not in _warned_files:
            _warned_files.add(key)
            from magent.log import get_logger  # lazy: see _warn_bad_record

            get_logger("attention").warning(
                "agent-state: %s is unreadable (%s)", root, exc
            )
        return records
    # lazy: the hook imports this module on every turn (see the docstring).
    from magent.nodes import TOO_DEEP, nests_too_deep

    for p in paths:
        try:
            text = p.read_text(encoding="utf-8")
            # A mirror's files are the node's: nested past the bound is
            # refused before json parses it, the same on every stack.
            too_deep = nests_too_deep(text)
            d = None if too_deep else json.loads(text)
        # RecursionError: the backstop for any nesting the scan did not refuse.
        except (OSError, ValueError, RecursionError) as exc:
            _warn_bad_record(p, f"unreadable ({exc})")
            continue
        if too_deep:
            _warn_bad_record(p, f"unreadable ({TOO_DEEP})")
        elif isinstance(d, dict):
            records.append(d)
        else:
            _warn_bad_record(p, "not a JSON object")
    return records


def all_states() -> list[dict[str, object]]:
    """Every readable state record in the store. Corrupt or non-object files
    are skipped — a half-written or vandalized record must never take the
    attention loop down with it — but each offending file is named in one
    WARNING per process (P6-09) so a regressed external writer is visible.
    Reading also opportunistically ages out long-dead records (P6-04, see
    ``maybe_sweep_stale``)."""
    maybe_sweep_stale()
    return read_store(STATE_DIR)
