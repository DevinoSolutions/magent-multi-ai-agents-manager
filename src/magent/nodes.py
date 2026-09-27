"""Pure data + policy for running a project on a pool machine (a node).

What a node IS (``Node``), what running a project there NEEDS (``Recipe``:
repos, files to push, the auto-memory dir), and where node data lives on this
PC (``~/.magent/nodes/``). Everything that touches a node or runs git is
``remote_mux``. A leaf: never imports magent.cli, never spawns a process. Its
only I/O is files under ``NODES_DIR`` (the node map and the per-node
mirror), the map's sidecar lock, local stat()s, and nodes.log (via
``magent.log``, itself a leaf).
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import json
import math
import os
import re
import stat
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path, PurePath
from typing import TYPE_CHECKING

from magent.config import NODE_AUTO, NODE_CLOUD, is_cloud, runs_on_node
from magent.lockfile import LockHeld, persistent_lock
from magent.log import get_logger
from magent.psmux import session_name
from magent.sessions import is_ide_tool
from magent.sessions.claude import encode_claude_project_path
from magent.titles import get_leaf_name

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

    from magent.config import MagentConfig, ProjectConfig

# Everything node-shaped on this PC: the placement map, per-node snapshots and
# load history, pulled transcripts. Import-bound, so it is registered in
# tests/conftest.py's _IMPORT_BOUND_PATHS -- the env redirect is too late for it.
NODES_DIR = Path.home() / ".magent" / "nodes"
# Which node each project was placed on: {project: NodeMapEntry fields}.
# Machine state, like account-map.json -- the pin (`"node": "second"`) is
# config; where `"auto"` landed is not. Read through read_node_map (best
# effort) or load_node_map_strict (before a write); written only through PR-D's
# update_node_map (cross-process lock), which sits on write_node_map -- never a
# direct write_node_map call from a feature.
NODE_MAP_PATH = NODES_DIR / "node-map.json"


class NodeConfigError(ValueError):
    """A project cannot be resolved to a node: no pool entry, no placement, no user."""


@dataclass(frozen=True)
class Node:
    """A resolved pool machine: who magent logs in as, where, and under what root."""

    nick: str
    host: str
    user: str
    root: str

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"


@dataclass(frozen=True)
class LocalGitState:
    """One LOCAL repo as git reports it (gathered by ``remote_mux``).

    ``ignored`` is ``git ls-files --others --ignored --exclude-standard
    --directory`` verbatim -- repo-relative, '/'-separated, a wholly ignored
    directory as one ``dir/`` entry. It is the raw material ``push_set`` picks
    from, carried here so this module never runs git itself.

    ``no_commits`` is an unborn HEAD: the branch is named but holds nothing
    yet, so there is nothing to push and nothing for the node to check out.
    Defaulted so every construction that predates it keeps compiling.
    """

    path: Path
    url: str
    branch: str
    dirty: bool
    unpushed: bool
    detached: bool
    ignored: tuple[str, ...] = ()
    no_commits: bool = False


@dataclass(frozen=True)
class RepoSpec:
    """One repo to clone-or-fetch on the node, and where."""

    url: str
    branch: str
    remote_dir: str


@dataclass(frozen=True)
class Recipe:
    """Everything needed to run one project somewhere else (master plan §3). The
    node backend consumes it; any future backend consumes the same Recipe."""

    project: str
    sid: str
    repos: tuple[RepoSpec, ...]
    push_files: tuple[Path, ...]
    memory_dir: Path | None
    remote_root: str
    warnings: tuple[str, ...] = ()
    # PR-D (C1): what the node backend needs beyond the §3 tuple. Defaulted, so
    # every construction that predates it still compiles. recipe_for sets
    # local_root: the project dir RESOLVED, and every push_files entry is
    # `relative_to` it (that relative path is where it lands under
    # remote_root). launch.node_recipe fills the command fields from
    # settings.tools, which this pure module never reads.
    local_root: Path | None = None
    tool: str = ""
    command: str = ""
    fresh_command: str | None = None


@dataclass(frozen=True)
class LoadSample:
    """One ``sample.sh`` reading from a node."""

    ts: float
    nproc: int
    load1: float
    load5: float
    load15: float
    mem_total_mb: int
    mem_avail_mb: int
    my_sessions: int


@dataclass(frozen=True)
class NodeMapEntry:
    """Where one project runs: ``node-map.json``'s value for that project.

    Any field added later MUST have a default, and ``_map_entry`` must read it
    with that default -- an older file (and an older magent reading a newer
    file) has to keep working."""

    nick: str
    sid: str
    placed_ts: float
    attached_existing: bool
    remote_root: str
    # PR-D: how to reach the session again without re-resolving the config --
    # the ssh target it was started through, and the node's ABSOLUTE folder
    # (bring_up.sh reports it). F2 and `sessions --json` read these.
    target: str = ""
    cwd: str = ""


# What ships besides git (spec §8, D3): a session can't start without its
# secrets and its local Claude settings, and none of them are in the clone.
# Matched against git's OWN ignored listing, so a tracked file (`.env.example`)
# can never be pushed over the clone's copy.
_PUSH_FIXED = (".claude/settings.local.json", "CLAUDE.local.md", ".mcp.json")
# Never pushed, whatever `push` says (spec §8 "Not transferred, ever"): this
# PC's keys, ccswap's account backups, the Claude login itself, and the usual
# credential files of other tools -- refusing one is cheap. Home-relative; a
# trailing '/' names a directory (everything under it), anything else one
# file. Matched case-insensitively on every OS (`_is_forbidden`).
_NEVER_PUSHED = (
    ".ssh/",
    ".claude-swap-backup/",
    ".claude/.credentials.json",
    ".aws/credentials",
    ".netrc",
    ".gnupg/",
    ".config/gh/hosts.yml",
    ".docker/config.json",
    ".kube/config",
)


def encoded_project_dir(path: str) -> str:
    """The directory NAME under ~/.claude/projects/ for a session whose cwd is
    the absolute path ``path``, on this PC or on a node. A ``~``-relative path
    names the wrong directory; expand it first. The rule is the CLI's, not the
    OS's: this delegates to the one encoder, never a second copy."""
    return encode_claude_project_path(path)


