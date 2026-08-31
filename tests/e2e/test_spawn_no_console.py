"""No psmux spawn from the console-less fleet may open a console window.

The incident this pins (2026-08-31, live desktop): pressing Alt+V spawned a
burst of EMPTY Windows Terminal windows, froze every terminal on the desktop,
and only then landed the paste. Mechanism: a console-subsystem child of a
console-less parent is given a brand-new console, and Windows 11's
default-terminal setting materializes each one as a real, visible WT window.
`magent serve` runs console-less (`launch.spawn_detached` uses
DETACHED_PROCESS|CREATE_NO_WINDOW), and every one-shot psmux client it
launched -- the narration flashes, the paste `send-keys`, the discovery
fan-out's per-session `has-session` probes -- carried no console flag, so one
press opened dozens of windows at once. The fix pins CREATE_NO_WINDOW on every
spawn in `psmux.py`.

The unit gate already walks `psmux.py`'s AST for the flag; this tier proves
the BEHAVIOUR, with nothing mocked in between:

    REAL http  ->  REAL `magent serve` spawned CONSOLE-LESS through the REAL
    production seam (`launch.spawn_detached` -- the same call the upload
    watchdog uses)  ->  REAL subprocess spawn of a REAL executable named
    `psmux` on PATH, which records, from inside its own process, whether the
    console it was given has a VISIBLE window.

The recording multiplexer is the same stand-in shape as
`tests/e2e/test_altv_flash.py`'s (a real executable the real product really
spawns), extended with the one observable that matters here:
``GetConsoleWindow()`` + ``IsWindowVisible`` as seen by the spawned client
itself. With the fix, every spawn gets a windowless conhost -- no visible
window under either console host (classic conhost or WT delegation). Without
it, a console-less serve hands every child a fresh visible console.

Win32-only by nature: POSIX has no console-window concept and the failure mode
does not exist there (the module skips cleanly on the other two e2e legs).

The detector itself is pinned honest by ``test_the_detector_sees_the_window``:
a deliberately FLAGLESS spawn from the same console-less shape must be seen as
visible, or the main assertion could pass vacuously forever. That control leg
is skipped when the box delegates its default terminal to Windows Terminal
(dev desktops), because WT-hosted consoles report a hidden conhost handle --
CI's windows-latest runners use the classic host and run it.
"""

import http.client
import json
import os
import socket
import sys
import time
import uuid
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        sys.platform != "win32",
        reason="console windows are a win32 concept; the bug cannot exist elsewhere",
    ),
]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Same atomic one-file-per-invocation recorder as test_altv_flash.py (shared
# appends measurably lose records on Windows), plus the console observation.
# The observation happens INSIDE the spawned client: GetConsoleWindow answers
# for the console this very process was attached to, which is exactly the
# console magent's spawn gave it -- no window enumeration, no racing against
# other windows on a busy desktop.
_SHIM_BODY = """
import ctypes, json, os, sys, time
argv = sys.argv[1:]
hwnd = ctypes.windll.kernel32.GetConsoleWindow()
visible = bool(hwnd) and bool(ctypes.windll.user32.IsWindowVisible(hwnd))
uniq = "%020d-%08d-%s" % (time.time_ns(), os.getpid(), os.urandom(4).hex())
tmp = os.path.join(RECDIR, "." + uniq)
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(json.dumps(
        {"t": time.time(), "argv": argv,
         "console_hwnd": int(hwnd), "console_visible": visible}))
final = os.path.join(RECDIR, uniq + ".json")
for attempt in range(50):
    try:
        os.replace(tmp, final)
        break
    except OSError:
        time.sleep(0.02)
else:
    sys.exit("shim could not publish its record: " + final)
sys.exit(0)
"""


def _write_shim(bin_dir: Path, rec_dir: Path) -> Path:
    """A REAL executable named `psmux` on PATH; returns the script path.

    On Windows the launcher is a .cmd -- itself a console-subsystem spawn
    (cmd.exe), so the console the python grandchild observes is the very
    console magent's subprocess call created for the .cmd. That indirection is
    the same one every fake-psmux e2e tier already relies on.
    """
    script = bin_dir / "psmux_shim.py"
    script.write_text(f"RECDIR = {str(rec_dir)!r}\n" + _SHIM_BODY, encoding="utf-8")
    (bin_dir / "psmux.cmd").write_text(
        f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8"
    )
    return script


def _read_calls(rec_dir: Path) -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(rec_dir.glob("*.json"))
    ]


