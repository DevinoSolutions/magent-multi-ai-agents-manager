from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Rect:
    x: int
    y: int
    w: int
    h: int


@dataclass
class MonitorRect(Rect):
    is_primary: bool = False
    scale_factor: float = 1.0


@dataclass
class TileSlot(Rect):
    monitor_index: int = 0
    label: str = ""


# Windows Terminal (and most GUI terminals) refuse to shrink below a
# DPI-scaled minimum size. Requesting a narrower tile does not shrink the
# window -- it overflows and overlaps its neighbour. Measured: WT bottoms out
# at ~837px wide on a 175% display, i.e. ~480 logical px times the DPI scale.
# Cap the column/row count per monitor so every tile clears that floor. A
# 1920px monitor at 175% therefore drops from 3 columns to 2 (960px tiles)
# while a 3840px monitor at 250% keeps all 3 (1280px tiles).
MIN_TILE_W = 480
MIN_TILE_H = 320


def _fit(count: int, extent: int, scale: float, min_logical: int) -> int:
    floor_px = min_logical * max(1.0, scale)
    return max(1, min(count, int(extent // floor_px)))


def compute_grid(monitors: list[MonitorRect], cols: int, rows: int) -> list[TileSlot]:
    monitors_sorted = sorted(monitors, key=lambda m: m.x)
    slots: list[TileSlot] = []
    for i, m in enumerate(monitors_sorted):
        c_cols = _fit(cols, m.w, m.scale_factor, MIN_TILE_W)
        c_rows = _fit(rows, m.h, m.scale_factor, MIN_TILE_H)
        col_edges = [m.x + round(c * m.w / c_cols) for c in range(c_cols + 1)]
        row_edges = [m.y + round(r * m.h / c_rows) for r in range(c_rows + 1)]
        for r in range(c_rows):
            for c in range(c_cols):
                slots.append(
                    TileSlot(
                        x=col_edges[c],
                        y=row_edges[r],
                        w=col_edges[c + 1] - col_edges[c],
                        h=row_edges[r + 1] - row_edges[r],
                        monitor_index=i,
                        label=f"r{r + 1}c{c + 1}",
                    )
                )
    return slots
