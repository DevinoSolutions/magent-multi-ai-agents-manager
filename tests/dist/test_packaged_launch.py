"""REAL launch from the INSTALLED artifact: `magent --go` via the packaged
console-script entry point opens one actual Windows Terminal window, tiled by
the real Win32 pipeline -- the definitive proof that a ``pip install magent-multi-ai-agents-manager``
user's core action works end to end from the shipped wheel.

Scope is deliberately ONE window: tests/e2e/test_real_launch.py already covers
multi-window geometry, grid cells and per-window tool overrides from the dev
tree; this test's only job is proving the INSTALLED artifact does the real
thing at all (real terminal, real ``magent:`` window, real command execution),
cheaply. Isolation + exact-target cleanup mirror test_real_launch: a redirected
home, a benign ``rem <uuid>`` tool (never a real agent), and teardown that
closes exactly the one uuid-titled window and kills only its marker cmd.exe.

Skips cleanly off-win32, without Windows Terminal, or when the real display
can't host even a 1x1 grid.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [
    pytest.mark.dist,
    pytest.mark.skipif(
        sys.platform != "win32", reason="real Windows Terminal launch is win32-only"
    ),
    pytest.mark.skipif(
        shutil.which("wt") is None, reason="Windows Terminal (wt) not on PATH"
    ),
]

_WM_CLOSE = 0x0010


def _child_env(home: Path) -> dict[str, str]:
    # PYTHONPATH/PYTHONHOME stripped too: inherited into the pristine venv's
    # interpreter they would splice dev paths back into sys.path.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith("MAGENT_")
        and k.upper() not in ("PYTHONPATH", "PYTHONHOME")
    }
    home_s = str(home)
    drive, tail = os.path.splitdrive(home_s)
    env["USERPROFILE"] = home_s
    env["HOMEDRIVE"] = drive
    env["HOMEPATH"] = tail or "\\"
    env["HOME"] = home_s
    env["APPDATA"] = home_s
    env["LOCALAPPDATA"] = home_s
    env["XDG_CONFIG_HOME"] = home_s
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


_PS_CMD_QUERY = (
    "Get-CimInstance Win32_Process -Filter \"Name='cmd.exe'\" | ForEach-Object { "
    "[pscustomobject]@{ pid = $_.ProcessId; cl = [string]$_.CommandLine } } | "
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
    procs = _cmd_processes()
    return {m: [p for p in procs if m in p["cl"]] for m in markers}


def _snapshot_md_handles(plat, titles: list[str]) -> dict[str, object]:
    snap = plat.snapshot_windows()
    return {t: snap[t] for t in titles if t in snap}


def _close_and_verify_gone(plat, titles: list[str], markers: list[str]) -> list[str]:
    """Close exactly the given windows (WM_CLOSE), force-kill any surviving
    marker-tagged cmd.exe by exact PID, and return whatever is still left.
    Never touches a window or process the test did not create."""
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
    verified gone even when the test body fails -- a failed cleanup is a loud
    teardown error, never a leaked real window on the user's desktop."""
    from magent.platform import get_platform

    reg: dict[str, list[str]] = {"titles": [], "markers": []}
    yield reg
    leftovers = _close_and_verify_gone(get_platform(), reg["titles"], reg["markers"])
    assert not leftovers, f"cleanup left real windows/processes behind: {leftovers}"


def test_installed_go_launches_one_real_md_window(
    packaged, home, neutral_cwd, tmp_path, cleanup_registry
):
    from magent.grid import compute_grid
    from magent.platform import get_platform

    plat = get_platform()
    plat.set_dpi_aware()
    monitors = plat.list_monitors()
    assert monitors, "no real monitors detected"
    if len(compute_grid(monitors, 1, 1)) < 1:
        pytest.skip("real display cannot host a 1x1 grid (DPI floor collapsed it)")

    unique = uuid.uuid4().hex[:10]
    name = f"mddist{unique}"
    title = f"magent:{name}"
    marker = f"mddist-launch-{unique}"

    proj = tmp_path / f"proj-{unique}"
    proj.mkdir()
    cfg = tmp_path / "magent.config.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 3,
                "layout": {"columns": 1, "rows": 1},
                "settings": {
                    "defaultTool": "probe",
                    "launchDelayMs": 400,
                    "psmux": False,
                    "uploadServer": False,
                    "tools": {"probe": f"rem {marker}"},
                },
                "projects": [
                    {
                        "path": str(proj),
                        "title": name,
                        "windows": [{"name": name}],
                    }
                ],
            }
        )
    )

    cleanup_registry["titles"] = [title]
    cleanup_registry["markers"] = [marker]

    result = subprocess.run(
        [str(packaged.entry_point), "--go", "--config", str(cfg)],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(neutral_cwd),
        env=_child_env(home),
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    # 1. The REAL window exists with the exact magent: title, from the installed CLI.
    handles = _wait_until(
        lambda: _snapshot_md_handles(plat, [title]) or None, timeout=20
    )
    assert handles and title in handles, (
        f"expected window {title!r}; magent: windows visible: "
        f"{[t for t in plat.snapshot_windows() if t.startswith('magent:')]}\n"
        f"stdout:\n{result.stdout}"
    )

    # 2. The benign tool command actually ran in that real terminal.
    procs = _marker_procs([marker])[marker]
    assert procs, f"tool command 'rem {marker}' never ran in a real cmd.exe"

    # 3. Cleanup closes exactly that one window; verify gone.
    leftovers = _close_and_verify_gone(plat, [title], [marker])
    assert not leftovers, f"cleanup failed: {leftovers}"
    assert plat.find_window(title) is None
