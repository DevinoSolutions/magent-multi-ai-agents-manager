"""Offline grid-math pins for the monitor-lab zoo topologies.

Runs EVERYWHERE (no virtual-display driver, no real windows): feeds each
committed golden topology -- the ``list_monitors()`` view the ``monitor_lab``
CI tier fabricates -- into ``grid.compute_grid`` and pins the exact slot math.

This closes the offline/grid layer of the multi-monitor coverage gap
(DESIGN.md R4-05): the mixed-DPI slot arithmetic, the per-monitor
column-collapse under the DPI floor, and the physical-pixel partitioning are
regression-locked here without any hardware, while the live placement is
proven by ``tests/platform/test_monitor_lab_tiling.py`` on the CI runner.

The fixtures are golden: regenerate them only via the documented flow (running
``compute_grid`` over the authored topologies) and commit the result -- a drift
here means either the fixtures or ``compute_grid`` changed, and the diff says
which.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from magent.grid import MonitorRect, compute_grid

_FIXTURES = Path(__file__).parent.parent / "platform" / "fixtures" / "topologies"

# Per-monitor column counts each topology MUST produce -- hardcoded literals
# (not derived from the code under test) so the DPI-floor collapse is a true
# pin. triple_720p's virtuals collapse 3 -> 2 columns (1.25 x 480 = 600px
# floor, 1280 // 600 = 2); the 4K panel keeps its full 3 x 2 grid at 200%.
_EXPECTED_COLS_PER_MONITOR = {
    "dual_mixed_dpi": [3, 3, 3],
    "solo_4k": [3, 3],
    "triple_720p": [3, 2, 2, 2],
}


def _topology_names() -> list[str]:
    return sorted(p.stem for p in _FIXTURES.glob("*.json"))


def _load(name: str) -> dict:
    return json.loads((_FIXTURES / name).with_suffix(".json").read_text())


def _monitors(doc: dict) -> list[MonitorRect]:
    return [
        MonitorRect(
            x=m["x"],
            y=m["y"],
            w=m["w"],
            h=m["h"],
            is_primary=m["is_primary"],
            scale_factor=m["scale_factor"],
        )
        for m in doc["monitors"]
    ]


def test_fixtures_present() -> None:
    assert set(_topology_names()) == set(_EXPECTED_COLS_PER_MONITOR), (
        "golden topology fixtures drifted from the documented zoo"
    )


@pytest.mark.parametrize("name", _topology_names())
def test_golden_slots_match_compute_grid(name: str) -> None:
    """compute_grid over the golden topology reproduces the committed slots
    byte-for-byte -- the exact-coordinate pin."""
    doc = _load(name)
    slots = compute_grid(_monitors(doc), doc["layout"]["cols"], doc["layout"]["rows"])
    got = [
        {
            "x": s.x,
            "y": s.y,
            "w": s.w,
            "h": s.h,
            "monitor_index": s.monitor_index,
            "label": s.label,
        }
        for s in slots
    ]
    assert got == doc["expected_slots"]


@pytest.mark.parametrize("name", _topology_names())
def test_dpi_floor_collapses_columns(name: str) -> None:
    """The per-monitor column count is pinned to literals, locking the DPI
    floor's column collapse independently of the coordinate pin."""
    doc = _load(name)
    slots = compute_grid(_monitors(doc), doc["layout"]["cols"], doc["layout"]["rows"])
    rows = doc["layout"]["rows"]
    per_mon: dict[int, int] = {}
    for s in slots:
        per_mon[s.monitor_index] = per_mon.get(s.monitor_index, 0) + 1
    cols_per_mon = [per_mon[i] // rows for i in sorted(per_mon)]
    assert cols_per_mon == _EXPECTED_COLS_PER_MONITOR[name]


@pytest.mark.parametrize("name", _topology_names())
def test_slots_partition_each_monitor(name: str) -> None:
    """Every monitor is tiled by a gap-free, non-overlapping partition whose
    slot rects sum back to the monitor's physical extent -- mixed DPI included."""
    doc = _load(name)
    monitors = _monitors(doc)
    slots = compute_grid(monitors, doc["layout"]["cols"], doc["layout"]["rows"])
    monitors_sorted = sorted(monitors, key=lambda m: m.x)
    for idx, mon in enumerate(monitors_sorted):
        mine = [s for s in slots if s.monitor_index == idx]
        assert mine, f"{name}: monitor {idx} got no slots"
        # Column edges (unique x) are contiguous and span the monitor width.
        xs = sorted({s.x for s in mine})
        for a, b in itertools.pairwise(xs):
            width = next(s.w for s in mine if s.x == a)
            assert a + width == b, f"{name}: column gap/overlap at x={a}"
        last_x = xs[-1]
        last_w = next(s.w for s in mine if s.x == last_x)
        assert xs[0] == mon.x
        assert last_x + last_w == mon.x + mon.w
        # Row edges likewise span the monitor height.
        ys = sorted({s.y for s in mine})
        for a, b in itertools.pairwise(ys):
            height = next(s.h for s in mine if s.y == a)
            assert a + height == b, f"{name}: row gap/overlap at y={a}"
        last_y = ys[-1]
        last_h = next(s.h for s in mine if s.y == last_y)
        assert ys[0] == mon.y
        assert last_y + last_h == mon.y + mon.h