def _epoch(value: object) -> float | None:
    """A timestamp read back from JSON, as a finite float, or None. bool is an
    int subclass (`"ts": true` is corruption, not 1.0); json.loads accepts
    NaN/Infinity and arbitrarily long integers, and neither is a time --
    float() of a 309+-digit int raises instead of saturating."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        out = float(value)
    except OverflowError:
        return None
    return out if math.isfinite(out) else None


def _map_entry(raw: object) -> NodeMapEntry | None:
    if not isinstance(raw, dict):
        return None
    nick, sid, root = raw.get("nick"), raw.get("sid"), raw.get("remote_root")
    ts, attached = raw.get("placed_ts"), raw.get("attached_existing")
    if not (isinstance(nick, str) and isinstance(sid, str) and isinstance(root, str)):
        return None
    placed_ts = _epoch(ts)
    if placed_ts is None or not isinstance(attached, bool):
        return None
    target, cwd = raw.get("target", ""), raw.get("cwd", "")
    return NodeMapEntry(
        nick=nick,
        sid=sid,
        placed_ts=placed_ts,
        attached_existing=attached,
        remote_root=root,
        # Optional fields degrade to their default rather than dropping an
        # entry that still says where a session runs.
        target=target if isinstance(target, str) else "",
        cwd=cwd if isinstance(cwd, str) else "",
    )


# A reader racing write_node_map's os.replace on Windows gets PermissionError
# for the instant the rename holds the file. Measured: one writer + one reader
# thread read a FULL 200-entry map as unreadable 15 times. Retry briefly rather
# than report "no placements".
_BUSY_RETRIES = 5
_BUSY_SLEEP_S = 0.02


def load_node_map_strict() -> dict[str, NodeMapEntry]:
    """``node-map.json`` keyed by project name, or an error -- never a guess.

    ``{}`` means exactly one thing here: the file does not exist. A file that
    is momentarily locked (``PermissionError``: the Windows reader racing an
    ``os.replace``) is retried ``_BUSY_RETRIES`` times ``_BUSY_SLEEP_S`` apart,
    then re-raised; any other ``OSError``, and a torn, non-object or too
    deeply nested file (``ValueError``), propagate. A malformed ENTRY is still
    dropped alone.

    PR-D's ``update_node_map`` MUST read through this for its
    read-modify-write: an unreadable map read as ``{}`` and written back would
    erase every placement. Code that only needs a best-effort answer calls
    ``read_node_map``."""
    for attempt in range(_BUSY_RETRIES + 1):
        try:
            text = NODE_MAP_PATH.read_text(encoding="utf-8")
            break
        except FileNotFoundError:
            return {}
        except PermissionError:
            if attempt == _BUSY_RETRIES:
                raise
            time.sleep(_BUSY_SLEEP_S)
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        # Name the file, as the non-object branch does: "Expecting value: line
        # 1 column 1" alone does not say WHICH file is refusing every write.
        raise ValueError(f"{NODE_MAP_PATH}: {exc}") from exc
    except RecursionError as e:
        # json.loads' answer to deep nesting. Re-raised as the ValueError every
        # caller already catches for a bad file, so none of them needs to know.
        raise ValueError(f"{NODE_MAP_PATH}: nested too deeply to read") from e
    if not isinstance(raw, dict):
        raise ValueError(f"{NODE_MAP_PATH}: not a JSON object")  # noqa: TRY004  # reason: a non-object file is corrupt DATA, the same family as the JSONDecodeError (a ValueError) a torn file raises; callers catch one type for every bad file
    out: dict[str, NodeMapEntry] = {}
    for project, value in raw.items():
        entry = _map_entry(value)
        # A key out of json.loads is always str: this isinstance narrows the
        # type for ty, it does not tolerate anything.
        if isinstance(project, str) and entry is not None:
            out[project] = entry
    return out


def read_node_map() -> dict[str, NodeMapEntry]:
    """``node-map.json`` keyed by project name, tolerantly: whatever
    ``load_node_map_strict`` raises -- a map still busy after its retries, a
    torn write, a file that is not a JSON object, one nested too deep for
    ``json.loads`` (``RecursionError``, which is not a ``ValueError``) -- reads
    as ``{}``. The map is a record of where things landed, and a bad one must
    never stop a launch or an F2 press. Never write back what this returns; see
    ``load_node_map_strict``."""
    try:
        return load_node_map_strict()
    except (OSError, ValueError, RecursionError):
        return {}


# What every surface says for a map ``load_node_map_strict`` refused.
MAP_UNREAD = "the node map could not be read"


def map_unread_text(exc: BaseException) -> str:
    """``MAP_UNREAD`` naming what refused the map by its CLASS only: the
    error's own text names the map's path and the parser's words, which go
    to nodes.log, never the screen."""
    return f"{MAP_UNREAD} ({type(exc).__name__})"


def write_node_map(entries: Mapping[str, NodeMapEntry]) -> None:
    """Replace ``node-map.json`` with ``entries`` atomically: a sibling temp
    file unique to this call (``tempfile.mkstemp``), then one ``os.replace``
    (atomic only within a filesystem, hence the sibling -- the
    ``config_io._save_raw_config_atomic`` idiom). A replace a Windows reader
    blocks (``PermissionError``) is retried ``_REPLACE_RETRIES`` times
    ``_REPLACE_SLEEP_S`` apart. A failed write leaves the previous map
    untouched and no temp file behind.

    The PRIMITIVE: atomic, but not serialized against another process's
    read-modify-write. Callers go through ``update_node_map``, which holds the
    cross-process lock around ``load_node_map_strict`` + this write
    (DECISION-13)."""
    payload = {project: dataclasses.asdict(e) for project, e in sorted(entries.items())}
    # allow_nan=False: a non-finite placed_ts would otherwise go to disk as a
    # bare NaN/Infinity literal, which is not JSON.
    text = json.dumps(payload, indent=2, allow_nan=False) + "\n"
    NODE_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Not a pid-derived name: threads share a pid, and a shared temp name made
    # 229 of 300 writes from two writer threads fail.
    fd, tmp_name = tempfile.mkstemp(
        dir=NODE_MAP_PATH.parent, prefix=NODE_MAP_PATH.name + ".", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            # On disk BEFORE the rename: a crash between an unsynced write and
            # the replace can leave a zero-length node-map.json, which the
            # strict reader (rightly) refuses forever after.
            fh.flush()
            os.fsync(fh.fileno())
        _replace_retrying(tmp, NODE_MAP_PATH)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


# Readers take no lock, so on Windows one holding node-map.json makes the
# writer's os.replace fail with PermissionError for as long as it reads --
# the mirror of load_node_map_strict's busy retry. Bounded: ~0.5 s outlasts
# any one read_text; a map held for good still surfaces as the error.
_REPLACE_RETRIES = 20
_REPLACE_SLEEP_S = 0.025


def _replace_retrying(src: Path, dst: Path) -> None:
    for attempt in range(_REPLACE_RETRIES + 1):
        try:
            os.replace(src, dst)
        except PermissionError:
            if attempt == _REPLACE_RETRIES:
                raise
            time.sleep(_REPLACE_SLEEP_S)
        else:
            return


# The sidecar every map writer holds (DECISION-13): ~/.magent/node-map.lock,
# through lockfile.persistent_lock. NOT lockfile.exclusive_lock -- that one
# never waits and unlinks its file on exit, so a waiter could lock a file the
# holder is about to delete and run beside it. This one waits (bounded) and
# its file is never deleted.
MAP_LOCK_NAME = "node-map"
# Far longer than one read-modify-write. A writer still waiting after this is
# stuck behind a hung process, and says so (LockHeld) rather than hanging.
MAP_LOCK_WAIT_S = 10.0

# Threads of ONE process queue here first, so a fan-out's writers wait on a
# cheap lock instead of polling the file lock against each other.
_MAP_LOCK = threading.Lock()


@contextlib.contextmanager
def map_lock(wait_s: float = MAP_LOCK_WAIT_S) -> Iterator[None]:
    """Hold the node map exclusively across threads AND processes, waiting up
    to ``wait_s`` in all for it. Raises LockHeld (an OSError) when it stays
    taken. ``update_node_map`` is the one production holder; a test holds it
    through here too, so both take the very same lock."""
    deadline = time.monotonic() + wait_s
    if not _MAP_LOCK.acquire(timeout=max(wait_s, 0.0)):
        raise LockHeld("the node map is held by another writer in this process")
    try:
        remaining = max(deadline - time.monotonic(), 0.0)
        with persistent_lock(MAP_LOCK_NAME, wait_s=remaining):
            yield
    finally:
        _MAP_LOCK.release()


def _sweep_stale_temps() -> None:
    """Delete ``node-map.json.*.tmp`` siblings (``write_node_map``'s
    ``mkstemp`` names). Called only under ``map_lock``: every writer holds it
    from mkstemp to replace, so any temp still there belongs to a writer that
    was killed mid-write."""
    for stale in NODE_MAP_PATH.parent.glob(f"{NODE_MAP_PATH.name}.*.tmp"):
        with contextlib.suppress(OSError):
            stale.unlink()


def update_node_map(
    project: str,
    entry: NodeMapEntry | None,
    *,
    expect: NodeMapEntry | None = None,
    wait_s: float = MAP_LOCK_WAIT_S,
) -> dict[str, NodeMapEntry]:
    """Set ``project``'s entry (or remove it, with None), keeping every other
    project's, and return the map as it now stands. The ONE writer entry point
    (DECISION-13): the read and the write happen under ``map_lock``, so `up`,
    `down`, placement and recall running at once each keep the others'
    entries.

    With ``expect``, the change is a compare-and-set: it applies only while
    ``project``'s entry is still exactly ``expect``, checked under the lock.
    `down` clearing the placement it just killed passes the entry it read, so
    a placement a concurrent `up` recorded meanwhile survives.

    Reads through ``load_node_map_strict``: a torn or unreadable map raises
    (ValueError / OSError) and is left as it is, never read as ``{}`` and
    written back over every placement. Raises LockHeld when another writer
    holds the map for longer than ``wait_s``."""
    with map_lock(wait_s):
        _sweep_stale_temps()
        current = load_node_map_strict()
        if expect is not None and current.get(project) != expect:
            return current
        if entry is None:
            if project not in current:
                return current
            del current[project]
        else:
            current[project] = entry
        write_node_map(current)
        return current


def open_target(
    project: str, entries: Mapping[str, NodeMapEntry]
) -> tuple[str, str] | None:
    """``(ssh target, folder)`` for opening ``project`` -- a window's name, so
    either a project name or its session id -- in an editor over Remote-SSH.
    None for a project no node holds, a cloud placement (it has no ssh
    target), or an entry with no recorded target or absolute folder -- the
    caller falls through to /api/sessions. Never ``remote_root`` as the
    folder: it keeps its ``~`` (DECISION-11) and a Remote-SSH folder URI
    does not expand one.

    Both values land in an editor argv, so a value the bring-up writer would
    never have recorded is None too (a tampered or buggy map): a folder that
    is not absolute or carries a control character (the writer's
    ``remote_mux._clean_absolute`` rule; ``-`` can't lead an absolute path, so
    no VS Code flag either), and a target that would read as an ssh option or
    has no host after its ``@`` (a hostless authority degrades to a LOCAL
    open of a node path)."""
    entry = entries.get(project) or next(
        (e for e in entries.values() if e.sid == project), None
    )
    if entry is None or entry.nick == NODE_CLOUD:
        return None
    target, cwd = entry.target, entry.cwd
    if not cwd.startswith("/") or any(unicodedata.category(ch) == "Cc" for ch in cwd):
        return None
    if target.startswith("-") or not target.rpartition("@")[2]:
        return None
    return target, cwd


def placement_of(
    proj: ProjectConfig, entries: Mapping[str, NodeMapEntry]
) -> tuple[str, NodeMapEntry] | None:
    """``(map key, entry)`` recording where ``proj`` was placed, or None: by
    its project name first, else by its session id -- a title edited since the
    bring-up can keep its sid, and the session it names still runs. The order
    ``open_target`` reads in. `down` asks this both to decide where to act and
    what to kill, so the two answers cannot diverge."""
    name = project_name(proj)
    if name in entries:
        return name, entries[name]
    sid = node_sid(proj)
    return next(((k, e) for k, e in entries.items() if e.sid == sid), None)


# A portable Unix login (useradd's default NAME_REGEX, minus the trailing-$
# machine-account form). Checked only on the DERIVED user: an explicit
# settings.nodes.<nick>.user is the operator's word.
_NODE_LOGIN = re.compile(r"[a-z_][a-z0-9_-]{0,31}")


def node_for_nick(
    config: MagentConfig, nick: str, *, local_user: str, label: str | None = None
) -> Node:
    """The pool node ``nick``, fully resolved -- the D4 user rule in its one
    home. Raises NodeConfigError.

    ``label`` (a project path) prefixes the unknown-nick error only: it is the
    one that is about the caller's reference. The other two name
    ``settings.nodes.<nick>``, the thing to fix. A nick read from the node map
    may have left ``settings.nodes`` since it was written; such a caller must
    catch NodeConfigError and re-place, never surface it.
    """
    prefix = f"{label}: " if label else ""
    pool = config.settings.nodes
    entry = pool.get(nick)
    if entry is None:
        known = ", ".join(sorted(pool)) or "none"
        raise NodeConfigError(
            f"{prefix}node {nick!r} is not in settings.nodes; known nodes: {known}"
        )
    user = entry.user if entry.user is not None else local_user.lower()
    if not user:
        raise NodeConfigError(
            f"settings.nodes.{nick}.user is not set and the local username is "
            "unknown; set it explicitly"
        )
    if entry.user is None and not _NODE_LOGIN.fullmatch(user):
        raise NodeConfigError(
            f"settings.nodes.{nick}.user is not set and the local username "
            f"{local_user!r} is not a node login ({user!r} does not match "
            f"{_NODE_LOGIN.pattern}); set it explicitly"
        )
    if entry.user is None and user == "root":
        raise NodeConfigError(
            f"settings.nodes.{nick}: magent is running as root and would run "
            'sessions as root on the node; write "user": "root" to mean it (D4)'
        )
    return Node(nick=nick, host=entry.host, user=user, root=entry.root)


def resolve(
    config: MagentConfig,
    proj: ProjectConfig,
    *,
    local_user: str,
    placed: str | None = None,
) -> Node:
    """The node ``proj`` runs on, fully resolved. Raises NodeConfigError.

    ``placed`` is the nick placement chose for a ``"node": "auto"`` project; a
    pinned project ignores it. ``local_user`` is ``env.local_username()``,
    passed in so this stays pure. A node with no ``user`` runs as the local
    user, lowercased to match the per-person node account convention (D4). That
    derived name must be a login ssh can use (a Windows ``USERNAME`` such as
    ``"Amin Dhouib"`` is not), and it may never be root -- running sessions as
    root has to be written down.
    """
    if proj.node is None:
        raise NodeConfigError(f"{proj.path}: not a node project")
    if is_cloud(proj):
        raise NodeConfigError(
            f'{proj.path}: "node": "cloud" runs on the cloud backend, not a pool '
            "machine; it has no Node to resolve"
        )
    nick = placed if proj.node == NODE_AUTO else proj.node
    if nick is None:
        raise NodeConfigError(
            f'{proj.path}: "node": "auto" needs a placement before it can resolve'
        )
    pool = config.settings.nodes
    if proj.node == NODE_AUTO and nick not in pool:
        known = ", ".join(sorted(pool)) or "none"
        raise NodeConfigError(
            f"{proj.path}: placement chose {nick!r}, which is no longer in "
            f"settings.nodes; re-place (known nodes: {known})"
        )
    return node_for_nick(config, nick, local_user=local_user, label=proj.path)


# --- The per-node mirror (written by node_sync, read by status/recall) -------
#   <nick>/sessions.json        {"ts": <PC epoch>, "sessions": [...]}: liveness
#   <nick>/load.jsonl           one LoadSample per line, ts on the PC's clock
#   <nick>/pull.json            {sid: {"since": <node epoch>, "realpath": ...}}
#   <nick>/<sid>/transcripts/   the node's ~/.claude/projects/<dir>/ contents
#   <nick>/<sid>/state/         that session's ~/.magent/state records
# Every path reads NODES_DIR at CALL time (never a second import-bound Path),
# so the test-isolation redirect of NODES_DIR covers all of them.

# A session directory sits beside these per-node files; no sid may take a name.
_RESERVED_NAMES = frozenset(
    {"sessions.json", "load.jsonl", "pull.json", "node-map.json"}
)
# Every path part must be a legal file name on THIS PC, which may be Windows.
_UNSAFE_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
# ntpath's reserved set on 3.13 (ntpath.isreserved is 3.13+, so it is copied):
# the superscript digits count as COM/LPT numbers too.
_DEVICE_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"COM{c}" for c in "123456789¹²³"),
        *(f"LPT{c}" for c in "123456789¹²³"),
    }
)


def _safe_part(part: str) -> bool:
    # A part ending in "." or " " is refused outright (Windows drops them), so
    # the device check needs only ntpath's: the stem before the FIRST dot,
    # trailing spaces dropped -- "CON .jsonl" opens the console.
    return (
        part not in ("", ".", "..")
        and _UNSAFE_CHARS.search(part) is None
        and not part.endswith((".", " "))
        and part.split(".", 1)[0].rstrip(" ").upper() not in _DEVICE_NAMES
    )


def pullable_sid(sid: str) -> bool:
    """Can ``sid`` name a directory under ``~/.magent/nodes/<nick>/`` here?"""
    return _safe_part(sid) and sid not in _RESERVED_NAMES


def node_dir(nick: str, *, nodes_dir: Path | None = None) -> Path:
    return (nodes_dir if nodes_dir is not None else NODES_DIR) / nick


def transcripts_dir(nick: str, sid: str, *, nodes_dir: Path | None = None) -> Path:
    """Where the daemon mirrors a node session's Claude project directory:
    its CONTENTS (``<uuid>.jsonl``, ``<uuid>/subagents/``, ``memory/``).
    ``sid`` is joined VERBATIM: ``psmux.session_name`` keeps ``/`` and ``\\``,
    so a node-map sid like ``/etc`` would resolve outside the node dir --
    callers (the attention reader, recall) pass it through
    ``pullable_sid`` first."""
    return node_dir(nick, nodes_dir=nodes_dir) / sid / "transcripts"


def state_dir(nick: str, sid: str, *, nodes_dir: Path | None = None) -> Path:
    """Where the daemon mirrors a node session's agent-state records.
    ``sid`` is joined VERBATIM, exactly as in ``transcripts_dir``: callers
    pass a node-map sid through ``pullable_sid`` first."""
    return node_dir(nick, nodes_dir=nodes_dir) / sid / "state"


def sessions_path(nick: str, *, nodes_dir: Path | None = None) -> Path:
    return node_dir(nick, nodes_dir=nodes_dir) / "sessions.json"


def load_path(nick: str, *, nodes_dir: Path | None = None) -> Path:
    return node_dir(nick, nodes_dir=nodes_dir) / "load.jsonl"


def pull_marks_path(nick: str, *, nodes_dir: Path | None = None) -> Path:
    return node_dir(nick, nodes_dir=nodes_dir) / "pull.json"


def write_text_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` in one ``os.replace`` from a sibling temp
    file (atomic only within a filesystem, hence the sibling). The temp name
    comes from ``tempfile.mkstemp`` -- unique across processes AND threads (the
    daemon writes several nodes at once) -- and ends in ``.tmp``, never
    ``.json``: readers glob ``*.json`` in a mirror dir and must never see a
    half-written file. A failed write leaves the old file and no temp behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp = Path(tmp_name)
    try:
        try:
            fh = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            # fdopen never took ownership: close the raw fd here, or it leaks
            # (and on Windows an open handle also blocks the unlink below).
            os.close(fd)
            raise
        with fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def write_json_atomic(path: Path, data: object) -> None:
    """``write_text_atomic`` of ``data`` as JSON. A NaN or infinity raises
    ValueError before any file is touched -- ``NaN`` is not JSON, and a
    non-finite timestamp would make every staleness check lie."""
    write_text_atomic(path, json.dumps(data, indent=2, allow_nan=False) + "\n")


@dataclass(frozen=True)
class NodeSessions:
    """A node's tmux session list as of the last successful pull (``ts`` is
    this PC's clock). The daemon rewrites it every tick the node answers, so
    an old ``ts`` means unreachable, never dead."""

    ts: float
    sessions: tuple[str, ...]


def read_sessions(nick: str, *, nodes_dir: Path | None = None) -> NodeSessions | None:
    """``<nick>/sessions.json``, or None when it is missing or not a snapshot.
    A non-finite ``ts`` is not a snapshot: it would read as fresh forever.
    Neither is a ``sessions`` list holding any non-string: that is corruption,
    and silently dropping the odd entry would report a live session dead."""
    try:
        raw = json.loads(
            sessions_path(nick, nodes_dir=nodes_dir).read_text(encoding="utf-8")
        )
    # RecursionError: json.loads' answer to deep nesting, a corrupt file too.
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(raw, dict):
        return None
    ts, names = raw.get("ts"), raw.get("sessions")
    # bool is an int subclass: `"ts": true` is corruption, not 1.0.
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    # float() of a 309-digit int raises OverflowError: not a snapshot either.
    try:
        ts_f = float(ts)
    except OverflowError:
        return None
    if not math.isfinite(ts_f) or not isinstance(names, list):
        return None
    if not all(isinstance(n, str) for n in names):
        return None
    return NodeSessions(ts=ts_f, sessions=tuple(names))


def sessions_stale(
    snap: NodeSessions | None, *, pull_interval_s: float, now: float
) -> bool:
    """Spec §7: a snapshot more than two pull intervals older OR newer than
    ``now`` reads ``stale``; a wall clock that jumped backwards therefore
    reads stale for at most one tick, never fresh forever."""
    return snap is None or abs(now - snap.ts) > 2 * pull_interval_s


def _is_env_file(name: str) -> bool:
    return name == ".env" or name.startswith(".env.")


def _from_git_listing(repo: Path, ignored: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for entry in ignored:
        if entry.endswith("/"):
            # A wholly ignored directory is never descended (node_modules is
            # not a push); only a fixed path that lives inside it can ship.
            # Listed unlooked-at: _push judges it like any git hit (the
            # credential stores first), so one file has one outcome however
            # git listed it -- an unreadable one warns, a missing one is none.
            found += [repo / fixed for fixed in _PUSH_FIXED if fixed.startswith(entry)]
        elif _is_env_file(entry.rsplit("/", 1)[-1]) or entry in _PUSH_FIXED:
            # git emits '/' on every OS, like the _PUSH_FIXED literals.
            found.append(repo / entry)
    return found


# What Path.resolve raises when a path cannot be resolved: a symlink loop
# (RuntimeError before Python 3.13, OSError from 3.13 on), an OS error, or a
# name the OS rejects (ValueError: an embedded NUL). The one list; both
# resolve helpers catch exactly this.
_RESOLVE_ERRORS = (OSError, RuntimeError, ValueError)


def _try_resolve(path: Path) -> Path | None:
    """``path`` resolved (symlinks followed), or None when it cannot be
    (``_RESOLVE_ERRORS``). Each caller states its own policy for None."""
    try:
        return path.resolve()
    except _RESOLVE_ERRORS:
        return None


def _resolved(path: Path) -> Path:
    """``path`` resolved, or a NodeConfigError naming it, chained to the OS's
    own error: a recipe cannot place a repo it cannot locate. The message
    reaches the screen, so it names that error by class; its words (another
    path among them) are the chained cause's, for nodes.log."""
    try:
        return path.resolve()
    except _RESOLVE_ERRORS as exc:
        raise NodeConfigError(
            f"{path}: cannot be resolved ({type(exc).__name__})"
        ) from exc


