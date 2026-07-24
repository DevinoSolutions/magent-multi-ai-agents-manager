"""Monitor-lab tiling tier: REAL windows tiled across REAL virtual monitors at
mixed DPI, on a hosted ``windows-latest`` runner.

This is the live half of the multi-monitor coverage that DESIGN.md R4-05 left
open. It fabricates a mixed-DPI, multi-monitor topology with the parsec-vdd
virtual-display driver (see ``monitor_lab.py``), then drives magent's real
``--go`` launch+tile pipeline and asserts -- in PHYSICAL pixels, across
monitors at different DPI scales -- that each real Windows Terminal window
lands inside the ``grid.compute_grid`` cell computed for its monitor.

CI-ONLY BY DESIGN -- like the ``needs_ssh`` tier, and for a harsher reason:
these tests INSTALL A KERNEL-ADJACENT DISPLAY DRIVER. They run ONLY when
``MDTEST_MONITOR_LAB=1`` (the ``monitor-lab`` CI job sets it on a throwaway
runner that is discarded at job end). NEVER set that variable or install
parsec-vdd on a real dev machine to run these locally -- without the variable
the whole module skips cleanly. The offline grid-math half lives in
``tests/unit/test_monitor_lab_topologies.py`` and runs everywhere.

The driver install, the ``lab`` session fixture (shared with the doctor-replay
tier so the driver installs once), and the real-window helpers live in
``lab_harness.py`` / ``conftest.py``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import uuid

import pytest

from . import lab_harness
from .lab_harness import (
    assert_in_slot,
    child_env,
    close_and_verify_gone,
    emit_events,
    md_handles,
    wait_until,
    window_rect,
)

pytestmark = lab_harness.PYTESTMARK

# The layout zoo: each entry is the list of virtual displays to add, as
# (width, height, dpi_percent). Positioned left-to-right by the lab; the
# runner's own (primary) monitor stays monitor 0.
#
# Every (resolution, dpi) here must be PHYSICALLY ACHIEVABLE on Windows: a
# panel cannot scale above the DPI at which its effective resolution drops
# below the OS minimum (~1024x768). e.g. 1280x720 cannot exceed 100% -- so the
# live triple uses 2560x1440@125 (effective 2048x1152). The OFFLINE grid-math
# test (tests/unit/test_monitor_lab_topologies.py) is free to pin synthetic,
# hardware-impossible topologies like 720p@125 to exercise column collapse.
ZOO: dict[str, list[tuple[int, int, int]]] = {
    "dual_mixed_dpi": [(1920, 1080, 100), (2560, 1440, 150)],
    "solo_4k": [(3840, 2160, 200)],
    "triple_1440p_125": [(2560, 1440, 125), (2560, 1440, 125), (2560, 1440, 125)],
}


# --------------------------------------------------------------------------- #
# the zoo
# --------------------------------------------------------------------------- #


@pytest.fixture(params=list(ZOO), ids=list(ZOO))
def topology(request, lab):
    """Fresh virtual-display topology per zoo layout: clean slate, add this
    layout's displays, yield its specs, then reset back to the clean slate so
    the next parametrization starts from the runner's bare primary."""
    specs = ZOO[request.param]
    lab.reset_displays()
    for w, h, dpi in specs:
        lab.add(w, h, dpi)
    time.sleep(2.0)  # let the new topology settle before enumeration
    try:
        yield specs
    finally:
        lab.reset_displays()


def test_windows_tile_across_virtual_monitors(topology, lab, tmp_path):
    if shutil.which("wt") is None:
        pytest.skip("Windows Terminal (wt) not on PATH")

    specs = topology
    monitors = lab.snapshot()
    virtuals = [m for m in monitors if not m.is_primary]
    assert len(virtuals) >= len(specs), (
        f"expected >= {len(specs)} virtual monitor(s); lab view: "
        f"{lab.snapshot_json()} (events: {lab.events[-20:]})"
    )

    from magent.grid import compute_grid
    from magent.platform import get_platform

    plat = get_platform()
    plat.set_dpi_aware()

    # One slot per monitor (columns=rows=1): target i -> slots[i] -> monitor i,
    # so a window lands on every monitor including each virtual one.
    slots = compute_grid(monitors, 1, 1)
    monitors_sorted = sorted(monitors, key=lambda m: m.x)
    assert len(slots) == len(monitors_sorted)
    n = len(slots)

    # 1. Each added virtual monitor reports the resolution AND DPI we set,
    #    THROUGH magent's own list_monitors() view -- the mixed-DPI
    #    precondition, in physical pixels. Virtuals are laid out left-to-right
    #    in add order, so virtuals[k] <-> specs[k].
    virtuals_sorted = sorted(virtuals, key=lambda m: m.x)
    for k, (w, _h, dpi) in enumerate(specs):
        vm = virtuals_sorted[k]
        assert vm.w == w, (
            f"virtual {k}: width {vm.w} != requested {w} (events: {lab.events[-12:]})"
        )
        assert round(vm.scale_factor * 100) == dpi, (
            f"virtual {k}: DPI {round(vm.scale_factor * 100)}% != {dpi}% "
            f"(events: {lab.events[-12:]})"
        )

    # 2. Launch one real wt window per monitor via the real --go pipeline.
    unique = uuid.uuid4().hex[:8]
    marker = f"mdml-{unique}"
    names = [f"mdml{unique}s{i}" for i in range(n)]
    titles = [f"magent:{name}" for name in names]

    proj = tmp_path / f"proj-{unique}"
    proj.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    cfg = tmp_path / "magent.config.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 3,
                "layout": {"columns": 1, "rows": 1},
                "settings": {
                    "defaultTool": "probe",
                    "settleSeconds": 1,
                    "launchDelayMs": 400,
                    "psmux": False,
                    "uploadServer": False,
                    "tools": {"probe": f"rem {marker}"},
                },
                "projects": [
                    {
                        "path": str(proj),
                        "title": f"mdml{unique}",
                        "windows": [{"name": name} for name in names],
                    }
                ],
            }
        )
    )

    try:
        result = subprocess.run(
            [sys.executable, "-m", "magent", "--go", "--config", str(cfg)],
            capture_output=True,
            text=True,
            timeout=300,
            env=child_env(home),
        )
        assert result.returncode == 0, (
            f"--go failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        handles = wait_until(
            lambda: (h := md_handles(plat, titles)) and len(h) == n and h,
            timeout=lab_harness.MATERIALIZE_TIMEOUT,
        )
        assert handles, (
            f"expected {n} windows {titles}; visible magent: windows: "
            f"{[t for t in plat.snapshot_windows() if t.startswith('magent:')]}"
        )

        # 3. Each window i sits in slots[i], on monitor i -- physical pixels,
        #    mixed DPI. The virtual-monitor windows (i >= 1) are the payload.
        for i, title in enumerate(titles):
            assert_in_slot(window_rect(handles[title]), slots[i], f"window {i}")
            assert slots[i].monitor_index == i
    finally:
        leftovers = close_and_verify_gone(plat, titles, marker)
        emit_events(lab, f"after {'-'.join(str(s) for s in specs[0])}")
        assert not leftovers, f"cleanup left windows/processes behind: {leftovers}"
