from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote

from magent.log import get_logger
from magent.sessions.claude import (
    build_claude_resume,
    claude_fresh_command,
    claude_fresh_form,
    claude_idle_probe,
    get_claude_session_ids,
)
from magent.sessions.codex import (
    build_codex_resume,
    codex_fresh_command,
    codex_fresh_form,
    get_codex_session_ids,
)
from magent.sessions.live import IdleProbe, LiveSession, SessionScan

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

# Re-exported for the reaper and its tests: the probe's value types live in the
# `live` leaf only so `claude.py` can import them without a cycle.
__all__ = ["IdleProbe", "LiveSession", "SessionScan"]


@dataclass(frozen=True)
class AgentTool:
    """Per-tool capabilities of a CLI agent (claude, codex, ...)."""

    # (project_dir, count, config_dir) -> that directory's resumable session
    # ids, newest first. `config_dir` names WHICH STORE answers for the project
    # (claude's CLAUDE_CONFIG_DIR); None means the tool's default store, which
    # is what every unrouted project passes. A tool whose store is not
    # account-scoped accepts and ignores it -- see `sessions/codex.py`.
    session_ids: Callable[[str, int, Path | None], list[str | None]] | None = None
    resume_command: Callable[[str, str | None], str] | None = None
    # (base_cmd, project_dir, config_dir) -> the command to run when that
    # directory has NO prior session for this tool to resume in that store, or
    # None to run base_cmd unchanged. See `build_start_command`. Equals
    # `fresh_form` plus a probe of the local session store.
    fresh_command: Callable[[str, str, Path | None], str | None] | None = None
    # base_cmd -> the command with its implicit resume dropped (claude's
    # `--continue`, codex's `resume --last`), WITHOUT asking any store whether
    # there is something to resume; None when base_cmd carries no implicit
    # resume to drop. For a session whose store is elsewhere (a pool node: the
    # nodes feature ships both forms and the node picks). Unset for a tool that
    # has no implicit-resume form at all.
    fresh_form: Callable[[str], str | None] | None = None
    happy: bool = False  # can be wrapped with `happy` for mobile access
    # Process image names (no ".exe", any case) a RUNNING instance of this
    # agent carries. `psmux.idle_sessions` never calls a pane idle while one of
    # these -- or an AGENT_RUNTIME_IMAGES host -- runs anywhere under it.
    images: tuple[str, ...] = ()
    # The reaper's only agent-specific hook: how to read this tool's live
    # sessions and their transcript activity. Only claude sets one in v1.
    idle_probe: IdleProbe | None = None

    @property
    def multi_window(self) -> bool:
        return self.session_ids is not None


AGENT_TOOLS: dict[str, AgentTool] = {
    "claude": AgentTool(
        session_ids=get_claude_session_ids,
        resume_command=build_claude_resume,
        fresh_command=claude_fresh_command,
        fresh_form=claude_fresh_form,
        happy=True,
        images=("claude",),
        idle_probe=claude_idle_probe,
    ),
    "codex": AgentTool(
        session_ids=get_codex_session_ids,
        resume_command=build_codex_resume,
        fresh_command=codex_fresh_command,
        fresh_form=codex_fresh_form,
        happy=True,
        images=("codex",),
    ),
}

# Runtimes an agent can equally run UNDER, as a script rather than its own
# binary: an npm-installed Claude Code is node.exe running cli.js, and codex's
# npm shim is node.exe over the native binary. A process snapshot carries image
# names only, never command lines, so any process on one of these counts as
# possibly the agent -- the reading that errs toward "not idle".
AGENT_RUNTIME_IMAGES: frozenset[str] = frozenset({"node"})


def agent_image_names(
    tools: Mapping[str, AgentTool] | None = None,
) -> frozenset[str]:
    """Every image name (lower-case, no ".exe") that may be a running agent:
    each entry's ``images`` plus AGENT_RUNTIME_IMAGES. ``tools`` defaults to the
    live ``AGENT_TOOLS`` read at call time (so a registry monkeypatch is seen,
    and the reaper can build the set from an injected stand-in registry)."""
    registry = AGENT_TOOLS if tools is None else tools
    return AGENT_RUNTIME_IMAGES | {
        image.lower() for tool in registry.values() for image in tool.images
    }


# Claude Code's Windows auto-updater renames a RUNNING claude.exe aside to
# claude.exe.old.<epoch-ms> to drop the new binary in, and the process's image
# name (QueryFullProcessImageNameW) reads the renamed file for the rest of its
# life -- the Toolhelp snapshot keeps the name it started under. Measured: 4 of
# 13 live agents on one box. ASCII digits only: a timestamp, nothing looser.
_RENAMED_ASIDE = re.compile(r"\.exe(?:\.old(?:\.[0-9]+)?)?\Z")