# The stat errors that mean "nothing is there": no such entry, a parent that
# is a file, a symlink loop, or a name Windows cannot hold (ERROR_INVALID_NAME,
# ERROR_CANT_RESOLVE_FILENAME). Every other OSError -- a folder this user may
# not read, a drive that is not ready -- is unknown, and unknown is never
# absent: ``path_mode`` raises it.
_ABSENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})
_ABSENT_WINERRORS = frozenset({123, 1921})


def path_mode(path: Path) -> int | None:
    """``path``'s ``st_mode`` (symlinks followed); None when nothing is there.
    Raises OSError when that cannot be told. The one existence check on the
    node path: from Python 3.14 ``Path.is_dir``/``is_file``/``exists`` answer
    False for EVERY OSError (on Windows they no longer stat at all), where
    3.10-3.13 raised -- an unreadable folder would read "not there" on one
    version and fail on another. ``os.stat`` answers alike on all of them."""
    try:
        return os.stat(path).st_mode
    except ValueError:
        return None  # a name the OS cannot hold (an embedded NUL)
    except OSError as exc:
        if (
            exc.errno in _ABSENT_ERRNOS
            or getattr(exc, "winerror", None) in _ABSENT_WINERRORS
        ):
            return None
        raise


def path_exists(path: Path) -> bool:
    """``Path.exists`` that raises when it cannot tell (``path_mode``)."""
    return path_mode(path) is not None


def path_is_dir(path: Path) -> bool:
    """``Path.is_dir`` that raises when it cannot tell (``path_mode``)."""
    mode = path_mode(path)
    return mode is not None and stat.S_ISDIR(mode)


def path_is_file(path: Path) -> bool:
    """``Path.is_file`` that raises when it cannot tell (``path_mode``)."""
    mode = path_mode(path)
    return mode is not None and stat.S_ISREG(mode)


