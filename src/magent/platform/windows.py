from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes
import subprocess
import time
from ctypes import POINTER, WINFUNCTYPE, byref, create_unicode_buffer, windll
from typing import Literal

from magent.grid import MonitorRect, Rect
from magent.log import get_logger
from magent.platform import (
    WT_NOT_FOUND_MESSAGE,
    Platform,
    PsmuxWindowOpts,
    TerminalLaunchOpts,
    TerminalNotFoundError,
    VSCodeLaunchOpts,
    find_psmux,
)
from magent.psmux import (
    capture_pane,
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
            "-d",
            opts.cwd,
            "--title",
            opts.title,
        ]
        if opts.color:
            args.extend(["--tabColor", opts.color])
        args.append("--suppressApplicationTitle")

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

        try:
            subprocess.Popen(args)
        except FileNotFoundError as exc:
            # wt is a hard dependency: turn the raw FileNotFoundError into a
            # typed, actionable error the launch shell surfaces as one clean
            # line (never a traceback). We fail fast -- no console fallback.
            raise TerminalNotFoundError(WT_NOT_FOUND_MESSAGE) from exc

    def launch_vscode(self, opts: VSCodeLaunchOpts) -> None:
        args = ["cmd", "/c", opts.command]
        if opts.ssh_host:
            args.extend(["--remote", f"ssh-remote+{opts.ssh_host}"])
        args.append(opts.dir)
        subprocess.Popen(args)

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
                    [psmux, "-L", w.window_name, "has-session"],
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
            batch = to_create[start : start + _BRING_UP_BATCH]
            if start:
                time.sleep(_BRING_UP_BATCH_PAUSE_S)

            creates = [
                subprocess.Popen(
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
                )
                for w in batch
            ]
            for p in creates:
                if p.wait() != 0:
                    raise subprocess.CalledProcessError(p.returncode, p.args)

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
            "--title",
            title,
        ]
        if color:
            args.extend(["--tabColor", color])
        args.append("--suppressApplicationTitle")
        args.extend(["--", psmux, "-L", session_name, "attach"])
        subprocess.Popen(args)

    def supports_psmux(self) -> bool:
        return True

    def supports_hotkey(self) -> bool:
        return True
