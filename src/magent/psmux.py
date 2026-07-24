"""Psmux (tmux multiplexer) lifecycle primitives.

Every subprocess interaction with the psmux binary lives here: session
creation, liveness checks, send-keys, status-line flashes, kills. Callers
(launch, upload_server, session_picker, cli/status) import tested primitives
instead of inlining ad-hoc ``subprocess.run`` calls.

The module is a pure leaf — no cli/ imports, no heavy subsystem imports at
top level. It sits alongside ``tiling.py``, ``procs.py``, and ``tailnet.py``
in the dependency graph.
"""

from __future__ import annotations

import functools
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from magent.config import MagentConfig

from magent.log import get_logger


@functools.lru_cache(maxsize=1)
def find_psmux() -> str | None:
    """Locate the psmux binary. LRU-cached for the process lifetime."""
    found = shutil.which("psmux")
    if found:
        return found
    if sys.platform == "win32":
        from magent.env import localappdata_dir

        local = localappdata_dir() / "psmux" / "psmux.exe"
        if local.is_file():
            return str(local)
    return None


@dataclass
class PsmuxWindowOpts:
    """One window to create inside a psmux session."""

    window_name: str
    cwd: str
    command: str


def session_name(title: str) -> str:
    """Sanitize a window title into a valid psmux/tmux session name."""
    return title.replace(".", "-").replace(":", "-").replace(" ", "-")


