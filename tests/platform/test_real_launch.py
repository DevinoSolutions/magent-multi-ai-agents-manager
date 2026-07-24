"""REAL end-to-end launch: `python -m magent --go` as a subprocess opening
two actual Windows Terminal windows, tiled by the real Win32 pipeline.

What this proves about the user experience (no fakes, no monkeypatching):

* a v3 config with per-window ``tool`` overrides (``windows: [{...}]``, PR
  #40/#41) launches one real terminal per window entry, titled exactly
  ``magent:<name>``;
* the real windows land inside the virtual screen, in the grid cells that
  ``grid.compute_grid`` derives from the real monitor set, without overlap;
* the override window's real OS process runs the override tool's command and
  the non-override window runs the default tool's command (proven via
  Win32_Process command lines + creation order).

Isolation: the child process gets a redirected HOME/USERPROFILE (its
``~/.magent`` logs land in tmp_path) and a ``--config`` in tmp_path. The
configured "tools" are benign ``cmd /k rem <uuid>`` commands -- real terminals,
real command execution, zero cost. Cleanup closes exactly the two uuid-titled
windows and verifies both the windows and their cmd.exe processes are gone.
"""

import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass

import pytest

pytestmark = [
    pytest.mark.platform,
    pytest.mark.skipif(
        sys.platform != "win32", reason="real Windows Terminal launch is win32-only"
    ),
    pytest.mark.skipif(
        shutil.which("wt") is None, reason="Windows Terminal (wt) not on PATH"
    ),
]

_WM_CLOSE = 0x0010
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79

_EDGE_TOL = 100  # px: window chrome / DPI rounding slack per edge
_OVERLAP_TOL = 48  # px: max tolerated intrusion into the neighbour cell


def _child_env(home) -> dict[str, str]:
    """Env for child processes: real user env, but HOME redirected so the
    child's ~/.magent (logs, state, pid files) lands under tmp_path, and no
    ambient MAGENT_* vars leak in. Exactly what a real user with that home
    directory would experience."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("MAGENT_")}
    home_s = str(home)
    drive, tail = os.path.splitdrive(home_s)
    env["USERPROFILE"] = home_s
    env["HOMEDRIVE"] = drive
    env["HOMEPATH"] = tail or "\\"
    env["HOME"] = home_s
    return env


def _wait_until(check, timeout: float, interval: float = 0.25):
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            return result
        time.sleep(interval)


@dataclass
class _WinRect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def center_x(self) -> float:
        return (self.left + self.right) / 2

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2


def _get_window_rect(hwnd) -> _WinRect:
    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    rect = RECT()
    assert ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return _WinRect(rect.left, rect.top, rect.right, rect.bottom)


def _virtual_screen() -> _WinRect:
    metrics = ctypes.windll.user32.GetSystemMetrics
    x = metrics(_SM_XVIRTUALSCREEN)
    y = metrics(_SM_YVIRTUALSCREEN)
    return _WinRect(
        x, y, x + metrics(_SM_CXVIRTUALSCREEN), y + metrics(_SM_CYVIRTUALSCREEN)
    )


# --- real process facts (Win32_Process via PowerShell CIM; no new deps) -----

_PS_CMD_QUERY = (
    "Get-CimInstance Win32_Process -Filter \"Name='cmd.exe'\" | ForEach-Object { "
    "[pscustomobject]@{ pid = $_.ProcessId; cl = [string]$_.CommandLine; "
    "ft = $(if ($_.CreationDate) { $_.CreationDate.ToFileTimeUtc() } else { 0 }) } } | "
    "ConvertTo-Json -Compress"
)


def _cmd_processes() -> list[dict]:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", _PS_CMD_QUERY],
        capture_output=True,
        text=True,
        timeout=90,
    )
    out = (r.stdout or "").strip()
    if not out:
        return []
    data = json.loads(out)
    if isinstance(data, dict):
        data = [data]
    return [d for d in data if isinstance(d, dict) and d.get("cl")]


def _marker_procs(markers: list[str]) -> dict[str, list[dict]]:
    """One CIM snapshot, filtered per marker (each PowerShell spawn is ~2s)."""
    procs = _cmd_processes()
    return {m: [p for p in procs if m in p["cl"]] for m in markers}


def _snapshot_md_handles(plat, titles: list[str]) -> dict[str, object]:
    snap = plat.snapshot_windows()
    return {t: snap[t] for t in titles if t in snap}


def _close_and_verify_gone(plat, titles: list[str], markers: list[str]) -> list[str]:
    """Close exactly the given windows (WM_CLOSE), force-kill any surviving
    marker-tagged cmd.exe processes by exact PID, and return whatever is still
    left. Never touches any window or process the test did not create."""
    for hwnd in _snapshot_md_handles(plat, titles).values():
        ctypes.windll.user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)

    _wait_until(lambda: not _snapshot_md_handles(plat, titles), timeout=15)

    for procs in _marker_procs(markers).values():
        for proc in procs:
            subprocess.run(
                ["taskkill", "/PID", str(proc["pid"]), "/T", "/F"],
                capture_output=True,
                check=False,
            )

    def _all_gone() -> bool:
        if _snapshot_md_handles(plat, titles):
            return False
        return not any(_marker_procs(markers).values())

    _wait_until(_all_gone, timeout=15)

    leftovers = [f"window {t}" for t in _snapshot_md_handles(plat, titles)]
    leftovers += [
        f"process pid={p['pid']}"
        for procs in _marker_procs(markers).values()
        for p in procs
    ]
    return leftovers


@pytest.fixture
def cleanup_registry():
    """Teardown-as-safety-net: whatever the test registers is closed and
    verified gone even when the test body fails; a failed cleanup is a loud
    teardown error, never a leaked real window on the user's desktop."""
    from magent.platform import get_platform

    reg: dict[str, list[str]] = {"titles": [], "markers": []}
    yield reg
    leftovers = _close_and_verify_gone(get_platform(), reg["titles"], reg["markers"])
    assert not leftovers, f"cleanup left real windows/processes behind: {leftovers}"


