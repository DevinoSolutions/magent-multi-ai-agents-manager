from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote

from magent.sessions.claude import build_claude_resume, get_claude_session_ids
from magent.sessions.codex import build_codex_resume, get_codex_session_ids

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class AgentTool:
    """Per-tool capabilities of a CLI agent (claude, codex, ...)."""

    session_ids: Callable[[str, int], list[str | None]] | None = None
    resume_command: Callable[[str, str | None], str] | None = None
    happy: bool = False  # can be wrapped with `happy` for mobile access

    @property
    def multi_window(self) -> bool:
        return self.session_ids is not None


AGENT_TOOLS: dict[str, AgentTool] = {
    "claude": AgentTool(
        session_ids=get_claude_session_ids,
        resume_command=build_claude_resume,
        happy=True,
    ),
    "codex": AgentTool(
        session_ids=get_codex_session_ids, resume_command=build_codex_resume, happy=True
    ),
}


def build_resume_command(tool: str, base_cmd: str, session_id: str | None) -> str:
    caps = AGENT_TOOLS.get(tool)
    if caps and caps.resume_command:
        return caps.resume_command(base_cmd, session_id)
    return base_cmd


# --- IDE tools (REC-F4) -------------------------------------------------------
# The IDE mirror of AGENT_TOOLS: tools launched as an IDE window instead of a
# CLI agent in a terminal. The dict is the single source of truth — adding an
# IDE is one entry here; IDE_TOOLS and both helpers derive from it.

IDE_COMMANDS: dict[str, str] = {
    "code": "code",
    "vscode": "code",  # config alias for VS Code
    "cursor": "cursor",
}

IDE_TOOLS: frozenset[str] = frozenset(IDE_COMMANDS)


def is_ide_tool(tool: str) -> bool:
    """True when `tool` names an IDE (opened as a window, not a CLI agent)."""
    return tool in IDE_COMMANDS


def ide_command(tool: str) -> str:
    """CLI executable that opens `tool`'s IDE window. Unknown tools fall back
    to "code", preserving the historical launch-path behavior."""
    return IDE_COMMANDS.get(tool, "code")


# --- "open the focused project in VS Code" (the F2 window hotkey) -------------
# Pure decision logic for the Alt+V listener's F2 handler. It lives here rather
# than in hotkey.py because hotkey.py raises ImportError off win32 at import
# time, and this math must be unit-testable on every OS.


def folder_for_session(payload: object, project: str) -> str | None:
    """The project folder to open, out of an ``/api/sessions`` response body.

    Matches the psmux socket id (``session``) first, then the display ``name``
    -- window titles carry the socket id, but a caller holding a display name
    should still resolve. Prefers ``resolved`` (absolute, baseDir-aware) over
    the raw config ``path``, which may be relative to the host's baseDir and
    therefore meaningless to a client. Returns None for a wrong-shaped body,
    an absent project, or an entry with no usable folder.
    """
    if not isinstance(payload, dict):
        return None
    entries = payload.get("sessions")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if project not in (entry.get("session"), entry.get("name")):
            continue
        for key in ("resolved", "path"):
            value = entry.get(key)
            if isinstance(value, str) and value:
                return value
        return None
    return None


def build_code_open_command(
    folder: str, ssh_host: str | None, code_bin: str
) -> list[str]:
    """argv that opens ``folder`` in VS Code, locally or over Remote-SSH.

    With an ssh target the folder lives on that host, so the window is opened
    through Remote-SSH -- the same ``--remote ssh-remote+<host>`` shape the
    launch path's ``launch_vscode`` builds. Only the HOSTNAME goes into the
    authority: a ``user@`` prefix is deliberately stripped, because VS Code
    resolves the login user from the machine's own ssh config (that is also
    what makes a plain ``Host`` alias work), and a target that is only a
    ``user@`` with no host degrades to a local open rather than a broken URI.
    """
    args = [code_bin]
    if ssh_host:
        host = ssh_host.split("@", 1)[1] if "@" in ssh_host else ssh_host
        if host:
            args.extend(["--remote", f"ssh-remote+{host}"])
    args.append(folder)
    return args


# Longest status-line message the flash endpoint accepts. A psmux status bar is
# one line wide, so anything past this is noise that would only push the useful
# prefix off-screen. Shared by the client (build_flash_url) and the server
# (upload_server's /api/flash) so both clamp to the same budget.
FLASH_MSG_MAX = 120


def build_flash_url(server_url: str, project: str, message: str) -> str:
    """URL that flashes ``message`` in the ``magent:<project>`` status line.

    The F2 handler's only channel for on-screen feedback: hotkey.py runs in a
    hidden background process with no terminal, so a failure it cannot report
    through the upload server is invisible to the user. Pure string math, so
    the shape stays testable on every OS (hotkey.py is win32-import-only).
    """
    return (
        f"{server_url.rstrip('/')}/api/flash"
        f"?project={quote(project)}&msg={quote(message[:FLASH_MSG_MAX])}"
    )
