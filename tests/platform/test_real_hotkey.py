"""REAL Alt+V hotkey chain: the actual global keyboard hook, driven by real
synthesized input, moving a real clipboard image into a real psmux session.

The unit suite drives ``_hook_decide`` with fabricated KBDLLHOOKSTRUCTs; it
never proves the shipping chain. This test does, with zero fakes:

* the listener is started EXACTLY the way the product starts it -- the same
  ``python -m magent hotkey -s <server_url>`` argv that
  ``cli/background._maybe_start_hotkey`` spawns -- so the real
  ``SetWindowsHookExW(WH_KEYBOARD_LL)`` hook + message loop are live;
* the upload server is the real ``magent serve`` subprocess, validating the
  project against a LIVE psmux session (``discover_sessions``);
* the foreground window is a REAL wt window opened by the product attach path
  (``attach_psmux``), titled through ``titles.make_title`` -- the exact title
  the hook's ``GetForegroundWindow``/``parse_title`` resolution reads;
* the clipboard carries a REAL CF_DIB image placed via
  OpenClipboard/SetClipboardData;
* the chord is REAL input: ``SendInput`` Alt-down, V-down, V-up, Alt-up (a
  WH_KEYBOARD_LL hook receives injected input; the product does not filter
  LLKHF_INJECTED, so this exercises the true keystroke path).

Product-visible effects asserted:

1. the DIB lands as a BMP file in the server's ``~/.magent/uploads/`` --
   byte-for-byte the BMP the product's DIB->BMP wrapper must produce for a
   32bpp BI_RGB DIB (file header + alpha bytes forced opaque);
2. the uploaded file's path is INJECTED into the live psmux session
   (``send_keys``), observed via a real ``capture-pane``;
3. the listener's own log records ``ok=True`` for the project.

Gating (never on a dev machine): SendInput fires GLOBAL keystrokes and the
test stomps the machine clipboard, so this runs only under
``MDTEST_INTERACTION=1`` -- set exclusively by the dedicated windows-latest CI
step (same posture as needs_ssh / monitor_lab). Focus quirks are expected on
hosted runners: every chord attempt re-asserts foreground and the chord is
retried before failing.

Real HOME, by necessity (empirically pinned in CI, run 29437354149): psmux
resolves its ``-L`` socket namespace from USERPROFILE/HOME, so a serve child
with a redirected HOME looks in a DIFFERENT socket dir and reports the live
session down (``discover_sessions`` -> "Unknown project"). The chain only
works when the session creator, the serve process, and the attach client all
share one profile -- which is exactly production reality (one user). So this
test uses the runner's REAL home for every party, mirroring the SSH attach
flagship's posture: additionally gated on ``GITHUB_ACTIONS=true`` /
``MDTEST_ALLOW_REAL_HOME=1``, uuid-namespaced artifacts, and the chord proof
is a NEW uploads file (pre-chord snapshot delta) with byte-exact content.
MAGENT_* vars are still stripped from every child.
"""

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.request import urlopen

import pytest

from magent import psmux

pytestmark = [
    pytest.mark.interaction,
    pytest.mark.skipif(
        os.environ.get("MDTEST_INTERACTION") != "1",
        reason="real-input interaction tier is CI-only (MDTEST_INTERACTION=1): "
        "it sends global keystrokes and overwrites the machine clipboard",
    ),
    pytest.mark.skipif(sys.platform != "win32", reason="Alt+V hotkey is win32-only"),
    pytest.mark.skipif(
        shutil.which("wt") is None, reason="Windows Terminal (wt) not on PATH"
    ),
    pytest.mark.skipif(psmux.find_psmux() is None, reason="psmux not installed"),
    # The chain must run against the REAL home (psmux sockets are per-profile;
    # see module docstring) -- same extra gate as the SSH attach flagship.
    pytest.mark.skipif(
        os.environ.get("GITHUB_ACTIONS") != "true"
        and os.environ.get("MDTEST_ALLOW_REAL_HOME") != "1",
        reason="touches the real HOME (uploads/pid/logs); CI or "
        "MDTEST_ALLOW_REAL_HOME=1 only",
    ),
]

_WM_CLOSE = 0x0010
_CF_DIB = 8


def _child_env() -> dict[str, str]:
    """The real environment minus ambient MAGENT_* vars. HOME is NOT
    redirected -- psmux sockets are per-profile (see module docstring)."""
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("MAGENT_")}


def _wait_until(check, timeout: float, interval: float = 0.5):
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            return result
        time.sleep(interval)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- real Win32 primitives (defined lazily: module must import on POSIX) -----