def has_session(name: str, psmux: str | None = None) -> bool:
    """True if a psmux session named ``name`` is alive."""
    binary = psmux or find_psmux()
    if not binary:
        return False
    return (
        subprocess.run(
            [binary, "-L", name, "has-session"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def kill_server(name: str, psmux: str | None = None) -> bool:
    """Kill the psmux server backing a single session. Returns True on success."""
    binary = psmux or find_psmux()
    if not binary:
        return False
    return (
        subprocess.run(
            [binary, "-L", name, "kill-server"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def kill_servers(names: list[str]) -> list[str]:
    """Kill multiple psmux servers. Returns the names that were attempted."""
    binary = find_psmux()
    if not binary:
        return []
    for name in names:
        kill_server(name, psmux=binary)
    return list(names)


def send_keys(
    name: str,
    *keys: str,
    target: str | None = None,
    psmux: str | None = None,
) -> bool:
    """Send keystrokes to a psmux session. Returns True on success."""
    binary = psmux or find_psmux()
    if not binary:
        return False
    cmd: list[str] = [binary, "-L", name, "send-keys"]
    if target:
        cmd += ["-t", target]
    cmd.append("--")
    cmd.extend(keys)
    return subprocess.run(cmd, capture_output=True, check=False).returncode == 0


def pane_cwd(name: str, psmux: str | None = None) -> str:
    """Return the current working directory of the active pane, or ``""``.

    Guarded like the inline closure it replaced (P1-06): a 3s timeout, utf-8
    decode with ``errors="replace"``, and any OSError/SubprocessError swallowed
    to ``""`` -- a hung, unlaunchable, or non-utf-8 psmux must never propagate
    to a caller fanning this across every live session.
    """
    binary = psmux or find_psmux()
    if not binary:
        return ""
    try:
        result = subprocess.run(
            [binary, "-L", name, "display-message", "-p", "#{pane_current_path}"],
            capture_output=True,
            timeout=3,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    else:
        return (result.stdout or "").strip() if result.returncode == 0 else ""


def flash_message(
    name: str,
    message: str,
    duration_ms: int,
    *,
    style: str | None = None,
    psmux: str | None = None,
) -> None:
    """Flash a transient message in the session's psmux status line.

    Non-disruptive — ``display-message`` repaints the status bar, not the
    agent pane. Never raises and never blocks for long.
    """
    binary = psmux or find_psmux()
    if not binary:
        return
    cmd: list[str] = [binary, "-L", name]
    if style:
        cmd += ["set", "-g", "message-style", style, ";"]
    cmd += ["display-message", "-d", str(duration_ms), message]
    try:
        subprocess.run(cmd, capture_output=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        get_logger("upload").warning(
            "status-line flash failed for project=%s: %s", name, exc
        )


def detach_client(name: str, psmux: str | None = None) -> bool:
    """Detach the client attached to ``name``. Returns True on success."""
    binary = psmux or find_psmux()
    if not binary:
        return False
    try:
        result = subprocess.run(
            [binary, "-L", name, "detach-client"],
            capture_output=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    else:
        return result.returncode == 0


def socket_id(session_dict: dict[str, object]) -> str:
    """The psmux socket id for a session dict: ``session`` key when present,
    else ``name``."""
    return str(session_dict.get("session") or session_dict.get("name") or "")


def _field_str(d: dict[str, object], key: str) -> str:
    """A descriptor dict's string field (narrows dict[str, object] to str)."""
    value = d.get(key, "")
    return value if isinstance(value, str) else ""


def eligible_projects(
    config: MagentConfig, group: str | None = None
) -> list[dict[str, object]]:
    """Projects that map to a persistent psmux session.

    A project is eligible when it is enabled, runs a CLI agent (not an IDE),
    and is local (no ``host``). When ``group`` is given, only projects tagged
    with that group (case-insensitive) are returned.
    """
    from magent.launch import _expand_base_dir, _resolve_path
    from magent.sessions import is_ide_tool
    from magent.titles import get_leaf_name

    base_dir = config.base_dir
    if base_dir:
        base_dir = _expand_base_dir(base_dir)

    out: list[dict[str, object]] = []
    for proj in config.projects:
        if not proj.enabled:
            continue
        if group and (not proj.group or proj.group.lower() != group.lower()):
            continue
        tool = proj.tool or config.settings.default_tool
        if is_ide_tool(tool):
            continue
        if proj.host:
            continue
        leaf = proj.title or get_leaf_name(proj.path)
        out.append(
            {
                "name": leaf,
                "session": session_name(leaf),
                "path": proj.path,
                "tool": tool,
                "group": proj.group,
                "resolved": _resolve_path(proj.path, base_dir),
                "cmd": config.settings.tools.get(tool, ""),
                "color": proj.color,
            }
        )
    return out


def psmux_status(
    config: MagentConfig, group: str | None = None
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    """Return ``(up, down, all_projects)`` for eligible projects."""
    binary = find_psmux()
    projects = eligible_projects(config, group)
    up: list[dict[str, object]] = []
    down: list[dict[str, object]] = []

    checkable: list[tuple[dict[str, object], subprocess.Popen[bytes]]] = []
    for p in projects:
        info: dict[str, object] = {
            "name": p["name"],
            "session": p["session"],
            "path": p["path"],
            "tool": p["tool"],
            "group": p.get("group"),
        }
        if binary and p["resolved"] and p["cmd"]:
            proc = subprocess.Popen(
                [binary, "-L", _field_str(p, "session"), "has-session"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            checkable.append((info, proc))
        else:
            down.append(info)

    for info, proc in checkable:
        (up if proc.wait() == 0 else down).append(info)

    return up, down, projects


def bring_up(
    config: MagentConfig,
    only: list[str] | None = None,
    group: str | None = None,
) -> list[str]:
    """Create detached psmux sessions for eligible projects.

    ``only`` restricts creation to the given session names; ``group``
    restricts to a single project group. Returns the names (re)created.
    """
    from magent.platform import get_platform

    plat = get_platform()
    windows: list[PsmuxWindowOpts] = []
    for p in eligible_projects(config, group):
        if only is not None and _field_str(p, "session") not in only:
            continue
        if not p["resolved"] or not p["cmd"]:
            continue
        windows.append(
            PsmuxWindowOpts(
                window_name=_field_str(p, "session"),
                cwd=_field_str(p, "resolved"),
                command=_field_str(p, "cmd"),
            )
        )
    if windows:
        plat.launch_psmux_session(windows)
    return [w.window_name for w in windows]


def config_sessions(config_path: str | None) -> list[dict[str, object]]:
    """Eligible psmux sessions from config — no psmux binary calls, fast path
    for the upload server's session list."""
    import json
    from pathlib import Path

    from magent.paths import find_config
    from magent.sessions import is_ide_tool

    config_file = find_config(config_path)
    if not config_file.exists():
        return []

    data = json.loads(config_file.read_text(encoding="utf-8"))
    default_tool = data.get("settings", {}).get("defaultTool", "claude")
    out: list[dict[str, object]] = []
    for p in data.get("projects", []):
        if not p.get("enabled", True):
            continue
        tool = p.get("tool", default_tool)
        if isinstance(tool, str) and is_ide_tool(tool):
            continue
        proj_name = p.get("title") or Path(p["path"]).name
        out.append(
            {
                "name": proj_name,
                "session": session_name(proj_name),
                "path": p["path"],
            }
        )
    return out


def discover_sessions(config_path: str | None) -> list[dict[str, object]]:
    """Active psmux sessions from config — concurrent liveness check."""
    from concurrent.futures import ThreadPoolExecutor

    candidates = config_sessions(config_path)
    binary = find_psmux()
    if not candidates or not binary:
        return []
    with ThreadPoolExecutor(max_workers=16) as pool:
        flags = list(
            pool.map(lambda c: has_session(socket_id(c), psmux=binary), candidates)
        )
    return [c for c, ok in zip(candidates, flags, strict=True) if ok]
