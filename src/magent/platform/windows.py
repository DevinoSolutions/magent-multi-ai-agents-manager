from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from ctypes import POINTER, WINFUNCTYPE, byref, create_unicode_buffer, windll
from pathlib import Path
from typing import Literal

from magent.attach_client import ssh_program
from magent.grid import MonitorRect, Rect
from magent.log import get_logger
from magent.platform import (
    WT_NOT_FOUND_MESSAGE,
    HandoffResult,
    Platform,
    PsmuxWindowOpts,
    TerminalLaunchOpts,
    TerminalNotFoundError,
    VSCodeLaunchOpts,
    _handoff_launcher,
    find_psmux,
)
from magent.procs import (
    NO_CONSOLE_SESSION,
    active_console_session_id,
    current_session_id,
    pid_alive,
    spawn_unjobbed,
)
from magent.psmux import (
    SEND_KEYS_TIMEOUT_S,
    await_clients,
    capture_pane,
    child_env,
    clear_stale_servers,
    code_on_path,
    decoration_argv,
    idle_sessions,
    image_stem,
    probe_sessions,
)

user32 = windll.user32
shcore = windll.shcore

# Sessions per bring-up wave, and the pause between waves (see the batched
# loop in launch_psmux_session).
_BRING_UP_BATCH = 5
_BRING_UP_BATCH_PAUSE_S = 2.0

# Send-keys verification (see _verify_sends_landed). A fresh session is a bare
# pwsh and the agent command is TYPED into it, so a shell that is still
# initializing can flush the pending input away (PSReadLine does exactly this)
# and swallow the command outright -- the pane then rests at a prompt forever
# while passing every liveness probe.
#
# The settle must outlast an ordinary-but-slow start: `cmd /c <agent>` needs to
# have spawned cmd before the probe runs, or a merely-slow pane reads as a
# casualty and gets a second command typed on top of it. Same order as
# _BRING_UP_BATCH_PAUSE_S, for the same reason (a loaded host).
_SEND_VERIFY_SETTLE_S = 2.0
# Total sends per pane INCLUDING the original -- so at most two re-sends. A
# pane still bare after that is a real fault to report, not one to keep
# hammering: each attempt costs the batch another settle.
_SEND_MAX_ATTEMPTS = 3

# Budgets for every psmux client the bring-up waits on. Each of these was a
# bare `wait()`, so one socket that stopped answering held `magent up` -- and
# the `magent attach` driving it over ssh -- forever; in the 2026-08-18 wedge
# every control command hung from any console. Each fan-out gets ONE deadline
# for the whole set (`psmux.await_clients`: the clients run concurrently, so a
# budget per client would cost N budgets), and a client still running at it is
# killed and reaped, never left behind.
#
# Every number errs long: erring short costs a session, erring long costs only
# time, and the bound exists for "never", not for "slow".
#
# The dedupe probe and the stale-server kill are one cheap round-trip per
# socket fanned out across the whole fleet, and the measured worst case for
# exactly that shape is ~19 s for a 46-socket has-session fan-out on a loaded
# host (see psmux.live_sessions): 30 s is that with half again on top. A probe
# that outruns it is UNKNOWN and its session is left alone -- never killed,
# never re-created -- so a false timeout costs a report line, not an agent.
_DEDUPE_TIMEOUT_S = 30.0
_CLEAR_TIMEOUT_S = _DEDUPE_TIMEOUT_S
# One wave's new-session clients. Healthy creation measured under a second
# (892 ms, right after the wedge cleared -- during it, forever), but it is the
# heaviest call here: it forks a server and a ConPTY in the middle of a spawn
# storm. A false timeout leaves a session without its agent command, so it gets
# the product's ceiling for one delivery attempt (upload_server.INJECT_TIMEOUT_S).
_CREATE_TIMEOUT_S = 60.0
# One wave's send-keys, and each round of re-sends. A control command against a
# busy socket has been measured from 3 s to past 70 s (DESIGN.md, "The upload
# reply is not hostage to the paste"), and a send that is killed may still have
# landed, so it is never re-sent -- the paste's one-attempt law, with the same
# 60 s cap. The status-line decorations are cosmetic and get the plain
# SEND_KEYS_TIMEOUT_S.
_SEND_TIMEOUT_S = 60.0

# Geometry-reclaim nudge (see Platform.nudge_windows). The delta must be large
# enough to change the terminal's character grid -- a sub-cell nudge resizes
# the window without changing the rows/cols it reports, which tells the psmux
# client nothing. The settle is the beat the terminal needs to push the new
# grid down its pty before we put the window back.
_NUDGE_DELTA_PX = 40
_NUDGE_SETTLE_S = 0.15

# Budget for the one-shot process-command-line scan (see process_cmdlines).
# It runs in front of attach's window spawning, so it must fail fast rather
# than stall the flow; a timeout is reported as "we could not look", and the
# caller then leaves every window alone.
_PROC_SCAN_TIMEOUT_S = 10.0

# Characters that make `cmd /k` quote-strip or re-parse an argv[0]; an ssh
# client path carrying one is handed to cmd as its bare name instead.
_CMD_METACHARS = frozenset(' &()^%!"')

# --- Session-0 desktop hand-off (see run_on_desktop) --------------------------
# Scratch root for one per-call directory holding the launcher script and its
# result files. Under the system temp dir rather than ~/.magent because the
# hand-off has to work before any magent state exists, and because the directory
# is per-call and deleted on success.
_HANDOFF_DIR_NAME = "magent-handoff"
_HANDOFF_TASK_PREFIX = "magent-handoff-"
# How often the poll looks for the result files. Small enough that a hand-off of
# a fast command (a `serve --ensure` is ~1s) does not feel like a round trip.
_HANDOFF_POLL_S = 0.25
# How long the task gets to write pid.txt before we conclude it never started.
# Distinct from the caller's timeout: "the command is slow" and "Task Scheduler
# never ran it" are different answers, and only the second one is worth
# abandoning a 900s budget for. Generous because the signal comes from a COLD
# powershell.exe: ~1.6s measured on an idle desktop, but a loaded box (CI
# proved it -- 5s was not enough on 2 of 5 windows-latest runners) can take
# well over 5s just to reach the launcher. A false "never started" here
# abandons a bring-up that is in fact under way, so the grace errs long; a
# task that truly never ran is still reported, only 30s later.
_HANDOFF_START_GRACE_S = 30.0
# How long a child that is GONE gets to still have its exit code written. The
# launcher writes rc.txt only after its wait() returns, so between the
# child's last breath and rc.txt landing there is a window in which "pid dead,
# no rc.txt" is the ordinary success path mid-flight, not a lost child. CI
# proved the window is real: on 3 of 5 windows-latest runners a `-c "exit 7"`
# child was reported "exited without an exit code" with its stdout already on
# disk. A launcher that truly died never writes it, and that is still caught --
# after this grace, not before.
_HANDOFF_EXIT_GRACE_S = 15.0
# How long a PRESENT rc.txt gets to become an integer. rc.txt existing is not
# the exit code being readable: the launcher renames a finished file into
# place, but a scanner can hold a file it has just seen written, and the
# PowerShell launcher before it created the file first and refused readers
# until it closed (measured: 298 of 300 first reads after the file appeared
# were a sharing violation). Treating that read as final reported "unreadable
# exit code ''" for commands that had succeeded, on five windows-latest CI
# runs. Only a complete (newline-terminated) integer ends the wait; this bounds
# the wait on an rc.txt that never becomes one (a file something keeps locked,
# a value that is not a number). It errs long because a false answer here is a
# succeeded bring-up reported as failed, while the cost of a long one falls
# only on an rc.txt that is broken anyway.
_HANDOFF_RC_GRACE_S = 10.0
# How long to keep retrying the scratch-directory delete after success. The
# launcher and the powershell.exe running it are still exiting for the few
# milliseconds after rc.txt lands, and on Windows a file still open makes rmtree
# fail -- silently, with ignore_errors, which is how CI grew a scratch directory
# per successful hand-off.
_HANDOFF_CLEANUP_GRACE_S = 5.0
# Every schtasks call itself is bounded -- Create/Run/Query/Delete are local and
# instant, so a hang is a wedge, not work.
_SCHTASKS_TIMEOUT_S = 15.0
# schtasks truncates /TR at ~261 characters -- silently, so past it the task
# runs a DIFFERENT command. That is why /TR carries only a fixed-length launcher
# and the real argv lives in a file next to it (argv.json).
_TR_MAX_CHARS = 261
# Bare `powershell.exe`, not an absolute path, and not `pwsh`: Windows PowerShell
# ships on every Windows box while PowerShell 7 does not, and /TR's length budget
# is the scarce resource here. Leaving it unqualified is not the same exposure as
# resolving OUR OWN schtasks off PATH (see _schtasks_exe): this string is run by
# Task Scheduler inside the logged-on user's session, against the user's own
# PATH, not against the possibly-hostile PATH of an ssh login.
_HANDOFF_SHELL = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File"
# The script that shell runs is written WITH a BOM. Windows PowerShell 5.1
# decodes a `-File` script that has none in the ANSI code page, which turns
# every non-ASCII character of the two paths it holds into mojibake -- and can
# end a literal early: the UTF-8 bytes of U+00D1 (C3 91) and U+0442 (D1 82)
# each hold one that cp1252 reads as a typographic single quote. One constant,
# because the tests that run a launcher script stage it with this too; a copy
# written any other way is not the file production runs.
_HANDOFF_SCRIPT_ENCODING = "utf-8-sig"


