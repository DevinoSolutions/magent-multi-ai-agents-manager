"""Projects as data: list, add, remove, enable/disable -- always validated,
always written through ``config_io.save`` (lock, validate, backup, atomic).

The one implementation behind ``/api/v1/projects`` and ``magent config
add/remove/enable/disable``. Refusals raise ``ProjectError`` with a wire
code; nothing here prints, prompts or exits. A leaf over ``config_io``,
``config``, ``psmux`` and ``launch``; never imports the cli package.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from magent import config_io, psmux
from magent.lockfile import LockHeld
from magent.paths import find_config
from magent.wire import WireError

if TYPE_CHECKING:
    from collections.abc import Iterator


class ProjectError(WireError):
    """A refused project change, carrying the wire error code."""


@dataclass(frozen=True)
class Project:
    """One configured project as the API reports it (spec 3.3)."""

    name: str
    session: str
    path: str
    group: str | None
    tool: str | None
    enabled: bool
    node: str | None
    windows: int


@dataclass(frozen=True)
class RemoveResult:
    removed: list[str] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _windows(value: object) -> int:
    if isinstance(value, bool):
        return 1
    if isinstance(value, int):
        return max(1, value)
    if isinstance(value, list):
        return max(1, len(value))
    return 1


def _project(raw: dict[str, object]) -> Project:
    path = _str(raw.get("path")) or ""
    name = _str(raw.get("title")) or Path(path.replace("\\", "/")).name or path
    enabled = raw.get("enabled", True)
    return Project(
        name=name,
        session=psmux.session_name(name),
        path=path,
        group=_str(raw.get("group")),
        tool=_str(raw.get("tool")),
        enabled=enabled if isinstance(enabled, bool) else True,
        node=_str(raw.get("node")),
        windows=_windows(raw.get("windows")),
    )


def _entries(data: dict[str, object]) -> list[dict[str, object]]:
    projects = data.get("projects")
    if not isinstance(projects, list):
        return []
    return [p for p in projects if isinstance(p, dict)]


def _matches(raw: dict[str, object], query: str) -> bool:
    """A project answers to its path, its path's leaf, or its title."""
    project = _project(raw)
    normalized = query.replace("\\", "/")
    return query in (project.name, Path(project.path).name) or (
        project.path.replace("\\", "/") == normalized
    )


def _config_file(config_path: str | None) -> Path:
    """The config file to change. A missing file is ``not_found`` on purpose:
    nothing here creates a config (``magent --init`` does), and an API caller
    pointed at a machine with no config should learn that, not get an empty
    project list it can append to."""
    path = find_config(config_path)
    if not path.exists():
        raise ProjectError("not_found", f"no config found at {path}")
    return path


def _load(path: Path) -> dict[str, object]:
    try:
        return config_io.load_raw(path)
    except (OSError, ValueError, TypeError) as exc:
        raise ProjectError("unavailable", f"config unreadable: {exc}") from exc


def _save(path: Path, data: dict[str, object]) -> None:
    """``config_io.save`` with its refusals in wire terms: content that would
    not load is ``invalid_request`` (``invalid_config``); a backup or write
    the filesystem refused (read-only file, unwritable backups dir, a reader
    holding the file past every retry, disk full) is ``unavailable``
    (``write_failed``). Either way the config on disk is the old one."""
    try:
        config_io.save(path, data)
    except config_io.ConfigWriteError as exc:
        raise ProjectError(
            "invalid_request", str(exc), {"reason": "invalid_config"}
        ) from exc
    except OSError as exc:
        raise ProjectError(
            "unavailable", f"config not written: {exc}", {"reason": "write_failed"}
        ) from exc


@contextmanager
def _locked() -> Iterator[None]:
    """``config_io.locked``: writers in this process queue, another process's
    writer is waited for (``config_io.LOCK_WAIT_S``), and only a lock that
    stays held is answered as ``conflict`` instead of ``LockHeld``."""
    try:
        with config_io.locked():
            yield
    except LockHeld as exc:
        raise ProjectError(
            "conflict", "the config is being changed by another magent"
        ) from exc


def list_projects(config_path: str | None) -> list[Project]:
    """Every project in config order, enabled or not."""
    return [_project(p) for p in _entries(_load(_config_file(config_path)))]


