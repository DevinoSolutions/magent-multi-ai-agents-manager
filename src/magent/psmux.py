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

import contextlib
import functools
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from magent.config import MagentConfig
    from magent.platform import Platform

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


def has_session(
    name: str, psmux: str | None = None, timeout: float | None = None
) -> bool:
    """True if a psmux session named ``name`` is alive.

    ``timeout`` bounds the probe and a timed-out probe answers False. Left at
    the default the call blocks exactly as before -- only the bring-up creation
    verify passes a bound, because a wedged psmux server answers nothing at all
    and would otherwise hold the whole fan-out hostage.
    """
    binary = psmux or find_psmux()
    if not binary:
        return False
    try:
        result = subprocess.run(
            [binary, "-L", name, "has-session"],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    else:
        return result.returncode == 0


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
# four bare tokens with nothing marking which half was the key. Both labels are
# capitalized so the two halves match register.
#
# The hint is deliberately pure ASCII. An earlier revision fronted the picker
# label with U+2630 (the menu hamburger), and its East-Asian *ambiguous* width
# bit for real: psmux counts it as one cell, Windows Terminal draws it as two,
# so every cell after it shifted right -- a stray highlighted cell inside the
# bar and, when the shift spilled the last column, a wrapped phantom row under
# the status line. A status bar is exactly the place where the renderer's and
# the multiplexer's width arithmetic must agree, so: no ambiguous-width glyphs
# here, ever. `</>` is the universal "code" mark for the VS Code half -- three
# plain ASCII cells, zero font support required.
#
# The two halves are separate literals because only F1 is unconditionally
# true. F1 is a psmux binding this module installs on the session's own
# server, so it works for any viewer of that session. F2 is handled by the
# magent hotkey listener, which shells out to `code` -- on a machine with no
# VS Code that half advertises a key that does nothing, so it is only emitted
# when `code` resolves (see `status_hints`).
_STATUS_HINTS_F1 = "#[bold,fg=cyan] F1 #[default]Proj. Picker "

_STATUS_HINTS_F2 = "#[bold,fg=cyan] F2 #[default]</> VS Code "

# Two more spaces between the halves: each half already carries one trailing
# space, so the seam reads as the same 3-column gap it always has.
_STATUS_HINTS_GAP = "  "

_STATUS_HINTS = _STATUS_HINTS_F1 + _STATUS_HINTS_GAP + _STATUS_HINTS_F2

# ...and the width budget has to travel with it, exactly like the brand's below.
# tmux truncates status-right at `status-right-length` (default 40, but a
# personal conf may set it far tighter), so the now-wider hint can render
# mid-label. Style directives don't count toward the limit; what's left --
# " F1 ", "Proj. Picker", the gap, " F2 ", "</>" and " VS Code " -- is
# 4 + 12 + 3 + 4 + 3 + 9 = 35 columns, every one a single unambiguous cell.
# 40 carries that plus headroom for a label tweak.
_STATUS_HINTS_LEN = "40"

# The F1-only variant needs its own budget: leaving 40 here would be harmless
# on paper but wrong in spirit -- the number is documentation of the text it
# guards. Visible cells are " F1 " + "Proj. Picker" + the half's own trailing
# space = 4 + 12 + 1 = 17, and 22 carries that plus the same 5 columns of
# headroom the full hint's 40 gives its 35.
_STATUS_HINTS_F1_LEN = "22"

# The product's own status-left brand, same plainness as the hints: one word,
# one accent. magent *owns* this per session rather than inheriting whatever a
# personal ~/.tmux.conf set, so every magent window reads the same.
_STATUS_BRAND = "#[bold,fg=green] magent #[default]"

# ...and the width budget has to travel with it. tmux truncates status-left at
# `status-left-length` (default 10, but a personal conf may set it far tighter),
# so setting the brand without the length can render it mid-word. Style
# directives don't count toward the limit; " magent " is 8 columns.
_STATUS_BRAND_LEN = "10"

# What a raw F2 says when it actually reaches psmux. See `decoration_argv` for
# why this can never double-fire on a Windows attach window. Pure ASCII for the
# same reason the hints are (this text lands in the status line via
# display-message), and one line: display-message truncates at the bar's width.
_F2_FALLBACK_MSG = (
    "F2 opens VS Code only from a magent window on Windows"
    " (hotkey listener not running in this client)"
)


def code_on_path() -> bool:
    """True when VS Code's ``code`` launcher resolves on THIS machine.

    The single owner of that probe, so the ``"code"`` literal and the hotkey
    listener's own ``shutil.which("code")`` in ``hotkey.py::_do_open_code``
    can't drift into advertising a key the listener would then refuse.
    """
    return shutil.which("code") is not None


def status_hints(code_hint: bool) -> tuple[str, str]:
    """The status-right text and its width budget, as a pair.

    Returned together because they are one decision: the budget documents the
    text it guards, and setting one without the other lets a personal
    ``~/.tmux.conf`` truncate the hint mid-label.

    ``code_hint`` is whether the F2 half is truthful here -- see
    ``decoration_argv``. Every variant is pure ASCII by construction (both
    halves are), which is load-bearing: an ambiguous-width glyph in a status
    bar desyncs psmux's and Windows Terminal's cell arithmetic.
    """
    if code_hint:
        return _STATUS_HINTS, _STATUS_HINTS_LEN
    return _STATUS_HINTS_F1, _STATUS_HINTS_F1_LEN


def decoration_argv(name: str, psmux: str, code_hint: bool) -> list[list[str]]:
    """The psmux commands that brand ``name`` and advertise its window hotkeys.

    Six of them: magent *owns* F1 -> detach-client per session (the hint has to
    be truthful on a machine with no personal ``bind -n F1`` in ~/.tmux.conf,
    and owning the binding keeps the existing "back to the picker" semantics
    rather than changing them), the status-right carries the hint text plus the
    width budget it needs, the status-left carries the product brand plus
    the width budget *it* needs, and the sixth is the F2 fallback below. Each
    half sets its text and its length together or neither: a personal conf with
    a tighter ``status-*-length`` would truncate the other half mid-label. All
    are ``-L <name>``-scoped, so they land on that session's own server and
    override whatever its tmux.conf set at start-up.

    Split out from ``decorate_session`` so the launch path can fan the same
    argvs out as raw Popens while callers with one session run them inline.

    ``code_hint`` gates the F2 half of the status-right and has no default:
    every call site has to decide. F1 (detach -> back to the picker) is a
    host-side psmux binding installed right here, so it is true for any viewer
    of the session; F2 is handled by the magent hotkey listener, which needs
    ``code`` on PATH, so its half is advertised only when ``code`` resolves on
    the machine doing the decorating. tmux's status line is session-scoped, not
    per-client, so a single answer per session is all the protocol allows --
    a per-viewer hint is out of scope, not an oversight.

    ...which is exactly why the sixth command exists. The hint is one answer for
    every viewer, but F2 itself is NOT: it is handled by the Windows hotkey
    listener, which intercepts the key in a magent-titled window and swallows it
    (``hotkey.py::_hook_decide`` returns 1, so the keystroke never reaches the
    terminal). Any other viewer of the same session -- Termius, a phone SSH app,
    a plain ``ssh`` from another box -- has no listener, so F2 fell through to
    the pane and died silently while the bar still advertised it. So when the F2
    half is advertised, magent also binds F2 on the session's own server to a
    ``display-message`` explaining the situation: that binding can only fire for
    a viewer where the key was going to be lost anyway, because the listener
    swallows it first everywhere else. When the F2 half is NOT advertised the
    sixth command is the matching ``unbind-key``, so a session that was
    decorated back when ``code`` resolved on this host doesn't keep answering a
    key nothing advertises any more.
    """
    hints, hints_len = status_hints(code_hint)
    f2 = (
        [psmux, "-L", name, "bind", "-n", "F2", "display-message", _F2_FALLBACK_MSG]
        if code_hint
        else [psmux, "-L", name, "unbind-key", "-n", "F2"]
    )
    return [
        [psmux, "-L", name, "bind", "-n", "F1", "detach-client"],
        [psmux, "-L", name, "set", "-g", "status-right", hints],
        [psmux, "-L", name, "set", "-g", "status-right-length", hints_len],
        [psmux, "-L", name, "set", "-g", "status-left", _STATUS_BRAND],
        [psmux, "-L", name, "set", "-g", "status-left-length", _STATUS_BRAND_LEN],
        f2,
    ]


def decorate_session(
    name: str, psmux: str | None = None, code_hint: bool | None = None
) -> None:
    """Brand one session's status line and advertise its F1/F2 hints.

    ``code_hint=None`` means "probe here": this machine is decorating, so
    whether ``code`` resolves here is exactly the question. Callers that
    already probed (``decorate_sessions``) pass the answer down instead.

    Best-effort and guarded exactly like ``flash_message``: a status bar is
    cosmetic, so a missing binary, a hung psmux, or a non-zero exit is logged
    and swallowed -- never propagated into a bring-up.
    """
    binary = psmux or find_psmux()
    if not binary:
        return
    if code_hint is None:
        code_hint = code_on_path()
    for cmd in decoration_argv(name, binary, code_hint):
        try:
            subprocess.run(cmd, capture_output=True, timeout=3, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            get_logger("launch").warning(
                "status-line decoration failed for session=%s: %s", name, exc
            )


def decorate_sessions(names: list[str], code_hint: bool | None = None) -> list[str]:
    """Decorate many sessions concurrently. Returns the names attempted.

    Each session is its own psmux server, so the round-trips per name
    would otherwise serialize across a large config -- same fan-out shape as
    ``revive_sessions``.

    The ``code`` probe is resolved ONCE for the whole batch and passed down:
    the answer is a property of this machine, not of a session, so a
    per-session ``shutil.which`` would be one filesystem sweep per name for
    one shared answer.
    """
    from concurrent.futures import ThreadPoolExecutor

    binary = find_psmux()
    if not binary or not names:
        return []
    hint = code_on_path() if code_hint is None else code_hint
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(
            pool.map(lambda n: decorate_session(n, psmux=binary, code_hint=hint), names)
        )
    return list(names)


# Runtime state, not config: the stamp lives beside the agent-state store and
# the logs under ~/.magent/ (same home as log.LOG_DIR / agent_state.STATE_DIR).
# It is a cache -- deleting it only costs one extra decoration pass.
DECOR_STAMP = Path.home() / ".magent" / "state" / "decor.stamp"

# How long a fired decoration pass counts as fresh. `magent attach` polls
# `up --json` up to ~20 times while it waits for a bring-up to stabilize, and
# each poll would otherwise fire six psmux commands per session (240+ processes
# against a host that is already busy). A minute is far longer than any single
# stabilization loop and far shorter than "the user changed something and
# re-attached", and newborn sessions never depend on it -- the launch/revive
# path decorates those directly at creation.
DECOR_TTL_S = 60.0


def _decor_stamp_fresh() -> bool:
    """True when a decoration pass ran within ``DECOR_TTL_S``.

    A missing/unreadable stamp answers False (decorate), and so does a stamp
    dated in the future: a bad clock must not be able to switch decoration off
    for longer than the TTL.
    """
    try:
        age = time.time() - DECOR_STAMP.stat().st_mtime
    except OSError:
        return False
    return 0 <= age < DECOR_TTL_S


def _touch_decor_stamp() -> None:
    """Mark a decoration pass as just-fired. Best-effort: a read-only home
    costs an un-throttled decoration, never an error."""
    with contextlib.suppress(OSError):
        DECOR_STAMP.parent.mkdir(parents=True, exist_ok=True)
        DECOR_STAMP.touch()


def decorate_sessions_async(
    names: list[str], code_hint: bool | None = None
) -> list[str]:
    """Fire the decoration commands and return WITHOUT waiting for any of them.

    The status-query variant of ``decorate_sessions``. That one runs each
    session's argvs serially under ``subprocess.run(..., timeout=3)``, so a host
    whose psmux servers are busy enough to time out pays 15s per session -- and
    `up --json` (the host side of `magent attach`) called it synchronously, so
    a 40-session config could spend ~45s decorating a status bar before printing
    a byte of JSON. The attach client's status timeout fired at 30s and retried
    with a 120s one, re-running the whole thing. Decoration is cosmetic and has
    always been best-effort, so the status path must never wait on it at all.

    The argvs for one session are order-independent (a ``bind``, four
    ``set -g``, and the F2 bind/unbind), so everything goes out at once as raw
    Popens -- same shape the launch path already uses for a fresh batch
    (``platform/windows.py::WindowsPlatform._decorate_batch``), minus the wait.
    All three stdio handles go to DEVNULL, which is load-bearing rather than
    tidy: this command is usually running under ``ssh``, and a child holding an
    inherited pipe open would keep that channel open after the JSON was printed.

    Throttled by ``DECOR_STAMP``: returns ``[]`` without firing anything when a
    pass ran less than ``DECOR_TTL_S`` ago, so attach's repeated status polls
    can't pile up hundreds of orphan processes against a wedged psmux server.

    Returns the names it fired for (``[]`` when throttled or unable to run).
    """
    binary = find_psmux()
    if not binary or not names:
        return []
    if _decor_stamp_fresh():
        return []
    hint = code_on_path() if code_hint is None else code_hint
    log = get_logger("launch")
    fired: list[str] = []
    for name in names:
        try:
            for cmd in decoration_argv(name, binary, hint):
                subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except OSError as exc:
            # Same posture as decorate_session's: cosmetic, so a session whose
            # commands can't even be spawned is logged and skipped.
            log.warning(
                "status-line decoration could not be spawned for %s: %s", name, exc
            )
        else:
            fired.append(name)
    _touch_decor_stamp()
    log.info("fired status-line decoration for %d session(s)", len(fired))
    return fired


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
        launch_verified(plat, windows)
    return [w.window_name for w in windows]


# Mirrors ``platform/windows.py::_SEND_VERIFY_SETTLE_S`` on purpose: a freshly
# created psmux server needs a beat before it answers control commands, and the
# two verifies run back to back in the same bring-up, so a different pause here
# would only be a second number to reason about.
_CREATE_VERIFY_SETTLE_S = 2.0
# A wedged psmux server answers nothing at all -- in the incident below every
# control command against its socket timed out. Bound each probe so one wedged
# server costs its own timeout instead of the whole fan-out's.
_CREATE_PROBE_TIMEOUT_S = 3.0


def _missing_sessions(names: list[str], binary: str) -> list[str]:
    """The subset of ``names`` whose session does not answer ``has-session``.

    Concurrent fan-out, the shape ``revive_sessions`` already uses: one bounded
    probe per session, all in flight together, so a 40-session bring-up pays
    roughly one round-trip rather than 40 sequential ones.
    """
    from concurrent.futures import ThreadPoolExecutor

    def _up(name: str) -> bool:
        return has_session(name, psmux=binary, timeout=_CREATE_PROBE_TIMEOUT_S)

    with ThreadPoolExecutor(max_workers=16) as pool:
        flags = list(pool.map(_up, names))
    return [n for n, ok in zip(names, flags, strict=True) if not ok]


def launch_verified(plat: Platform, windows: list[PsmuxWindowOpts]) -> list[str]:
    """Create ``windows`` through the platform, then prove each session exists.

    ``launch_psmux_session`` reports success the moment its ``new-session``
    processes exit 0, which is not the same thing as a live psmux server.
    During a ~40-session attach bring-up storm one project's server wedged --
    every control command against its socket timed out -- and NOTHING detected
    it: creation had no verify at all, so the picker showed that project down
    forever and only a second, storm-free bring-up recreated it. This is the
    creation-level twin of ``platform/windows.py::_verify_sends_landed``.

    ASYMMETRY WITH THE SEND VERIFIER, deliberately: that one treats an
    empty/unreadable pane reading as "not a casualty", because re-sending into
    a pane whose state is unknown would type the agent command into a live
    agent. Here the remedy is ``new-session``, which ``launch_psmux_session``
    already skips for any session that answers ``has-session`` -- so re-running
    it is safe, and an unknown state (including a probe that TIMED OUT against
    a wedged server) counts as MISSING and is retried. Unknown is safe to
    re-create; it is not safe to inject into.

    Never raises out of the verify: one stuck session must not cost the wave
    its remaining ones. Returns the names still missing after the one retry.
    """
    if not windows:
        return []
    plat.launch_psmux_session(windows)

    log = get_logger("launch")
    binary = find_psmux()
    if not binary:
        return []

    # Settle first: the storm's timeouts were transient churn, and probing at
    # t=0 would misclassify slow-but-fine servers on a loaded host.
    time.sleep(_CREATE_VERIFY_SETTLE_S)
    missing = _missing_sessions([w.window_name for w in windows], binary)
    if not missing:
        return []

    log.warning(
        "session did not come up after bring-up; respawning: %s", ", ".join(missing)
    )
    stuck = set(missing)
    try:
        # Back through the full launch path on purpose -- a hand-rolled
        # ``new-session`` here would diverge from the original recipe (batch
        # pacing, send-keys verification, status-line decoration).
        plat.launch_psmux_session([w for w in windows if w.window_name in stuck])
    except (OSError, subprocess.SubprocessError):
        log.exception("respawn failed for %s", ", ".join(missing))
        return missing

    time.sleep(_CREATE_VERIFY_SETTLE_S)
    still_missing = _missing_sessions(missing, binary)
    if still_missing:
        log.error(
            "session never came up after respawn; left down: %s",
            ", ".join(still_missing),
        )
    return still_missing


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