def _workspace_root_files(project_dir: Path) -> list[Path]:
    # A workspace root is not a repo, so git lists nothing there -- its own env
    # files and local Claude settings would otherwise never leave this PC.
    try:
        entries = list(project_dir.iterdir())
    except (OSError, ValueError) as exc:
        # By class, chained, as ``_resolved`` does.
        raise NodeConfigError(
            f"{project_dir}: cannot be listed ({type(exc).__name__})"
        ) from exc
    found = [p for p in entries if _is_env_file(p.name) and path_is_file(p)]
    found += [project_dir / f for f in _PUSH_FIXED if path_is_file(project_dir / f)]
    return found


def _inside_a_repo(project_dir: Path, states: Sequence[LocalGitState]) -> bool:
    """Is ``project_dir`` one of ``states``' repos, or inside one? Judged on
    RESOLVED paths: a project configured as a junction/symlink to its repo, or
    as a monorepo subdirectory, is still inside it -- and the root listing,
    which cannot tell a tracked ``.env.example`` from an ignored ``.env``,
    must not run there. (``recipe_for`` refuses a project inside a larger repo;
    ``push_set`` stays safe for one regardless.) A path that will not resolve
    counts as inside: the fail-safe direction ships less."""
    root = _try_resolve(project_dir)
    repos = [_try_resolve(state.path) for state in states]
    if root is None or None in repos:
        return True
    return any(repo is not None and root.is_relative_to(repo) for repo in repos)


def _forbidden_roots(home: Path) -> list[tuple[PurePath, bool]]:
    """Each ``_NEVER_PUSHED`` entry under ``home``, resolved (a store that is
    itself a symlink is judged where it lands), paired with "is a directory".
    A store that will not resolve is judged at its own lexical place -- under
    the RESOLVED home, so a home reached through a symlink still matches the
    resolved targets ``_is_forbidden`` is handed (fail toward shipping less)."""
    base = _try_resolve(home) or home
    stores = [
        (base / entry.rstrip("/"), entry.endswith("/")) for entry in _NEVER_PUSHED
    ]
    return [(_try_resolve(store) or store, is_dir) for store, is_dir in stores]


def _folded(path: PurePath) -> tuple[str, ...]:
    return tuple(part.casefold() for part in path.parts)


def _is_forbidden(target: PurePath, forbidden: Sequence[tuple[PurePath, bool]]) -> bool:
    """``target`` (already resolved) is a credential file, or lies under a
    credential directory. Casefolded on EVERY OS: a PosixPath compares
    case-sensitively, but APFS does not, so ``.SSH/id_ed25519`` IS the key
    there. On a case-sensitive filesystem the refusal is merely cautious --
    the fail-safe direction."""
    parts = _folded(target)
    for store, is_dir in forbidden:
        store_parts = _folded(store)
        if parts == store_parts or (
            is_dir and parts[: len(store_parts)] == store_parts
        ):
            return True
    return False


def _shippable_git_target(
    path: Path, forbidden: Sequence[tuple[PurePath, bool]]
) -> Path | None:
    """A path git's listing reported is a snapshot claim: its resolved target
    (symlinks followed) when it is a regular file now and not a credential
    store's, else None -- gone since the listing included. Raises OSError when
    whether it is a file cannot be told (``path_mode``): the caller skips it
    too, but says so."""
    target = _try_resolve(path)
    if target is None or _is_forbidden(target, forbidden):
        return None
    mode = path_mode(target)
    return target if mode is not None and stat.S_ISREG(mode) else None


def _listed_name(hit: Path, repo: Path, root: Path) -> str:
    """How a warning names a git hit: as git listed it, under its repo's place
    in the project (``api/cfg/.env`` in a workspace), or absolute when the repo
    is not under the project."""
    base = _try_resolve(repo)
    if base is not None and base.is_relative_to(root):
        return (base.relative_to(root) / hit.relative_to(repo)).as_posix()
    return str(hit)


def _classify_extras(
    project_dir: Path,
    extras: Sequence[str],
    forbidden: Sequence[tuple[PurePath, bool]],
) -> tuple[list[Path], list[str]]:
    shipped: list[Path] = []
    warnings: list[str] = []
    # Both sides resolved: a symlink inside the project that points out of it
    # is outside, and so is a project reached through a symlinked parent.
    root = _resolved(project_dir)
    for extra in extras:
        target = _try_resolve(project_dir / extra)
        if target is None:
            warnings.append(f"push: {extra} cannot be resolved; skipped")
        elif not target.is_relative_to(root):
            warnings.append(f"push: {extra} is outside the project; skipped")
        elif _is_forbidden(target, forbidden):
            warnings.append(f"push: {extra} is never pushed (credentials); skipped")
        elif (mode := path_mode(target)) is not None and stat.S_ISDIR(mode):
            warnings.append(f"push: {extra} is a directory; list its files; skipped")
        elif mode is None or not stat.S_ISREG(mode):
            warnings.append(f"push: {extra} does not exist; skipped")
        else:
            shipped.append(_named_path(project_dir, extra, target, root))
    return shipped, warnings


def _named_path(project_dir: Path, extra: str, target: Path, root: Path) -> Path:
    """Where a validated extra ships: the path the user WROTE, normalized
    (``./a//b`` is ``a/b``), so a symlink ships under its own name rather than
    its target's. Only when that name still lands on ``target`` and lies
    lexically inside ``project_dir``; otherwise (a ``..`` through a symlinked
    directory, which POSIX resolves differently from the lexical collapse)
    the validated target's own place."""
    named = Path(os.path.normpath(project_dir / extra))
    if named.is_relative_to(project_dir) and _try_resolve(named) == target:
        return named
    return project_dir / target.relative_to(root)


def _one_per_file(found: Sequence[Path], project_dir: Path) -> tuple[Path, ...]:
    """``found`` with each file once, sorted. A file is its NAME in its resolved
    directory: a linked project reaches ``repo/.env`` through git's listing and
    ``link/.env`` through an extra, and that is one push, kept under the path
    lexically inside ``project_dir``.

    Two names for one target both ship only when the link is a FILE symlink
    (``a.json -> b.json``: two names in one directory, and the node needs
    both). Through a DIRECTORY symlink (``cfg -> shared``) an extra's
    ``cfg/.env`` and git's ``shared/.env`` share a resolved directory and a
    name, so they collapse to one entry.

    On Windows ``WindowsPath`` equality is case-insensitive, so ``.ENV`` and
    ``.env`` dedupe there. ``PosixPath`` equality is not: two genuinely
    different files on a case-sensitive filesystem rightly both ship, but one
    file reached under two casings on a case-insensitive one (macOS APFS)
    double-ships (pre-existing, deliberately not fixed here)."""
    kept: dict[Path, Path] = {}
    for path in found:
        key = (_try_resolve(path.parent) or path.parent) / path.name
        held = kept.get(key)
        if held is None or (
            path.is_relative_to(project_dir) and not held.is_relative_to(project_dir)
        ):
            kept[key] = path
    return tuple(sorted(kept.values(), key=str))


def _under_root(path: Path, root: Path, project_dir: Path) -> Path:
    """A push entry re-spelled under the RESOLVED project ``root``, so the
    Recipe can promise ``path.relative_to(local_root)``. One lexically under
    the configured ``project_dir`` (an extra or a workspace-root file named
    through a link) keeps its tail -- its own name -- and swaps the prefix,
    which resolves to ``root``. One already under ``root`` is kept. Otherwise
    (a git hit listed under a repo reached through a link) its directory is
    resolved, as ``_one_per_file`` keys it. A file under none of these would
    land outside the node folder: a NodeConfigError, never a push. ``_push``
    already drops every entry whose target resolves outside ``root``, so this
    is a guard that should never fire."""
    if path.is_relative_to(project_dir):
        return root / path.relative_to(project_dir)
    if path.is_relative_to(root):
        return path
    parent = _try_resolve(path.parent)
    if parent is not None and parent.is_relative_to(root):
        return parent / path.name
    raise NodeConfigError(f"{path}: would ship from outside the project {root}")


def _push(
    project_dir: Path,
    states: Sequence[LocalGitState],
    *,
    home: Path,
    extras: Sequence[str],
) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    """``push_set`` and ``push_warnings`` in one pass: each extra resolved once,
    the credential stores resolved once."""
    forbidden = _forbidden_roots(home)
    root = _resolved(project_dir)
    found: list[Path] = []
    warnings: list[str] = []
    for state in states:
        for hit in _from_git_listing(state.path, state.ignored):
            try:
                target = _shippable_git_target(hit, forbidden)
            except OSError as exc:
                # Still skipped -- a listed file is a snapshot claim -- but
                # never silently: the class on screen, the rest in the log.
                name = _listed_name(hit, state.path, root)
                get_logger("nodes").warning("push file %s: %s", hit, exc)
                warnings.append(
                    f"push: {name} cannot be read ({type(exc).__name__}); skipped"
                )
                continue
            if target is None:
                continue
            # git DESCENDS a junction/directory link (Git for Windows lists
            # `cfg/.env` when `cfg` is a junction out of the project), so a
            # hit is held to the same rule as an extra: resolved, inside.
            if not target.is_relative_to(root):
                name = _listed_name(hit, state.path, root)
                warnings.append(f"push: {name} is outside the project; skipped")
                continue
            found.append(hit)
    if not _inside_a_repo(project_dir, states):
        found += _workspace_root_files(project_dir)
    shipped, extra_warnings = _classify_extras(project_dir, extras, forbidden)
    warnings += extra_warnings
    return _one_per_file([*found, *shipped], project_dir), tuple(warnings)


def push_set(
    project_dir: Path,
    states: Sequence[LocalGitState],
    *,
    home: Path,
    extras: Sequence[str] = (),
) -> tuple[Path, ...]:
    """The local files a bring-up ships beside the clone (spec §8).

    Per repo, from git's ignored listing: ``.env``/``.env.*`` at any depth,
    ``.claude/settings.local.json``, ``CLAUDE.local.md``, an ignored
    ``.mcp.json``. A workspace root's own copies. Then ``extras`` (a project's
    ``push``). Tracked files never ship, ignored directories are never
    descended, nothing under ``home``'s credential stores ever ships, and an
    extra must resolve (symlinks followed) inside ``project_dir``. A git hit
    ships only while it is a regular file, and -- like an extra -- only when it
    resolves inside ``project_dir`` (git descends a junction or directory link
    that leads out of the project). The workspace-root listing runs only
    when ``project_dir`` -- resolved -- is inside none of ``states``' repos
    (``_inside_a_repo``). Sorted, one entry per file (``_one_per_file``),
    absolute."""
    return _push(project_dir, states, home=home, extras=extras)[0]


