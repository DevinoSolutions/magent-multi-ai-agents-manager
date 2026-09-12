from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes
import shutil
import subprocess
import tempfile
import time
import uuid
from ctypes import POINTER, WINFUNCTYPE, byref, create_unicode_buffer, windll
from pathlib import Path
from typing import Literal

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
    find_psmux,
)
from magent.procs import current_session_id, spawn_unjobbed
from magent.psmux import (
    capture_pane,
    child_env,
    code_on_path,
    decoration_argv,
    is_idle_command,
    pane_current_commands,
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

# --- Session-0 desktop hand-off (see run_on_desktop) --------------------------
# Scratch root for one per-call directory holding the shim and its three result
# files. Under the system temp dir rather than ~/.magent because the hand-off
# has to work before any magent state exists, and because the directory is
# per-call and deleted on success.
_HANDOFF_DIR_NAME = "magent-handoff"
_HANDOFF_TASK_PREFIX = "magent-handoff-"
# How often the poll looks for rc.txt. Small enough that a hand-off of a fast
# command (a `serve --ensure` is ~1s) does not feel like a round trip.
_HANDOFF_POLL_S = 0.25
# How long the task gets to LEAVE "Ready" before we conclude it never started.
# Distinct from the caller's timeout: "the command is slow" and "Task Scheduler
# never ran it" are different answers, and only the second one is worth
# abandoning a 900s budget for.
_HANDOFF_START_GRACE_S = 5.0
# Every schtasks call itself is bounded -- create/run/query/delete are local and
# instant, so a hang is a wedge, not work.
_SCHTASKS_TIMEOUT_S = 30.0
# schtasks truncates /tr at 261 characters, which is why the task runs a SHIM
# FILE rather than the real command line: `magent up --config <long path>` blows
# through that limit trivially, and schtasks does not error -- it silently keeps
# a prefix, i.e. runs a different command.
_TR_MAX_CHARS = 261


def _handoff_shim(argv: list[str], cwd: str, out: Path, err: Path, rc: Path) -> str:
    """The batch file the scheduled task runs on the user's desktop.

    Three things it must do beyond running the command, all of them load-bearing:

    * ``cd /d`` back into the CALLER's directory. ``find_config`` walks up from
      the working directory, and a scheduled task starts in ``system32`` -- so a
      hand-off that skipped this would silently pick a different config than the
      command the user actually typed.
    * export ``MAGENT_SESSION0_POLICY=refuse`` for the child. If the hand-off
      somehow lands in Session 0 again (a service context we did not anticipate),
      the child refuses instead of handing off in turn: a recursion whose every
      level creates a scheduled task is not a failure anyone wants to debug.
    * write ``rc.txt`` LAST. It is the poll's completion signal, and the two
      output redirections are only closed when the command exits -- so a reader
      that sees rc.txt can never read a half-written out.txt.

    ``subprocess.list2cmdline`` builds the command line by the same MS C-runtime
    rules the child's own argv parser uses, so a path with spaces survives; the
    redirection filenames are quoted separately because cmd parses those itself.
    """
    return (
        "@echo off\r\n"
        'set "MAGENT_SESSION0_POLICY=refuse"\r\n'
        f'cd /d "{cwd}"\r\n'
        f'{subprocess.list2cmdline(argv)} > "{out}" 2> "{err}"\r\n'
        f'> "{rc}" echo %ERRORLEVEL%\r\n'
    )


def _one_line(text: str, limit: int = 200) -> str:
    """The last non-empty line of a tool's output, clipped -- diagnostics go in
    a single ``detail`` string, and schtasks answers in a multi-line table."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:limit] if lines else ""


def _read_handoff_text(path: Path) -> str:
    """Read one of the shim's output files; absent or unreadable reads empty.

    ``errors="replace"`` rather than a codepage guess: the child's console
    encoding is the machine's, this text is RELAYED to a human, and a mojibake
    character in a diagnostic is strictly better than losing the diagnostic to a
    UnicodeDecodeError.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace")
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

    def supports_attention_signals(self) -> bool:
        return True

    def supports_wt_keybindings(self) -> bool:
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
            args.extend(["--", "cmd", "/k", "ssh", "-t", opts.ssh_host, remote])
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

    def launch_psmux_session(self, windows: list[PsmuxWindowOpts]) -> None:
        psmux = find_psmux()
        if not psmux:
            raise FileNotFoundError("psmux not found on PATH")
        if not windows:
            return

        checks = [
            (
                w,
                subprocess.Popen(
                    # `-t <name>`: a bare has-session exits 0 even for a socket
                    # with no server, which made this dedupe skip creating every
                    # session on a cold machine. See psmux.has_session.
                    [psmux, "-L", w.window_name, "has-session", "-t", w.window_name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ),
            )
            for w in windows
        ]
        to_create = [w for w, p in checks if p.wait() != 0]

        if not to_create:
            return

        kills = [
            subprocess.Popen(
                [psmux, "-L", w.window_name, "kill-server"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for w in to_create
        ]
        for p in kills:
            p.wait()

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
            batch = []
            for w, p in zip(wave, creates, strict=True):
                if p.wait() == 0:
                    batch.append(w)
                else:
                    get_logger("platform").error(
                        "psmux new-session for %s exited %s; skipping it and"
                        " continuing the bring-up",
                        w.window_name,
                        p.returncode,
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
            # with no agent running.
            for p in senders:
                p.wait()

            # A send-keys that *exits 0* still proves nothing: the keystrokes
            # reached psmux, not necessarily the shell reading its console.
            self._verify_sends_landed(psmux, batch)

            self._decorate_batch(psmux, batch, code_hint)

    @staticmethod
    def _verify_sends_landed(psmux: str, batch: list[PsmuxWindowOpts]) -> None:
        """Re-type the agent command into any pane the send-keys never reached.

        Detection is the same primitive ``revive_sessions`` uses -- a pane
        whose ``#{pane_current_command}`` is a bare shell has no agent. The
        probe runs immediately before each re-send and is the ONLY guard
        against the dangerous edge: re-sending into a live agent would type
        the command text into its input box. So anything that is not a shell
        is left alone, and an empty/unreadable reading counts as "not a
        casualty" -- never inject into a pane whose state we could not
        establish (``psmux.agent_idle`` takes the same posture).

        Probed as one fan-out per round (``pane_current_commands``), not a
        round-trip per session: a full batch would otherwise serialize five.

        Never raises. A pane that stays bare through every attempt is logged
        and left as-is -- at worst exactly what it was before this pass -- so
        one stuck pane cannot cost the wave its remaining sessions.
        """
        log = get_logger("platform")
        pending = {w.window_name: w for w in batch}
        sends = 1  # the caller already typed the command once
        while True:
            time.sleep(_SEND_VERIFY_SETTLE_S)
            readings = pane_current_commands(list(pending), psmux=psmux)
            pending = {
                name: w
                for name, w in pending.items()
                if is_idle_command(readings.get(name, ""))
            }
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
            try:
                resends = [
                    subprocess.Popen(
                        _send_argv(psmux, w),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    for w in pending.values()
                ]
            except OSError:
                # Spawning the retry itself failed -- same outcome as a pane
                # that never recovers (bare shell), so it reports at the same
                # level, with the traceback since this one is a host fault.
                log.exception("could not spawn a send-keys re-send for %s", names)
                return
            for p in resends:
                p.wait()

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
                for cmd in decoration_argv(w.window_name, psmux, code_hint)
            ]
        except OSError as exc:
            get_logger("platform").warning("status-line decoration failed: %s", exc)
            return
        for p in decorations:
            p.wait()

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
        # No `env=child_env()` here, deliberately: this is the user-facing
        # ATTACH client, not a creation/control command. Attaching is the one
        # psmux operation where nesting is a real question rather than a false
        # alarm, and psmux's own guard is the right authority on it. Stripping
        # the markers here would be magent overriding a warning meant for the
        # human, and it buys nothing -- the spawn goes through `wt`, which the
        # markers do not concern.
        subprocess.Popen(args)

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
        return True

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

        The task runs a shim FILE, never the real command line: ``/tr`` is
        capped at 261 characters and truncates silently past it.

        ``schtasks`` is resolved with ``shutil.which`` rather than out of the
        system directory. That is a deliberate trade: it makes PATH part of this
        function's trust boundary, and it is the seam that lets the unit tier
        prove the whole create/run/poll/delete choreography against a fake
        binary instead of writing real scheduled tasks on a developer's box --
        which is the only way this code can be tested at all.

        Never raises; every failure is an ``rc=None`` result whose ``detail``
        names the phase and, when something is worth looking at, the scratch
        directory it was left in.
        """
        log = get_logger("launch")
        schtasks = shutil.which("schtasks")
        if not schtasks:
            return HandoffResult(rc=None, detail="schtasks not found")

        nonce = uuid.uuid4().hex[:12]
        task = f"{_HANDOFF_TASK_PREFIX}{nonce}"
        work = Path(tempfile.gettempdir()) / _HANDOFF_DIR_NAME / nonce
        shim = work / "run.cmd"
        out, err, rc_file = work / "out.txt", work / "err.txt", work / "rc.txt"
        try:
            work.mkdir(parents=True, exist_ok=True)
            shim.write_text(
                _handoff_shim(argv, str(Path.cwd()), out, err, rc_file),
                encoding="utf-8",
            )
        except OSError as exc:
            return HandoffResult(
                rc=None, detail=f"could not stage the hand-off in {work}: {exc}"
            )

        run_spec = f'cmd /c "{shim}"'
        if len(run_spec) > _TR_MAX_CHARS:
            return HandoffResult(
                rc=None,
                detail=(
                    f"/tr would be {len(run_spec)} characters and schtasks "
                    f"truncates at {_TR_MAX_CHARS}; scratch left at {work}"
                ),
            )

        log.info("session-0 hand-off %s: %s", task, subprocess.list2cmdline(argv)[:500])
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
        create_argv = ["/create", "/tn", task, "/tr", run_spec]
        create_argv += ["/sc", "once", "/st", "00:00", "/it", "/f"]
        try:
            created = self._schtasks(schtasks, create_argv)
            if created is None or created.returncode != 0:
                log.error("session-0 hand-off %s: create failed", task)
                return HandoffResult(
                    rc=None, detail=self._schtasks_detail("create", created, work)
                )
            started = self._schtasks(schtasks, ["/run", "/tn", task])
            if started is None or started.returncode != 0:
                log.error("session-0 hand-off %s: run failed", task)
                return HandoffResult(
                    rc=None, detail=self._schtasks_detail("run", started, work)
                )
            result = self._await_handoff(
                schtasks, task, work, (out, err, rc_file), timeout_s
            )
        finally:
            # Always, on every path: a one-shot task left behind is clutter in
            # Task Scheduler and a name the next hand-off cannot reuse (the
            # past-dated trigger above is what keeps it from ever FIRING).
            self._schtasks(schtasks, ["/delete", "/tn", task, "/f"])
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
        files: tuple[Path, Path, Path],
        timeout_s: float,
    ) -> HandoffResult:
        """Poll for the shim's ``rc.txt``, bailing early if it never started.

        ``out.txt`` is the "it is actually running" signal: cmd creates it the
        moment it opens the redirection, before the command produces a byte. So
        "no out.txt after the start grace" means Task Scheduler did not run the
        task -- nobody logged on, a policy refusal -- and that deserves its own
        answer rather than burning the caller's whole budget in silence.
        """
        out, err, rc_file = files
        deadline = time.monotonic() + timeout_s
        start_deadline = time.monotonic() + _HANDOFF_START_GRACE_S
        checked_start = False
        while time.monotonic() < deadline:
            if rc_file.exists():
                stdout, stderr = _read_handoff_text(out), _read_handoff_text(err)
                raw = _read_handoff_text(rc_file).strip()
                try:
                    rc = int(raw)
                except ValueError:
                    return HandoffResult(
                        rc=None,
                        stdout=stdout,
                        stderr=stderr,
                        detail=(
                            f"the desktop shim wrote an unreadable exit code "
                            f"{raw!r}; scratch left at {work}"
                        ),
                    )
                # The hand-off itself worked, whatever the command decided.
                shutil.rmtree(work, ignore_errors=True)
                return HandoffResult(rc=rc, stdout=stdout, stderr=stderr)
            if (
                not checked_start
                and not out.exists()
                and time.monotonic() > start_deadline
            ):
                checked_start = True
                query = self._schtasks(schtasks, ["/query", "/tn", task])
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
                            f"on at the desktop?); schtasks /query said "
                            f"{said!r}; scratch left at {work}"
                        ),
                    )
            time.sleep(_HANDOFF_POLL_S)
        return HandoffResult(
            rc=None,
            timed_out=True,
            stdout=_read_handoff_text(out),
            stderr=_read_handoff_text(err),
            detail=(
                f"the desktop command wrote no exit code within {timeout_s:.0f}s "
                f"and may still be running; scratch left at {work}"
            ),
        )

    def supports_psmux(self) -> bool:
        return True

    def supports_hotkey(self) -> bool:
        return True
