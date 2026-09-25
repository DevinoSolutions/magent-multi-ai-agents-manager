"""Pure data + policy for running a project on a pool machine (a node).

What a node IS (``Node``), what running a project there NEEDS (``Recipe``:
repos, files to push, the auto-memory dir), and where node data lives on this
PC (``~/.magent/nodes/``). Everything that touches a node or runs git is
``remote_mux``. A leaf: never imports magent.cli, never spawns a process. Its
only I/O is the node-map file and local stat()s.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from magent.sessions.claude import encode_claude_project_path

if TYPE_CHECKING:
    from collections.abc import Mapping

# Everything node-shaped on this PC: the placement map, per-node snapshots and
# load history, pulled transcripts. Import-bound, so it is registered in
# tests/conftest.py's _IMPORT_BOUND_PATHS -- the env redirect is too late for it.
NODES_DIR = Path.home() / ".magent" / "nodes"
# Which node each project was placed on: {project: NodeMapEntry fields}.
# Machine state, like account-map.json -- the pin (`"node": "second"`) is
# config; where `"auto"` landed is not. Read through read_node_map; written
# only through PR-D's update_node_map (cross-process lock), which sits on
# write_node_map -- never a direct write_node_map call from a feature.
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
    """

    path: Path
    url: str
    branch: str
    dirty: bool
    unpushed: bool
    detached: bool
    ignored: tuple[str, ...] = ()


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


def encoded_project_dir(path: str) -> str:
    """The directory NAME under ~/.claude/projects/ for a session whose cwd is
    the absolute path ``path``, on this PC or on a node. A ``~``-relative path
    names the wrong directory; expand it first. The rule is the CLI's, not the
    OS's: this delegates to the one encoder, never a second copy."""
    return encode_claude_project_path(path)


def _map_entry(raw: object) -> NodeMapEntry | None:
    if not isinstance(raw, dict):
        return None
    nick, sid, root = raw.get("nick"), raw.get("sid"), raw.get("remote_root")
    ts, attached = raw.get("placed_ts"), raw.get("attached_existing")
    if not (isinstance(nick, str) and isinstance(sid, str) and isinstance(root, str)):
        return None
    # bool is an int subclass: `"placed_ts": true` is corruption, not 1.0.
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    if not isinstance(attached, bool):
        return None
    return NodeMapEntry(
        nick=nick,
        sid=sid,
        placed_ts=float(ts),
        attached_existing=attached,
        remote_root=root,
    )


def read_node_map() -> dict[str, NodeMapEntry]:
    """``node-map.json`` keyed by project name. A missing file, a torn write,
    or anything that is not a JSON object reads as ``{}``, and a malformed
    entry is dropped alone -- the map is a record of where things landed, and
    a bad one must never stop a launch."""
    try:
        raw = json.loads(NODE_MAP_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, NodeMapEntry] = {}
    for project, value in raw.items():
        entry = _map_entry(value)
        if isinstance(project, str) and entry is not None:
            out[project] = entry
    return out


def write_node_map(entries: Mapping[str, NodeMapEntry]) -> None:
    """Replace ``node-map.json`` with ``entries`` atomically: a sibling temp
    file, then one ``os.replace`` (atomic only within a filesystem, hence the
    sibling -- the ``config_io._save_raw_config_atomic`` idiom). A failed write
    leaves the previous map untouched and no temp file behind.

    The PRIMITIVE: atomic, but not serialized against another process's
    read-modify-write. Callers go through PR-D's ``update_node_map``, which
    holds the cross-process lock around read + this write (DECISION-13)."""
    NODE_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = NODE_MAP_PATH.with_name(f"{NODE_MAP_PATH.name}.{os.getpid()}.tmp")
    payload = {project: dataclasses.asdict(e) for project, e in sorted(entries.items())}
    try:
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, NODE_MAP_PATH)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
