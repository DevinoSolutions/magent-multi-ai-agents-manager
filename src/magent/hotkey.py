"""Global window listener for the magent: windows on this machine.

Three jobs, all gated on the same thing -- the focused window's title parsing
as a magent: title (titles.parse_title), never a hand-rolled prefix check:

  * Alt+V  -- upload the clipboard image into that project's psmux pane.
  * F2     -- open that project's folder in VS Code (locally or over Remote-SSH).
  * focus  -- re-assert that window's geometry to psmux when it gains focus,
              so the session renders at THIS machine's size (see _focus_decide).

Alt+V and F2 ride a low-level keyboard hook; the focus job rides a
SetWinEventHook on EVENT_SYSTEM_FOREGROUND. Both are pumped by the one message
loop in run_hotkey.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from magent.log import (
    HEARTBEAT_INTERVAL,
    clear_heartbeat,
    get_logger,
    write_heartbeat,
)
from magent.procs import pid_alive
from magent.sessions import (
    build_code_open_command,
    build_flash_url,
    folder_for_session,
)
from magent.titles import parse_title

if TYPE_CHECKING:
    from collections.abc import Callable

if sys.platform != "win32":
    raise ImportError("hotkey module is Windows-only")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.GetClipboardData.restype = ctypes.c_void_p
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalSize.argtypes = [ctypes.c_void_p]

user32.CallNextHookEx.argtypes = [
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
]
user32.CallNextHookEx.restype = ctypes.c_long

user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.wintypes.DWORD,
]
user32.SetWindowsHookExW.restype = ctypes.c_void_p

user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]

user32.SetWinEventHook.argtypes = [
    ctypes.wintypes.DWORD,  # eventMin
    ctypes.wintypes.DWORD,  # eventMax
    ctypes.c_void_p,  # hmodWinEventProc
    ctypes.c_void_p,  # pfnWinEventProc
    ctypes.wintypes.DWORD,  # idProcess
    ctypes.wintypes.DWORD,  # idThread
    ctypes.wintypes.DWORD,  # dwFlags
]
user32.SetWinEventHook.restype = ctypes.c_void_p

user32.UnhookWinEvent.argtypes = [ctypes.c_void_p]

user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short

kernel32.OpenProcess.argtypes = [
    ctypes.wintypes.DWORD,
    ctypes.wintypes.BOOL,
    ctypes.wintypes.DWORD,
]
kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
kernel32.GetExitCodeProcess.argtypes = [
    ctypes.wintypes.HANDLE,
    ctypes.POINTER(ctypes.wintypes.DWORD),
]
kernel32.GetExitCodeProcess.restype = ctypes.wintypes.BOOL
kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]

VK_V = 0x56
# F2 opens the focused project's folder in VS Code. No modifier: the magent:
# window title is the gate, exactly as it is for Alt+V, and F-keys are free in
# an agent pane (psmux itself owns F1 -> detach-client, see psmux.decorate_session).
VK_F2 = 0x71
VK_MENU = 0x12
VK_LMENU = 0xA4
VK_RMENU = 0xA5
# A low-level keyboard hook reports the physical Alt as VK_LMENU/VK_RMENU,
# never the generic VK_MENU -- match all three or Alt is never detected.
_ALT_KEYS = (VK_MENU, VK_LMENU, VK_RMENU)
# Mouse buttons, read through GetAsyncKeyState: a button held down means the
# user is mid-drag/mid-resize on the window, and the focus nudge must not fight
# them for it (see _focus_decide).
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
_MOUSE_BUTTONS = (VK_LBUTTON, VK_RBUTTON)
_KEY_DOWN_MASK = 0x8000
CF_DIB = 8
BI_BITFIELDS = 3
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
HC_ACTION = 0

# Foreground-change accessibility event, delivered out-of-context (i.e. posted
# to this process's message queue rather than injected into the foreground one,
# which a pure-Python listener cannot host).
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000

# How long a project's window is left alone after a focus nudge. Alt-tabbing
# around a wall of magent: windows must not turn into a storm of resizes: one
# reclaim per window per quarter-minute is plenty to keep the geometry this
# machine's, and cheap enough to be invisible.
FOCUS_NUDGE_DEBOUNCE_S = 15.0

HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long,
    ctypes.c_int,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
)

WINEVENTPROC = ctypes.WINFUNCTYPE(
    None,
    ctypes.c_void_p,  # hWinEventHook
    ctypes.wintypes.DWORD,  # event
    ctypes.c_void_p,  # hwnd
    ctypes.c_long,  # idObject
    ctypes.c_long,  # idChild
    ctypes.wintypes.DWORD,  # idEventThread
    ctypes.wintypes.DWORD,  # dwmsEventTime
)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.wintypes.DWORD),
        ("scanCode", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


def window_title(hwnd: int) -> str:
    """The title of one window by handle, or "" if it has none.

    Split out of ``get_active_window_title`` because the foreground event hook
    is handed the HWND directly -- re-querying GetForegroundWindow from the
    callback would race the very event being reported.
    """
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def get_active_window_title() -> str:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    return window_title(hwnd)


def clipboard_has_image() -> bool:
    if not user32.OpenClipboard(None):
        return False
    try:
        return bool(user32.IsClipboardFormatAvailable(CF_DIB))
    finally:
        user32.CloseClipboard()


def get_clipboard_image() -> bytes | None:
    if not user32.OpenClipboard(None):
        return None
    try:
        if not user32.IsClipboardFormatAvailable(CF_DIB):
            return None
        handle = user32.GetClipboardData(CF_DIB)
        if not handle:
            return None

        size = kernel32.GlobalSize(handle)
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            dib_data = bytearray(ctypes.string_at(ptr, size))
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()

    return _dib_to_bmp(dib_data)


_DIB_HEADER_SIZES = frozenset({40, 52, 56, 108, 124})  # BITMAPINFOHEADER..V5
_DIB_BPP = frozenset({1, 4, 8, 16, 24, 32})


def _dib_to_bmp(dib: bytearray) -> bytes | None:
    """Wrap a clipboard DIB in a BMP file header.

    Two things the naive `14 + header_size` offset gets wrong:
      * BI_BITFIELDS DIBs (what GDI / .NET / many screenshot tools produce)
        store 3 color masks (12 bytes) between a BITMAPINFOHEADER and the
        pixels. Skipping them points the pixel offset 12 bytes too early, so
        decoders read masks as pixels and the image renders as garbage/black.
      * 32bpp DIBs usually carry an all-zero alpha channel. BMP's 4th byte is
        officially reserved, but many decoders honor it as alpha -> the whole
        image is treated as transparent and composites to black.
    """
    if len(dib) < 40:
        return None
    header_size = int.from_bytes(dib[0:4], "little")
    bpp = int.from_bytes(dib[14:16], "little")
    compression = int.from_bytes(dib[16:20], "little")
    clr_used = int.from_bytes(dib[32:36], "little")

    # Reject malformed/hostile headers before the offset arithmetic below,
    # which can otherwise overflow a 4-byte field (OverflowError) on a
    # clipboard payload we don't control.
    if header_size not in _DIB_HEADER_SIZES or bpp not in _DIB_BPP or clr_used > 256:
        return None

    extra = 0
    # Masks only trail a plain BITMAPINFOHEADER; V4/V5 headers embed them.
    if compression == BI_BITFIELDS and header_size == 40:
        extra += 12
    if bpp <= 8:
        extra += (clr_used or (1 << bpp)) * 4

    px_start = header_size + extra
    if px_start > len(dib):  # pixel offset past the buffer -> malformed
        return None

    if bpp == 32 and px_start < len(dib):
        n = len(range(px_start + 3, len(dib), 4))
        dib[px_start + 3 :: 4] = b"\xff" * n

    file_size = 14 + len(dib)
    offset = 14 + px_start
    bmp_header = (
        b"BM"
        + file_size.to_bytes(4, "little")
        + b"\x00\x00\x00\x00"
        + offset.to_bytes(4, "little")
    )
    return bmp_header + bytes(dib)


def project_from_title(title: str) -> str | None:
    parsed = parse_title(title)
    return parsed[0] if parsed is not None else None


def upload_image(server_url: str, project: str, image_data: bytes) -> bool:
    boundary = "----MagentUpload"
    delim = f"--{boundary}"
    body = (
        (
            f"{delim}\r\n"
            f'Content-Disposition: form-data; name="project"\r\n'
            f"\r\n"
            f"{project}\r\n"
            f"{delim}\r\n"
            f'Content-Disposition: form-data; name="inject"\r\n'
            f"\r\n"
            f"1\r\n"
            f"{delim}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="clipboard.bmp"\r\n'
            f"Content-Type: image/bmp\r\n"
            f"\r\n"
        ).encode()
        + image_data
        + f"\r\n{delim}--\r\n".encode()
    )

    # ?project= lets the server flash "uploading" in the magent:<project> status line
    # the moment the request lands -- before it reads the image bytes -- so the
    # feedback shows in the same window you pasted into.
    req = Request(
        f"{server_url}/upload?project={quote(project)}",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read())
            return bool(result.get("ok", False)) if isinstance(result, dict) else False
    except (URLError, OSError, json.JSONDecodeError) as exc:
        # Log the specific cause (server down vs. 20s timeout vs. malformed
        # response) -- the caller only sees the bare False, so without this the
        # reason a paste failed is unrecoverable from the logs (P2-07).
        get_logger("hotkey").warning(
            "upload transport error (%s): %s", type(exc).__name__, exc
        )
        return False


# --- Background-listener lifecycle -------------------------------------------
# attach starts the Alt+V listener hidden in the background (no terminal of its
# own), because its progress now shows in the magent: windows. A pid file lets
# `magent status` report it and `magent down --all` stop it.
#
# Beside the pid file the listener writes a manifest describing itself -- the
# magent version it runs and the (server_url, ssh_host) target it was wired to.
# Without it the listener is opaque: `launch.start_hotkey_listener` sees a live
# pid and keeps it, so an old process survives a pip upgrade running old code
# (F2 silently dead), and a locally-wired listener blocks the remote-wired one
# `magent attach` wants (F2 opens the wrong thing). The manifest is what makes
# "is this listener still the one I want?" answerable.

_PID_PATH = Path.home() / ".magent" / "hotkey.pid"
_MANIFEST_PATH = Path.home() / ".magent" / "hotkey.json"


def listener_pid() -> int | None:
    """PID of the running Alt+V listener, or None. Clears a stale pid file."""
    try:
        pid = int(_PID_PATH.read_text().strip())
    except (OSError, ValueError):
        return None
    if pid_alive(pid):
        return pid
    with contextlib.suppress(OSError):
        _PID_PATH.unlink()
    return None


def stop_listener() -> bool:
    """Stop the running Alt+V listener. Returns True only if the kill actually
    succeeded. On failure the pid file is kept (not unlinked) so `status` or a
    retry can still find the process."""
    log = get_logger("hotkey")
    pid = listener_pid()
    if not pid:
        return False
    result = subprocess.run(
        ["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False
    )
    if result.returncode != 0:
        log.warning("taskkill pid %d failed rc=%d", pid, result.returncode)
        return False
    with contextlib.suppress(OSError):
        _PID_PATH.unlink()
    _clear_manifest()
    # taskkill /F gives the listener no chance to run run_hotkey's finally, so
    # its heartbeat file would otherwise outlive it and read as a listener that
    # crashed rather than one that was deliberately stopped.
    clear_heartbeat("hotkey")
    return True


def listener_manifest() -> dict[str, str | None] | None:
    """What the running listener was started with -- ``version``, ``server_url``
    and ``ssh_host`` -- or None when there is no readable manifest.

    None is also what every pre-3.6.0 listener yields (it wrote no manifest at
    all), which callers must read as "too old to describe itself", i.e. stale.
    """
    try:
        raw = json.loads(_MANIFEST_PATH.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None

    def _text(key: str) -> str | None:
        value = raw.get(key)
        return value if isinstance(value, str) else None

    return {
        "version": _text("version"),
        "server_url": _text("server_url"),
        "ssh_host": _text("ssh_host"),
    }


def _write_pid() -> None:
    try:
        _PID_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PID_PATH.write_text(str(os.getpid()))
    except OSError:
        pass


def _write_manifest(server_url: str, ssh_host: str | None) -> None:
    """Self-describe: record the version and target this listener runs with.

    Best-effort like ``_write_pid``: an unwritable manifest reads back as None,
    which only makes a later starter restart the listener -- never worse than
    the pre-manifest behavior.
    """
    from magent import __version__  # PEP 562 lazy; only paid by a real listener

    try:
        _MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        _MANIFEST_PATH.write_text(
            json.dumps(
                {
                    "version": __version__,
                    "server_url": server_url,
                    "ssh_host": ssh_host,
                }
            )
        )
    except OSError:
        pass


def _clear_pid() -> None:
    try:
        if _PID_PATH.read_text().strip() == str(os.getpid()):
            _PID_PATH.unlink()
    except OSError:
        pass


def _clear_manifest() -> None:
    with contextlib.suppress(OSError):
        _MANIFEST_PATH.unlink()


# Every Alt+V press ends in exactly one line carrying this prefix, so the whole
# history of the chord is one grep:
#
#     grep ALTV ~/.magent/logs/hotkey.log
#
# The listener runs hidden with no terminal of its own, so without a record like
# this a press that goes nowhere is indistinguishable from a press that was
# never delivered -- which is precisely the "we don't know when it doesn't work"
# problem. Outcomes are a closed vocabulary (ALTV_OUTCOMES) so the log stays
# greppable per-outcome, not just per-prefix.
ALTV_LOG_PREFIX = "ALTV"

ALTV_OUTCOMES = (
    "ok",  # image uploaded and injected
    "not-a-magent-window",  # pass-through: the chord was not ours to handle
    "no-image",  # magent window focused, but the clipboard holds no image
    "clipboard-unreadable",  # CF_DIB said yes, the read came back empty
    "upload-rejected",  # the server answered, and said no
    "error",  # anything unforeseen, with a traceback in the log
)


def _altv_report(
    server_url: str, project: str, outcome: str, message: str | None = None
) -> None:
    """Record one Alt+V outcome, and show the failing ones to the user.

    ``message`` (a failure) goes to the magent:<project> status line through the
    same flash channel F2 already uses -- no new notification subsystem, and no
    dependence on the listener having a console. The log line is written either
    way and is the durable record; the flash is best-effort by construction
    (``_flash_status`` swallows everything) because feedback must never be able
    to break the action it reports on.
    """
    log = get_logger("hotkey")
    if message is None:
        log.info("%s outcome=%s project=%s", ALTV_LOG_PREFIX, outcome, project)
        return
    log.warning(
        "%s outcome=%s project=%s: %s", ALTV_LOG_PREFIX, outcome, project, message
    )
    _flash_status(server_url, project, f"Alt+V: {message}")


def _do_upload(server_url: str, project: str) -> None:
    """Run upload in a thread so the hook callback returns quickly.

    Progress on the happy path shows in the magent:<project> window's own status
    line, driven by the upload server (see upload_server._flash). Every FAILURE
    path reports itself through ``_altv_report``: a press that silently does
    nothing is the worst outcome available here, because the user cannot tell it
    from a listener that is not running at all.
    """
    try:
        image_data = get_clipboard_image()
        if not image_data:
            # clipboard_has_image() said yes at the hook, so this is a real
            # read failure (a format we cannot decode, or a race with another
            # app taking the clipboard), not an empty clipboard.
            _altv_report(
                server_url, project, "clipboard-unreadable", "could not read the image"
            )
            return
        if upload_image(server_url, project, image_data):
            _altv_report(server_url, project, "ok")
        else:
            _altv_report(
                server_url,
                project,
                "upload-rejected",
                "upload failed - is `magent serve` running?",
            )
    except Exception:  # noqa: BLE001  # reason: this runs on a detached background thread with no console; anything that escapes here would vanish, so everything is logged and shown instead
        get_logger("hotkey").exception(
            "%s outcome=error project=%s", ALTV_LOG_PREFIX, project
        )
        _flash_status(server_url, project, "Alt+V: upload error - see hotkey.log")


def _flash_status(server_url: str, project: str, message: str) -> None:
    """Best-effort: show ``message`` in the magent:<project> status line.

    The listener runs hidden with no terminal, so without this every F2 outcome
    is invisible on screen -- the user sees a key that does nothing while the
    real reason sits in hotkey.log. The whole call is swallowed on purpose:
    feedback must never be able to break the action it reports on, and the log
    line beside each call site stays the durable record.
    """
    with (
        contextlib.suppress(Exception),
        urlopen(build_flash_url(server_url, project, message), timeout=2),
    ):
        pass


def _do_open_code(server_url: str, project: str, ssh_host: str | None) -> None:
    """Open the focused project's folder in VS Code, off the hook callback.

    Threaded for the same reason as ``_do_upload``: the /api/sessions round
    trip must never block a system-wide keyboard hook. Every failure mode --
    server down, project absent from the payload, no folder on the entry, no
    ``code`` on PATH -- is a log line, a status-line flash, and a no-op; the
    listener has to outlive all of them.
    """
    log = get_logger("hotkey")
    try:
        _flash_status(server_url, project, "F2: opening VS Code...")
        code_bin = shutil.which("code")
        if not code_bin:
            log.warning("F2: 'code' is not on PATH; cannot open project=%s", project)
            _flash_status(server_url, project, "F2: 'code' not found on PATH")
            return
        with urlopen(f"{server_url}/api/sessions", timeout=10) as resp:
            payload = json.loads(resp.read())
        folder = folder_for_session(payload, project)
        if not folder:
            log.warning("F2: no folder for project=%s in /api/sessions", project)
            _flash_status(
                server_url,
                project,
                f"F2: no folder known for {project} (host magent too old?)",
            )
            return
        # code is code.cmd on Windows; shutil.which resolves the .cmd and
        # Popen on that resolved path runs it without a shell.
        subprocess.Popen(build_code_open_command(folder, ssh_host, code_bin))
        log.info(
            "F2: opened project=%s folder=%s ssh_host=%s", project, folder, ssh_host
        )
        _flash_status(server_url, project, f"F2: VS Code -> {folder}")
    except Exception:
        log.exception("F2: open project=%s failed", project)
        _flash_status(server_url, project, "F2: failed - see hotkey.log")


# --- Focus-driven geometry reclaim -------------------------------------------
# psmux 3.3.6 arbitrates no geometry of its own: a session renders at whatever
# the LAST client resize-or-attach event reported and nothing server-side ever
# recomputes it -- its `window-size` options are unimplemented, a client
# detaching or dying releases nothing, and the server-side resize commands are
# no-ops (`resize-window` is worse: a no-op that HANGS under `window-size
# manual`, so magent never calls any of them). `magent attach` already reclaims
# the geometry once, at attach time (cli/attach._reclaim_geometry) -- but the
# moment the OTHER machine resizes its window, it takes the session back and
# this machine's windows render squeezed again until the next attach.
#
# So the listener keeps reclaiming: when a magent: window GAINS FOCUS, nudge it.
# The window you are actually looking at therefore always ends up carrying this
# machine's geometry, with no config surface and nothing for the user to run.


def _mouse_button_down() -> bool:
    """True while a mouse button is physically held down.

    The probe that keeps the nudge off a window the user has hold of: a drag or
    an edge-resize is exactly a stream of foreground/geometry activity, and
    moving the window out from under the pointer mid-gesture would be the one
    way this feature could be worse than the stale size it fixes.
    """
    return any(user32.GetAsyncKeyState(vk) & _KEY_DOWN_MASK for vk in _MOUSE_BUTTONS)


def _focus_decide(
    title: str,
    last_nudge: dict[str, float],
    now: float,
    mouse_down: Callable[[], bool] = _mouse_button_down,
) -> str | None:
    """Pure decision for one foreground-change event: the project whose window
    should be re-nudged, or None to skip.

    Extracted out of the callback itself for the same reason as
    ``_hook_decide``: the code that runs on every foreground change
    system-wide has to be reachable from a unit test without a live hook.
    ``last_nudge`` carries the per-project debounce stamps across calls (and is
    stamped here, so the decision and its record cannot drift apart);
    ``mouse_down`` is injected so the probe can be faked.

    Skips, in order: a window that is not one of ours (the titles grammar is
    the gate, exactly as it is for Alt+V and F2), a project nudged less than
    ``FOCUS_NUDGE_DEBOUNCE_S`` ago, and a window the user has hold of. Only the
    debounce skip stamps anything -- a mouse-down skip leaves the stamp alone,
    so the very next focus event retries instead of waiting out the window.
    """
    project = project_from_title(title)
    if project is None:
        return None
    previous = last_nudge.get(project)
    if previous is not None and now - previous < FOCUS_NUDGE_DEBOUNCE_S:
        return None
    if mouse_down():
        return None
    last_nudge[project] = now
    return project


def _do_nudge(hwnd: int, project: str) -> None:
    """Re-assert one window's geometry to psmux, off the hook callback.

    Threaded for the same reason as ``_do_upload``/``_do_open_code``: the nudge
    sleeps through a settle beat (platform.nudge_windows) and a foreground-event
    callback must never block. The resize logic itself is NOT reimplemented here
    -- it is the one platform primitive that `magent attach` reclaims with, so
    both paths keep the same delta, settle and restore-exact-rect behavior.
    """
    log = get_logger("hotkey")
    try:
        # heavy subsystem: in-body per policy (and keeps the listener's import
        # graph identical to what it was before this hook existed).
        from magent.platform import get_platform

        plat = get_platform()
        if not plat.supports_window_nudge():
            return
        nudged = plat.nudge_windows([hwnd])
        log.info("focus nudge project=%s hwnd=%s nudged=%d", project, hwnd, nudged)
    except Exception:
        log.exception("focus nudge project=%s failed", project)


def _make_win_event_proc(
    last_nudge: dict[str, float],
) -> Callable[[int, int, int, int, int, int, int], None]:
    """Wrap ``_focus_decide`` so an uncaught exception can never break the
    event hook -- the same guard, for the same ctypes-callback reason, as
    ``_make_hook_proc`` gives the keyboard hook: a Python exception cannot
    cross the C boundary, so without this a bad event would dump a traceback to
    a hidden daemon's invisible stderr instead of into hotkey.log.
    """
    log = get_logger("hotkey")

    def win_event_proc(
        _hook: int,
        _event: int,
        hwnd: int,
        _id_object: int,
        _id_child: int,
        _thread_id: int,
        _timestamp: int,
    ) -> None:
        try:
            if not hwnd:
                return
            project = _focus_decide(window_title(hwnd), last_nudge, time.monotonic())
            if project is None:
                return
            threading.Thread(
                target=_do_nudge, args=(hwnd, project), daemon=True
            ).start()
        except Exception:
            log.exception("foreground-event callback error")

    return win_event_proc


def _heartbeat_loop(stop_event: threading.Event) -> None:
    """Pulse a liveness heartbeat on an interval until told to stop.

    Runs on its own daemon thread -- GetMessageW blocks when idle, so a
    heartbeat cannot live inside the message loop. This proves the loop is
    alive, not that Alt+V itself works; per-keystroke callback failures are
    E8's `log.exception` in hook_proc, not this heartbeat.
    """
    while not stop_event.is_set():
        write_heartbeat("hotkey")
        stop_event.wait(HEARTBEAT_INTERVAL)


def _hook_decide(
    state: dict[str, bool],
    server_url: str,
    nCode: int,
    wParam: int,
    lParam: int,
    ssh_host: str | None = None,
) -> int:
    """Pure decision logic for one keyboard-hook callback. Extracted out of
    the hook callback itself so it's reachable from a unit test without a
    live hook + message loop; `state` carries `alt_held` across calls the
    way the old closure's `nonlocal` did."""
    if nCode != HC_ACTION:
        return int(user32.CallNextHookEx(None, nCode, wParam, lParam))

    kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents

    if kb.vkCode in _ALT_KEYS:
        state["alt_held"] = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
        return int(user32.CallNextHookEx(None, nCode, wParam, lParam))

    if kb.vkCode == VK_F2 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
        project = project_from_title(get_active_window_title())
        if project is None:
            return int(user32.CallNextHookEx(None, nCode, wParam, lParam))
        threading.Thread(
            target=_do_open_code, args=(server_url, project, ssh_host), daemon=True
        ).start()
        return 1

    if (
        kb.vkCode == VK_V
        and state["alt_held"]
        and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
    ):
        title = get_active_window_title()
        project = project_from_title(title)

        if project is None:
            # A pass-through, not a failure: Alt+V outside a magent: window is
            # the other app's chord. Recorded at DEBUG (with the title, which is
            # the whole diagnosis when a user swears they WERE focused on one)
            # -- at INFO this would log every Alt+V the user ever presses.
            get_logger("hotkey").debug(
                "%s outcome=not-a-magent-window title=%r",
                ALTV_LOG_PREFIX,
                title,
            )
            return int(user32.CallNextHookEx(None, nCode, wParam, lParam))

        if not clipboard_has_image():
            # Pass the chord through (the pane may want a plain Alt+V), but say
            # why nothing was uploaded. Pressing it in the right window and
            # getting silence is the exact complaint this exists to answer.
            # Threaded because the report does a socket round-trip and this is
            # a system-wide keyboard hook.
            threading.Thread(
                target=_altv_report,
                args=(
                    server_url,
                    project,
                    "no-image",
                    "clipboard has no image - copy one first",
                ),
                daemon=True,
            ).start()
            return int(user32.CallNextHookEx(None, nCode, wParam, lParam))

        threading.Thread(
            target=_do_upload, args=(server_url, project), daemon=True
        ).start()
        return 1

    return int(user32.CallNextHookEx(None, nCode, wParam, lParam))


