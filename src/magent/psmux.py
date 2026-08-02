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

    The explicit ``-t <name>`` is REQUIRED: without it ``display-message``
    answers for the *calling client's own* pane, so a magent run from inside a
    psmux session reports its own cwd for every session it probes --
    ``capture_pane`` passes ``-t`` for the same reason.

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
            [
                binary,
                "-L",
                name,
                "display-message",
                "-t",
                name,
                "-p",
                "#{pane_current_path}",
            ],
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


def capture_pane(name: str, psmux: str | None = None) -> str:
    """Return the active pane's visible text, or ``""``. Same guards as
    ``pane_cwd``: bounded, decode-tolerant, and never raises."""
    binary = psmux or find_psmux()
    if not binary:
        return ""
    try:
        result = subprocess.run(
            [binary, "-L", name, "capture-pane", "-p", "-t", name],
            capture_output=True,
            timeout=3,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    else:
        return (result.stdout or "") if result.returncode == 0 else ""


# Foreground commands that mean "this pane is sitting at a prompt with no
# agent running". ``cmd`` is deliberately NOT in this set: on Windows the agent
# launchers are .cmd shims, so cmd.exe is the foreground interpreter for the
# seconds an agent takes to boot -- calling that idle would type a second
# command into a live agent. A genuinely dead pane rests at pwsh (Windows,
# where sessions are created with a pwsh default shell) or a POSIX shell.
_IDLE_SHELLS: frozenset[str] = frozenset(
    {"pwsh", "powershell", "bash", "zsh", "fish", "sh", "dash", "nu", "ksh", "tcsh"}
)


def pane_current_command(name: str, psmux: str | None = None) -> str:
    """Return the active pane's foreground command (``pwsh``, ``claude``, ...).

    The explicit ``-t <name>`` is REQUIRED: without it ``display-message``
    answers for the *calling client's own* pane, and magent commands are often
    run from inside a psmux session -- ``capture_pane`` passes ``-t`` for the
    same reason. Same guards as ``pane_cwd``: bounded, decode-tolerant, and
    any OSError/SubprocessError swallowed to ``""``.
    """
    binary = psmux or find_psmux()
    if not binary:
        return ""
    try:
        result = subprocess.run(
            [
                binary,
                "-L",
                name,
                "display-message",
                "-t",
                name,
                "-p",
                "#{pane_current_command}",
            ],
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


def pane_current_commands(names: list[str], psmux: str | None = None) -> dict[str, str]:
    """``pane_current_command`` for many sessions in ONE process fan-out.

    Every probe is spawned before any is read -- the shape the picker's
    liveness sweep already uses -- so a caller building a table over 40 live
    sessions pays roughly one psmux round-trip instead of 40 sequential ones.
    Guarded exactly like the single-session form: bounded, decode-tolerant, and
    a failed, hung, or unlaunchable probe degrades to ``""`` for that session
    rather than propagating.
    """
    binary = psmux or find_psmux()
    if not binary or not names:
        return dict.fromkeys(names, "")
    procs: dict[str, subprocess.Popen[str] | None] = {}
    for name in names:
        try:
            procs[name] = subprocess.Popen(
                [
                    binary,
                    "-L",
                    name,
                    "display-message",
                    "-t",
                    name,
                    "-p",
                    "#{pane_current_command}",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            procs[name] = None

    out: dict[str, str] = {}
    for name, proc in procs.items():
        if proc is None:
            out[name] = ""
            continue
        try:
            stdout, _ = proc.communicate(timeout=5)
        except subprocess.SubprocessError:
            proc.kill()
            out[name] = ""
        else:
            out[name] = (stdout or "").strip() if proc.returncode == 0 else ""
    return out


def is_idle_command(raw: str) -> bool:
    """True when a ``#{pane_current_command}`` reading is a bare shell.

    Split out of ``agent_idle`` so a caller that already holds a pane's
    foreground command (``status``'s session table) classifies it without
    paying a second psmux round-trip. An empty or unreadable reading is False
    on purpose -- see ``agent_idle``.
    """
    stripped = raw.strip()
    if not stripped:
        return False
    leaf = stripped.replace("\\", "/").rsplit("/", 1)[-1]
    if leaf.lower().endswith(".exe"):
        leaf = leaf[: -len(".exe")]
    return leaf.lower() in _IDLE_SHELLS


def agent_idle(name: str, psmux: str | None = None) -> bool:
    """True when the session's pane rests at a bare shell -- its agent is gone.

    An empty or unreadable reading is False on purpose: never inject keystrokes
    into a pane whose state we could not establish.
    """
    return is_idle_command(pane_current_command(name, psmux=psmux))


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


# The hint line every magent session advertises in its psmux status bar.
# Defined once here so the launch path and the `up` path can never drift.
# Each key name is its own badge (bold + accent, same `#[...]` idiom as
# _STATUS_BRAND below) so "F1"/"F2" read as keys rather than as stray words, and
# each label says what the key actually does: the old " F1 picker  F2 code " was
# four bare tokens with nothing marking which half was the key. F1 gets a TRIGRAM
# FOR HEAVEN -- the universal "menu/list" hamburger -- because the picker *is* a
# list, and the label names what it lists ("Proj. Picker"; a bare "picker" never
# said what it picked). Both labels are capitalized so the two halves match
# register. The glyph is written as a \N{...} escape rather than pasted in so
# which one is meant survives an editor that mangles it, and it's an ordinary
# Unicode codepoint (no Nerd-Font private-use range), so it renders in Windows
# Terminal without a patched font. No space separates it from its label on
# purpose: U+2630 is East-Asian *ambiguous* width, so a terminal that renders it
# wide already pads it, and an explicit space read as a gap. F2 gets plain ASCII
# `</>`: Unicode has no VS Code / editor sign outside those same private-use
# ranges, and `</>` is the universal "code" mark -- zero font support required,
# no width ambiguity, hence the ordinary space before its label.
_STATUS_HINTS = (
    "#[bold,fg=cyan] F1 #[default]\N{TRIGRAM FOR HEAVEN}Proj. Picker   "
    "#[bold,fg=cyan] F2 #[default]</> VS Code "
)

# ...and the width budget has to travel with it, exactly like the brand's below.
# tmux truncates status-right at `status-right-length` (default 40, but a
# personal conf may set it far tighter), so the now-wider hint can render
# mid-label. Style directives don't count toward the limit; what's left --
# " F1 ", the menu glyph, "Proj. Picker", the gap, " F2 ", "</>" and " VS Code "
# -- is 4 + 2 + 12 + 3 + 4 + 3 + 9 = 37 columns worst case: everything is a
# single cell except the menu glyph, counted as 2 because its East-Asian
# *ambiguous* width lets a terminal render it either way. 44 carries the wide
# case plus headroom for a label tweak.
_STATUS_HINTS_LEN = "44"

# The product's own status-left brand, same plainness as the hints: one word,
# one accent. magent *owns* this per session rather than inheriting whatever a
# personal ~/.tmux.conf set, so every magent window reads the same.
_STATUS_BRAND = "#[bold,fg=green] magent #[default]"

# ...and the width budget has to travel with it. tmux truncates status-left at
# `status-left-length` (default 10, but a personal conf may set it far tighter),
# so setting the brand without the length can render it mid-word. Style
# directives don't count toward the limit; " magent " is 8 columns.
_STATUS_BRAND_LEN = "10"


def decoration_argv(name: str, psmux: str) -> list[list[str]]:
    """The psmux commands that brand ``name`` and advertise its window hotkeys.

    Five of them: magent *owns* F1 -> detach-client per session (the hint has to
    be truthful on a machine with no personal ``bind -n F1`` in ~/.tmux.conf,
    and owning the binding keeps the existing "back to the picker" semantics
    rather than changing them), the status-right carries the hint text plus the
    width budget it needs, and the status-left carries the product brand plus
    the width budget *it* needs. Each half sets its text and its length together
    or neither: a personal conf with a tighter ``status-*-length`` would truncate
    the other half mid-label. All are ``-L <name>``-scoped, so they land on that
    session's own server and override whatever its tmux.conf set at start-up.

    Split out from ``decorate_session`` so the launch path can fan the same
    argvs out as raw Popens while callers with one session run them inline.
    """
    return [
        [psmux, "-L", name, "bind", "-n", "F1", "detach-client"],
        [psmux, "-L", name, "set", "-g", "status-right", _STATUS_HINTS],
        [psmux, "-L", name, "set", "-g", "status-right-length", _STATUS_HINTS_LEN],
        [psmux, "-L", name, "set", "-g", "status-left", _STATUS_BRAND],
        [psmux, "-L", name, "set", "-g", "status-left-length", _STATUS_BRAND_LEN],
    ]


def decorate_session(name: str, psmux: str | None = None) -> None:
    """Brand one session's status line and advertise its F1/F2 hints.

    Best-effort and guarded exactly like ``flash_message``: a status bar is
    cosmetic, so a missing binary, a hung psmux, or a non-zero exit is logged
    and swallowed -- never propagated into a bring-up.
    """
    binary = psmux or find_psmux()
    if not binary:
        return
    for cmd in decoration_argv(name, binary):
        try:
            subprocess.run(cmd, capture_output=True, timeout=3, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            get_logger("launch").warning(
                "status-line decoration failed for session=%s: %s", name, exc
            )


def decorate_sessions(names: list[str]) -> list[str]:
    """Decorate many sessions concurrently. Returns the names attempted.

    Each session is its own psmux server, so the round-trips per name
    would otherwise serialize across a large config -- same fan-out shape as
    ``revive_sessions``.
    """
    from concurrent.futures import ThreadPoolExecutor

    binary = find_psmux()
    if not binary or not names:
        return []
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda n: decorate_session(n, psmux=binary), names))
    return list(names)


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
    seen: set[str] = set()
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
        sid = session_name(leaf)
        # One session id, one entry: duplicate config entries for a project
        # produced two identical status rows, hence two identically-titled
        # attach windows -- and the second could never be tiled, since both
        # resolve to the same window handle. First occurrence wins.
        if sid in seen:
            continue
        seen.add(sid)
        out.append(
            {
                "name": leaf,
                "session": sid,
                "path": proj.path,
                "tool": tool,
                "group": proj.group,
                "resolved": _resolve_path(proj.path, base_dir),
                "cmd": config.settings.tools.get(tool, ""),
                "color": proj.color,
            }
        )
    return out


def _down_reason(binary: str | None, project: dict[str, object]) -> str:
    """Name the one thing that keeps ``project`` from ever being probed."""
    if not binary:
        return "psmux not installed"
    if not project["resolved"]:
        return "folder not found"
    return "no agent command"


def psmux_status(
    config: MagentConfig, group: str | None = None
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    """Return ``(up, down, all_projects)`` for eligible projects.

    A project that never gets probed at all (no psmux binary, a path that does
    not resolve on this machine, or no agent command) carries a ``reason`` on
    its down entry. Without it such a project reports down forever with zero
    explanation -- ``bring_up`` skips exactly the same projects silently, so
    "it says down and `up` does nothing" was the only symptom. Sessions that
    DID get probed and simply failed ``has-session`` stay reason-less: that is
    ordinary down, and nothing to explain.

    Precedence is binary-first: a missing psmux is a machine-wide blocker that
    makes every other reason moot, so naming it once beats telling the user
    about a folder they would still not be able to launch.
    """
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
            info["reason"] = _down_reason(binary, p)
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


def revive_sessions(
    config: MagentConfig,
    only: list[str] | None = None,
    group: str | None = None,
) -> list[str]:
    """Re-launch the agent in live sessions whose pane fell back to a shell.

    A session whose agent was Ctrl-C'ed (or whose original send-keys died)
    still answers ``has-session``, so ``up``/``attach`` reuse it and hand the
    user a window parked at a bare prompt forever. Candidates are probed
    concurrently -- the check is two psmux round-trips per session and a large
    config would otherwise serialize them. Returns the session ids revived.
    """
    from concurrent.futures import ThreadPoolExecutor

    binary = find_psmux()
    if not binary:
        return []

    candidates: list[dict[str, object]] = []
    for p in eligible_projects(config, group):
        if only is not None and _field_str(p, "session") not in only:
            continue
        if not p["cmd"]:
            continue
        candidates.append(p)
    if not candidates:
        return []

    def _revivable(p: dict[str, object]) -> bool:
        sid = _field_str(p, "session")
        return has_session(sid, psmux=binary) and agent_idle(sid, psmux=binary)

    with ThreadPoolExecutor(max_workers=16) as pool:
        flags = list(pool.map(_revivable, candidates))

    revived: list[str] = []
    for p, ok in zip(candidates, flags, strict=True):
        if not ok:
            continue
        sid = _field_str(p, "session")
        # The configured command already IS the resume command -- claude's
        # registry default is ``claude --continue``, which picks the dead
        # pane's conversation back up. ``sessions.build_resume_command`` is
        # deliberately NOT used here: with no session id claude's builder
        # *strips* ``--continue``, starting a fresh chat -- the opposite of
        # reviving. Injection shape mirrors ``launch_psmux_session``.
        resume = _field_str(p, "cmd")
        keys = f"cmd /c {resume}" if sys.platform == "win32" else resume
        if send_keys(sid, keys, "Enter", target=sid, psmux=binary):
            revived.append(sid)
    return revived


def config_sessions(config_path: str | None) -> list[dict[str, object]]:
    """Eligible psmux sessions from config — no psmux binary calls, fast path
    for the upload server's session list."""
    import json
    from pathlib import Path

    # Same machinery `eligible_projects` uses, imported in-body in the same
    # style: a configured ``path`` may be relative to ``baseDir``, and every
    # consumer of this list that has to *act* on the folder (the F2 "open in
    # VS Code" hotkey) needs an absolute one.
    from magent.launch import _expand_base_dir, _resolve_path
    from magent.paths import find_config
    from magent.sessions import is_ide_tool

    config_file = find_config(config_path)
    if not config_file.exists():
        return []

    data = json.loads(config_file.read_text(encoding="utf-8"))
    default_tool = data.get("settings", {}).get("defaultTool", "claude")
    raw_base = data.get("baseDir")
    base_dir = (
        _expand_base_dir(raw_base) if isinstance(raw_base, str) and raw_base else None
    )
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
                # "" (never None) when the folder can't be resolved, so a JSON
                # consumer can treat it as a plain string field.
                "resolved": _resolve_path(p["path"], base_dir) or "",
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