def _schtasks_exe() -> str | None:
    """Absolute path to ``schtasks.exe``, or None when it cannot be found.

    THE seam the unit tier monkeypatches to point at a recording fake -- no
    test may create a real scheduled task, and there is no test-only
    environment variable anywhere in this path.

    System directory FIRST, PATH only as a fallback. ``run_on_desktop`` is
    reached from an ssh login, whose PATH is not this machine's to trust, and
    handing that PATH the choice of what to execute as the logged-on user would
    make a hand-off into an execution primitive for whoever set it.
    """
    try:
        buffer = create_unicode_buffer(260)
        if windll.kernel32.GetSystemDirectoryW(buffer, 260):
            candidate = Path(buffer.value) / "schtasks.exe"
            if candidate.exists():
                return str(candidate)
    except OSError:
        get_logger("launch").warning(
            "session-0 hand-off: system-directory probe failed; falling back to PATH"
        )
    return shutil.which("schtasks")


def _ps_quote(value: str) -> str:
    """Wrap ``value`` as ONE PowerShell single-quoted literal.

    Single-quoted, so nothing inside is expanded: these are paths and the
    parked-pane notice, and a ``$`` or a backtick in one must arrive exactly
    as written. Doubling is the only escape a single-quoted PowerShell string
    has, and PowerShell ends such a string on FIVE code points, not one:
    U+0027 and the typographic U+2018, U+2019, U+201A and U+201B. Every one of
    them is doubled, or a value holding one breaks out.
    """
    return "'" + re.sub("(['\u2018\u2019\u201a\u201b])", r"\1\1", value) + "'"


def _handoff_script(python: str, launcher: Path) -> str:
    """The PowerShell the scheduled task runs in the user's own session.

    Three lines, and none of them is the command. PowerShell cannot be the
    launcher: 5.1's ``Start-Process`` with redirection drops the handle
    CreateProcess returned, and ``$p.Handle`` then re-opens the child by pid,
    after the fact -- so a child that has already exited leaves ``ExitCode``
    at ``$null`` and rc.txt empty (the measured "hand-off failed" for a
    bring-up that worked). The launcher is ``launch.py`` (a copy of
    ``_handoff_launcher``), run by the interpreter magent itself is running
    under; it holds the handle, and the argv reaches it through
    ``argv.json``, never through a quoted command line.

    * export ``MAGENT_SESSION0_POLICY=refuse`` for the child, so a hand-off
      that somehow landed in Session 0 again refuses instead of handing off in
      turn -- a recursion whose every level writes a scheduled task.
    * ``-I``: PYTHONPATH, PYTHONHOME and the script's directory stay off
      ``sys.path``, so nothing in the user's environment or the scratch
      directory can put a different module under the launcher's imports.
    * ``&`` with both paths as single-quoted literals: nothing in either is
      expanded, and PowerShell hands each to CreateProcess as ONE argument.
    """
    return "\n".join(
        (
            "$ErrorActionPreference = 'Stop'",
            "$env:MAGENT_SESSION0_POLICY = 'refuse'",
            f"& {_ps_quote(python)} -I {_ps_quote(str(launcher))}",
            "",
        )
    )


def _stage_handoff(work: Path, argv: list[str]) -> Path:
    """Write one hand-off's three files into ``work`` and return ``run.ps1``.

    ``argv.json`` carries the command and the CALLER's working directory:
    ``find_config`` walks up from the working directory and a scheduled task
    starts in ``system32``, so without it a hand-off could bring up a
    different config's projects than the command the user actually typed.
    ``launch.py`` is ``_handoff_launcher``'s own source, byte for byte.
    ``run.ps1`` is written WITH a BOM -- see _HANDOFF_SCRIPT_ENCODING.

    Raises OSError, or UnicodeEncodeError for a path that has no UTF-8 form
    (a lone surrogate in the scratch or interpreter path).
    """
    work.mkdir(parents=True, exist_ok=True)
    _handoff_launcher.write_spec(work, argv, str(Path.cwd()))
    launcher = work / _handoff_launcher.LAUNCHER
    launcher.write_bytes(Path(_handoff_launcher.__file__).read_bytes())
    script = work / "run.ps1"
    script.write_text(
        _handoff_script(sys.executable, launcher), encoding=_HANDOFF_SCRIPT_ENCODING
    )
    return script


def _remove_scratch(work: Path) -> None:
    """Delete a finished hand-off's scratch directory, retrying briefly.

    A file in it can still be open for a moment after rc.txt lands. The
    launcher closed its own copies of the redirect files right after starting
    the command, but it and the powershell.exe running it are still exiting,
    and a scanner may be reading a file it has just seen written. An open file
    makes rmtree fail on Windows. Bounded by ``_HANDOFF_CLEANUP_GRACE_S``; a
    directory that outlives it is left behind rather than fought over (the
    next call uses a new one).
    """
    deadline = time.monotonic() + _HANDOFF_CLEANUP_GRACE_S
    while True:
        shutil.rmtree(work, ignore_errors=True)
        if not work.exists() or time.monotonic() >= deadline:
            return
        time.sleep(0.1)


def _printable(text: str) -> str:
    """``text`` with every lone surrogate written as an escape. The argv and
    the paths a hand-off names may hold one (Windows allows it), and the log
    file is UTF-8: a record that cannot be encoded is not written at all --
    logging prints a traceback on stderr instead."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _one_line(text: str, limit: int = 200) -> str:
    """The last non-empty line of a tool's output, clipped -- diagnostics go in
    a single ``detail`` string, and schtasks answers in a multi-line table."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:limit] if lines else ""


