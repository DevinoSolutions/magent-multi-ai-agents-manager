from __future__ import annotations

import os
import re
import struct
from pathlib import Path
from typing import NamedTuple

from magent import log, procs
from magent.sessions.live import SESSION_ID_RE, IdleProbe, LiveSession, SessionScan

# Session files already reported unusable. Once per file per episode: a file
# that parses again leaves the set, so its next fault is logged afresh.
_warned_files: set[str] = set()


def _warn_once(path: Path, why: str) -> None:
    key = str(path)
    if key in _warned_files:
        return
    _warned_files.add(key)
    log.get_logger("reap").warning(
        "unusable claude session file %s: %s", path.name, why
    )


class _SessionFile(NamedTuple):
    pid: int
    session_id: str
    cwd: str
    proc_start: int
    kind: str
    status: str
    status_ts: float


# claude.exe caps an encoded project-dir name at 200 UTF-16 units and makes the
# cut name unique with the path's Java String.hashCode in base 36.
_CLAUDE_DIR_MAX = 200
_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _utf16_units(text: str) -> tuple[int, ...]:
    """``text`` as JavaScript sees it: UTF-16 code units (a character outside
    the BMP is two of them, so it becomes two dashes, exactly as in claude)."""
    raw = text.encode("utf-16-le", "surrogatepass")
    return struct.unpack(f"<{len(raw) // 2}H", raw)


def _java_string_hash(text: str) -> int:
    """Java's ``String.hashCode`` over UTF-16 units as a signed 32-bit int --
    the loop claude.exe runs (``(h << 5) - h + unit | 0``)."""
    h = 0
    for unit in _utf16_units(text):
        h = (h * 31 + unit) & 0xFFFFFFFF
    return h - (1 << 32) if h >= 1 << 31 else h


def _base36(n: int) -> str:
    digits = ""
    while True:
        n, rest = divmod(n, 36)
        digits = _BASE36[rest] + digits
        if n == 0:
            return digits


def encode_claude_project_path(project_dir: str) -> str:
    """The directory Claude Code files a project's sessions under
    (``<config dir>/projects/<this>``) -- the ONE encoder in magent.

    Claude's rule, read off claude.exe and measured against the real store
    (295 of 297 entries; the 2 misses were sessions that cd'd mid-run):
    every UTF-16 unit outside ``[A-Za-z0-9]`` becomes ``-``, one for one --
    dots, underscores and the drive-letter colon included, the drive letter's
    case kept -- and a name over 200 units is cut to 200 and suffixed with
    ``-`` + base36(|hashCode of the original path|). The previous rule kept '.'
    and '_', named the wrong directory for every such project, and so made the
    fresh-start probe drop ``--continue`` there."""
    encoded = "".join(
        chr(unit) if chr(unit).isascii() and chr(unit).isalnum() else "-"
        for unit in _utf16_units(project_dir)
    )
    if len(encoded) <= _CLAUDE_DIR_MAX:
        return encoded
    return f"{encoded[:_CLAUDE_DIR_MAX]}-{_base36(abs(_java_string_hash(project_dir)))}"


def build_claude_resume(base_cmd: str, session_id: str | None) -> str:
    stripped = re.sub(r"--continue\s*", "", base_cmd)
    stripped = re.sub(r"--resume\s+\S+", "", stripped).strip()
    if session_id:
        return f"{stripped} --resume {session_id}"
    return stripped


# "Pick the current directory's most recent conversation back up", with no
# session named. Matched as a whole token (and with the trailing run of spaces,
# so removing one leaves no double space) rather than as a substring, so a
# longer flag that merely starts the same way is never touched.
#
# Long form ONLY, deliberately. claude also accepts `-c`, but a configured
# command may run claude through an interpreter -- `bash -c claude --continue`
# -- where that same token belongs to the WRAPPER, and stripping it would
# corrupt a working command. That is far worse than leaving the rarer
# `claude -c` spelling on its pre-existing behavior. The token lookahead
# handles the fully quoted wrapper payload (`bash -c "claude --continue"`) for
# free: the flag is followed by a quote, not whitespace, so nothing matches and
# the command is left exactly as the user wrote it.
_CONTINUE_RE = re.compile(r"(?:(?<=\s)|\A)--continue(?=\s|\Z)\s*")
# A session the user named explicitly (``--resume <id>``, ``--resume=<id>``,
# ``-r <id>``) or claude's interactive resume picker (a bare ``--resume``).
# Either way the command spells out what the user wants; the fresh-start
# rewrite below stays out of it.
_EXPLICIT_RESUME_RE = re.compile(r"(?:(?<=\s)|\A)(?:--resume|-r)(?=[\s=]|\Z)")


