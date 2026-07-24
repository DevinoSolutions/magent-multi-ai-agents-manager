"""Shared monitor-lab tier scaffolding (win32/CI-only, import-harmless on POSIX).

Both live monitor-lab test modules -- ``test_monitor_lab_tiling.py`` (the zoo)
and ``test_doctor_replay.py`` (bug-report topology replay) -- drive REAL ``wt``
windows across REAL parsec-vdd virtual monitors via the same helpers: install
the pinned driver, snapshot physical window rects through Win32, launch the real
``--go`` pipeline, and assert each window lands in its ``compute_grid`` cell.
Those helpers (and the driver install + the gating marks) live here so neither
test module copy-pastes them; the ``lab`` session fixture that shares ONE driver
install across both modules lives in ``conftest.py``.

Import safety: this module is imported at collection time on every OS, so
nothing at module scope may touch ``ctypes.windll`` -- every Win32 call sits
inside a function body that runs only on a gated win32 runner.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

# The whole tier is CI-only: win32 + an explicit opt-in env var. Without the
# var every monitor-lab module skips cleanly, so the parsec-vdd driver is never
# installed on a dev box. Reused verbatim by both test modules' ``pytestmark``.
PYTESTMARK = [
    pytest.mark.monitor_lab,
    pytest.mark.skipif(
        sys.platform != "win32", reason="virtual-monitor lab is win32-only"
    ),
    pytest.mark.skipif(
        os.environ.get("MDTEST_MONITOR_LAB") != "1",
        reason=(
            "monitor-lab tier installs the parsec-vdd display driver; CI-only "
            "(set MDTEST_MONITOR_LAB=1). Never enable on a dev machine."
        ),
    ),
]

# Pinned parsec-vdd release (WHQL-signed, silent /S install, no reboot).
PARSEC_URL = (
    "https://github.com/nomi-san/parsec-vdd/releases/download/v0.45.1/"
    "ParsecVDisplay-v0.45-portable.zip"
)
PARSEC_SHA256 = "9792e4121d85ed3e4c40c2d8ba36ec8657e13227a8357d719d923e801238ccdd"

WM_CLOSE = 0x0010
EDGE_TOL = 100  # px: window chrome / DPI rounding slack per edge
MATERIALIZE_TIMEOUT = 90  # s: slow CI VMs are slow to paint wt windows


# --------------------------------------------------------------------------- #
# driver install
# --------------------------------------------------------------------------- #


def install_parsec_vdd(workdir: Path) -> None:
    zip_path = workdir / "parsec.zip"
    urllib.request.urlretrieve(PARSEC_URL, zip_path)  # pinned https, sha256 below
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    if digest != PARSEC_SHA256:
        raise AssertionError(
            f"parsec-vdd zip sha256 mismatch: got {digest}, want {PARSEC_SHA256}"
        )
    extract = workdir / "parsec"
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract)
    installers = list(extract.rglob("parsec-vdd-*.exe"))
    if not installers:
        raise AssertionError("parsec-vdd installer not found in portable zip")
    proc = subprocess.run(
        [str(installers[0]), "/S"], capture_output=True, timeout=180, check=False
    )
    assert proc.returncode == 0, f"parsec-vdd silent install failed: {proc.returncode}"
    time.sleep(15)  # driver store settle (~15s once, per the spike)


def emit_events(controller: object, tag: str) -> None:
    """Surface the lab's structured event log as a CI ``::warning`` -- the lab
    never writes to stdout, so this is the one diagnostic channel."""
    events = getattr(controller, "events", None)
    if events:
        joined = " | ".join(events[-40:])
        print(f"::warning title=monitor-lab {tag}::{joined}")  # noqa: T201  # reason: GitHub Actions ::warning marker is the lab diagnostic channel


# --------------------------------------------------------------------------- #
# real-window helpers (physical rects via Win32, mirrors test_real_launch.py)
# --------------------------------------------------------------------------- #


@dataclass
class WinRect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def cx(self) -> float:
        return (self.left + self.right) / 2

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2


def window_rect(hwnd: object) -> WinRect:
    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    rect = RECT()
    assert ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return WinRect(rect.left, rect.top, rect.right, rect.bottom)


def child_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("MAGENT_")}
    home_s = str(home)
    drive, tail = os.path.splitdrive(home_s)
    env["USERPROFILE"] = home_s
    env["HOMEDRIVE"] = drive
    env["HOMEPATH"] = tail or "\\"
    env["HOME"] = home_s
    return env


def wait_until(check, timeout: float, interval: float = 1.0):
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            return result
        time.sleep(interval)


def md_handles(plat, titles: list[str]) -> dict[str, object]:
    snap = plat.snapshot_windows()
    return {t: snap[t] for t in titles if t in snap}


def cmd_procs_with(marker: str) -> list[int]:
    query = (
        "Get-CimInstance Win32_Process -Filter \"Name='cmd.exe'\" | "
        "ForEach-Object { if ($_.CommandLine -like '*" + marker + "*') "
        "{ $_.ProcessId } } | ConvertTo-Json -Compress"
    )
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", query],
        capture_output=True,
        text=True,
        timeout=90,
    )
    out = (r.stdout or "").strip()
    if not out:
        return []
    data = json.loads(out)
    return [data] if isinstance(data, int) else list(data)


def close_and_verify_gone(plat, titles: list[str], marker: str) -> list[str]:
    for hwnd in md_handles(plat, titles).values():
        ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    wait_until(lambda: not md_handles(plat, titles), timeout=15)
    for pid in cmd_procs_with(marker):
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False
        )
    wait_until(
        lambda: not md_handles(plat, titles) and not cmd_procs_with(marker),
        timeout=15,
    )
    leftovers = [f"window {t}" for t in md_handles(plat, titles)]
    leftovers += [f"process pid={p}" for p in cmd_procs_with(marker)]
    return leftovers


def assert_in_slot(rect: WinRect, slot, label: str) -> None:
    assert abs(rect.left - slot.x) <= EDGE_TOL, f"{label}: left {rect.left} vs {slot.x}"
    assert abs(rect.top - slot.y) <= EDGE_TOL, f"{label}: top {rect.top} vs {slot.y}"
    assert abs(rect.right - (slot.x + slot.w)) <= EDGE_TOL, (
        f"{label}: right {rect.right} vs {slot.x + slot.w}"
    )
    assert abs(rect.bottom - (slot.y + slot.h)) <= EDGE_TOL, (
        f"{label}: bottom {rect.bottom} vs {slot.y + slot.h}"
    )
    assert slot.x <= rect.cx <= slot.x + slot.w, f"{label}: center_x outside its cell"
    assert slot.y <= rect.cy <= slot.y + slot.h, f"{label}: center_y outside its cell"