def _make_dib() -> tuple[bytes, bytes]:
    """A real 4x4 32bpp BI_RGB DIB and the exact BMP the product must write.

    The product's DIB->BMP wrapper (hotkey._dib_to_bmp) prepends a 14-byte BMP
    file header and forces every 4th (alpha) byte opaque for 32bpp DIBs. The
    pixels here already carry 0xFF alpha, so expected == header + dib
    unchanged -- an independent hand-built expectation, not a call into the
    code under test.
    """
    width = height = 4
    pixels = b"".join(
        bytes((16 * i % 256, 32 * i % 256, 48 * i % 256, 0xFF))
        for i in range(width * height)
    )
    header = (
        (40).to_bytes(4, "little")
        + width.to_bytes(4, "little")
        + height.to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + (32).to_bytes(2, "little")
        + (0).to_bytes(4, "little")  # BI_RGB
        + len(pixels).to_bytes(4, "little")
        + b"\x00" * 16  # x/y ppm, clr used, clr important
    )
    dib = header + pixels
    file_size = 14 + len(dib)
    offset = 14 + 40
    bmp = (
        b"BM"
        + file_size.to_bytes(4, "little")
        + b"\x00\x00\x00\x00"
        + offset.to_bytes(4, "little")
        + dib
    )
    return dib, bmp


def _set_clipboard_dib(dib: bytes) -> None:
    import ctypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

    GMEM_MOVEABLE = 0x0002
    opened = _wait_until(lambda: bool(user32.OpenClipboard(None)), timeout=10)
    assert opened, "could not open the clipboard"
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(dib))
        assert handle, "GlobalAlloc failed"
        ptr = kernel32.GlobalLock(handle)
        assert ptr, "GlobalLock failed"
        ctypes.memmove(ptr, dib, len(dib))
        kernel32.GlobalUnlock(handle)
        # Ownership passes to the clipboard on success; never freed by us.
        assert user32.SetClipboardData(_CF_DIB, handle), "SetClipboardData failed"
    finally:
        user32.CloseClipboard()


def _clear_clipboard() -> None:
    import ctypes

    user32 = ctypes.windll.user32
    if user32.OpenClipboard(None):
        user32.EmptyClipboard()
        user32.CloseClipboard()