def _recorded_int(raw: str) -> int | None:
    """What the launcher recorded, as an integer -- or None if it is not one
    YET.

    Complete means newline-terminated. The launcher writes ``<n>\\n`` to a
    temporary name and renames it into place, so it never shows a prefix; the
    rule stays because a prefix -- the ``1`` of ``12`` -- must never be
    final, whoever wrote the file. The one rule every read of pid.txt and
    rc.txt goes through.
    """
    if not raw.endswith("\n"):
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _read_recorded_int(path: Path) -> int | None:
    """The integer the launcher recorded in ``path`` (its pid.txt or rc.txt),
    or None until it has written one.

    None covers every "not yet": the file is absent, present but empty,
    present but held by something else (a scanner that has just seen it
    written, and this poll reads every 250ms), or present with a value that is
    not complete. All of them mean "no answer yet", never "it failed"; only a
    complete integer (see ``_recorded_int``) is an answer.
    """
    return _recorded_int(_read_handoff_text(path))


def _handoff_finished(out: Path, err: Path, work: Path, rc: int) -> HandoffResult:
    """The command's own exit code came back: the hand-off itself worked,
    whatever the command decided, so its scratch directory goes."""
    stdout, stderr = _read_handoff_text(out), _read_handoff_text(err)
    _remove_scratch(work)
    return HandoffResult(rc=rc, stdout=stdout, stderr=stderr)


def _settle_exit_code(
    files: tuple[Path, Path, Path, Path], task: str, work: Path, waited_s: float
) -> HandoffResult:
    """The last word on an rc.txt the poll has stopped waiting on.

    One more read, and it is decisive: an exit code that became complete since
    the poll's last look is the answer, like any other. Otherwise this is the
    fourth answer, distinct from the other three: not "never started" and not
    "lost its child" (the launcher got as far as its exit code), and not "may
    still be running" (the launcher writes rc.txt after ``wait()`` returns, so
    the command is done). The command FINISHED and we cannot say how, so no exit code is
    fabricated -- but its output, complete by now, is relayed.

    ``detail`` names what that read saw, in our words: a file that refused the
    read and a launcher that wrote no value are different bugs. The OS's own
    text for a failed read goes to the log, not the screen.
    """
    out, err, _pid, rc_file = files
    try:
        raw = rc_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        get_logger("launch").warning(
            "session-0 hand-off %s: rc.txt unreadable after %.1fs: %s",
            task,
            waited_s,
            exc,
        )
        # Say only what is known: errno 13 is usually a share lock (a
        # scanner's), but an ACL denial and a delete-pending file raise it too.
        why = (
            "was locked or refused"
            if isinstance(exc, PermissionError)
            else "could not be read"
        )
        seen = f"{why} ({type(exc).__name__})"
    else:
        rc = _recorded_int(raw)
        if rc is not None:
            return _handoff_finished(out, err, work, rc)
        text = raw.strip()
        seen = f"held {text[:40]!r}, not a complete exit code" if text else "was empty"
    return HandoffResult(
        rc=None,
        stdout=_read_handoff_text(out),
        stderr=_read_handoff_text(err),
        detail=(
            f"the desktop command finished but its exit code never became "
            f"readable within {waited_s:.1f}s -- rc.txt {seen}; task {task}, "
            f"scratch left at {work}"
        ),
    )


def _read_handoff_text(path: Path) -> str:
    """Read the command's out.txt or err.txt, which the launcher hands it as
    its stdout and stderr; absent or unreadable reads empty.

    UTF-8 first, then the ANSI code page. The child's stdout is a FILE, so a
    default Python child writes it in the ANSI code page, not UTF-8, and reading
    that as UTF-8 relayed every accented letter as U+FFFD. UTF-8 still goes
    first because a child in Python's UTF-8 mode writes it, and ANSI text that
    also parses as UTF-8 is already mojibake (``Ã©``). ``mbcs`` is the ANSI
    code page whatever Python's own UTF-8 mode says; it exists only on Windows,
    the only place this module imports (its module-level ``windll`` import fails
    anywhere else, so the LookupError ``mbcs`` would raise there is
    unreachable). ``errors="replace"`` on that last step: this text is RELAYED
    to a human, and a mojibake character in a diagnostic is strictly better
    than losing the diagnostic to a UnicodeDecodeError. A character the code
    page cannot hold never reaches this file raw: a magent child writes it as
    an escape (``\\u4e2d``, see ``cli/app.py``), relayed as written.

    Known limit: the fallback is WHOLE-FILE. One byte that is not UTF-8
    anywhere decodes the entire file as ANSI, so a UTF-8 child whose output
    also holds such a byte -- or whose last character was cut mid-sequence
    because a timeout read the file while it was still being written -- reads
    as mojibake throughout. Accepted: the text is a relayed diagnostic.
    """
    try:
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return path.read_text(encoding="mbcs", errors="replace")
    except OSError:
        return ""


def _send_argv(psmux: str, w: PsmuxWindowOpts) -> list[str]:
    """The one argv that types a window's agent command into its pane.

    Shared by the first send and every re-send (_verify_sends_landed): a retry
    that diverged from the original would resurrect the pane with a command
    the user never configured.
    """
    return [
        psmux,
        "-L",
        w.window_name,
        "send-keys",
        "-t",
        w.window_name,
        f"cmd /c {w.command}",
        "Enter",
    ]


def _wait_for_panes_ready(
    binary: str, names: list[str], timeout_s: float = 10.0
) -> None:
    """Best-effort wait for a batch's panes to render their shell prompts, so
    send-keys doesn't race a still-starting shell on a loaded machine.

    The whole batch shares ONE deadline: waiting per session multiplied the
    budget by the batch size, so a degraded host burned up to 50s per wave.

    Bounded and advisory: some environments (headless CI service sessions)
    never render anything into psmux's virtual screen even though the pane
    shell runs and accepts input fine -- there the wait burns its budget once
    per batch and send-keys proceeds regardless. Never raises."""
    deadline = time.monotonic() + timeout_s
    pending = list(names)
    while pending:
        pending = [n for n in pending if not capture_pane(n, psmux=binary).strip()]
        if not pending or time.monotonic() >= deadline:
            return
        time.sleep(0.3)