def agent_image_stem(raw: str) -> str:
    """``C:\\x\\CLAUDE.EXE.OLD.1790669558315`` -> ``claude``: the leaf name,
    lower-cased, with a trailing ``.exe``, ``.exe.old`` or ``.exe.old.<digits>``
    dropped -- the spelling an agent process's IDENTITY image is compared to
    ``agent_image_names`` (and to its own earlier reading) in. For agent
    identity only: ``psmux.image_stem`` still drops ``.exe`` alone, because a
    shell reading is matched exactly and a looser rule there is its own risk."""
    leaf = raw.strip().replace("\\", "/").rsplit("/", 1)[-1].lower()
    return _RENAMED_ASIDE.sub("", leaf, count=1)


def build_resume_command(tool: str, base_cmd: str, session_id: str | None) -> str:
    caps = AGENT_TOOLS.get(tool)
    if caps and caps.resume_command:
        return caps.resume_command(base_cmd, session_id)
    return base_cmd


def build_start_command(
    tool: str,
    base_cmd: str,
    project_dir: str | None,
    *,
    config_dir: Path | None = None,
) -> str:
    """``base_cmd``, with its implicit "resume the latest conversation" flag
    dropped when ``project_dir`` has nothing to resume.

    The edge this exists for: ``claude --continue`` (the registry default)
    fails outright in a directory that never hosted a conversation, leaving the
    pane at a dead shell that every retry and every revive re-kills the same
    way. The verdict is taken HERE, at command-build time, and never at
    runtime: agent commands are delivered into psmux panes with send-keys, so
    magent never observes the command's exit code -- and a shell-level
    ``claude --continue || claude`` would relaunch a FRESH agent on any later
    nonzero exit, silently discarding a live conversation and hiding auth or
    CLI failures behind a fake recovery.

    Only a positively-determined "this directory has no stored session"
    rewrites anything. An unknown tool, a tool with no probe, an unresolvable
    directory, a command carrying no implicit-resume flag, an explicitly named
    session, and a probe that ERRORS all keep ``base_cmd`` exactly as
    configured -- so a genuine resume failure stays visible in the pane, where
    the user can read it.

    ``project_dir`` must be a directory on the machine that will RUN the
    command: pass None for a remote project rather than deciding it from this
    machine's session store.

    ``config_dir`` names WHICH of that tool's stores the probe must answer
    from -- the config directory the pane will run under. None is the tool's
    default store and reads exactly the files it always read; a routed pane
    passes its account's profile, because that is the store its transcripts
    will land in. Keyword-only: this is a property of the environment the
    command runs in, never a fourth thing to confuse with the command itself.
    """
    caps = AGENT_TOOLS.get(tool)
    if not base_cmd or not project_dir or not caps or not caps.fresh_command:
        return base_cmd
    log = get_logger("launch")
    try:
        fresh = caps.fresh_command(base_cmd, project_dir, config_dir)
    except OSError:
        # A probe that cannot read the session store proves nothing about
        # whether a session exists. Keep the configured command and say so.
        log.warning(
            "could not probe %s sessions in %s; running %r as configured",
            tool,
            project_dir,
            base_cmd,
            exc_info=True,
        )
        return base_cmd
    if fresh is None or fresh == base_cmd:
        return base_cmd
    log.info(
        "no prior %s session in %s; starting fresh: %r -> %r",
        tool,
        project_dir,
        base_cmd,
        fresh,
    )
    return fresh


def fresh_start_command(tool: str, base_cmd: str) -> str | None:
    """``tool``'s fresh form of ``base_cmd`` (see ``AgentTool.fresh_form``), or
    None. Unlike ``build_start_command`` this never probes a store.

    None means base_cmd carries no implicit resume to drop; the caller ships
    base_cmd alone."""
    caps = AGENT_TOOLS.get(tool)
    if caps is None or caps.fresh_form is None:
        return None
    return caps.fresh_form(base_cmd)


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
    folder: str, ssh_host: str | None, code_bin: str, *, keep_user: bool = False
) -> list[str]:
    """argv that opens ``folder`` in VS Code, locally or over Remote-SSH.

    With an ssh target the folder lives on that host, so the window is opened
    through Remote-SSH -- the same ``--remote ssh-remote+<host>`` shape the
    launch path's ``launch_vscode`` builds. Only the HOSTNAME goes into the
    authority: a ``user@`` prefix is deliberately stripped, because VS Code
    resolves the login user from the machine's own ssh config (that is also
    what makes a plain ``Host`` alias work), and a target that is only a
    ``user@`` with no host degrades to a local open rather than a broken URI.

    ``keep_user`` keeps a ``user@`` in the authority -- a pool node's user is
    resolved by magent and may not exist in the ssh config (the nodes
    feature). A target with no hostname still opens locally either way, and
    one with an empty user (``@host``) keeps only the hostname.
    """
    args = [code_bin]
    if ssh_host:
        user, at, host_part = ssh_host.partition("@")
        hostname = host_part if at else ssh_host
        if hostname:
            # An empty user (`@host`) would build `ssh-remote+@host`.
            authority = ssh_host if keep_user and user else hostname
            args.extend(["--remote", f"ssh-remote+{authority}"])
    args.append(folder)
    return args