def push_warnings(
    project_dir: Path, extras: Sequence[str], *, home: Path
) -> tuple[str, ...]:
    """Why an entry in a project's ``push`` will not ship -- missing, outside
    the project, a directory, a credential store, or a path that will not
    resolve. Warnings, never errors (spec §8)."""
    return tuple(_classify_extras(project_dir, extras, _forbidden_roots(home))[1])


# A URL with a scheme (RFC 3986's scheme grammar, so `git+https` counts), split
# into scheme, authority (up to the first '/', '?' or '#') and the rest.
_SCHEME_URL = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://([^/?#]*)(.*)", re.DOTALL)
# git's remote-helper form `<transport>::<address>` (same scheme grammar as
# git's is_urlschemechar), split into the transport and the address.
_TRANSPORT_URL = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)::(.*)", re.DOTALL)
# Schemes whose login is an ssh user (`git@`), not a secret.
_SSH_SCHEMES = frozenset({"ssh", "git+ssh", "ssh+git"})


def _without_credentials(url: str) -> tuple[str, bool]:
    """``url`` without the credentials its userinfo carries, and whether any
    were stripped. They would otherwise land in the node's ``.git/config``,
    the Recipe's repr and every log line.

    A ``<transport>::<address>`` URL (git's remote-helper form, e.g.
    ``https::https://u:pw@h``) is peeled layer by layer -- a stacked prefix
    included -- and its address judged by the rules below; every transport
    prefix is re-attached byte-for-byte. An address that is no URL (``ext::``
    carries a command) therefore passes through untouched.

    Only a ``scheme://`` URL is inspected; scp-like ``git@host:org/repo`` and
    anything schemeless pass through byte-for-byte, as does a URL whose
    authority has no '@'. The userinfo is everything before the authority's
    LAST '@' (an IPv6 ``[...]`` host holds none). An ssh-family scheme keeps
    its login and loses only the password (``ssh://user:pw@h`` ->
    ``ssh://user@h``; ``ssh://git@h`` is untouched; an empty login goes with
    its password, ``ssh://:pw@h`` -> ``ssh://h``). Every other scheme loses
    the WHOLE userinfo: a token-only login (``https://ghp_...@h``) is the
    credential. A userinfo holding nothing but ':' (or an ssh password that
    is empty) is no credential: the URL is left byte-for-byte and not
    reported as stripped. A stripped URL's scheme is lowercased."""
    prefix = ""
    address = url
    while _SCHEME_URL.fullmatch(address) is None:
        transport = _TRANSPORT_URL.fullmatch(address)
        if transport is None:
            return url, False
        prefix += f"{transport.group(1)}::"
        address = transport.group(2)
    stripped = _without_userinfo_secret(address)
    if stripped is None:
        return url, False
    return f"{prefix}{stripped}", True


def _without_userinfo_secret(url: str) -> str | None:
    """A ``scheme://`` ``url`` with its credential removed (the rules of
    ``_without_credentials``), or None when it carries no credential."""
    match = _SCHEME_URL.fullmatch(url)
    if match is None:
        return None
    scheme, authority, rest = match.groups()
    userinfo, at, host = authority.rpartition("@")
    if not at or not userinfo.replace(":", ""):
        return None
    scheme = scheme.lower()
    if scheme in _SSH_SCHEMES:
        login, _colon, password = userinfo.partition(":")
        if not password:
            return None
        if not login:
            return f"{scheme}://{host}{rest}"
        return f"{scheme}://{login}@{host}{rest}"
    return f"{scheme}://{host}{rest}"


def project_name(proj: ProjectConfig) -> str:
    """The name a project goes by: its title, else its folder's leaf name. The
    node map's key, and -- sanitized -- its session id. One function, so the
    map, the sid and ``recipe_for`` can never disagree about a project."""
    return proj.title or get_leaf_name(proj.path)


def node_sid(proj: ProjectConfig) -> str:
    """The tmux session id of ``proj`` on its node: the same sanitizer every
    local session uses, so a node session and its window share one name."""
    return session_name(project_name(proj))


def node_projects(
    config: MagentConfig, group: str | None = None
) -> list[ProjectConfig]:
    """The enabled projects that run on a pool node (pinned or ``auto``), in
    config order, one per session id. Cloud projects are not pool projects, and
    an IDE project stays on this PC (there is no agent to host). The group
    filter matches ``psmux.eligible_projects``': case-insensitive."""
    out: list[ProjectConfig] = []
    seen: set[str] = set()
    for proj in config.projects:
        if not proj.enabled or not runs_on_node(proj):
            continue
        if group and (not proj.group or proj.group.lower() != group.lower()):
            continue
        if is_ide_tool(proj.tool or config.settings.default_tool):
            continue
        sid = node_sid(proj)
        if sid in seen:
            continue
        seen.add(sid)
        out.append(proj)
    return out


def _not_a_folder_name(name: str) -> bool:
    """``name`` cannot be the node folder: ``.``/``..`` (``..`` climbs to the
    node user's HOME), a control character, or a trailing dot or space --
    Windows opens ``api.`` as ``api``, so the local and remote names would
    diverge."""
    return (
        name in {".", ".."}
        or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name)
        or name.endswith((".", " "))
    )


def remote_root_for(node: Node, project_dir: Path) -> str:
    """Where ``project_dir`` lives on ``node``: the node's root plus the local
    folder's NAME (never its path). A folder with no name -- a drive root such
    as ``C:\\`` or ``/`` -- is refused: it would land AT the node's root, among
    every other project's folder. So is a name that is not a folder name
    (``_not_a_folder_name``): refused, never rewritten.

    The result is UNQUOTED and may start with ``~``, which stays unexpanded. A
    placement caller must run it through ``absolute_remote()`` BEFORE any
    ``shlex.quote`` -- a quoted ``~`` is never expanded by the node's shell."""
    return _remote_root_under(node.root, project_dir)


# The root spelled for a folder whose node nobody can tell -- an ``auto``
# project while the map is unreadable (D17's "(node unknown)").
UNKNOWN_NODE_ROOT = "(node unknown)"


def unknown_node_remote_root(project_dir: Path) -> str:
    """``remote_root_for`` when the node is UNKNOWN, not absent: the same
    folder-name refusals, under ``UNKNOWN_NODE_ROOT``. Only for the fleet
    folder check, which compares leaves alone -- nothing is dialed with it."""
    return _remote_root_under(UNKNOWN_NODE_ROOT, project_dir)


def on_unknown_node(recipe: Recipe) -> bool:
    """Whether ``recipe``'s folder came from ``unknown_node_remote_root``."""
    return recipe.remote_root.startswith(f"{UNKNOWN_NODE_ROOT}/")


def _remote_root_under(root: str, project_dir: Path) -> str:
    name = project_dir.name
    if not name:
        raise NodeConfigError(f"{project_dir}: a drive root cannot be a node project")
    if _not_a_folder_name(name):
        raise NodeConfigError(f"{project_dir}: {name!r} cannot name a node folder")
    return f"{root.rstrip('/')}/{name}"


def folder_leaf(recipe: Recipe) -> str:
    """The node folder NAME ``recipe`` lands in: what the leaf rule keys on."""
    return recipe.remote_root.rstrip("/").rsplit("/", 1)[-1]


def remote_root_collisions(recipes: Sequence[Recipe]) -> list[tuple[Recipe, ...]]:
    """Every group of two or more ``recipes`` that would share a node folder
    NAME -- the one statement of that rule. Groups come in the order their
    first member appears, members in input order; the same Recipe object
    given twice is one member, not a collision.

    ``remote_root_for`` keys on the local folder's leaf name, so ``C:/a/api``
    and ``C:/b/api`` both become ``<root>/api`` -- one clone would overwrite
    the other. The key is the LEAF (the last ``/`` segment of
    ``remote_root``) across the whole fleet, not the full path and not the
    node: ``auto`` placement may later co-locate any two projects on one
    node, and two projects with one leaf under different roots would then
    collide. So a caller checks the whole fleet's recipes, never one node's.
    Pure."""
    groups: dict[str, list[Recipe]] = {}
    for recipe in recipes:
        group = groups.setdefault(folder_leaf(recipe), [])
        if not any(member is recipe for member in group):
            group.append(recipe)
    return [tuple(group) for group in groups.values() if len(group) > 1]


def remote_root_collision_text(group: Sequence[Recipe]) -> str:
    """Why ``group`` (one of ``remote_root_collisions``) cannot run: names
    every project and every folder in it, and the fix."""
    names = [repr(recipe.project) for recipe in group]
    who = f"{', '.join(names[:-1])} and {names[-1]}"
    folders = ", ".join(recipe.remote_root for recipe in group)
    return (
        f"projects {who} would share the node folder name "
        f"{folder_leaf(group[0])!r} ({folders}); a node folder is named after "
        "the local folder and any two projects may land on one node, so rename "
        "one of them"
    )


def assert_distinct_remote_roots(recipes: Sequence[Recipe]) -> None:
    """Raise NodeConfigError, naming the whole group, for the first of
    ``remote_root_collisions(recipes)``; do nothing when there is none. A
    caller that must refuse only the colliding projects (a bring-up batch)
    reads ``remote_root_collisions`` itself. Pure."""
    collisions = remote_root_collisions(recipes)
    if collisions:
        raise NodeConfigError(remote_root_collision_text(collisions[0]))


def absolute_remote(path: str, home: str) -> str:
    """``path`` with a leading ``~`` expanded against the node's ``home`` (what
    ``printenv HOME`` said there). The node's shell would expand it too, but a
    path that crosses as an argument or into JSON must already be absolute.
    ``home``'s trailing slashes are dropped once, so ``~`` and ``~/x`` spell
    the home alike (``/`` stays ``/``)."""
    base = home.rstrip("/")
    if path == "~":
        return base or "/"
    if path.startswith("~/"):
        return f"{base}/{path[2:]}"
    return path