def add(
    config_path: str | None,
    path: str,
    *,
    title: str | None = None,
    group: str | None = None,
    tool: str | None = None,
    color: str | None = None,
    host: str | None = None,
    windows: int | None = None,
    node: str | None = None,
) -> Project:
    """Append a project and save. Refused, file untouched, when its session
    name is already taken (``conflict``, ``duplicate_session``) or the result
    would not load, e.g. an unknown node (``invalid_request``).

    The duplicate check is stricter than the loader's
    ``config._check_session_names`` on purpose: it counts every project,
    IDE-tool and disabled ones included, and refuses two local projects
    sharing a name where the loader lets the first win. An API caller cannot
    see the config, so a silent first-wins dedupe would hide its new project
    from every later ``sessions`` read (decision 5)."""
    if not path.strip():
        raise ProjectError("invalid_request", "path is required")
    entry: dict[str, object] = {"path": path.replace("\\", "/")}
    for key, value in (
        ("group", group),
        ("tool", tool),
        ("color", color),
        ("title", title),
        ("host", host),
        ("windows", windows),
        ("node", node),
    ):
        if value:
            entry[key] = value
    config_file = _config_file(config_path)
    new = _project(entry)
    with _locked():
        data = _load(config_file)
        clash = [
            p.path for p in map(_project, _entries(data)) if p.session == new.session
        ]
        if clash:
            raise ProjectError(
                "conflict",
                f"{clash[0]} already uses the session name {new.session!r}; "
                "give the new project a distinct title",
                {"reason": "duplicate_session"},
            )
        projects = data.get("projects")
        if not isinstance(projects, list):
            projects = []
            data["projects"] = projects
        projects.append(entry)
        _save(config_file, data)
    return new


def remove(config_path: str | None, query: str, *, stop: bool = False) -> RemoveResult:
    """Remove every project answering to ``query``; with ``stop``, stop
    their sessions first through the verifying shutdown path.

    The sessions are stopped BEFORE the config lock is taken (a verified
    shutdown can take seconds, and every other writer would wait on it or be
    refused), then the lock is taken and the match is made again on a fresh
    read, so a project that another writer removed meanwhile is not removed
    twice. ``not_found`` when nothing matches on either read."""
    config_file = _config_file(config_path)
    doomed = [p for p in _entries(_load(config_file)) if _matches(p, query)]
    if not doomed:
        raise ProjectError("not_found", f"no project matching {query!r}")
    stopped: list[str] = []
    if stop:
        stopped = _stop([_project(p) for p in doomed], str(config_file))
    with _locked():
        data = _load(config_file)
        entries = _entries(data)
        doomed = [p for p in entries if _matches(p, query)]
        if not doomed:
            raise ProjectError("not_found", f"no project matching {query!r}")
        data["projects"] = [p for p in entries if p not in doomed]
        _save(config_file, data)
    return RemoveResult([_project(p).name for p in doomed], stopped)


def _stop(projects: list[Project], config_path: str) -> list[str]:
    """Stop the projects' sessions (local ones through ``psmux.stop_sessions``,
    node ones through ``launch.stop_node_sessions``) and name the ones that
    verifiably stopped. A failure on the way -- the typed config refusing to
    load (``ConfigError``, a ``ValueError``), a node map or lock problem
    (``OSError``, ``LockHeld`` included), a kill that ran out of time
    (``subprocess.SubprocessError``) -- is ``unavailable`` with reason
    ``stop_failed``; nothing is removed from the config then."""
    from magent import launch  # in-body: launch is heavy
    from magent.config import load_config, node_is_pool

    local = [p.session for p in projects if not node_is_pool(p.node)]
    on_nodes = [p.session for p in projects if node_is_pool(p.node)]
    try:
        stopped, _still = psmux.stop_sessions(local)
        if on_nodes:
            node_stopped, _node_still = launch.stop_node_sessions(
                load_config(config_path), on_nodes
            )
            stopped += node_stopped
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise ProjectError(
            "unavailable", f"sessions not stopped: {exc}", {"reason": "stop_failed"}
        ) from exc
    return stopped


def set_enabled(config_path: str | None, query: str, enabled: bool) -> Project:
    """Enable or disable the FIRST project answering to ``query``."""
    config_file = _config_file(config_path)
    with _locked():
        data = _load(config_file)
        for entry in _entries(data):
            if _matches(entry, query):
                entry["enabled"] = enabled
                _save(config_file, data)
                return _project(entry)
    raise ProjectError("not_found", f"no project matching {query!r}")