def _send_alt_v() -> None:
    """Synthesize the real Alt+V chord with SendInput (no new deps)."""
    import ctypes
    import ctypes.wintypes
    from typing import ClassVar

    ULONG_PTR = ctypes.wintypes.WPARAM

    class KEYBDINPUT(ctypes.Structure):
        _fields_: ClassVar = [
            ("wVk", ctypes.wintypes.WORD),
            ("wScan", ctypes.wintypes.WORD),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("time", ctypes.wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_: ClassVar = [("ki", KEYBDINPUT), ("padding", ctypes.c_byte * 32)]

    class INPUT(ctypes.Structure):
        _fields_: ClassVar = [("type", ctypes.wintypes.DWORD), ("union", _INPUTUNION)]

    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    VK_MENU, VK_V = 0x12, 0x56

    def _key(vk: int, flags: int) -> INPUT:
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki = KEYBDINPUT(vk, 0, flags, 0, 0)
        return inp

    user32 = ctypes.windll.user32
    for step in (
        _key(VK_MENU, 0),
        _key(VK_V, 0),
        _key(VK_V, KEYEVENTF_KEYUP),
        _key(VK_MENU, KEYEVENTF_KEYUP),
    ):
        sent = user32.SendInput(1, ctypes.byref(step), ctypes.sizeof(INPUT))
        assert sent == 1, "SendInput was blocked"
        time.sleep(0.05)


def _foreground_title() -> str:
    import ctypes

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def _force_foreground(plat, hwnd, title: str) -> bool:
    """Bring ``hwnd`` to the foreground and confirm the hook would see it.

    Hosted runners have foreground-lock quirks; try the product's own
    focus_window first, then the AttachThreadInput escalation.
    """
    import ctypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    plat.focus_window(hwnd)
    if _wait_until(lambda: _foreground_title() == title, timeout=2, interval=0.2):
        return True

    # Escalation: attach our input queue to the foreground thread's so
    # SetForegroundWindow is permitted, per the classic Win32 recipe.
    fg = user32.GetForegroundWindow()
    if fg:
        fg_thread = user32.GetWindowThreadProcessId(fg, None)
        our_thread = kernel32.GetCurrentThreadId()
        user32.AttachThreadInput(our_thread, fg_thread, True)
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            user32.AttachThreadInput(our_thread, fg_thread, False)
    return bool(
        _wait_until(lambda: _foreground_title() == title, timeout=2, interval=0.2)
    )


def _capture_pane(name: str) -> str:
    binary = psmux.find_psmux()
    assert binary is not None
    try:
        result = subprocess.run(
            [binary, "-L", name, "capture-pane", "-p", "-t", name],
            capture_output=True,
            timeout=5,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout or ""


def _http_ok(url: str, needle: str) -> bool:
    with contextlib.suppress(OSError), urlopen(url, timeout=5) as resp:
        return needle in resp.read().decode("utf-8", errors="replace")
    return False


def _read_log(home, logname: str) -> str:
    log_file = home / ".magent" / "logs" / f"{logname}.log"
    try:
        return log_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "<no log file>"


def _tail(path, limit: int = 2000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return "<no output file>"


def test_real_alt_v_uploads_clipboard_image_into_live_session(tmp_path):
    import ctypes

    from magent.platform import get_platform
    from magent.psmux import PsmuxWindowOpts
    from magent.titles import make_title

    plat = get_platform()
    unique = uuid.uuid4().hex[:10]
    name = f"mdhk{unique}"  # already a valid psmux session name (no . : space)
    title = make_title(name)
    port = _free_port()
    server_url = f"http://127.0.0.1:{port}"

    proj = tmp_path / f"proj-{unique}"
    proj.mkdir()
    home = Path.home()  # REAL home: psmux sockets are per-profile (docstring)
    env = _child_env()
    cfg = tmp_path / "magent.config.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 3,
                "layout": {"columns": 1, "rows": 1},
                "settings": {
                    "defaultTool": "probe",
                    "psmux": True,
                    "uploadServer": False,
                    "tools": {"probe": f"rem mdhk-{unique}"},
                },
                "projects": [{"path": str(proj), "title": name}],
            }
        )
    )

    serve_proc = None
    hotkey_proc = None
    session_created = False
    handles = []
    uploads = home / ".magent" / "uploads"
    pre_existing = set(uploads.glob("*_clipboard.bmp")) if uploads.is_dir() else set()

    def _sink(path):
        fh = path.open("wb")
        handles.append(fh)
        return fh

    try:
        # 1. A LIVE psmux session (the server validates projects against it).
        plat.launch_psmux_session(
            [
                PsmuxWindowOpts(
                    window_name=name, cwd=str(proj), command=f"rem mdhk-{unique}"
                )
            ]
        )
        session_created = True
        assert _wait_until(lambda: psmux.has_session(name), timeout=10), (
            f"psmux session {name!r} never came up"
        )

        # 2. The REAL upload server, as its own process with the tmp HOME.
        #    Child output goes to files (not PIPEs) so a crash is diagnosable
        #    from the assertion message without deadlocking on a full pipe.
        serve_out = tmp_path / "serve.out.log"
        serve_err = tmp_path / "serve.err.log"
        serve_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "magent",
                "--config",
                str(cfg),
                "serve",
                "-p",
                str(port),
            ],
            env=env,
            stdout=_sink(serve_out),
            stderr=_sink(serve_err),
        )

        def _serve_diag() -> str:
            return (
                f"serve alive: {serve_proc.poll() is None} "
                f"(rc={serve_proc.poll()})\n"
                f"serve stdout:\n{_tail(serve_out)}\n"
                f"serve stderr:\n{_tail(serve_err)}\n"
                f"upload log:\n{_read_log(home, 'upload')}"
            )

        # Two-phase readiness so a failure names the guilty half: first the
        # server must answer at all, then it must list our live session.
        assert _wait_until(
            lambda: _http_ok(f"{server_url}/api/sessions", '"ok": true'), timeout=60
        ), f"upload server never answered /api/sessions\n{_serve_diag()}"
        assert _wait_until(
            lambda: _http_ok(f"{server_url}/api/sessions", name), timeout=60
        ), (
            f"upload server answered but never listed session {name!r} "
            f"(discover_sessions found no live psmux session?)\n"
            f"has_session={psmux.has_session(name)} "
            f"find_psmux={psmux.find_psmux()!r}\n{_serve_diag()}"
        )

        # 3. The REAL listener, spawned with the product's exact argv
        #    (cli/background._maybe_start_hotkey builds this same command).
        hotkey_out = tmp_path / "hotkey.out.log"
        hotkey_err = tmp_path / "hotkey.err.log"
        hotkey_proc = subprocess.Popen(
            [sys.executable, "-m", "magent", "hotkey", "-s", server_url],
            env=env,
            stdout=_sink(hotkey_out),
            stderr=_sink(hotkey_err),
        )
        # hotkey.pid is written only AFTER SetWindowsHookExW succeeded -- the
        # product's own "hook is live" signal. Real HOME, so require the pid
        # file to carry OUR child's pid (never a stale leftover).
        pid_file = home / ".magent" / "hotkey.pid"

        def _hook_live() -> bool:
            try:
                return int(pid_file.read_text().strip()) == hotkey_proc.pid
            except (OSError, ValueError):
                return False

        assert _wait_until(_hook_live, timeout=60), (
            "listener never wrote its hotkey.pid (hook install failed?)\n"
            f"listener alive: {hotkey_proc.poll() is None}\n"
            f"listener stdout:\n{_tail(hotkey_out)}\n"
            f"listener stderr:\n{_tail(hotkey_err)}"
        )

        # 4. The REAL magent: window via the product attach path; focus it.
        plat.attach_psmux(name, title)
        hwnd = _wait_until(lambda: plat.find_window(title), timeout=90)
        assert hwnd, f"attach window {title!r} never materialized"

        # 5. A REAL image on the REAL clipboard.
        dib, expected_bmp = _make_dib()
        _set_clipboard_dib(dib)

        # 6. The chord, with focus re-asserted per attempt (hosted-runner
        #    foreground quirks are expected; the assertion stays strict --
        #    the upload must happen). Real HOME, so the proof is a NEW file
        #    beyond the pre-test snapshot (byte-identity asserted below).
        def _uploaded():
            if not uploads.is_dir():
                return None
            new = sorted(set(uploads.glob("*_clipboard.bmp")) - pre_existing)
            return new[-1] if new else None

        attempts_log: list[str] = []
        for attempt in range(6):
            focused = _force_foreground(plat, hwnd, title)
            attempts_log.append(
                f"attempt {attempt}: focused={focused} "
                f"foreground={_foreground_title()!r}"
            )
            _send_alt_v()
            if _wait_until(_uploaded, timeout=10, interval=0.5):
                break
        dest = _uploaded()
        assert dest is not None, (
            "Alt+V never produced an upload;\n"
            + "\n".join(attempts_log)
            + f"\nlistener alive: {hotkey_proc.poll() is None}"
            + f"\nlistener stderr:\n{_tail(hotkey_err)}"
            + "\nhotkey log:\n"
            + _read_log(home, "hotkey")
            + "\nupload log:\n"
            + _read_log(home, "upload")
        )

        # 7a. The uploaded artifact is byte-for-byte the BMP the product's
        #     DIB->BMP wrapper must produce for our DIB.
        assert dest.read_bytes() == expected_bmp, (
            f"uploaded BMP does not match the expected wrap of the clipboard "
            f"DIB ({dest})"
        )

        # 7b. The path was injected into the LIVE psmux session.
        assert _wait_until(lambda: dest.name in _capture_pane(name), timeout=20), (
            f"uploaded path never appeared in the psmux pane; pane:\n"
            f"{_capture_pane(name)}"
        )

        # 7c. The listener's own log recorded the success.
        assert _wait_until(
            lambda: f"upload project={name} ok=True" in _read_log(home, "hotkey"),
            timeout=10,
        ), f"hotkey log missing success line:\n{_read_log(home, 'hotkey')}"
    finally:
        if hotkey_proc is not None:
            hotkey_proc.kill()
            hotkey_proc.wait(timeout=15)
        if serve_proc is not None:
            serve_proc.kill()
            serve_proc.wait(timeout=15)
        if session_created:
            psmux.kill_server(name)
        hwnd = plat.find_window(title)
        if hwnd:
            ctypes.windll.user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
        _wait_until(lambda: plat.find_window(title) is None, timeout=15)
        _clear_clipboard()
        for fh in handles:
            with contextlib.suppress(OSError):
                fh.close()
        # Real-HOME tidy-up: remove exactly the artifacts this test created
        # (uploaded file, stale listener pid left by the hard kill).
        if uploads.is_dir():
            for f in set(uploads.glob("*_clipboard.bmp")) - pre_existing:
                with contextlib.suppress(OSError):
                    f.unlink()
        with contextlib.suppress(OSError):
            (home / ".magent" / "hotkey.pid").unlink()

    assert not psmux.has_session(name), f"cleanup left psmux session {name!r} alive"
    assert plat.find_window(title) is None, f"cleanup left window {title!r} open"