def refusal_for(state: LocalGitState, *, allow_dirty: bool = False) -> str | None:
    """Why ``state``'s repo cannot be reproduced on a node, naming the fix; or
    None. D7: magent never runs the fix. ``allow_dirty`` accepts a dirty or
    unpushed tree (the node gets origin's copy); it cannot conjure an origin,
    a branch or a first commit, so those three are refused regardless. A
    whitespace-only url is no origin; an empty branch is refused as a detached
    HEAD (there is no branch to push or check out); a branch with no commits
    never names ``git push``, which would fail ("src refspec does not match
    any")."""
    if not state.url.strip():
        return (
            f"{state.path}: no 'origin' remote; the node clones from origin -- "
            "add one and push"
        )
    if state.detached or not state.branch:
        return (
            f"{state.path}: HEAD is detached; the node checks out a branch -- "
            "run git switch <branch> first"
        )
    if state.no_commits:
        return (
            f"{state.path}: branch {state.branch} has no commits yet; the node "
            "checks out a branch from origin -- make a first commit and push"
        )
    if allow_dirty:
        return None
    if state.dirty:
        return (
            f"{state.path}: uncommitted changes (dirty tree) would not be on the "
            "node; commit and push them, or pass --allow-dirty"
        )
    if state.unpushed:
        return (
            f"{state.path}: branch {state.branch} has commits not on origin; run "
            f"git push -u origin {state.branch}, or pass --allow-dirty"
        )
    return None


# How a vetted file is opened -- by the payload (``remote_mux._read_regular``)
# and by ``_memory_state``'s look at each memory file, so what the recipe
# finds it cannot open is what the payload cannot read: never through a
# final-component link, never blocking on a FIFO. Read off the module, so
# Windows (which has neither flag, and wants O_BINARY) needs no
# `sys.platform` branch.
READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_BINARY", 0)
)


def _memory_is_link(memory: Path) -> bool:
    """Is the memory folder ``memory`` itself a link? Decided by ``realpath``:
    resolving it must change nothing but its parent's own resolution -- so a
    link ABOVE it (a dotfiles ``~/.claude``) is not one, and a Windows
    junction, which is no symlink to pathlib, is."""
    real = Path(os.path.realpath(memory))
    return real != Path(os.path.realpath(memory.parent)) / memory.name


def walk_memory(
    memory: Path, unreadable: Callable[[Path, OSError], None] | None = None
) -> Iterator[Path]:
    """Every REGULAR file under the memory folder ``memory`` that may ship, in
    walk order. THE memory walk: the payload's (``remote_mux._memory_files``)
    and the recipe's (``_memory_state``) are this one, so the recipe can only
    ever name what the payload walks. A link is never followed -- not a file
    link, not a folder link, and not ``memory`` itself being one (nothing is
    walked): the folder is Claude's, and a link in it can name ``~/.ssh``.

    "A link" is decided by ``realpath``, not ``is_symlink``: a Windows
    junction -- which any standard user can make -- is not a symlink to
    pathlib, and ``os.walk(followlinks=False)`` descends into one (a junction
    back to ``memory`` would keep it from ever returning). A subfolder is kept
    only when resolving it changes nothing but its parent's own resolution:
    then it is no link and, folder by folder, lies inside ``memory``'s
    realpath. Every file must resolve inside the resolved folder too.

    What is skipped is logged, never raised: a bring-up never fails because
    of memory. What cannot be READ -- a folder that cannot be listed, a file
    that cannot be stat-ed -- is also handed to ``unreadable``, with its
    error."""
    log = get_logger("nodes")
    if _memory_is_link(memory):
        log.warning("memory folder %s is a link; no memory shipped", memory)
        return
    real_mem = Path(os.path.realpath(memory))

    def cannot_list(exc: OSError) -> None:
        # os.walk's default is to skip a folder it cannot list in silence.
        where = Path(exc.filename) if exc.filename else memory
        log.warning("memory folder %s cannot be read; skipped: %s", where, exc)
        if unreadable is not None:
            unreadable(where, exc)

    for dirpath, dirnames, filenames in os.walk(memory, onerror=cannot_list):
        base = Path(dirpath)
        real_base = Path(os.path.realpath(base))
        kept: list[str] = []
        for name in dirnames:
            if Path(os.path.realpath(base / name)) == real_base / name:
                kept.append(name)
            else:
                log.warning("memory link %s skipped", base / name)
        dirnames[:] = kept  # os.walk descends only into what is left
        for name in filenames:
            path = base / name
            try:
                regular = not path.is_symlink() and path_is_file(path)
            except OSError as exc:
                # Named, not taken for "not a file" (Python 3.14's is_file).
                log.warning("memory file %s cannot be read; skipped: %s", path, exc)
                if unreadable is not None:
                    unreadable(path, exc)
                continue
            if not regular:
                log.warning("memory entry %s is not a regular file; skipped", path)
                continue
            if not Path(os.path.realpath(path)).is_relative_to(real_mem):
                log.warning("memory entry %s resolves outside memory; skipped", path)
                continue
            yield path


def _memory_state(memory: Path) -> tuple[bool, tuple[str, ...]]:
    """Whether the memory folder ``memory`` ships, and one warning per part of
    it that cannot be read. Never raises: a bring-up never fails because of
    memory, and nor does an unreadable folder pass for none -- each is named,
    class only, with the path and the full error in nodes.log. The walk IS
    the payload's (``walk_memory``), so nothing the payload never walks --
    behind a link, or ``memory`` being one -- is ever named; run here, its
    warnings reach the screen with the recipe's. Each file it yields is
    also opened the payload's way (``READ_FLAGS``) and closed at once:
    stat-able is not readable -- a Windows deny-read ACL leaves the stat
    working -- and the payload's own read failure is logged, not shown."""

    def none_shipped(exc: OSError) -> tuple[bool, tuple[str, ...]]:
        return False, (
            f"memory: cannot be read ({type(exc).__name__}); no memory shipped",
        )

    log = get_logger("nodes")
    try:
        if not path_is_dir(memory):
            return False, ()
    except OSError as exc:
        log.warning("memory folder %s cannot be read; skipped: %s", memory, exc)
        return none_shipped(exc)
    unread: list[tuple[Path, OSError]] = []
    for path in walk_memory(memory, lambda where, exc: unread.append((where, exc))):
        try:
            os.close(os.open(path, READ_FLAGS))
        except OSError as exc:
            log.warning("memory file %s cannot be read; skipped: %s", path, exc)
            unread.append((path, exc))
    warned: list[str] = []
    for where, exc in unread:
        if where == memory:
            return none_shipped(exc)
        # walk_memory names every folder by joining onto ``memory``.
        rel = (
            where.relative_to(memory).as_posix()
            if where.is_relative_to(memory)
            else where.name
        )
        warned.append(f"memory: {rel} cannot be read ({type(exc).__name__}); skipped")
    return True, tuple(warned)


def recipe_for(
    proj: ProjectConfig,
    node: Node,
    states: Sequence[LocalGitState],
    *,
    home: Path,
    project_dir: Path,
) -> Recipe:
    """Everything a bring-up of ``proj`` on ``node`` needs (spec §7c).

    ``project_dir`` is the resolved LOCAL project directory (a config path may
    be baseDir-relative, so the caller resolves it). The node mirrors its NAME,
    not its path: a repo project lands at ``<root>/<name>``, a workspace's
    child repos at ``<root>/<name>/<child>``. Remote paths keep the root's
    ``~`` unexpanded -- the node's shell owns that expansion, and bring_up.sh
    reports the absolute paths back. The sid is ``psmux.session_name`` of the
    same label every local session uses, so a node session and its window
    share one name. Repo placement is judged on RESOLVED paths, like
    ``push_set``: a project configured as a junction/symlink to its repo is
    still that repo; a path that will not resolve is a NodeConfigError."""
    project = project_name(proj)
    remote_root = remote_root_for(node, project_dir)
    root = _resolved(project_dir)
    repos: list[RepoSpec] = []
    repo_warnings: list[str] = []
    seen: set[Path] = set()
    for state in states:
        repo = _resolved(state.path)
        if repo in seen:
            continue
        seen.add(repo)
        if repo == root:
            remote_dir = remote_root
        elif repo.parent == root:
            remote_dir = f"{remote_root}/{state.path.name}"
        elif root.is_relative_to(repo):
            # A monorepo subdirectory: push_set is safe for it, but a node
            # clones whole repos -- the project cannot be placed on its own.
            raise NodeConfigError(
                f"{state.path}: the project is inside a larger repo; a node "
                "project is one repo, or a folder of repos"
            )
        else:
            raise NodeConfigError(
                f"{state.path} is neither the project nor a direct child of it; "
                "a node project is one repo, or a folder of repos"
            )
        url, stripped = _without_credentials(state.url)
        if stripped:
            repo_warnings.append(
                f"repo {remote_dir}: origin URL carried credentials; stripped "
                "-- the node authenticates with its own gh token"
            )
        repos.append(RepoSpec(url=url, branch=state.branch, remote_dir=remote_dir))
    if not repos:
        raise NodeConfigError(
            f"{project_dir}: has no git repo; a node project is one repo, "
            "or a folder of repos"
        )
    found, push_warned = _push(
        project_dir, states, home=home, extras=tuple(proj.push or ())
    )
    push_files = tuple(
        sorted((_under_root(p, root, project_dir) for p in found), key=str)
    )
    memory = (
        home / ".claude" / "projects" / encoded_project_dir(str(project_dir)) / "memory"
    )
    has_memory, memory_warned = _memory_state(memory)
    return Recipe(
        project=project,
        sid=session_name(project),
        repos=tuple(repos),
        push_files=push_files,
        memory_dir=memory if has_memory else None,
        remote_root=remote_root,
        warnings=(*repo_warnings, *push_warned, *memory_warned),
        local_root=root,
    )


# A node session's state as this PC knows it: from the sync daemon's last
# pull, never from a live ssh call (status must stay fast and offline-safe).
NODE_SESSION_STATES = ("live", "stale", "dead")


def node_session_state(
    sid: str, snap: NodeSessions | None, *, pull_interval_s: float, now: float
) -> str:
    """``live`` if the last fresh pull listed ``sid``, ``dead`` if it did
    not, ``stale`` when there is no fresh pull -- an unreachable node says
    nothing about its sessions, so it never reads dead (spec §7)."""
    if snap is None or sessions_stale(snap, pull_interval_s=pull_interval_s, now=now):
        return "stale"
    return "live" if sid in snap.sessions else "dead"


