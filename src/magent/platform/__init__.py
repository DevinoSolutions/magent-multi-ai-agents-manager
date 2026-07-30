from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from magent.psmux import PsmuxWindowOpts, find_psmux

if TYPE_CHECKING:
    from magent.grid import MonitorRect, Rect

__all__ = ["PsmuxWindowOpts", "find_psmux"]


@dataclass
class TerminalLaunchOpts:
    title: str
    cwd: str
    command: str
    color: str | None = None
    ssh_host: str | None = None
    ssh_remote_dir: str | None = None
    ssh_shell: str = "bash -lc"


@dataclass
class VSCodeLaunchOpts:
    dir: str
    ssh_host: str | None = None
    command: str = "code"


# The install instruction shared by every "Windows Terminal is missing" surface,
# so the guidance the user sees stays identical wherever it appears: it is
# composed into WT_NOT_FOUND_MESSAGE (raised on the launch path) and reused
# verbatim by cli/doctor.py's terminal check.
WT_INSTALL_HINT = (
    "winget install Microsoft.WindowsTerminal (or from the Microsoft Store)"
)

WT_NOT_FOUND_MESSAGE = (
    "Windows Terminal (wt) not found — magent needs it to launch project "
    f"windows. Install: {WT_INSTALL_HINT}, then re-run."
)


class TerminalNotFoundError(RuntimeError):
    """The OS terminal emulator magent shells out to for project windows is
    not installed / not on PATH. Its message is user-facing and actionable (an
    install hint); the launch path prints that message in place of a raw
    traceback and fails fast -- there is no degraded-window fallback."""


class Platform(ABC):
    @abstractmethod
    def set_dpi_aware(self) -> None: ...

    @abstractmethod
    def list_monitors(self) -> list[MonitorRect]: ...

    @abstractmethod
    def find_window(
        self, title: str, mode: Literal["exact", "contains"] = "exact"
    ) -> object | None: ...

    @abstractmethod
    def move_window(self, handle: object, rect: Rect) -> None: ...

    @abstractmethod
    def launch_terminal(self, opts: TerminalLaunchOpts) -> None: ...

    @abstractmethod
    def launch_vscode(self, opts: VSCodeLaunchOpts) -> None: ...

    def snapshot_windows(self) -> dict[str, object]:
        """Return {title: handle} for all visible windows in a single pass."""
        return {}

    def launch_psmux_session(self, windows: list[PsmuxWindowOpts]) -> None:
        raise NotImplementedError("psmux is only supported on Windows")

    def attach_psmux(
        self,
        session_name: str,
        title: str,
        color: str | None = None,
        config_path: str | None = None,
    ) -> None:
        raise NotImplementedError("psmux is only supported on Windows")

    def supports_psmux(self) -> bool:
        """True if this platform can run persistent psmux sessions."""
        return False

    def supports_hotkey(self) -> bool:
        """True if this platform can run the Alt+V clipboard-image listener."""
        return False

    def supports_attention_signals(self) -> bool:
        """True if this platform can badge titles / flash / focus windows."""
        return False

    def set_window_title(self, handle: object, title: str) -> bool:
        """Rewrite a window's title in place. False = unsupported or failed."""
        return False

    def supports_window_nudge(self) -> bool:
        """True if this platform can force a terminal window to re-emit a
        client resize event (see ``nudge_windows``)."""
        return False

    def nudge_windows(self, handles: list[object]) -> int:
        """Make each window emit a resize event, then restore its exact rect.

        A terminal only tells whatever it hosts (a psmux client, and over SSH
        the remote one via SIGWINCH) about its character grid when the OS
        window is resized. psmux 3.3.6 has no size arbitration of its own: a
        session renders at whatever the LAST client resize-or-attach event
        reported, and nothing server-side ever recomputes it -- so a session
        another machine sized stays squeezed for this one until a local client
        resize says otherwise. This is the one lever that always works.

        Implementations resize by a delta guaranteed to cross a character
        cell, pause long enough for the terminal to propagate it, then put the
        window back exactly where it was. Best-effort per window; returns how
        many were actually nudged. The ABC default is a no-op.
        """
        return 0

    def flash_window(self, handle: object) -> bool:
        """Flash the window's taskbar presence to request the user's attention."""
        return False

    def focus_window(self, handle: object) -> bool:
        """Restore (if minimized) and bring the window to the foreground."""
        return False


def get_platform() -> Platform:
    if sys.platform == "win32":
        from magent.platform.windows import WindowsPlatform

        return WindowsPlatform()
    if sys.platform == "darwin":
        from magent.platform.macos import MacOSPlatform

        return MacOSPlatform()
    from magent.platform.linux import LinuxPlatform

    return LinuxPlatform()