class WindowsPlatform(Platform):
    def set_dpi_aware(self) -> None:
        try:
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (OSError, AttributeError):
            pass
        else:
            return
        try:
            shcore.SetProcessDpiAwareness(2)
        except (OSError, AttributeError):
            pass
        else:
            return
        try:
            user32.SetProcessDPIAware()
        except (OSError, AttributeError):
            get_logger("platform").warning(
                "could not set DPI awareness; tiling may be misaligned"
            )

    def list_monitors(self) -> list[MonitorRect]:
        monitors: list[MonitorRect] = []

        MONITORINFOF_PRIMARY = 0x00000001

        class MONITORINFOEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.wintypes.DWORD),
                ("rcMonitor", ctypes.wintypes.RECT),
                ("rcWork", ctypes.wintypes.RECT),
                ("dwFlags", ctypes.wintypes.DWORD),
                ("szDevice", ctypes.c_wchar * 32),
            ]

        MONITORENUMPROC = WINFUNCTYPE(
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            POINTER(ctypes.wintypes.RECT),
            ctypes.c_void_p,
        )

        def callback(hmon: int, hdc: int, lprect: object, lparam: int) -> int:
            info = MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(MONITORINFOEXW)
            user32.GetMonitorInfoW(hmon, byref(info))
            wa = info.rcWork
            is_primary = bool(info.dwFlags & MONITORINFOF_PRIMARY)

            scale = 1.0
            try:
                dpi_x = ctypes.c_uint()
                dpi_y = ctypes.c_uint()
                shcore.GetDpiForMonitor(hmon, 0, byref(dpi_x), byref(dpi_y))
                scale = dpi_x.value / 96.0
            except (OSError, AttributeError):
                get_logger("platform").warning(
                    "DPI query failed for a monitor; assuming scale 1.0"
                )

            monitors.append(
                MonitorRect(
                    x=wa.left,
                    y=wa.top,
                    w=wa.right - wa.left,
                    h=wa.bottom - wa.top,
                    is_primary=is_primary,
                    scale_factor=scale,
                )
            )
            return 1

        user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(callback), 0)
        return monitors

    def find_window(
        self, title: str, mode: Literal["exact", "contains"] = "exact"
    ) -> int | None:
        if mode not in ("exact", "contains"):
            raise ValueError(f"unknown find_window mode: {mode!r}")
        result: int | None = None

        WNDENUMPROC = WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd: int, _: int) -> bool:
            nonlocal result
            if not user32.IsWindowVisible(hwnd):
                return True
            buf = create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            text = buf.value
            if mode == "exact" and text == title:
                result = hwnd
                return False
            if mode == "contains" and title.lower() in text.lower():
                result = hwnd
                return False
            return True

        user32.EnumWindows(WNDENUMPROC(callback), 0)
        return result

    def snapshot_windows(self) -> dict[str, object]:
        # dict is invariant, so the ABC's dict[str, object] contract can't be
        # overridden with dict[str, int]; the handle is an opaque HWND anyway.
        titles: dict[str, object] = {}
        WNDENUMPROC = WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd: int, _: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            buf = create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            if buf.value:
                titles[buf.value] = hwnd
            return True

        user32.EnumWindows(WNDENUMPROC(callback), 0)
        return titles

    def move_window(self, handle: object, rect: Rect) -> None:
        # A minimized window still enumerates and MoveWindow silently updates
        # its restored placement, but it stays in the taskbar -- so a re-tile
        # appears to skip it. Restore first so every window lands on screen.
        if user32.IsIconic(handle):
            user32.ShowWindow(handle, 9)  # SW_RESTORE
        user32.MoveWindow(handle, rect.x, rect.y, rect.w, rect.h, True)
        user32.MoveWindow(handle, rect.x, rect.y, rect.w, rect.h, True)

    def supports_window_nudge(self) -> bool:
        return True

    def nudge_windows(self, handles: list[object]) -> int:
        """Shrink each window by a cell-crossing delta, let the terminals
        propagate the new grid, then restore every original rect.

        Batched on purpose: the settle is one shared pause rather than one per
        window, so a 40-window attach pays ~0.15s total instead of ~6s. Every
        step is guarded -- a window closed mid-flight (dead HWND, or a rect
        query that fails) is skipped, never fatal.
        """
        # A 1px nudge can land inside the same character cell and change
        # nothing the terminal would report; 40px crosses a row at any
        # sane font size, and the window is restored before it can be seen.
        delta = _NUDGE_DELTA_PX
        restore: list[tuple[object, int, int, int, int]] = []
        for handle in handles:
            rect = ctypes.wintypes.RECT()
            with contextlib.suppress(OSError):
                if not user32.GetWindowRect(handle, byref(rect)):
                    continue
                w, h = rect.right - rect.left, rect.bottom - rect.top
                if w <= delta or h <= delta:
                    continue
                user32.MoveWindow(handle, rect.left, rect.top, w, h - delta, True)
                restore.append((handle, rect.left, rect.top, w, h))
        if not restore:
            return 0
        # The terminal needs a beat to notice the new size and push it down
        # its pty (over SSH: a real SIGWINCH to the remote psmux client).
        # Shrink and restore back-to-back and the pair can coalesce into "no
        # net change", which is exactly the stale state we are clearing.
        time.sleep(_NUDGE_SETTLE_S)
        nudged = 0
        for handle, x, y, w, h in restore:
            with contextlib.suppress(OSError):
                user32.MoveWindow(handle, x, y, w, h, True)
                nudged += 1
        return nudged

    def supports_window_close(self) -> bool:
        return True

    def close_window(self, handle: object) -> bool:
        """Post WM_CLOSE -- the same request the window's own X button sends.

        Deliberately never TerminateProcess: this is called to clear a pane
        whose process already exited, and a stale handle that turns out to be
        alive must be allowed to refuse. PostMessage is asynchronous, so a True
        here means "the request was queued", not "the window is gone".
        """
        WM_CLOSE = 0x0010
        return bool(user32.PostMessageW(handle, WM_CLOSE, 0, 0))

    def supports_process_scan(self) -> bool:
        return True

    def process_cmdlines(self, names: list[str]) -> list[str]:
        """One CIM query for every matching process's command line.

        One subprocess for the whole batch (the filter is OR-ed server-side)
        rather than one per name: this runs on the attach path, in front of
        window spawning, so it has to cost a fixed ~fraction of a second no
        matter how many sessions are involved. ctypes would avoid the
        PowerShell boot, but reading another process's command line that way
        means NtQueryInformationProcess + a cross-bitness PEB walk, which is a
        lot of fragile surface for a diagnostic.
        """
        if not names:
            return []
        # Every name is a module-level literal in cli/attach.py, never user
        # input -- but keep the filter to bare executable names so it stays
        # that way and cannot grow into an injection seam.
        safe = [n for n in names if n.replace(".", "").replace("-", "").isalnum()]
        if not safe:
            return []
        where = " or ".join(f"Name='{n}'" for n in safe)
        script = (
            f'Get-CimInstance Win32_Process -Filter "{where}"'
            " | ForEach-Object { $_.CommandLine } | Where-Object { $_ }"
        )
        try:
            proc = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    script,
                ],
                capture_output=True,
                text=True,
                timeout=_PROC_SCAN_TIMEOUT_S,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise OSError(f"process scan failed: {exc}") from exc
        if proc.returncode != 0:
            detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ""
            raise OSError(f"process scan exited {proc.returncode}: {detail[:200]}")
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

    def pane_reset_command(self, shell_image: str, notice: str) -> str | None:
        if image_stem(shell_image) not in {"pwsh", "powershell"}:
            return None  # only the two PowerShell stems have a scripted reset
        # poc-reap2 A4, verbatim: turn OFF the modes the agent left on (mouse
        # 1000/1002/1003/1006, focus 1004, bracketed paste 2004, the kitty
        # keyboard stack <u and modifyOtherKeys >4;0m), pop the alternate screen
        # (1049l) and show the cursor (25h); then clear and print the notice.
        # The notice is ONE single-quoted literal whatever quote characters
        # it holds (_ps_quote doubles all five): to the parser, none of it
        # is code.
        return (
            "$e=[char]27; [Console]::Write("
            '"$e[?1000l$e[?1002l$e[?1003l$e[?1006l$e[?1004l$e[?2004l'
            '$e[<u$e[>4;0m$e[?1049l$e[?25h"); '
            "Clear-Host; Write-Host " + _ps_quote(notice)
        )

    def supports_attention_signals(self) -> bool:
        return True

    def supports_wt_keybindings(self) -> bool:
        return True

    def supports_attach_windows(self) -> bool:
        return True

    def set_window_title(self, handle: object, title: str) -> bool:
        return bool(user32.SetWindowTextW(handle, title))

    def flash_window(self, handle: object) -> bool:
        FLASHW_ALL = 0x00000003
        FLASHW_TIMERNOFG = 0x0000000C  # keep flashing until the window is focused

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.wintypes.UINT),
                ("hwnd", ctypes.c_void_p),
                ("dwFlags", ctypes.wintypes.DWORD),
                ("uCount", ctypes.wintypes.UINT),
                ("dwTimeout", ctypes.wintypes.DWORD),
            ]

        info = FLASHWINFO(
            cbSize=ctypes.sizeof(FLASHWINFO),
            hwnd=handle,
            dwFlags=FLASHW_ALL | FLASHW_TIMERNOFG,
            uCount=0,
            dwTimeout=0,
        )
        # Returns the window's PREVIOUS flash state, not success -- no signal
        # worth propagating beyond "the call was made".
        user32.FlashWindowEx(byref(info))
        return True

    def focus_window(self, handle: object) -> bool:
        if user32.IsIconic(handle):
            user32.ShowWindow(handle, 9)  # SW_RESTORE
        return bool(user32.SetForegroundWindow(handle))

    def launch_terminal(self, opts: TerminalLaunchOpts) -> None:
        args = [
            "wt",
            "-w",
            "new",
            # The title lock, in the argv literal rather than appended later:
            # magent's window title is what tiling, attach's already-open
            # dedupe, corpse pairing and the Alt+V hotkey resolve a window BY,
            # and without this flag the program in the tab (Claude Code, the
            # shell, ssh) renames it out of the grammar with one OSC escape and
            # every one of those consumers loses the window for good. Keeping it
            # inseparable from the `wt` token is what lets the MD006 lint rule
            # prove no spawn site can ever ship without it.
            "--suppressApplicationTitle",
            "-d",
            opts.cwd,
            "--title",
            opts.title,
        ]
        if opts.color:
            args.extend(["--tabColor", opts.color])

        if opts.ssh_host:
            remote_dir = opts.ssh_remote_dir or opts.cwd
            inner = f"cd {remote_dir} && {opts.command}"
            remote = f"{opts.ssh_shell} '{inner}'" if opts.ssh_shell else inner
            # Pass ssh + args as separate argv elements so the remote command
            # is a single, cleanly-quoted token. Building one `ssh ... "..."`
            # string and handing it to `cmd /k` double-nests the quotes, which
            # cmd mangles (the inner quotes leak to the remote shell).
            # argv[0] by attach_client's rule, so this pane dials the same
            # client (and agent) as the attach panes and the node calls.
            client = ssh_program()
            # `cmd /k` strips the first and last quote of a line that starts
            # with one, so a client path that needs quoting (C:\Program Files)
            # would eat the remote command's closing quote, and one carrying
            # `&` or `^` is re-parsed by cmd. Only the PATH fallback yields
            # such a path, and the bare name finds it again.
            if any(c in _CMD_METACHARS for c in client):
                client = "ssh"
            args.extend(["--", "cmd", "/k", client, "-t", opts.ssh_host, remote])
        else:
            args.extend(["--", "cmd", "/k", opts.command])

        # heavy subsystem: in-body per policy (magent.env pulls pydantic in).
        from magent.env import spawn_child_env

        try:
            # `env=`: this window hosts the project's agent exactly like a psmux
            # pane does, so it gets the same scrubbed block -- no inherited
            # CLAUDE_CODE_* session identity, no inherited NO_COLOR. See
            # env.spawn_child_env.
            #
            # Best-effort on Windows, honestly: `wt -w new` is a request to the
            # running Windows Terminal MONARCH when one exists, and the tab it
            # opens is then a child of THAT process's environment, not of this
            # Popen's. It binds when wt is cold (and on every POSIX backend).
            # The airtight path is psmux `new-session`, which is the default
            # here; this is the belt to its braces.
            subprocess.Popen(args, env=spawn_child_env())
        except FileNotFoundError as exc:
            # wt is a hard dependency: turn the raw FileNotFoundError into a
            # typed, actionable error the launch shell surfaces as one clean
            # line (never a traceback). We fail fast -- no console fallback.
            raise TerminalNotFoundError(WT_NOT_FOUND_MESSAGE) from exc

    def launch_vscode(self, opts: VSCodeLaunchOpts) -> None:
        # heavy subsystem: in-body per policy (magent.env pulls pydantic in).
        from magent.env import spawn_child_env

        args = ["cmd", "/c", opts.command]
        if opts.ssh_host:
            args.extend(["--remote", f"ssh-remote+{opts.ssh_host}"])
        args.append(opts.dir)
        # An IDE window is an agent host too: its integrated terminal inherits
        # the editor's environment, and that is where a user runs `claude`.
        # Same monarch caveat as `wt` -- `code` forwards to a running instance.
        subprocess.Popen(args, env=spawn_child_env())

    def launch_psmux_session(self, windows: list[PsmuxWindowOpts]) -> dict[str, str]:
        psmux = find_psmux()
        if not psmux:
            raise FileNotFoundError("psmux not found on PATH")
        if not windows:
            return {}
        log = get_logger("platform")
        # Windows deliberately NOT created, each with the reason the report
        # prints (psmux.launch_verified carries it to every bring-up printer).
        refused: dict[str, str] = {}

        # The dedupe has THREE answers (psmux.probe_sessions), and only a
        # positive "no such session" may lead to kill-server + new-session. A
        # probe that never answered says nothing about the session -- in the
        # 2026-08-18 wedge the sockets that stopped answering were FROZEN LIVE
        # agents, and killing and re-creating them is the mass restart that
        # would have thrown every one of them away.
        states = probe_sessions(
            [w.window_name for w in windows], psmux, timeout=_DEDUPE_TIMEOUT_S
        )
        for w in windows:
            if states[w.window_name] == "unknown":
                refused[w.window_name] = (
                    f"could not tell whether {w.window_name} is running"
                    f" (has-session gave no answer within {_DEDUPE_TIMEOUT_S:g}s);"
                    " left it alone -- not killed, not re-created"
                )
        if refused:
            log.error(
                "has-session gave no answer for %s; leaving them alone rather"
                " than killing or re-creating a session whose state is unknown",
                ", ".join(refused),
            )
        to_create = [w for w in windows if states[w.window_name] == "absent"]
        if not to_create:
            return refused

        uncleared = set(
            clear_stale_servers(
                [w.window_name for w in to_create], psmux, timeout=_CLEAR_TIMEOUT_S
            )
        )
        for w in to_create:
            if w.window_name in uncleared:
                refused[w.window_name] = (
                    f"could not clear {w.window_name}'s old psmux server"
                    f" (kill-server gave no answer within {_CLEAR_TIMEOUT_S:g}s),"
                    " so it was not re-created on top of it"
                )
        if uncleared:
            log.error(
                "kill-server gave no answer for %s; not creating a session on"
                " top of a server that could not be cleared",
                ", ".join(sorted(uncleared)),
            )
        to_create = [w for w in to_create if w.window_name not in uncleared]
        if not to_create:
            return refused

        # One probe for the whole bring-up: the launching machine IS the one
        # whose windows these are, and `code` is not going to appear on PATH
        # between two batches. Per-window would be one filesystem sweep each.
        code_hint = code_on_path()

        # Batched bring-up: creating every session AND cold-starting every
        # agent at once is a resource storm (dozens of ConPTYs + agent
        # processes spawning simultaneously starved the host to the point
        # that attaches failed). Each batch is created, gets its agent
        # command, and is given a beat to start before the next wave.
        for start in range(0, len(to_create), _BRING_UP_BATCH):
            wave = to_create[start : start + _BRING_UP_BATCH]
            if start:
                time.sleep(_BRING_UP_BATCH_PAUSE_S)

            # TWO things are special about THIS spawn, and no other in this
            # file. Both exist because this is the one call that gives a psmux
            # session its SERVER -- the process that will host the project's
            # agent for the rest of the day.
            #
            # 1. `spawn_unjobbed`: the server must not be born inside the job
            #    object of whatever created it. When the bring-up runs over SSH
            #    (`magent attach` sends `magent up` to the host -- the normal
            #    remote path) Windows OpenSSH has wrapped the whole session in a
            #    kill-on-close job, and job membership is inherited all the way
            #    down. A plain Popen here therefore couples every session's
            #    lifetime to the LAPTOP'S WI-FI: one flap and sshd tears the job
            #    down, killing the psmux servers and the agents inside them,
            #    while sessions created locally on the host survive untouched.
            #    Measured exactly that way -- 45 sessions at 10:50, 16 at 11:03,
            #    with no magent process running in between. See
            #    `procs.spawn_unjobbed`.
            #
            # 2. `env=child_env()`: psmux's nested-session guard fires for
            #    `new-session` alone. magent is routinely driven FROM a magent
            #    psmux window -- the interactive menu's "u" especially -- and a
            #    psmux that sees PSMUX_SESSION/TMUX refuses to create a sibling
            #    session ("sessions should be nested with care") while still
            #    exiting 0, so the whole wave silently produced nothing. Control
            #    commands (has-session, kill-server, send-keys, display-message,
            #    capture-pane, the decoration `set`s) are measurably indifferent
            #    to the markers -- byte-identical results with and without -- so
            #    they keep the plain inherited environment rather than a rebuilt
            #    block under every round-trip.
            #
            # Deliberately NOT added here: any console flag. `spawn_unjobbed`
            # changes job membership and nothing else, so this child keeps
            # inheriting the caller's console exactly as it always has -- psmux
            # allocates the session's pty itself, and detaching the console
            # would be a second, unrelated change to a spawn that works.
            creates = [
                spawn_unjobbed(
                    [
                        psmux,
                        "-L",
                        w.window_name,
                        "new-session",
                        "-d",
                        "-s",
                        w.window_name,
                        "-c",
                        w.cwd,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=child_env(),
                )
                for w in wave
            ]
            # A window psmux refuses to create ("failed to create session 'X'",
            # rc 1 -- a stale socket, a vanished cwd, a nesting guard) used to
            # raise CalledProcessError straight out of here, killing the whole
            # bring-up: every later batch was abandoned and the caller got a
            # traceback instead of its sessions. One bad window now costs only
            # itself. Nothing is raised: `psmux.launch_verified` re-probes every
            # name right after this returns, respawns what is missing, and
            # reports what stayed down -- that is the component that owns the
            # "did it come up?" answer, and it can only do its job if it runs.
            #
            # A client that outruns the wave's one deadline is killed and its
            # window refused -- contained exactly like a refusal: the rest of
            # the wave, and every later wave, carry on.
            batch = []
            codes = await_clients(creates, _CREATE_TIMEOUT_S)
            for w, rc in zip(wave, codes, strict=True):
                if rc == 0:
                    batch.append(w)
                elif rc is None:
                    refused[w.window_name] = (
                        f"psmux new-session for {w.window_name} gave no answer"
                        f" within {_CREATE_TIMEOUT_S:g}s"
                    )
                    log.error(
                        "psmux new-session for %s gave no answer within %gs;"
                        " killed it, skipping the window and continuing the"
                        " bring-up",
                        w.window_name,
                        _CREATE_TIMEOUT_S,
                    )
                else:
                    log.error(
                        "psmux new-session for %s exited %s; skipping it and"
                        " continuing the bring-up",
                        w.window_name,
                        rc,
                    )
            if not batch:
                continue

            _wait_for_panes_ready(psmux, [w.window_name for w in batch])

            senders = [
                subprocess.Popen(
                    _send_argv(psmux, w),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                for w in batch
            ]
            # Must wait: when this runs remotely (`magent up` over SSH -- the
            # host side of attach), sshd kills the whole process tree the
            # moment the CLI exits, and fire-and-forget senders die before
            # the keystrokes land -- every session then sits at a bare shell
            # with no agent running. Bounded like everything else here, and a
            # send that is killed is NEVER re-sent: it may still have landed,
            # and a second copy types the command into a running agent.
            sent = await_clients(senders, _SEND_TIMEOUT_S)
            unsure = {
                w.window_name for w, rc in zip(batch, sent, strict=True) if rc is None
            }
            if unsure:
                log.warning(
                    "send-keys gave no answer within %gs for %s; killed it and"
                    " not re-sending -- it may still have landed",
                    _SEND_TIMEOUT_S,
                    ", ".join(sorted(unsure)),
                )

            # A send-keys that *exits 0* still proves nothing: the keystrokes
            # reached psmux, not necessarily the shell reading its console.
            self._verify_sends_landed(
                psmux, [w for w in batch if w.window_name not in unsure]
            )

            self._decorate_batch(psmux, batch, code_hint)

        return refused

    @staticmethod
    def _verify_sends_landed(psmux: str, batch: list[PsmuxWindowOpts]) -> None:
        """Re-type the agent command into any pane the send-keys never reached.

        Detection is the same verdict ``revive_sessions`` acts on,
        ``psmux.idle_sessions``: a pane is a casualty only when it rests at
        its shell with no agent anywhere under it. The probe runs immediately
        before each re-send and is the ONLY guard against the dangerous edge:
        re-sending into a live agent would type the command text into its
        input box -- and an agent that is already up and running its Bash tool
        reads ``bash`` in the foreground, so the foreground reading alone
        cannot be the guard. Anything unknown (an unreadable pane, a failed
        process snapshot, an unreadable console) counts as "not a casualty":
        never inject into a pane whose state we could not establish. A
        console veto -- a shell-resting pane that is still unsafe to type
        into -- is named in this log, since it leaves the pending set exactly
        like a send that landed.

        One verdict per round for the whole pending set (one foreground
        fan-out, one pane-pid fan-out, one process snapshot, and one
        console-helper spawn when a pane passed the tree stages), not a
        round-trip per session: a full batch would otherwise serialize five.

        A window marked ``resend=False`` never enters the pending set: its
        command may run only once (a cloud pane's ``claude --cloud`` starts a
        new cloud session per typing), so it is not probed and never re-typed,
        and it does not shield its neighbours from the verification.

        Never raises. A pane that stays bare through every attempt is logged
        and left as-is -- at worst exactly what it was before this pass -- so
        one stuck pane cannot cost the wave its remaining sessions.
        """
        log = get_logger("platform")
        # A pane whose command may run only once (a cloud pane) is never
        # re-typed, whatever its shell reading says -- spec §18.5.
        pending = {w.window_name: w for w in batch if w.resend}
        if not pending:
            return
        sends = 1  # the caller already typed the command once
        while True:
            time.sleep(_SEND_VERIFY_SETTLE_S)
            vetoed: dict[str, str] = {}
            idle = idle_sessions(list(pending), psmux=psmux, vetoed=vetoed)
            for name, reason in vetoed.items():
                # Resting at its shell but unsafe to type into: it leaves
                # `pending` like a landed send, so say here that it did not.
                log.warning("not re-sending into %s: %s", name, reason)
            pending = {name: w for name, w in pending.items() if name in idle}
            if not pending:
                return
            names = ", ".join(pending)
            if sends >= _SEND_MAX_ATTEMPTS:
                log.error(
                    "agent command never landed in %s after %d sends; "
                    "pane left at a bare shell",
                    names,
                    sends,
                )
                return
            sends += 1
            log.warning(
                "send-keys did not land in %s; re-sending (send %d of %d)",
                names,
                sends,
                _SEND_MAX_ATTEMPTS,
            )
            retry = list(pending.values())
            try:
                resends = [
                    subprocess.Popen(
                        _send_argv(psmux, w),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    for w in retry
                ]
            except OSError:
                # Spawning the retry itself failed -- same outcome as a pane
                # that never recovers (bare shell), so it reports at the same
                # level, with the traceback since this one is a host fault.
                log.exception("could not spawn a send-keys re-send for %s", names)
                return
            # A re-send that is killed may still land, so it is never followed
            # by another: that pane leaves the retry set.
            answered = await_clients(resends, _SEND_TIMEOUT_S)
            unsure = [
                w.window_name
                for w, rc in zip(retry, answered, strict=True)
                if rc is None
            ]
            if unsure:
                log.warning(
                    "send-keys re-send gave no answer within %gs for %s; not"
                    " sending to them again",
                    _SEND_TIMEOUT_S,
                    ", ".join(unsure),
                )
                pending = {n: w for n, w in pending.items() if n not in unsure}
                if not pending:
                    return

    @staticmethod
    def _decorate_batch(
        psmux: str, batch: list[PsmuxWindowOpts], code_hint: bool
    ) -> None:
        """Advertise the F1 (and, when truthful, F2) hints in a fresh batch.

        Fanned out as Popens like the creates/senders above -- each session is
        its own psmux server, so serializing two round-trips per session would
        add real time to a large bring-up. Purely cosmetic, so the whole thing
        is swallowed on error: a status bar must never fail a bring-up.

        ``code_hint`` is resolved once by the caller for the whole bring-up.
        """
        try:
            decorations = [
                subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                for w in batch
                for cmd in decoration_argv(w.window_name, psmux, code_hint, nick=w.nick)
            ]
        except OSError as exc:
            get_logger("platform").warning("status-line decoration failed: %s", exc)
            return
        unanswered = await_clients(decorations, SEND_KEYS_TIMEOUT_S).count(None)
        if unanswered:
            get_logger("platform").warning(
                "%d status-line decoration command(s) gave no answer within %gs;"
                " killed them",
                unanswered,
                SEND_KEYS_TIMEOUT_S,
            )

    def attach_psmux(
        self,
        session_name: str,
        title: str,
        color: str | None = None,
        config_path: str | None = None,
    ) -> None:
        psmux = find_psmux()
        if not psmux:
            return
        args = [
            "wt",
            "-w",
            "new",
            # In the literal, not appended: see launch_terminal. This pane is
            # the one that matters most -- a psmux attach hosts an agent for
            # days and re-renders its title constantly.
            "--suppressApplicationTitle",
            "--title",
            title,
        ]
        if color:
            args.extend(["--tabColor", color])
        args.extend(["--", psmux, "-L", session_name, "attach"])
        # heavy subsystem: in-body per policy (magent.env pulls pydantic in).
        from magent.env import attach_client_env

        # Not `spawn_child_env()` here, deliberately: this is the user-facing
        # ATTACH client, not a creation/control command. Attaching is the one
        # psmux operation where nesting is a real question rather than a false
        # alarm, and psmux's own guard is the right authority on it -- so the
        # nesting markers still are NOT stripped. `attach_client_env` removes
        # exactly one thing, and only when it can prove it was inherited: a
        # colour override an agent harness set for its own tool output, which
        # would otherwise render this window monochrome (the psmux client is
        # this pane's renderer and honours NO_COLOR). A human's own NO_COLOR
        # survives, and with no harness marker this is `env=None` -- the plain
        # inherited environment, exactly as before. See env.attach_client_env.
        subprocess.Popen(args, env=attach_client_env())

    def logon_session_is_interactive(self) -> bool:
        """False when this magent runs where no desktop can see it.

        Two independent signals, either of which is enough, because on Windows
        they name the same fact from opposite ends:

        * ``procs.current_session_id() == 0`` -- logon Session 0, the services
          session, which has no desktop composited onto any monitor.
        * ``env.is_ssh_login()`` -- Windows OpenSSH is a SERVICE, so every
          process it spawns for a login is in Session 0 by construction. This
          is a truthful signal about Windows, not a test hook: there is no
          Windows configuration in which an incoming ssh login lands on the
          interactive desktop. It is carried as well as the session id because
          the ctypes probe can answer None on a machine the environment can
          still speak plainly about.

        An UNKNOWN session id counts as interactive. A probe that fails on some
        future Windows must not be able to stop an ordinary desktop launch --
        the cost of a false "not interactive" is a user who cannot start their
        fleet, and the cost of a false "interactive" is the Session-0 fleet we
        already know how to detect afterwards.
        """
        # heavy subsystem: in-body per policy (magent.env pulls pydantic in).
        from magent.env import is_ssh_login

        if is_ssh_login():
            return False
        return current_session_id() != 0

    def supports_desktop_handoff(self) -> bool:
        """True when there is a logged-on desktop to hand work TO.

        Note what this does NOT do: pick a session. ``WTSGetActiveConsoleSessionId``
        can name an RDP session that is not the physical desktop the user is
        looking at, so every "find the interactive session" heuristic is wrong
        on some real machine. This reads one OS fact -- is any usable console
        session attached at all -- and leaves the PLACING to Task Scheduler's
        "run only when the user is logged on" trigger.

        False when nobody is logged on, which makes the disposition ``refuse``
        with a message that says so, rather than a hand-off that would sit
        waiting out its budget for a task Windows is never going to start.
        """
        session = active_console_session_id()
        return session is not None and session not in NO_CONSOLE_SESSION

    def _schtasks(
        self, exe: str, args: list[str]
    ) -> subprocess.CompletedProcess[str] | None:
        """One bounded, console-less schtasks call. None = it would not run.

        ``CREATE_NO_WINDOW`` for the same reason every psmux control spawn
        carries it: the caller is often a console-less process (a `serve`, an
        ssh command with no tty), and a console-subsystem child of one gets a
        brand-new Windows Terminal window it flashes on the user's desktop --
        which, on the hand-off path, is the very desktop we are trying not to
        disturb.
        """
        try:
            return subprocess.run(
                [exe, *args],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=_SCHTASKS_TIMEOUT_S,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def _schtasks_detail(
        self, phase: str, done: subprocess.CompletedProcess[str] | None, work: Path
    ) -> str:
        if done is None:
            return f"schtasks /{phase} would not run; scratch left at {work}"
        return (
            f"schtasks /{phase} exited {done.returncode}: "
            f"{_one_line(done.stderr) or _one_line(done.stdout)}; "
            f"scratch left at {work}"
        )

    def run_on_desktop(self, argv: list[str], *, timeout_s: float) -> HandoffResult:
        """Run ``argv`` in the logged-on user's session via Task Scheduler.

        Task Scheduler and not ``CreateProcessAsUser``: the API route needs a
        token from another session, which means ``SeTcbPrivilege`` -- i.e. an
        elevated magent -- for something the user is entitled to do to their own
        desktop. A one-shot ``/it`` ("run only when the user is logged on") task
        needs no stored credentials, no admin rights and no password, and it is
        WINDOWS that places the process in the interactive session rather than
        magent picking one. Measured on this machine from a real Session-0 sshd
        login: Session 1, Medium integrity, desktop visible, ~1.6s.

        The task runs a SCRIPT FILE, never the real command line: ``/TR`` is
        capped at ~261 characters and truncates silently past it, so a long
        ``--config`` path would otherwise have Task Scheduler run a different
        command than the one asked for.

        ``schtasks`` comes from ``_schtasks_exe()`` -- the system directory
        first, and the one seam the unit tier replaces with a recording fake.
        No test may create a real scheduled task.

        Never raises; every failure is an ``rc=None`` result whose ``detail``
        names the phase and, when something is worth looking at, the task name
        and the scratch directory it was left in.
        """
        log = get_logger("launch")
        schtasks = _schtasks_exe()
        if not schtasks:
            return HandoffResult(rc=None, detail="schtasks not found")

        nonce = uuid.uuid4().hex[:12]
        task = f"{_HANDOFF_TASK_PREFIX}{nonce}"
        work = Path(tempfile.gettempdir()) / _HANDOFF_DIR_NAME / nonce
        out = work / _handoff_launcher.OUT
        err = work / _handoff_launcher.ERR
        pid_file = work / _handoff_launcher.PID
        rc_file = work / _handoff_launcher.RC
        try:
            script = _stage_handoff(work, argv)
        except (OSError, UnicodeEncodeError) as exc:
            # A lone surrogate lands here: legal in a Windows path, but run.ps1
            # is UTF-8 and cannot hold one. Refused before anything runs, in
            # words that can themselves be printed and logged.
            return HandoffResult(
                rc=None,
                detail=_printable(f"could not stage the hand-off in {work}: {exc}"),
            )

        run_spec = f'{_HANDOFF_SHELL} "{script}"'
        if len(run_spec) > _TR_MAX_CHARS:
            return HandoffResult(
                rc=None,
                detail=(
                    f"/TR would be {len(run_spec)} characters and schtasks "
                    f"truncates at {_TR_MAX_CHARS}; scratch left at {work}"
                ),
            )

        log.info(
            "session-0 hand-off %s: %s",
            task,
            _printable(subprocess.list2cmdline(argv))[:500],
        )
        # `/sc once` demands a trigger, and `/run` fires the task now, so the
        # trigger time exists only to satisfy schtasks. `/st 00:00` is TODAY at
        # midnight -- already in the past, so the trigger can never fire on its
        # own. That matters on the one path `finally` cannot cover: a caller
        # killed mid-wait (an ssh drop takes the whole host-side `magent up`
        # with it) leaves the task registered, and a future-dated trigger would
        # re-run somebody's bring-up tonight. schtasks prints a "may not run
        # because /ST is earlier than current time" warning and exits 0
        # (measured); `/run` ignores the trigger entirely. `/it` is the whole
        # point -- "run only when the user is logged on" is what puts the
        # process in their interactive session, and it needs no stored
        # credentials to do it. `/f` makes a re-run idempotent rather than an
        # "already exists" failure.
        create_argv = ["/Create", "/F", "/TN", task, "/TR", run_spec]
        create_argv += ["/SC", "ONCE", "/ST", "00:00", "/IT"]
        try:
            created = self._schtasks(schtasks, create_argv)
            if created is None or created.returncode != 0:
                log.error("session-0 hand-off %s: create failed", task)
                return HandoffResult(
                    rc=None, detail=self._schtasks_detail("Create", created, work)
                )
            started = self._schtasks(schtasks, ["/Run", "/TN", task])
            if started is None or started.returncode != 0:
                log.error("session-0 hand-off %s: run failed", task)
                return HandoffResult(
                    rc=None, detail=self._schtasks_detail("Run", started, work)
                )
            result = self._await_handoff(
                schtasks, task, work, (out, err, pid_file, rc_file), timeout_s
            )
        finally:
            # Always, on every path, and it must not be able to raise: this
            # runs while the real error may already be propagating. A one-shot
            # task left behind is clutter in Task Scheduler and a name the next
            # hand-off cannot reuse (the past-dated trigger above is what keeps
            # it from ever FIRING).
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                self._schtasks(schtasks, ["/Delete", "/F", "/TN", task])
        log.info(
            "session-0 hand-off %s: rc=%s timed_out=%s %s",
            task,
            result.rc,
            result.timed_out,
            result.detail,
        )
        return result

    def _await_handoff(
        self,
        schtasks: str,
        task: str,
        work: Path,
        files: tuple[Path, Path, Path, Path],
        timeout_s: float,
    ) -> HandoffResult:
        """Poll for the launcher's exit code, bailing early on a task that
        never started or a child that died without writing one.

        ``pid.txt`` is the "it really started" signal -- the launcher writes it
        the moment its CreateProcess returns, before the command has produced a
        byte. Two different failures hide behind "no rc.txt yet", and both
        deserve a precise answer instead of the caller's whole budget spent in
        silence. No pid.txt after the start grace, from a task that is not
        running, means TASK SCHEDULER never ran it (nobody logged on, a policy
        refusal); a launcher that merely could not record its pid is still
        running, and its rc.txt still answers. A pid that is gone with no
        rc.txt means the LAUNCHER died mid-flight and nothing will ever write
        one.

        rc.txt existing is not the exit code being read (see
        ``_HANDOFF_RC_GRACE_S``): only a complete integer ends the wait, and
        an rc.txt that stays anything else past that grace -- or past the
        budget -- is a fourth answer of its own (``_settle_exit_code``).
        """
        out, err, pid_file, rc_file = files
        deadline = time.monotonic() + timeout_s
        start_deadline = time.monotonic() + _HANDOFF_START_GRACE_S
        checked_start = False
        gone_since: float | None = None
        rc_seen_since: float | None = None
        while True:
            rc = _read_recorded_int(rc_file)
            if rc is not None:
                return _handoff_finished(out, err, work, rc)
            # Read first, THEN check the budget: an exit code that landed
            # during the last sleep is the answer, not a timeout.
            if time.monotonic() >= deadline:
                break
            if rc_file.exists():
                # The file is there and a readable value is not yet. Not a
                # lost child either -- the launcher got as far as its exit
                # code -- so the pid checks below are moot, and running them
                # would be wrong: a child gone longer than the exit grace whose
                # rc.txt is only now becoming readable is the loaded-runner
                # success path, not a lost child.
                if rc_seen_since is None:
                    rc_seen_since = time.monotonic()
                waited = time.monotonic() - rc_seen_since
                if waited >= _HANDOFF_RC_GRACE_S:
                    return _settle_exit_code(files, task, work, waited)
                time.sleep(_HANDOFF_POLL_S)
                continue
            pid = _read_recorded_int(pid_file)
            if pid is not None and not pid_alive(pid):
                # It ran and is gone with no exit code. Usually the launcher
                # is a few milliseconds from writing one; only after the exit
                # grace is this a launcher that lost its child (a crash, a
                # kill) and nothing is coming.
                if gone_since is None:
                    gone_since = time.monotonic()
                if time.monotonic() - gone_since >= _HANDOFF_EXIT_GRACE_S:
                    return HandoffResult(
                        rc=None,
                        stdout=_read_handoff_text(out),
                        stderr=_read_handoff_text(err),
                        detail=(
                            f"the desktop command (pid {pid}) exited without an exit "
                            f"code; task {task}, scratch left at {work}"
                        ),
                    )
            if not checked_start and pid is None and time.monotonic() > start_deadline:
                checked_start = True
                query = self._schtasks(schtasks, ["/Query", "/TN", task])
                said = _one_line(query.stdout if query else "")
                # Only a task that is NOT running is abandoned. The status
                # column is localized, so this reads as "we could see it
                # running" rather than "we parsed the table".
                if "running" not in said.casefold():
                    return HandoffResult(
                        rc=None,
                        detail=(
                            "Task Scheduler never started the hand-off within "
                            f"{_HANDOFF_START_GRACE_S:.0f}s (is anyone logged "
                            f"on at the desktop?); schtasks /Query said "
                            f"{said!r}; task {task}, scratch left at {work}"
                        ),
                    )
            time.sleep(_HANDOFF_POLL_S)
        if rc_file.exists():
            # The budget ran out before rc.txt read as a number: the command
            # is done (rc.txt lands after wait() returns), so this is not "may
            # still be running" -- and the settling read may yet find the code
            # complete.
            waited = 0.0 if rc_seen_since is None else time.monotonic() - rc_seen_since
            return _settle_exit_code(files, task, work, waited)
        # Deliberately NO kill. A bring-up still running on the desktop past
        # our budget is doing the work that was asked for, and the pid we hold
        # is a number Windows recycles freely -- killing it could take out an
        # unrelated process of the user's. We stop waiting; we do not intervene.
        return HandoffResult(
            rc=None,
            timed_out=True,
            stdout=_read_handoff_text(out),
            stderr=_read_handoff_text(err),
            detail=(
                f"the desktop command wrote no exit code within {timeout_s:.0f}s "
                f"and may still be running; task {task}, scratch left at {work}"
            ),
        )

    def supports_psmux(self) -> bool:
        return True

    def supports_hotkey(self) -> bool:
        return True