def default_config_dir() -> Path:
    """Claude Code's own config directory, ``~/.claude`` -- the store that
    answers for a project no account was chosen for.

    Resolved at CALL time and deliberately not a module constant: an
    import-bound ``Path.home()`` is computed once, before any environment
    redirect can reach it, which is precisely the defect class
    ``tests/conftest.py``'s ``_IMPORT_BOUND_PATHS`` tripwire exists to catch.
    """
    return Path.home() / ".claude"


def _projects_dir(config_dir: Path | None, project_dir: str) -> Path:
    """Where this store keeps ``project_dir``'s conversations.

    ``config_dir`` is a claude CONFIG directory -- the value of
    ``CLAUDE_CONFIG_DIR`` -- not a home directory: claude keeps its transcripts
    in ``<config dir>/projects/<encoded cwd>``, and under an account profile
    that config dir is the profile, not ``~``. None means the default store, so
    every caller that names no account reads exactly the path it always did.
    """
    root = config_dir if config_dir is not None else default_config_dir()
    return root / "projects" / encode_claude_project_path(project_dir)


def has_claude_session(project_dir: str, config_dir: Path | None = None) -> bool:
    """True when ``project_dir`` has at least one stored claude conversation
    IN ``config_dir``'s store (the default ``~/.claude`` one when None).

    Existence only -- first hit wins, no stat and no sort. This runs once per
    project on every status/attach sweep, so it must stay a directory peek
    rather than the full mtime-ordered listing ``get_claude_session_ids``
    builds. ``Path.glob`` over a directory that does not exist yields nothing
    instead of raising, which is exactly the "no sessions here" answer.

    Which store answers is a real question, not a test seam: a pane launched
    under ``CLAUDE_CONFIG_DIR=<profile>`` writes its transcripts there, so a
    probe that always read ``~/.claude`` would answer for a store that pane
    never touches -- dropping ``--continue`` from a project that does have a
    conversation, or keeping it for one that does not.
    """
    sess_dir = _projects_dir(config_dir, project_dir)
    return next(sess_dir.glob("*.jsonl"), None) is not None


def claude_fresh_form(base_cmd: str) -> str | None:
    """``base_cmd`` without its implicit-resume flag, or None when it has none
    or names a session explicitly. No store is read: whether there is anything
    to resume is the caller's question -- on this PC ``claude_fresh_command``
    asks it, and on a pool node ``bring_up.sh`` asks the node's own store."""
    if _EXPLICIT_RESUME_RE.search(base_cmd) or not _CONTINUE_RE.search(base_cmd):
        return None
    return _CONTINUE_RE.sub("", base_cmd).strip()


def claude_fresh_command(
    base_cmd: str, project_dir: str, config_dir: Path | None = None
) -> str | None:
    """``base_cmd`` minus its implicit-resume flag when ``project_dir`` has no
    conversation to resume -- or None to run ``base_cmd`` exactly as configured.

    ``claude --continue`` (the registry default) resumes the most recent
    conversation *for the current working directory*. In a directory that never
    hosted one -- a project just added to magent, a fresh machine, a cleaned
    ``~/.claude/projects`` -- there is nothing to continue: claude prints "No
    conversation found to continue" and exits, so the pane is left at a dead
    shell, the agent never starts, and revive re-runs the same failing command
    forever. Dropping the flag is the honest repair: what the user asked for
    was an agent in that folder.

    The probe answers exactly one question -- does this directory have a stored
    conversation at all, in ``config_dir``'s store -- and only a NO rewrites
    anything. A session file that exists but is empty or corrupt counts as YES
    and keeps ``--continue``: that failure is a real defect the user needs to
    SEE in the pane, not something to paper over with a silently fresh chat.

    A project whose account changed has no transcript in the NEW store, so this
    answers NO and the agent starts fresh rather than dying on "No conversation
    found to continue" at a dead shell. That is the honest answer for that
    store; surfacing it to the user is the caller's job.
    """
    fresh = claude_fresh_form(base_cmd)
    if fresh is None or has_claude_session(project_dir, config_dir):
        return None
    return fresh