def _get(port: int, path: str, timeout: float = 30.0) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")
    finally:
        conn.close()


def _post_upload(port: int, project: str, payload: bytes) -> tuple[int, dict]:
    boundary = "mdnoconsole"
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="probe.png"\r\n'
            f"Content-Type: image/png\r\n\r\n"
        ).encode()
        + payload
        + f"\r\n--{boundary}--\r\n".encode()
    )
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=90)
    try:
        conn.request(
            "POST",
            f"/upload?project={project}",
            body=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")
    finally:
        conn.close()


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """One REAL `magent serve`, spawned CONSOLE-LESS via `launch.spawn_detached`.

    That spawn seam is the point of this tier: `test_altv_flash.py`'s serve
    inherits pytest's console, which quietly makes the bug unobservable. Here
    the environment is arranged in this process (spawn_detached inherits it,
    exactly as production's does from the watchdog) and the child is born the
    way the fleet really is born.
    """
    unique = uuid.uuid4().hex[:10]
    home = tmp_path / "home"
    home.mkdir()
    rec_dir = tmp_path / "psmux-calls"
    rec_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim_script = _write_shim(bin_dir, rec_dir)

    projects = []
    for i in range(3):  # >1 so /api/sessions probes a real fan-out
        proj = tmp_path / f"proj-{unique}-{i}"
        proj.mkdir()
        projects.append(
            {"path": str(proj), "title": f"mdnc-{unique}-{i}", "tool": "probe"}
        )
    cfg = tmp_path / "magent.config.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 3,
                "projects": projects,
                "settings": {
                    "defaultTool": "probe",
                    "tools": {"probe": f"rem mdnc-{unique}"},
                    "uploadServer": False,
                    "attention": {
                        "badge": False,
                        "flash": False,
                        "toast": False,
                        "ntfy": False,
                    },
                },
            }
        )
    )

    home_s = str(home)
    drive, tail = os.path.splitdrive(home_s)
    monkeypatch.setenv("USERPROFILE", home_s)
    monkeypatch.setenv("HOMEDRIVE", drive)
    monkeypatch.setenv("HOMEPATH", tail or "\\")
    monkeypatch.setenv("HOME", home_s)
    # The three test-isolation laws for anything that starts a real serve: no
    # system-wide keyboard hook, no second real serve, no priority sweep that
    # reaches the developer's real fleet by image name.
    monkeypatch.setenv("MAGENT_HOTKEY_SUPERVISOR", "0")
    monkeypatch.setenv("MAGENT_UPLOAD_SUPERVISOR", "0")
    monkeypatch.setenv("MAGENT_PSMUX_BOOST", "0")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))

    from magent import launch

    port = _free_port()
    proc = launch.spawn_detached(
        [
            sys.executable,
            "-m",
            "magent",
            "--config",
            str(cfg),
            "serve",
            "-p",
            str(port),
            "--host",
            "127.0.0.1",
        ]
    )
    deadline = time.monotonic() + 30
    ready = False
    while time.monotonic() < deadline:
        try:
            status, body = _get(port, "/health", timeout=2)
            if status == 200 and body.get("ok"):
                ready = True
                break
        except (OSError, ValueError):
            pass
        time.sleep(0.25)
    if not ready:
        proc.kill()
        pytest.fail("console-less serve never answered /health within 30s")

    yield {
        "port": port,
        "rec_dir": rec_dir,
        "shim_script": shim_script,
        "project": projects[0]["title"],
        "proc": proc,
    }
    proc.kill()


def _await_records(rec_dir: Path, at_least: int, deadline_s: float) -> list[dict]:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        calls = _read_calls(rec_dir)
        if len(calls) >= at_least:
            return calls
        time.sleep(0.2)
    return _read_calls(rec_dir)