def _make_hook_proc(
    state: dict[str, bool], server_url: str, ssh_host: str | None = None
) -> Callable[[int, int, int], int]:
    """Wrap `_hook_decide` so an uncaught exception can never break the hook
    chain. A ctypes WINFUNCTYPE callback can't propagate a Python exception
    across the C boundary -- CPython prints the traceback and returns the
    restype default instead, which skips CallNextHookEx for that event and
    sends the traceback to a hidden daemon's invisible stderr. This wrap
    logs the exception and still always calls CallNextHookEx."""
    log = get_logger("hotkey")

    def hook_proc(nCode: int, wParam: int, lParam: int) -> int:
        try:
            return _hook_decide(state, server_url, nCode, wParam, lParam, ssh_host)
        except Exception:
            log.exception("Alt+V hook callback error")
            return int(
                user32.CallNextHookEx(None, nCode, wParam, lParam)
            )  # chain never broken

    return hook_proc


def run_hotkey(server_url: str, ssh_host: str | None = None) -> None:
    """Run the window listener until the message loop ends.

    Installs both hooks the module needs -- the low-level keyboard hook behind
    Alt+V and F2, and the EVENT_SYSTEM_FOREGROUND hook behind the focus geometry
    reclaim -- and lets one GetMessageW loop pump them until it ends, then
    unhooks both.

    ``ssh_host`` is the SSH target the magent: windows are attached to, if any:
    it makes F2 open the project through VS Code Remote-SSH instead of looking
    for the folder on this machine. None (the local case) opens it locally.
    """
    log = get_logger("hotkey")
    state = {"alt_held": False}
    hook_fn = HOOKPROC(_make_hook_proc(state, server_url, ssh_host))

    hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, hook_fn, None, 0)
    if not hook:
        raise RuntimeError("Failed to install keyboard hook")

    # The focus hook is a bonus, not the product: a listener that installed the
    # keyboard hook already earns its keep, so a SetWinEventHook that refuses
    # degrades to "no continuous reclaim" (attach-time reclaim still works) and
    # never costs the user Alt+V or F2. Both ctypes trampolines are locals of
    # this frame on purpose -- they must outlive the hooks that call them.
    event_fn = WINEVENTPROC(_make_win_event_proc({}))
    event_hook: object = None
    try:
        event_hook = user32.SetWinEventHook(
            EVENT_SYSTEM_FOREGROUND,
            EVENT_SYSTEM_FOREGROUND,
            None,
            event_fn,
            0,
            0,
            WINEVENT_OUTOFCONTEXT,
        )
    except OSError:
        log.exception("focus geometry reclaim: SetWinEventHook raised")
    if not event_hook:
        log.warning("focus geometry reclaim disabled: no foreground event hook")

    # Record the pid only once the hook is actually installed, so a failed
    # listener never leaves a pid file claiming it's running. The manifest
    # rides with it: pid says "alive", manifest says "and this is what I am".
    _write_pid()
    _write_manifest(server_url, ssh_host)
    stop_event = threading.Event()
    threading.Thread(target=_heartbeat_loop, args=(stop_event,), daemon=True).start()
    log.info("listener started")
    msg = ctypes.wintypes.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        stop_event.set()
        user32.UnhookWindowsHookEx(hook)
        if event_hook:
            user32.UnhookWinEvent(event_hook)
        _clear_pid()
        _clear_manifest()
        log.info("listener stopped")