# The executable token of a configured claude command, typed into pwsh by
# psmux: no quotes, no spaces, no metacharacters (a path is fine).
_EXE_RE = re.compile(r"[A-Za-z0-9_.:\\/-]+")


def cloud_pane_command(base_cmd: str, task: str) -> str:
    """The one command a cloud pane runs: ``<exe> --cloud "<task>"`` (spec §18.5).

    Only the executable survives from ``base_cmd``: every flag there
    (``--continue``, ``--resume``, a model) belongs to a LOCAL conversation, and
    ``--cloud`` always starts a new one. Re-validates ``task`` against config's
    rule so no caller can type an unchecked string. Raises ValueError."""
    # in-body: config imports magent.sessions, so a top-level import cycles
    from magent.config import CLOUD_ID_LIKE, CLOUD_TASK_RE

    parts = base_cmd.split()
    exe = parts[0] if parts else ""
    if not _EXE_RE.fullmatch(exe):
        raise ValueError(f"cannot type claude executable {exe!r} into a pane safely")
    if not CLOUD_TASK_RE.fullmatch(task) or CLOUD_ID_LIKE.search(task):
        raise ValueError(f"unsafe cloud task {task!r}")
    return f'{exe} --cloud "{task}"'


def get_claude_session_ids(
    project_dir: str,
    count: int,
    config_dir: Path | None = None,
) -> list[str | None]:
    """``project_dir``'s stored conversation ids, newest first, out of
    ``config_dir``'s store (the default ``~/.claude`` one when None)."""
    sess_dir = _projects_dir(config_dir, project_dir)

    if not sess_dir.is_dir():
        return [None] * count

    files = sorted(
        sess_dir.glob("*.jsonl"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )

    ids: list[str | None] = [f.stem for f in files[:count]]
    while len(ids) < count:
        ids.append(None)
    return ids


def _parse_session_file(stem: str, text: str) -> _SessionFile | None:
    """Parse one ``<pid>.json``'s bytes. Any missing field, wrong type, or a
    pid that does not match the file name makes it unusable (None)."""
    import json

    from magent.json_depth import nests_too_deep

    # Refused by magent's own depth bound, never left to the parser's recursion
    # limit: json.loads reads absurd nesting without a RecursionError on some
    # interpreters, so the same file would be usable there and not here.
    if nests_too_deep(text):
        return None
    try:
        raw = json.loads(text)
    except (ValueError, RecursionError):  # RecursionError: the backstop
        return None
    if not isinstance(raw, dict):
        return None
    pid = raw.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or str(pid) != stem:
        return None
    sid = raw.get("sessionId")
    if not isinstance(sid, str) or not SESSION_ID_RE.fullmatch(sid):
        return None
    cwd = raw.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None
    proc_start = raw.get("procStart")
    if (
        not isinstance(proc_start, str)
        or not proc_start.isascii()
        or not proc_start.isdigit()
    ):
        return None
    try:
        start = int(proc_start)
    except ValueError:  # past int()'s digit limit: no FILETIME is that long
        return None
    kind = raw.get("kind")
    status = raw.get("status")
    if not isinstance(kind, str) or not isinstance(status, str):
        return None
    updated = raw.get("statusUpdatedAt")
    if isinstance(updated, bool) or not isinstance(updated, int):
        return None
    try:
        status_ts = updated / 1000.0
    except OverflowError:  # past any float: no clock reads that
        return None
    return _SessionFile(
        pid=pid,
        session_id=sid,
        cwd=cwd,
        proc_start=start,
        kind=kind,
        status=status,
        status_ts=status_ts,
    )


def read_session_files(config_dir: Path) -> SessionScan | None:
    """Live claude sessions from ``<config_dir>/sessions/<pid>.json``, keyed by
    pid, plus the pids whose files could not be used. The ONLY reader of this
    directory in magent.

    A file is returned only when a process with that pid is alive AND its
    creation FILETIME equals ``procStart`` exactly (the stale-file rule: a hard
    kill leaves the file behind with its last status). Only ``*.json`` names are
    opened, so the ``.key`` files are never read; ``name`` is never read, so it
    can never be logged.

    A file that is there but unusable (unreadable, not UTF-8, not one JSON
    object, nested past the parser's depth, a missing or mistyped field, a pid
    that is not its name) is logged and its name's pid is reported in
    ``unusable``: an agent nobody can read is unknown, never absent. A name that
    is not a pid names no process and is left out.

    A directory that does NOT exist yet is the normal empty state and returns
    an empty scan (a fresh machine with no claude sessions). ``None`` means the
    directory is there but could not be *listed* (a permission error, or a file
    where the directory should be) -- an unknown the reaper vetoes on.
    """
    sessions_dir = config_dir / "sessions"
    try:
        entries = sorted(sessions_dir.iterdir())
    except FileNotFoundError:
        return SessionScan({}, frozenset())  # no dir yet: normal, not a failure
    except OSError:
        return None  # there, but unlistable: unknown
    files = [p for p in entries if p.suffix == ".json"]
    out: dict[int, LiveSession] = {}
    unusable: set[int] = set()

    def _unusable(path: Path, why: str) -> None:
        _warn_once(path, why)
        if path.stem.isascii() and path.stem.isdigit():
            unusable.add(int(path.stem))

    for path in files:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            continue  # vanished between listing and read: normal, silent
        except OSError as exc:
            _unusable(path, f"unreadable ({exc})")
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            # Caught mid-write (the file is not replaced atomically, and free
            # text can be non-ASCII): one unusable file, never a raise that
            # would stop the sweep for the whole fleet.
            _unusable(path, "not valid UTF-8")
            continue
        parsed = _parse_session_file(path.stem, text)
        if parsed is None:
            _unusable(path, "missing/invalid field or pid mismatch")
            continue
        _warned_files.discard(str(path))  # usable again: the next fault is news
        ident = procs.process_identity(parsed.pid)
        if ident is None or ident.created != parsed.proc_start:
            continue  # dead or reused pid: drop silently, a stale file is normal
        out[parsed.pid] = LiveSession(
            pid=parsed.pid,
            created=ident.created,
            image=ident.image,
            session_id=parsed.session_id,
            cwd=parsed.cwd,
            status=parsed.status,
            status_ts=parsed.status_ts,
            kind=parsed.kind,
            quiet=parsed.status == "idle",
        )
    return SessionScan(out, frozenset(unusable))


def last_activity(session: LiveSession, config_dir: Path) -> float | None:
    """The newest mtime across ``session``'s main transcript and its
    ``subagents/*.jsonl``, or None when the main transcript does not exist or
    the tree cannot be read.

    Never a partial reading: a subagent file that vanished between the listing
    and its stat is skipped (cleanup removing an old one is normal), but any
    other failure -- including a ``subagents`` dir that is there and cannot be
    listed -- is None, the unknown the reaper vetoes on. A missing
    ``subagents`` dir just means there are none."""
    proj = _projects_dir(config_dir, session.cwd)
    main = proj / f"{session.session_id}.jsonl"
    try:
        newest = main.stat().st_mtime
    except OSError:
        return None
    sub_dir = proj / session.session_id / "subagents"
    try:
        with os.scandir(sub_dir) as it:
            names = [e.path for e in it if e.name.endswith(".jsonl")]
    except FileNotFoundError:
        return newest  # no subagents dir: no subagents
    except OSError:
        return None  # there, but unlistable: unknown
    for sub in names:
        try:
            # os.stat, not the DirEntry's cached listing data: on NTFS the
            # listing's attributes "may not be current" (FindFirstFile's own
            # caveat), and this is the file being written right now.
            mtime = os.stat(sub).st_mtime
        except FileNotFoundError:
            continue  # removed since the listing
        except OSError:
            return None
        newest = max(newest, mtime)
    return newest


claude_idle_probe = IdleProbe(
    sessions_by_pid=read_session_files, last_activity=last_activity
)