def _assert_rect_matches_slot(rect: _WinRect, slot, label: str) -> None:
    assert abs(rect.left - slot.x) <= _EDGE_TOL, (
        f"{label}: left {rect.left} vs slot {slot.x}"
    )
    assert abs(rect.top - slot.y) <= _EDGE_TOL, (
        f"{label}: top {rect.top} vs slot {slot.y}"
    )
    assert abs(rect.right - (slot.x + slot.w)) <= _EDGE_TOL, (
        f"{label}: right {rect.right} vs slot {slot.x + slot.w}"
    )
    assert abs(rect.bottom - (slot.y + slot.h)) <= _EDGE_TOL, (
        f"{label}: bottom {rect.bottom} vs slot {slot.y + slot.h}"
    )
    assert slot.x <= rect.center_x <= slot.x + slot.w, (
        f"{label}: center_x outside its cell"
    )
    assert slot.y <= rect.center_y <= slot.y + slot.h, (
        f"{label}: center_y outside its cell"
    )


def test_per_window_tool_override_launches_real_tiled_windows(
    tmp_path, cleanup_registry
):
    from magent.grid import compute_grid
    from magent.platform import get_platform

    plat = get_platform()
    plat.set_dpi_aware()
    monitors = plat.list_monitors()
    assert monitors, "no real monitors detected"
    slots = compute_grid(monitors, 2, 1)
    if len(slots) < 2:
        pytest.skip("real display cannot host a 2x1 grid (DPI floor collapsed it)")

    unique = uuid.uuid4().hex[:10]
    name_a = f"mdrl{unique}a"  # default-tool window
    name_b = f"mdrl{unique}b"  # override-tool window
    title_a, title_b = f"magent:{name_a}", f"magent:{name_b}"
    marker_a = f"mdrl-default-{unique}"
    marker_b = f"mdrl-override-{unique}"

    proj = tmp_path / f"proj-{unique}"
    proj.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    cfg = tmp_path / "magent.config.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 3,
                "layout": {"columns": 2, "rows": 1},
                "settings": {
                    "defaultTool": "probe1",
                    "settleSeconds": 1,
                    "launchDelayMs": 400,
                    "psmux": False,
                    "uploadServer": False,
                    "tools": {
                        "probe1": f"rem {marker_a}",
                        "probe2": f"rem {marker_b}",
                    },
                },
                "projects": [
                    {
                        "path": str(proj),
                        "title": f"mdrl{unique}",
                        "windows": [
                            {"name": name_a},
                            {"name": name_b, "tool": "probe2"},
                        ],
                    }
                ],
            }
        )
    )

    cleanup_registry["titles"] = [title_a, title_b]
    cleanup_registry["markers"] = [marker_a, marker_b]

    result = subprocess.run(
        [sys.executable, "-m", "magent", "--go", "--config", str(cfg)],
        capture_output=True,
        text=True,
        timeout=180,
        env=_child_env(home),
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "not found" not in result.stdout, (
        f"tiling gave up on a window:\n{result.stdout}"
    )

    # 1. Both REAL windows exist with the exact expected magent: titles.
    #    Slow/overloaded CI VMs can take far longer than a couple of seconds to
    #    actually materialize the wt windows (observed CI flake: window title
    #    "not found"). Poll window enumeration ~1s up to a generous 90s deadline,
    #    exiting the instant both titles are visible -- the assertion below stays
    #    exactly as strict (both windows must be present, no tolerance added).
    handles = _wait_until(
        lambda: (
            (h := _snapshot_md_handles(plat, [title_a, title_b])) and len(h) == 2 and h
        ),
        timeout=90,
        interval=1.0,
    )
    assert handles, (
        f"expected windows {title_a!r} and {title_b!r}; magent: windows visible: "
        f"{[t for t in plat.snapshot_windows() if t.startswith('magent:')]}"
    )

    rect_a = _get_window_rect(handles[title_a])
    rect_b = _get_window_rect(handles[title_b])

    # 2a. Both inside the real virtual screen bounds.
    screen = _virtual_screen()
    for label, rect in (("A", rect_a), ("B", rect_b)):
        assert rect.left >= screen.left - _EDGE_TOL, f"{label} off-screen left"
        assert rect.top >= screen.top - _EDGE_TOL, f"{label} off-screen top"
        assert rect.right <= screen.right + _EDGE_TOL, f"{label} off-screen right"
        assert rect.bottom <= screen.bottom + _EDGE_TOL, f"{label} off-screen bottom"

    # 2b. Each window sits in the grid cell the launch pipeline computed for it
    #     (window A -> slots[0], window B -> slots[1], per launch order).
    slot_a, slot_b = slots[0], slots[1]
    _assert_rect_matches_slot(rect_a, slot_a, "window A")
    _assert_rect_matches_slot(rect_b, slot_b, "window B")

    # 2c. Ordered per the grid and non-overlapping (beyond chrome tolerance).
    if slot_a.monitor_index == slot_b.monitor_index:
        assert slot_a.x < slot_b.x  # compute_grid emits row-major, left-to-right
        assert rect_a.center_x < rect_b.center_x, "grid order violated on screen"
        assert rect_a.right - rect_b.left <= _OVERLAP_TOL, (
            f"windows overlap: A.right={rect_a.right} B.left={rect_b.left}"
        )

    # 3. The REAL processes: the override window's command line carries the
    #    probe2 command, the default window's carries probe1's -- each ran
    #    exactly once, and creation order matches the config's window order
    #    (A launched first, B 400ms later), pinning command<->window identity.
    by_marker = _marker_procs([marker_a, marker_b])
    procs_a, procs_b = by_marker[marker_a], by_marker[marker_b]
    assert len(procs_a) == 1, (
        f"default tool command ran {len(procs_a)} times: {procs_a}"
    )
    assert len(procs_b) == 1, (
        f"override tool command ran {len(procs_b)} times: {procs_b}"
    )
    assert f"rem {marker_a}" in procs_a[0]["cl"]
    assert f"rem {marker_b}" in procs_b[0]["cl"]
    assert procs_a[0]["ft"] < procs_b[0]["ft"], (
        "creation order contradicts config window order: "
        f"default={procs_a[0]}, override={procs_b[0]}"
    )

    # 4. Cleanup kills exactly those two windows; verify gone.
    leftovers = _close_and_verify_gone(plat, [title_a, title_b], [marker_a, marker_b])
    assert not leftovers, f"cleanup failed: {leftovers}"
    assert plat.find_window(title_a) is None
    assert plat.find_window(title_b) is None