# Longest status-line message the flash endpoint accepts. A psmux status bar is
# one line wide, so anything past this is noise that would only push the useful
# prefix off-screen. Shared by the client (build_flash_url) and the server
# (upload_server's /api/flash) so both clamp to the same budget.
FLASH_MSG_MAX = 120


# What a flash asks the status bar to LOOK like. Two values only: this is a
# one-line bar saying whether the thing you pressed worked, not a palette.
# It travels with EVERY message rather than only with failures, because psmux's
# ``message-style`` is a global option on that socket -- set it once for a red
# failure and every later message inherits red until something sets it back. A
# green "cannot reach magent serve" is worse than no colour at all.
FLASH_TINT_OK = "ok"
FLASH_TINT_ERR = "err"


def build_flash_url(
    server_url: str,
    project: str,
    message: str,
    duration_ms: int | None = None,
    tint: str | None = None,
) -> str:
    """URL that flashes ``message`` in the ``magent:<project>`` status line.

    The Alt+V/F2 handler's only channel for on-screen feedback: hotkey.py runs
    in a hidden background process with no terminal, so a failure it cannot
    report through the upload server is invisible to the user. Pure string math,
    so the shape stays testable on every OS (hotkey.py is win32-import-only).

    ``duration_ms`` is for a message that is a PHASE rather than a result: an
    "uploading..." that expires while the upload is still running leaves a blank
    bar, which reads exactly like the silence this whole channel exists to end.
    Omitted, the server picks its own default. ``tint`` is FLASH_TINT_OK /
    FLASH_TINT_ERR -- see their note on why it rides along on every message.
    """
    url = (
        f"{server_url.rstrip('/')}/api/flash"
        f"?project={quote(project)}&msg={quote(message[:FLASH_MSG_MAX])}"
    )
    if duration_ms:
        url = f"{url}&ms={int(duration_ms)}"
    if tint:
        url = f"{url}&tint={quote(tint)}"
    return url


# Largest upload `magent serve` accepts, per REQUEST (an Alt+V press carrying a
# whole Explorer selection is one request). Lives here, not in upload_server,
# because the Alt+V listener pre-checks the same number BEFORE reading a file
# off disk -- and altv must stay a leaf that never imports the server.
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

_MIB = 1024 * 1024


def upload_limit_text(limit: int) -> str:
    """The limit as a person reads it: ``"100 MB"``.

    A limit that is not a whole number of megabytes (a test-lowered cap) names
    its exact byte count rather than rounding to a false ``"0 MB"``.
    """
    if limit >= _MIB and limit % _MIB == 0:
        return f"{limit // _MIB} MB"
    return f"{limit} bytes"


# A path made only of these characters needs no quoting in any shell or prompt
# it lands in. Backslash is here on purpose: it is the Windows separator, and a
# bare `C:\x\y.py` is what a user would type.
_BARE_PATH = re.compile(r"[\w@%+=:,./\\~-]+")
# What a double-quoted string still interprets in a POSIX shell.
_DQ_ACTIVE = frozenset('"$`')


def _quote_path(path: str) -> str:
    if _BARE_PATH.fullmatch(path):
        return path
    if _DQ_ACTIVE.isdisjoint(path):
        # Double quotes first: that is what Windows Terminal writes when a file
        # is dropped on it, and a Windows path cannot contain a `"` anyway.
        return f'"{path}"'
    return "'" + path.replace("'", "'\\''") + "'"


# Unicode categories no paste may carry: control characters (C0 with ESC and
# TAB, DEL, and C1 with NEL U+0085) and the line and paragraph separators
# (U+2028, U+2029).
_UNPASTEABLE_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})


def unpasteable_path(path: str) -> bool:
    """Whether ``path`` holds a character no paste may carry.

    Any of them can end or rewrite the input line it lands in -- and in an
    agent pane a line break SUBMITS -- so no amount of quoting makes one safe.
    CF_HDROP cannot carry a C0 control on Windows, but NEL and the Unicode
    separators are legal NTFS name characters.
    """
    return any(unicodedata.category(ch) in _UNPASTEABLE_CATEGORIES for ch in path)


def paths_line(paths: list[str]) -> str:
    """Several file paths as ONE pasteable line: space-separated, each quoted
    only when it has to be.

    One line because a paste is one attempt (the double-paste law): several
    files are one paste, not one per file racing into the input line. A single
    plain path comes back byte-for-byte itself, which is exactly what the
    one-image inject has always pasted.

    Raises ``ValueError`` for a path ``unpasteable_path`` refuses: a caller
    that could meet one checks first and says so; reaching here with one is a
    bug, and the line is never built.
    """
    if any(unpasteable_path(p) for p in paths):
        raise ValueError("a path holds a control or line-break character")
    return " ".join(_quote_path(p) for p in paths)
