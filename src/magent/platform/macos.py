from __future__ import annotations

import json
import shutil
import subprocess
from typing import Literal

from magent.grid import MonitorRect, Rect
from magent.log import get_logger
from magent.platform import Platform, TerminalLaunchOpts, VSCodeLaunchOpts

_log = get_logger("platform")

SWIFT_MONITORS = """\
import AppKit
import Foundation
var monitors: [[String: Any]] = []
for (i, screen) in NSScreen.screens.enumerated() {
    let f = screen.frame
    let v = screen.visibleFrame
    monitors.append([
        "x": Int(v.origin.x), "y": Int(v.origin.y),
        "w": Int(v.size.width), "h": Int(v.size.height),
        "full_h": Int(f.size.height),
        "is_primary": i == 0,
        "scale": screen.backingScaleFactor,
    ])
}
let data = try! JSONSerialization.data(withJSONObject: monitors)
print(String(data: data, encoding: .utf8)!)
"""


class MacOSPlatform(Platform):
    def set_dpi_aware(self) -> None:
        pass

    def list_monitors(self) -> list[MonitorRect]:
        if not shutil.which("swift"):
            return []
        try:
            result = subprocess.run(
                ["swift", "-e", SWIFT_MONITORS],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            _log.warning(
                "list_monitors: %s timed out after %ss; treating as no monitors",
                "swift",
                30,
            )
            return []
        if result.returncode != 0 or not result.stdout.strip():
            return []
        raw = json.loads(result.stdout)
        monitors: list[MonitorRect] = []
        for m in raw:
            full_h = m["full_h"]
            y_top = full_h - m["y"] - m["h"]
            monitors.append(
                MonitorRect(
                    x=m["x"],
                    y=y_top if y_top >= 0 else m["y"],
                    w=m["w"],
                    h=m["h"],
                    is_primary=m["is_primary"],
                    scale_factor=m["scale"],
                )
            )
        return monitors

    def find_window(
        self, title: str, mode: Literal["exact", "contains"] = "exact"
    ) -> dict[str, str] | None:
        if mode not in ("exact", "contains"):
            raise ValueError(f"unknown find_window mode: {mode!r}")
        script = """
        tell application "System Events"
            set windowList to {}
            repeat with proc in (every process whose visible is true)
                repeat with w in (every window of proc)
                    set end of windowList to {procName:name of proc, winName:name of w}
                end repeat
            end repeat
        end tell
        return windowList
        """
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        for line in result.stdout.strip().split(", "):
            parts = line.split(":")
            if len(parts) < 2:
                continue
            win_name = parts[-1].strip()
            proc_name = parts[0].strip()
            if mode == "exact" and win_name == title:
                return {"process": proc_name, "window": win_name}
            if mode == "contains" and title.lower() in win_name.lower():
                return {"process": proc_name, "window": win_name}
        return None

    def snapshot_windows(self) -> dict[str, object]:
        # {title: {"process","window"}} for every visible window in one System
        # Events pass -- the handle shape move_window consumes, and the resolver
        # the launch-path tiling (tiling.place_windows) calls. Without this
        # override the ABC default `{}` made tiling a silent no-op on macOS.
        # Each record is emitted `proc<TAB>title` on its own line so a title
        # containing ':' or ', ' (every 'magent:' window) parses intact -- unlike
        # find_window's comma/colon split. Requires Automation permission for
        # the invoking process; when TCC blocks it osascript fails and this
        # returns {} (tiling then no-ops, matching the pre-override default).
        script = """
        set out to {}
        tell application "System Events"
            repeat with proc in (every process whose visible is true)
                set procName to name of proc
                try
                    repeat with w in (every window of proc)
                        try
                            set end of out to procName & tab & (name of w)
                        end try
                    end repeat
                end try
            end repeat
        end tell
        set AppleScript's text item delimiters to linefeed
        return out as text
        """
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        titles: dict[str, object] = {}
        for line in result.stdout.splitlines():
            if "\t" not in line:
                continue
            proc_name, win_name = line.split("\t", 1)
            win_name = win_name.strip()
            if win_name:
                titles[win_name] = {"process": proc_name.strip(), "window": win_name}
        return titles

    def move_window(self, handle: object, rect: Rect) -> None:
        if not isinstance(handle, dict):
            return
        proc = handle.get("process")
        win = handle.get("window")
        if not isinstance(proc, str) or not isinstance(win, str):
            return
        script = f"""
        tell application "System Events"
            tell process "{proc}"
                set position of window "{win}" to {{{rect.x}, {rect.y}}}
                set size of window "{win}" to {{{rect.w}, {rect.h}}}
            end tell
        end tell
        """
        subprocess.run(["osascript", "-e", script], timeout=10, check=False)

    def launch_terminal(self, opts: TerminalLaunchOpts) -> None:
        """Open one project window, titled with magent's grammar.

        Title ownership (the same concern as the Windows
        ``--suppressApplicationTitle`` flag and the Linux backend's per-emulator
        locks): every branch below already uses the stickiest channel the app
        offers, and none of the three can be made airtight from the outside.

        * kitty -- ``--title`` permanently fixes the OS window title (documented
          kitty behavior). This one IS a lock.
        * iTerm -- ``set name of session`` is the explicit session name, which
          iTerm prefers over the app-supplied title; but the displayed title is
          composed from the profile's Title Components, so a profile that
          includes the app title can still show one. HONEST GAP, no per-launch
          lever.
        * Terminal.app -- ``custom title`` is the sticky channel (an app OSC
          title cannot clear it), but the profile's Window settings decide
          whether the process name is appended alongside it. HONEST GAP for the
          exact-string match tiling wants.

        macOS also has no attention backend (``supports_attention_signals()`` is
        False), so unlike Windows there is no reassertion pass to repair a
        stomped title. Tracked in DESIGN.md's known-debt ledger.

        Environment ownership splits the same three ways, and for the same
        reason -- how much of the child does this Popen actually own?

        * kitty -- spawned directly, so it takes ``env.spawn_child_env()`` and
          the window really does start without the launching shell's agent
          session markers or colour overrides.
        * iTerm / Terminal.app -- HONEST GAP. The Popen here spawns
          ``osascript``; the tab is created by an already-running,
          launchd-started app whose environment no caller can reach, so an
          ``env=`` on this Popen would look like a fix and be a no-op. Passing
          it anyway would be worse than not passing it, so it is left off
          deliberately and named here instead.
        """
        # heavy subsystem: in-body per policy (magent.env pulls pydantic in).
        from magent.env import spawn_child_env

        if opts.ssh_host:
            remote_dir = opts.ssh_remote_dir or opts.cwd
            inner = f"cd {remote_dir} && {opts.command}"
            if opts.ssh_shell:
                cmd = f"ssh -t {opts.ssh_host} \"{opts.ssh_shell} '{inner}'\""
            else:
                cmd = f'ssh -t {opts.ssh_host} "{inner}"'
        else:
            cmd = f"cd {opts.cwd} && {opts.command}"

        if shutil.which("kitty"):
            args = [
                "kitty",
                "--title",
                opts.title,
                "--directory",
                opts.cwd,
                "sh",
                "-c",
                cmd,
            ]
            subprocess.Popen(args, env=spawn_child_env())
        elif self._has_app("iTerm"):
            script = f"""
            tell application "iTerm"
                create window with default profile command "cd {opts.cwd} && {cmd}"
                tell current session of current window
                    set name to "{opts.title}"
                end tell
            end tell
            """
            subprocess.Popen(["osascript", "-e", script])
        else:
            script = f"""
            tell application "Terminal"
                do script "cd {opts.cwd} && {cmd}"
                set custom title of front window to "{opts.title}"
            end tell
            """
            subprocess.Popen(["osascript", "-e", script])

    def launch_vscode(self, opts: VSCodeLaunchOpts) -> None:
        # heavy subsystem: in-body per policy (magent.env pulls pydantic in).
        from magent.env import spawn_child_env

        args = [opts.command]
        if opts.ssh_host:
            args.extend(["--remote", f"ssh-remote+{opts.ssh_host}"])
        args.append(opts.dir)
        # An IDE window is an agent host too -- its integrated terminal is where
        # a user runs `claude`, and it inherits the editor's environment.
        subprocess.Popen(args, env=spawn_child_env())

    @staticmethod
    def _has_app(name: str) -> bool:
        result = subprocess.run(
            [
                "mdfind",
                f"kMDItemKind == 'Application' && kMDItemDisplayName == '{name}'",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return bool(result.stdout.strip())
