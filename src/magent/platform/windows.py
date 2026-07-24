from __future__ import annotations

import ctypes
import ctypes.wintypes
import subprocess
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

user32 = windll.user32
shcore = windll.shcore


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
            for w in to_create
        ]
        for p in creates:
            if p.wait() != 0:
                raise subprocess.CalledProcessError(p.returncode, p.args)

        for w in to_create:
            subprocess.Popen(
                [
                    psmux,
                    "-L",
                    w.window_name,
                    "send-keys",
                    "-t",
                    w.window_name,
                    f"cmd /c {w.command}",
                    "Enter",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
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
