from __future__ import annotations

import re
import shutil
import subprocess
from typing import Literal

from magent.grid import MonitorRect, Rect
from magent.log import get_logger
from magent.platform import Platform, TerminalLaunchOpts, VSCodeLaunchOpts

_log = get_logger("platform")


class LinuxPlatform(Platform):
    def set_dpi_aware(self) -> None:
        pass

    def list_monitors(self) -> list[MonitorRect]:
        if not shutil.which("xrandr"):
            return []
        try:
            result = subprocess.run(
                ["xrandr", "--query"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except subprocess.TimeoutExpired:
            _log.warning(
                "list_monitors: %s timed out after %ss; treating as no monitors",
                "xrandr",
                10,
            )
            return []
        monitors: list[MonitorRect] = []
        is_first = True
        for line in result.stdout.splitlines():
            match = re.match(
                r"^(\S+)\s+connected\s+(primary\s+)?(\d+)x(\d+)\+(\d+)\+(\d+)",
                line,
            )
            if not match:
                continue
            _name, primary, w, h, x, y = match.groups()
            w, h, x, y = int(w), int(h), int(x), int(y)

            scale = 1.0
            size_match = re.search(r"(\d+)mm x (\d+)mm", line)
            if size_match:
                phys_w_mm = int(size_match.group(1))
                if phys_w_mm > 0:
                    dpi = w / (phys_w_mm / 25.4)
                    scale = round(dpi / 96.0, 2)

            monitors.append(
                MonitorRect(
                    x=x,
                    y=y,
                    w=w,
                    h=h,
                    is_primary=primary is not None
                    or (is_first and not any(m.is_primary for m in monitors)),
                    scale_factor=max(1.0, scale),
                )
            )
            is_first = False

        return monitors

    def find_window(
        self, title: str, mode: Literal["exact", "contains"] = "exact"
    ) -> str | None:
        if mode not in ("exact", "contains"):
            raise ValueError(f"unknown find_window mode: {mode!r}")
        if shutil.which("xdotool"):
            pattern = f"^{re.escape(title)}$" if mode == "exact" else re.escape(title)
            result = subprocess.run(
                ["xdotool", "search", "--name", pattern],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            wids = result.stdout.strip().splitlines()
            if wids:
                return wids[0]

        if shutil.which("wmctrl"):
            result = subprocess.run(
                ["wmctrl", "-l"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            for line in result.stdout.splitlines():
                parts = line.split(None, 3)
                if len(parts) < 4:
                    continue
                wid, _, _, win_title = parts
                if mode == "exact" and win_title == title:
                    return wid
                if mode == "contains" and title.lower() in win_title.lower():
                    return wid

        return None

    def snapshot_windows(self) -> dict[str, object]:
        # {title: window-id} for every managed window, in one pass. `wmctrl -l`
        # emits `<id> <desktop> <host> <title>`; that id round-trips straight
        # back into move_window's `wmctrl -i -r`. This is the resolver the
        # launch-path tiling (tiling.place_windows) calls -- without it the ABC
        # default `{}` made every window "not found" and tiling a silent no-op
        # on Linux. Needs a running window manager to populate the EWMH client
        # list; with none, wmctrl returns nothing and the xdotool fallback (or,
        # failing that, `{}`) takes over.
        titles: dict[str, object] = {}
        if shutil.which("wmctrl"):
            result = subprocess.run(
                ["wmctrl", "-l"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            for line in result.stdout.splitlines():
                parts = line.split(None, 3)
                if len(parts) < 4:
                    continue
                wid, _, _, win_title = parts
                titles[win_title] = wid
        if titles:
            return titles

        # WM-less / empty-client-list fallback: enumerate visible named windows
        # via xdotool (works without EWMH). Decimal xdotool ids also parse under
        # `wmctrl -i -r` (strtoul base 0), so they round-trip into move_window.
        if shutil.which("xdotool"):
            search = subprocess.run(
                ["xdotool", "search", "--onlyvisible", "--name", "."],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            for wid in search.stdout.split():
                name = subprocess.run(
                    ["xdotool", "getwindowname", wid],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                win_title = name.stdout.strip()
                if win_title:
                    titles[win_title] = wid
        return titles

    def move_window(self, handle: object, rect: Rect) -> None:
        if not handle:
            return
        if shutil.which("wmctrl"):
            subprocess.run(
                [
                    "wmctrl",
                    "-i",
                    "-r",
                    str(handle),
                    "-e",
                    f"0,{rect.x},{rect.y},{rect.w},{rect.h}",
                ],
                timeout=5,
                check=False,
            )

    def launch_terminal(self, opts: TerminalLaunchOpts) -> None:
        if opts.ssh_host:
            remote_dir = opts.ssh_remote_dir or opts.cwd
            inner = f"cd {remote_dir} && {opts.command}"
            if opts.ssh_shell:
                cmd = f"ssh -t {opts.ssh_host} \"{opts.ssh_shell} '{inner}'\""
            else:
                cmd = f'ssh -t {opts.ssh_host} "{inner}"'
        else:
            cmd = opts.command

        if shutil.which("kitty"):
            subprocess.Popen(
                [
                    "kitty",
                    "--title",
                    opts.title,
                    "--directory",
                    opts.cwd,
                    "sh",
                    "-c",
                    cmd,
                ]
            )
        elif shutil.which("alacritty"):
            subprocess.Popen(
                [
                    "alacritty",
                    "--title",
                    opts.title,
                    "--working-directory",
                    opts.cwd,
                    "-e",
                    "sh",
                    "-c",
                    cmd,
                ]
            )
        elif shutil.which("gnome-terminal"):
            subprocess.Popen(
                [
                    "gnome-terminal",
                    f"--title={opts.title}",
                    f"--working-directory={opts.cwd}",
                    "--",
                    "sh",
                    "-c",
                    cmd,
                ]
            )
        elif shutil.which("konsole"):
            subprocess.Popen(
                [
                    "konsole",
                    "--title",
                    opts.title,
                    "--workdir",
                    opts.cwd,
                    "-e",
                    "sh",
                    "-c",
                    cmd,
                ]
            )
        elif shutil.which("xterm"):
            subprocess.Popen(
                ["xterm", "-T", opts.title, "-e", f"cd {opts.cwd} && {cmd}"]
            )
        else:
            raise RuntimeError(
                "No supported terminal emulator found. Install one of: kitty, alacritty, gnome-terminal, konsole, xterm"
            )

    def launch_vscode(self, opts: VSCodeLaunchOpts) -> None:
        args = [opts.command]
        if opts.ssh_host:
            args.extend(["--remote", f"ssh-remote+{opts.ssh_host}"])
        args.append(opts.dir)
        subprocess.Popen(args)