def test_no_spawn_from_a_console_less_serve_opens_a_console_window(fleet):
    """The regression proper: flash + paste-inject + discovery fan-out, all
    driven over real HTTP against a serve with no console, and not one of the
    resulting psmux spawns may have a visible console window."""
    port, rec_dir = fleet["port"], fleet["rec_dir"]

    status, body = _get(
        port, f"/api/flash?project={fleet['project']}&msg=Alt%2BV%3A+capturing..."
    )
    assert status == 200 and body.get("ok"), body
    status, body = _get(
        port, f"/api/flash?project={fleet['project']}&msg=Alt%2BV%3A+uploading..."
    )
    assert status == 200 and body.get("ok"), body
    status, body = _post_upload(port, fleet["project"], b"\x89PNG-not-really-" * 8)
    assert status == 200 and body.get("ok"), body
    status, body = _get(port, "/api/sessions")
    assert status == 200 and body.get("ok"), body

    # 2 flashes + 1 send-keys + >=1 discovery probe. Waiting for at least 4
    # keeps the visibility sweep below from passing on an empty directory.
    calls = _await_records(rec_dir, at_least=4, deadline_s=30)
    argvs = [c["argv"] for c in calls]
    flat = [tok for argv in argvs for tok in argv]
    assert "display-message" in flat, argvs
    assert "send-keys" in flat, argvs
    assert "has-session" in flat, argvs

    offenders = [c for c in calls if c["console_visible"]]
    assert not offenders, (
        f"{len(offenders)} of {len(calls)} psmux spawn(s) from the console-less "
        f"serve got a VISIBLE console window -- this is the Alt+V "
        f"empty-terminal storm:\n" + "\n".join(json.dumps(c) for c in offenders)
    )


def _wt_owns_the_default_terminal() -> bool:
    """True when this box delegates new consoles to Windows Terminal.

    Under WT delegation a flagless console spawn opens a real WT window, but
    the conhost handle `GetConsoleWindow` returns reports as hidden -- the
    detector's positive direction is only classic-conhost-deterministic. The
    zero GUID means 'let Windows decide', which on the Server images CI runs
    is the classic host.
    """
    import winreg

    zero = "{00000000-0000-0000-0000-000000000000}"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Console\%%Startup") as key:
            for value in ("DelegationConsole", "DelegationTerminal"):
                try:
                    data, _ = winreg.QueryValueEx(key, value)
                except OSError:
                    continue
                if isinstance(data, str) and data and data.lower() != zero.lower():
                    return True
    except OSError:
        return False
    return False


def test_the_detector_sees_the_window_when_the_flag_is_absent(fleet, tmp_path):
    """Harness honesty: the same console-less shape spawning the same shim
    WITHOUT CREATE_NO_WINDOW must be observed as a visible console window --
    otherwise the main test's 'nothing was visible' could be vacuous."""
    if _wt_owns_the_default_terminal():
        pytest.skip(
            "default terminal is delegated to Windows Terminal on this box; "
            "the flagless control leg is only deterministic under classic "
            "conhost (CI runs it)"
        )
    control_rec = tmp_path / "control-calls"
    control_rec.mkdir()
    control_script = tmp_path / "control_shim.py"
    control_script.write_text(
        f"RECDIR = {str(control_rec)!r}\n" + _SHIM_BODY, encoding="utf-8"
    )
    driver = tmp_path / "flagless_driver.py"
    driver.write_text(
        "import subprocess, sys\n"
        f"subprocess.run([sys.executable, {str(control_script)!r}, 'has-session'],"
        " capture_output=True, timeout=60, check=False)\n",
        encoding="utf-8",
    )

    from magent import launch

    proc = launch.spawn_detached([sys.executable, str(driver)])
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.25)
    assert proc.poll() is not None, "flagless driver never exited"

    calls = _await_records(control_rec, at_least=1, deadline_s=10)
    assert calls, "the control spawn published no record"
    assert calls[0]["console_visible"], (
        "a FLAGLESS spawn from a console-less parent was observed as having no "
        "visible console window -- the detector cannot see the bug it exists "
        f"to catch: {json.dumps(calls[0])}"
    )
    # Belt and braces for the ctypes plumbing itself: a console existed at all.
    assert calls[0]["console_hwnd"] != 0