def session_rows(config: MagentConfig, *, now: float) -> list[dict[str, object]]:
    """One row per node project, config order: ``name``, ``session``,
    ``node`` (the nick; None for an ``auto`` project not yet placed, or whose
    placement cannot be read) and ``state`` (``NODE_SESSION_STATES``). An
    unplaced project is ``dead``.
    The map's recorded sid wins over the derived one: it is the id the
    session was started under.

    The map is read STRICTLY: the tolerant reader's ``{}`` for a torn, busy or
    non-object map would read as "nothing was ever placed" and turn a live
    session dead. A map this PC cannot read says nothing about any row -- the
    node AND the sid come from it -- so every row reads ``stale``, the same
    law as an unreachable node, and the reason goes to nodes.log."""
    try:
        entries: dict[str, NodeMapEntry] | None = load_node_map_strict()
    except (OSError, ValueError) as exc:
        get_logger("nodes").warning(
            "node map unreadable; every node session reads stale: %s", exc
        )
        entries = None
    interval = config.settings.node_sync.pull_interval_s
    rows: list[dict[str, object]] = []
    for proj in node_projects(config):
        name = project_name(proj)
        pinned = None if proj.node == NODE_AUTO else proj.node
        if entries is None:
            nick, sid, state = pinned, node_sid(proj), "stale"
        else:
            entry = entries.get(name)
            nick = entry.nick if entry else pinned
            sid = entry.sid if entry and entry.sid else node_sid(proj)
            state = (
                "dead"
                if nick is None
                else node_session_state(
                    sid, read_sessions(nick), pull_interval_s=interval, now=now
                )
            )
        rows.append({"name": name, "session": sid, "node": nick, "state": state})
    return rows


# --- placement for "node": "auto" (spec §11) -----------------------------------

# The auto sentinel is config.NODE_AUTO (B, DECISION-10/22); nodes has no copy.
# "cloud" is never a candidate: config refuses a pool entry named "cloud", so
# `place`, which only walks settings.nodes, cannot reach it.
PLACEMENT_WINDOW_S = 30 * 60
MIN_WINDOW_SAMPLES = 5
SPIKE_WEIGHT = 0.5
SPIKE_RATIO = 1.5
MEM_WEIGHT = 0.5
MEM_FLOOR = 0.15
# DECISION-11: under 10 % free (newest sample) a node is not eligible at all
# while any other node is above it -- the soft MEM term alone is at most 0.075.
MEM_HARD_FLOOR = 0.10
SESSION_WEIGHT = 0.05


@dataclass(frozen=True)
class NodeScore:
    """One node's §11 score and the terms it is made of (``node plan`` prints
    every one of them). ``live`` marks a score taken from a live sample;
    ``below_floor`` a node under ``MEM_HARD_FLOOR`` free memory."""

    nick: str
    samples: int
    p75: float
    spike: float
    mem: float
    my_sessions: int
    score: float
    live: bool = False
    below_floor: bool = False


def _finite(value: object) -> float:
    """``value`` as a float. Three refusals:

    - TypeError for a non-number. A bool and a numeric string both count:
      json's ``true`` is a Python bool (an int subclass), and ``sample.sh``
      prints bare numbers, so ``"1.5"`` is not a reading. The isinstance
      guard is also what narrows ``object`` for ty.
    - ValueError for NaN or an infinity: json accepts them, the snapshot
      writer does not.
    - OverflowError for an int too large for a float (json has no bound on
      an integer's digits)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"not a number: {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"non-finite reading: {number}")
    return number


def _integral(value: object) -> int:
    """``value`` as an int, as strict as ``_finite``: TypeError for a
    non-number (a bool and a str included), ValueError for a float that is
    not finite or not whole (``16.9``), OverflowError for an int too large
    for a float. A whole float (``16.0``) is taken.

    The overflow bound is the scorer's: every count ends up in float
    arithmetic (load per core, the free-memory fraction), so a count json
    parsed as a 401-digit int is refused HERE, where every caller already
    catches OverflowError, rather than crashing ``score_node`` later."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"not a number: {value!r}")
    if isinstance(value, int):
        float(value)  # OverflowError past a float's range; the value stays exact
        return value
    if not math.isfinite(value) or not value.is_integer():
        raise ValueError(f"not a whole reading: {value}")
    return int(value)


def _load_sample(raw: object) -> LoadSample:
    """``magent_sample``'s JSON object as a LoadSample -- the ONE parse, shared
    by ``remote_mux.sample()``, ``remote_mux.parse_pull`` and
    ``read_load_history``. KeyError, TypeError, ValueError or OverflowError
    when it is not one: a JSON list or string is a TypeError, and every field
    goes through ``_finite``/``_integral``, whose refusals (non-number, bool,
    string, NaN, infinity, fractional count, an integer too large for a
    float) are those exceptions."""
    if not isinstance(raw, dict):
        raise TypeError(f"expected an object, got {type(raw).__name__}")
    return LoadSample(
        ts=_finite(raw["ts"]),
        nproc=_integral(raw["nproc"]),
        load1=_finite(raw["load1"]),
        load5=_finite(raw["load5"]),
        load15=_finite(raw["load15"]),
        mem_total_mb=_integral(raw["mem_total_mb"]),
        mem_avail_mb=_integral(raw["mem_avail_mb"]),
        my_sessions=_integral(raw["my_sessions"]),
    )


def parse_load_lines(lines: Iterable[str]) -> list[LoadSample]:
    """LoadSamples out of ``load.jsonl`` lines, through the one strict parse.
    A line it refuses is skipped, never fatal: the daemon appends while a
    reader reads, so a torn last line is normal, and one bad reading must not
    take the whole placement pass down with it."""
    samples: list[LoadSample] = []
    for line in lines:
        try:
            samples.append(_load_sample(json.loads(line)))
        # RecursionError: json.loads' answer to deep nesting, which is not a
        # ValueError -- a corrupt line like any other.
        except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
            continue
    return samples


def read_load_history(nick: str, *, nodes_dir: Path | None = None) -> list[LoadSample]:
    """Every sample the daemon kept for ``nick`` (``<nick>/load.jsonl``), or
    [] when it never sampled that node -- the file does not exist. A file
    that is there and cannot be read (any other OSError, or bytes that are
    not UTF-8: UnicodeDecodeError, a ValueError) raises: that is unknown,
    not "never sampled"."""
    path = load_path(nick, nodes_dir=nodes_dir)  # E's (DECISION-19): one layout owner
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return parse_load_lines(text.splitlines())


def in_window(samples: Iterable[LoadSample], *, now: float) -> list[LoadSample]:
    """The samples placement may use: the last ``PLACEMENT_WINDOW_S``. No
    upper bound: every ts is stamped on THIS PC's clock (node_sync), so a
    sample past ``now`` is only ever a few seconds ahead of the caller's own
    reading of the clock (tests seed at ``time.time() + 30`` for that
    reason), never a node's clock drifting."""
    start = now - PLACEMENT_WINDOW_S
    return [s for s in samples if s.ts >= start]


def _p75(values: Sequence[float]) -> float:
    """75th percentile, linear between the closest ranks (what
    ``statistics.quantiles(method="inclusive")`` returns -- written out because
    that needs two points and one live sample is a legitimate window)."""
    ordered = sorted(values)
    pos = 0.75 * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def score_node(
    nick: str,
    window: Sequence[LoadSample],
    *,
    extra_sessions: int = 0,
    live: bool = False,
) -> NodeScore | None:
    """Spec §11 over one node's window; None when there is nothing to score.

    Load is per core (``load1``, spec §11's ``u``), so a 32-core box at load 8
    reads as quiet. Memory and my session count come from the NEWEST sample:
    they are levels, not rates. Two samples sharing the newest ts resolve to
    the worse one (less free memory, then more of my sessions), so the score
    never depends on the order they were read in. ``extra_sessions`` counts
    projects this same pass already put here.

    A negative reading is clamped to zero -- load per core, the free-memory
    fraction and my session count alike -- so a broken sampler can make a
    node look idle at best, never better than idle, and never win placement
    on an impossible number.
    """
    if not window:
        return None
    usage = [max(0.0, s.load1 / max(s.nproc, 1)) for s in window]
    p75 = _p75(usage)
    spike = SPIKE_WEIGHT * max(0.0, max(usage) - SPIKE_RATIO * p75)
    latest = max(window, key=lambda s: (s.ts, -s.mem_avail_mb, s.my_sessions))
    mem = 0.0
    below_floor = False
    if latest.mem_total_mb > 0:
        free = max(0.0, latest.mem_avail_mb / latest.mem_total_mb)
        mem = MEM_WEIGHT * max(0.0, MEM_FLOOR - free)
        below_floor = free < MEM_HARD_FLOOR
    mine = max(0, latest.my_sessions) + extra_sessions
    return NodeScore(
        nick=nick,
        samples=len(window),
        p75=p75,
        spike=spike,
        mem=mem,
        my_sessions=mine,
        score=p75 + spike + mem + SESSION_WEIGHT * mine,
        live=live,
        below_floor=below_floor,
    )


# Why a placement came out the way it did -- a closed vocabulary, printed by
# `magent node plan` and the launch notes.
PLACE_REASONS: dict[str, str] = {
    "kept": "already placed there (node-map.json)",
    "re-placed": "its node left settings.nodes; placed again by load",
    "placed": "lowest load score over the last 30 minutes",
    "no-data": "no node has load samples to score",
    "unknown": f"{MAP_UNREAD}, so where it runs is unknown",
}


@dataclass(frozen=True)
class Placement:
    """Where an ``auto`` project goes, why, and every score behind it.
    ``nick`` is None only for ``no-data`` and ``unknown`` (the map could not
    be read). ``note`` is a line to print."""

    nick: str | None
    reason: str
    scores: tuple[NodeScore, ...] = ()
    note: str | None = None


def place(
    config: MagentConfig,
    samples: Mapping[str, Sequence[LoadSample]],
    *,
    now: float,
    map_entry: str | None,
    placed: Mapping[str, int] | None = None,
    live: frozenset[str] = frozenset(),
) -> Placement:
    """Spec §11: the lowest score over the last 30 minutes wins; ties go to
    the node listed first in ``settings.nodes``. A node under
    ``MEM_HARD_FLOOR`` free memory is not a candidate while any other scored
    node is above it; when all are below, the score alone decides.

    Pure: it never talks to a node (``placement_samples`` owns the one live
    reading a sparse node gets). ``map_entry`` is the nick node-map.json already
    holds for the project -- it wins while that nick is still configured.
    ``placed`` counts projects this same pass already put on a node, so a batch
    spreads instead of piling onto one box before its next sample. ``live``
    names the nodes whose only sample is a live one.
    """
    nicks = list(config.settings.nodes)
    extra = placed or {}
    scored = tuple(
        score
        for nick in nicks
        if (
            score := score_node(
                nick,
                in_window(samples.get(nick, ()), now=now),
                extra_sessions=extra.get(nick, 0),
                live=nick in live,
            )
        )
        is not None
    )
    if map_entry is not None and map_entry in config.settings.nodes:
        return Placement(map_entry, "kept", scored)
    vanished = (
        f"{map_entry!r} is no longer in settings.nodes"
        if map_entry is not None
        else None
    )
    if not scored:
        return Placement(None, "no-data", scored, vanished)
    order = {nick: index for index, nick in enumerate(nicks)}
    candidates = [s for s in scored if not s.below_floor] or list(scored)
    # round(..., 9): scores equal up to float noise from summing the terms tie
    # exactly, so config order decides, not the last bit.
    best = min(candidates, key=lambda s: (round(s.score, 9), order[s.nick]))
    if vanished is None:
        return Placement(best.nick, "placed", scored)
    return Placement(
        best.nick, "re-placed", scored, f"{vanished}; re-placed on {best.nick!r}"
    )


