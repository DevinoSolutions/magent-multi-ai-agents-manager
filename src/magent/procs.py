"""Process-liveness leaf: the one owner of "is this pid alive?".

Before this module, cli/background.py and hotkey.py each carried a private
``_pid_alive`` (P1-09) -- the hotkey copy existed only because hotkey.py
raises ImportError off-Windows, so nothing importable-from-anywhere owned the
check. Like paths.py / titles.py / tailnet.py this is a true leaf:
stdlib-only, no dependency on any magent module, importable by cli
commands, subsystems, and the win32-only hotkey module alike.
"""

from __future__ import annotations

import os
import sys


def pid_alive(pid: int | None) -> bool:
    """Portable best-effort liveness check for a pid (None/0/negative: dead)."""
    if not pid or pid < 0:
        return False
    if sys.platform == "win32":
        import ctypes  # win-only: ctypes.windll doesn't exist off Windows

        k = ctypes.windll.kernel32
        handle = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            k.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    else:
        return True
