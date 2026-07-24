"""Doctor-replay tier: replay a user's captured monitor topology, live.

When a bug report includes ``magent doctor --json`` (which now carries the
exact monitor topology under the ``monitors`` key), CI can materialize that
topology in the parsec-vdd virtual-monitor lab and run magent's REAL
``--go`` tiling assertions against it -- the same physical-pixel, mixed-DPI
placement proof as ``test_monitor_lab_tiling.py``, but driven from committed
sample reports (``fixtures/doctor_reports/*.json``) instead of the hand-authored
zoo.

The planner (``doctor_replay.py``) maps each report monitor to the closest
resolution+DPI the lab can achieve; every deviation is logged loudly as a CI
``::warning``. Exact report ORIGINS -- negative-x "left-of-primary" monitors
especially -- are NOT reproduced positionally here (the runner's own primary is
immovable); that origin math is pinned OFFLINE by
``tests/unit/test_doctor_replay_offline.py`` feeding the same parsed reports
into ``compute_grid``. This live tier proves resolution+DPI materialization and
real-window tiling across the resulting mixed-DPI monitors.

CI-ONLY BY DESIGN -- installs the parsec-vdd driver; gated exactly like the zoo
tier (``lab_harness.PYTESTMARK``: win32 + ``MDTEST_MONITOR_LAB=1``). Shares the
one session-scoped ``lab`` fixture (``conftest.py``) so the driver installs once.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from . import lab_harness
from .doctor_replay import parse_doctor_report, plan_replay
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

_REPORTS = Path(__file__).parent / "fixtures" / "doctor_reports"


def _report_names() -> list[str]:
    return sorted(p.stem for p in _REPORTS.glob("*.json") if not p.stem.startswith("_"))


def _warn(tag: str, msg: str) -> None:
    print(f"::warning title=doctor-replay {tag}::{msg}")  # noqa: T201  # reason: GitHub Actions ::warning marker is the tier's diagnostic channel


@pytest.fixture(params=_report_names(), ids=_report_names())
def replay_topology(request, lab):
    """Parse the sample doctor report, plan the achievable topology, materialize
    it in the lab (one virtual display per report monitor, left-to-right), yield
    the plan, then reset to the clean slate for the next report."""
    name = request.param
    monitors = parse_doctor_report((_REPORTS / name).with_suffix(".json").read_text())
    plan = plan_replay(monitors)
    for dev in plan.all_deviations():
        _warn(name, f"deviation: {dev}")

    lab.reset_displays()
    for w, h, dpi in plan.lab_specs():
        lab.add(w, h, dpi)
    time.sleep(2.0)  # let the new topology settle before enumeration
    try:
        yield name, plan
    finally:
        lab.reset_displays()


def test_replayed_topology_tiles(replay_topology, lab, tmp_path):
    if shutil.which("wt") is None:
        pytest.skip("Windows Terminal (wt) not on PATH")

    name, plan = replay_topology
    specs = plan.lab_specs()
    monitors = lab.snapshot()
    virtuals = [m for m in monitors if not m.is_primary]
    assert len(virtuals) >= len(specs), (
        f"{name}: expected >= {len(specs)} virtual monitor(s); lab view: "
        f"{lab.snapshot_json()} (events: {lab.events[-20:]})"
    )

    from magent.grid import compute_grid
    from magent.platform import get_platform

    plat = get_platform()
    plat.set_dpi_aware()

    # One slot per monitor (columns=rows=1): target i -> slots[i] -> monitor i.
    slots = compute_grid(monitors, 1, 1)
    monitors_sorted = sorted(monitors, key=lambda m: m.x)
    assert len(slots) == len(monitors_sorted)
    n = len(slots)

    # 1. Each materialized virtual monitor reports the PLANNED resolution AND
    #    DPI through magent's own list_monitors() view -- the mixed-DPI
    #    precondition in physical pixels. Virtuals are laid out left-to-right in
    #    add order, so virtuals_sorted[k] <-> specs[k] <-> plan.monitors[k].
    virtuals_sorted = sorted(virtuals, key=lambda m: m.x)
    for k, (w, _h, dpi) in enumerate(specs):
        vm = virtuals_sorted[k]
        assert vm.w == w, (
            f"{name}: virtual {k}: width {vm.w} != planned {w} "
            f"(events: {lab.events[-12:]})"
        )
        assert round(vm.scale_factor * 100) == dpi, (
            f"{name}: virtual {k}: DPI {round(vm.scale_factor * 100)}% != {dpi}% "
            f"(events: {lab.events[-12:]})"
        )

    # 2. Launch one real wt window per monitor via the real --go pipeline.
    unique = uuid.uuid4().hex[:8]
    marker = f"mddr-{unique}"
    names = [f"mddr{unique}s{i}" for i in range(n)]
    titles = [f"magent:{nm}" for nm in names]

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
                        "title": f"mddr{unique}",
                        "windows": [{"name": nm} for nm in names],
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
            f"{name}: --go failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        handles = wait_until(
            lambda: (h := md_handles(plat, titles)) and len(h) == n and h,
            timeout=lab_harness.MATERIALIZE_TIMEOUT,
        )
        assert handles, (
            f"{name}: expected {n} windows {titles}; visible magent: windows: "
            f"{[t for t in plat.snapshot_windows() if t.startswith('magent:')]}"
        )

        # 3. Each window i sits in slots[i], on monitor i -- physical pixels,
        #    mixed DPI. The virtual-monitor windows (i >= 1) are the payload.
        for i, title in enumerate(titles):
            assert_in_slot(window_rect(handles[title]), slots[i], f"{name} window {i}")
            assert slots[i].monitor_index == i
    finally:
        leftovers = close_and_verify_gone(plat, titles, marker)
        emit_events(lab, f"after replay {name}")
        assert not leftovers, (
            f"{name}: cleanup left windows/processes behind: {leftovers}"
        )