def placement_samples(
    config: MagentConfig,
    *,
    now: float,
    live_sample: Callable[[str], LoadSample | None] | None,
    nodes_dir: Path | None = None,
    on_unreadable: Callable[[str, OSError | ValueError], None] | None = None,
) -> tuple[dict[str, list[LoadSample]], frozenset[str]]:
    """Each configured node's window, ready for ``place``, plus which nodes
    were read live.

    Spec §11's sparse rule: a node with fewer than ``MIN_WINDOW_SAMPLES`` in
    the window gets exactly ONE live reading, and that reading is its only
    sample -- three quiet samples from before someone started a build must not
    win. ``live_sample`` is the caller's seam to ``remote_mux.sample`` (this
    module never talks to a node); None -- a dry run -- scores a thin node on
    what it has. A failed live reading leaves the node unscored.

    The sparse nodes are probed at once, one thread each, so a pool where
    every node is thin (a fresh install, the sync daemon off) costs one probe
    timeout before launch, not one per node. ``live_sample`` signals an
    unreachable node by returning None, which leaves that node unscored; it
    must not raise for that. Anything it does raise propagates (the caller's
    wrapper is where a transport error becomes None). The live reading is
    deliberately NOT appended to ``load.jsonl``: the sync daemon owns sampling
    and that file has one writer, so a second placement pass inside the
    window probes the node again.

    A history that cannot be read is an empty window, so the sparse rule
    applies to it; it is logged in full to nodes.log and handed to
    ``on_unreadable`` so the caller can SAY so -- unknown, never read as
    "the daemon never sampled this node".
    """
    windows: dict[str, list[LoadSample]] = {}
    for nick in config.settings.nodes:
        try:
            history = read_load_history(nick, nodes_dir=nodes_dir)
        except (OSError, ValueError) as exc:
            get_logger("nodes").warning(
                "load history for node %s is unreadable: %s", nick, exc
            )
            if on_unreadable is not None:
                on_unreadable(nick, exc)
            history = []
        windows[nick] = in_window(history, now=now)
    sparse = [nick for nick, w in windows.items() if len(w) < MIN_WINDOW_SAMPLES]
    readings: dict[str, LoadSample | None] = {}
    if live_sample is not None and sparse:
        with ThreadPoolExecutor(max_workers=len(sparse)) as executor:
            readings = dict(zip(sparse, executor.map(live_sample, sparse), strict=True))
    samples: dict[str, list[LoadSample]] = {}
    sampled: set[str] = set()
    for nick, window in windows.items():
        if nick not in readings:
            samples[nick] = window
            continue
        reading = readings[nick]
        if reading is None:
            samples[nick] = []
            continue
        samples[nick] = [replace(reading, ts=now)]
        sampled.add(nick)
    return samples, frozenset(sampled)


# --- resume (spec §12) ----------------------------------------------------------

# A top-level conversation's file is named by its session id (a UUID); the
# subagent logs beside it are ``agent-<hex>.jsonl`` and are not resumable.
# Always ``fullmatch``: the id lands on a ``claude --resume`` line, and ``$``
# (or ``match``) would let a trailing newline -- an Enter -- ride along.
_SESSION_STEM = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def latest_transcript_id(
    nick: str, sid: str, *, nodes_dir: Path | None = None
) -> str | None:
    """The newest pulled conversation's id, or None when nothing was pulled.

    The file stem IS the session id (verified: every record's ``sessionId``
    equals it). "Newest" means most recently active ON THE NODE: tar keeps the
    node's mtimes, so the pulled file carries them. A member stored without a
    usable mtime gets the local pull time instead and can win -- rare, and
    still a valid id. A tie breaks by name, so the answer never depends on
    directory order.

    Only a regular file whose stem is a whole UUID counts. Each candidate is
    stat'ed on its own: one file vanishing mid-pull is skipped, it does not
    blank the answer for the whole folder.
    """
    folder = transcripts_dir(nick, sid, nodes_dir=nodes_dir)
    try:
        paths = list(folder.glob("*.jsonl"))
    except OSError:
        return None
    candidates: list[tuple[float, str, str]] = []
    for path in paths:
        if _SESSION_STEM.fullmatch(path.stem) is None:
            continue
        try:
            info = path.stat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            candidates.append((info.st_mtime, path.name, path.stem))
    newest = max(candidates, default=None)
    return None if newest is None else newest[2]


# --- what a node's repos looked like (spec §12 step 2) --------------------------


@dataclass(frozen=True)
class RepoStatus:
    """One repo on a node at last contact. ``head == ""`` means no repo was
    found there; ``dirty``/``unpushed`` None means unknown."""

    remote_dir: str
    head: str
    branch: str
    dirty: bool | None
    unpushed: int | None


@dataclass(frozen=True)
class RepoRecord:
    """The last known state of a node session's repos, and where it came from
    (``bring-up`` or ``recall``) -- so a recall from a node that no longer
    answers can still say which commit the work was at."""

    ts: float
    source: str
    repos: tuple[RepoStatus, ...]


# repo_status.sh's stdout is the node's words, so it is bounded here: a session
# root is one repo or a workspace of a handful, and no field needs more than a
# path's length. A line past either bound is dropped, never truncated.
REPO_STATUS_MAX_LINES = 256
REPO_STATUS_MAX_FIELD = 4096
# An unpushed count wider than this is not a count git produced for a repo.
_COUNT_MAX_DIGITS = 9
# C0, DEL and C1: nothing a node reports may drive the terminal it is shown on.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _count(text: str) -> int | None:
    # str.isdigit() is true for U+00B2 SUPERSCRIPT TWO (which int() refuses)
    # and for full-width digits, and int() of thousands of digits is slow and
    # raises on 3.11+: every one of those reads as unknown.
    if text.isascii() and text.isdigit() and len(text) <= _COUNT_MAX_DIGITS:
        return int(text)
    return None


def parse_repo_status(text: str) -> list[RepoStatus]:
    """``repo_status.sh``'s lines. A line that is not five tab-separated
    fields, or has a field longer than ``REPO_STATUS_MAX_FIELD``, is not a
    status line and is dropped; at most ``REPO_STATUS_MAX_LINES`` lines are
    read. Control characters are stripped from every field. A ``dirty`` token
    other than ``true``/``false`` (``unknown``, ``missing``) and a count that
    is not a small ASCII number both mean unknown (None)."""
    out: list[RepoStatus] = []
    for line in text.split("\n")[:REPO_STATUS_MAX_LINES]:
        fields = line.split("\t")
        if len(fields) != 5 or any(len(f) > REPO_STATUS_MAX_FIELD for f in fields):
            continue
        remote_dir, head, branch, dirty, unpushed = (
            _CONTROL_CHARS.sub("", f) for f in fields
        )
        state = {"true": True, "false": False}.get(dirty)
        out.append(RepoStatus(remote_dir, head, branch, state, _count(unpushed)))
    return out


def repo_record_path(nick: str, sid: str, *, nodes_dir: Path | None = None) -> Path:
    """``<nick>/<sid>/repos.json``. An unsafe ``sid`` raises NodeConfigError
    here rather than trusting every caller to have run ``pullable_sid``: the
    sid comes from the node map, and ``../../x`` must never name a file
    outside the node's own directory."""
    if not pullable_sid(sid):
        raise NodeConfigError(f"not a safe session id for a repo record: {sid!r}")
    return node_dir(nick, nodes_dir=nodes_dir) / sid / "repos.json"  # E's layout owner


def write_repo_record(
    nick: str, sid: str, record: RepoRecord, *, nodes_dir: Path | None = None
) -> bool:
    """Replace ``<nick>/<sid>/repos.json`` through ``write_json_atomic`` (a
    unique temp file, one replace, no temp left behind). False, with a log
    line, when nothing was written: an OSError, a non-finite ``ts``
    (ValueError, since NaN is not JSON), or an unsafe ``sid``
    (NodeConfigError, also a ValueError). A failed write keeps the old
    record."""
    body = {
        "ts": record.ts,
        "source": record.source,
        "repos": [dataclasses.asdict(r) for r in record.repos],
    }
    try:
        write_json_atomic(repo_record_path(nick, sid, nodes_dir=nodes_dir), body)
    except (OSError, ValueError):
        get_logger("nodes").warning(
            "could not write the repo record for %s/%r", nick, sid, exc_info=True
        )
        return False
    return True


def _repo_status(row: object) -> RepoStatus | None:
    if not isinstance(row, dict):
        return None
    remote_dir, head, branch = row.get("remote_dir"), row.get("head"), row.get("branch")
    if not (
        isinstance(remote_dir, str)
        and isinstance(head, str)
        and isinstance(branch, str)
    ):
        return None
    dirty, unpushed = row.get("dirty"), row.get("unpushed")
    return RepoStatus(
        remote_dir=remote_dir,
        head=head,
        branch=branch,
        dirty=dirty if isinstance(dirty, bool) else None,
        unpushed=unpushed
        if isinstance(unpushed, int) and not isinstance(unpushed, bool)
        else None,
    )


def read_repo_record(
    nick: str, sid: str, *, nodes_dir: Path | None = None
) -> RepoRecord | None:
    """The stored record, or an error -- never a guess. None means exactly
    one thing: there is no record file. A file that cannot be read raises
    its OSError; one that is torn or not UTF-8, is not a record -- not an
    object, a ``ts`` that is not a finite number (the node map's rule,
    ``_epoch``), a bad ``source`` or ``repos`` -- or an unsafe ``sid``
    (NodeConfigError) raises a ValueError. Unknown is never read as "no
    record was ever written". A malformed row is dropped on its own."""
    path = repo_record_path(nick, sid, nodes_dir=nodes_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    body = json.loads(text)
    if not isinstance(body, dict):
        raise ValueError(f"{path}: not a JSON object")  # noqa: TRY004  # reason: corrupt DATA, the same family as a torn file's JSONDecodeError; callers catch one type for every bad file
    ts, source, rows = _epoch(body.get("ts")), body.get("source"), body.get("repos")
    if ts is None or not isinstance(source, str) or not isinstance(rows, list):
        raise ValueError(f"{path}: not a repo record")
    repos = tuple(s for s in (_repo_status(r) for r in rows) if s is not None)
    return RepoRecord(ts=ts, source=source, repos=repos)
