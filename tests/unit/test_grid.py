import itertools

from magent.grid import MonitorRect, TileSlot, compute_grid


def _mon(x, y, w, h, primary=False, scale=1.0):
    return MonitorRect(x=x, y=y, w=w, h=h, is_primary=primary, scale_factor=scale)


class TestComputeGrid:
    def test_single_monitor_2x1(self):
        monitors = [_mon(0, 0, 1920, 1080, primary=True)]
        slots = compute_grid(monitors, cols=2, rows=1)
        assert len(slots) == 2
        assert slots[0] == TileSlot(
            x=0, y=0, w=960, h=1080, monitor_index=0, label="r1c1"
        )
        assert slots[1] == TileSlot(
            x=960, y=0, w=960, h=1080, monitor_index=0, label="r1c2"
        )

    def test_single_monitor_2x2(self):
        monitors = [_mon(0, 0, 1920, 1080)]
        slots = compute_grid(monitors, cols=2, rows=2)
        assert len(slots) == 4
        assert slots[0] == TileSlot(
            x=0, y=0, w=960, h=540, monitor_index=0, label="r1c1"
        )
        assert slots[1] == TileSlot(
            x=960, y=0, w=960, h=540, monitor_index=0, label="r1c2"
        )
        assert slots[2] == TileSlot(
            x=0, y=540, w=960, h=540, monitor_index=0, label="r2c1"
        )
        assert slots[3] == TileSlot(
            x=960, y=540, w=960, h=540, monitor_index=0, label="r2c2"
        )

    def test_two_monitors_different_sizes(self):
        monitors = [_mon(0, 0, 1920, 1080), _mon(1920, 0, 2560, 1440)]
        slots = compute_grid(monitors, cols=2, rows=1)
        assert len(slots) == 4
        assert slots[0].x == 0
        assert slots[0].w == 960
        assert slots[1].x == 960
        assert slots[1].w == 960
        assert slots[2].x == 1920
        assert slots[2].w == 1280
        assert slots[3].x == 1920 + 1280
        assert slots[3].w == 1280

    def test_monitors_sorted_by_x(self):
        monitors = [_mon(1920, 0, 1920, 1080), _mon(0, 0, 1920, 1080)]
        slots = compute_grid(monitors, cols=1, rows=1)
        assert slots[0].x == 0
        assert slots[0].monitor_index == 0
        assert slots[1].x == 1920
        assert slots[1].monitor_index == 1

    def test_taskbar_offset(self):
        monitors = [_mon(0, 40, 1920, 1040)]
        slots = compute_grid(monitors, cols=2, rows=1)
        assert slots[0].y == 40
        assert slots[0].h == 1040

    def test_three_monitors_mixed_res(self):
        monitors = [
            _mon(0, 0, 1920, 1080),
            _mon(1920, 0, 2560, 1440),
            _mon(4480, 0, 3840, 2160),
        ]
        slots = compute_grid(monitors, cols=2, rows=1)
        assert len(slots) == 6

    def test_1x1_grid(self):
        monitors = [_mon(0, 0, 1920, 1080)]
        slots = compute_grid(monitors, cols=1, rows=1)
        assert len(slots) == 1
        assert slots[0] == TileSlot(
            x=0, y=0, w=1920, h=1080, monitor_index=0, label="r1c1"
        )

    def test_odd_width_tiles_cover_full_extent(self):
        # 1921 / 3 does not divide evenly; the tiles must still span [0, 1921]
        # edge-to-edge with no gap or overlap and only a 1px width spread.
        monitors = [_mon(0, 0, 1921, 1080)]
        slots = compute_grid(monitors, cols=3, rows=1)
        assert slots[0].x == 0
        assert slots[-1].x + slots[-1].w == 1921
        for a, b in itertools.pairwise(slots):
            assert a.x + a.w == b.x  # touching, no gap/overlap
        assert max(s.w for s in slots) - min(s.w for s in slots) <= 1
        assert sum(s.w for s in slots) == 1921

    def test_dpi_clamps_columns_on_narrow_monitor(self):
        # A 1920px monitor at 175% cannot fit 3 Windows-Terminal columns
        # (min ~837px each); it must drop to 2 columns of 960px.
        monitors = [_mon(0, 0, 1920, 996, scale=1.75)]
        slots = compute_grid(monitors, cols=3, rows=1)
        assert len(slots) == 2
        assert [s.w for s in slots] == [960, 960]

    def test_dpi_keeps_columns_on_wide_hidpi_monitor(self):
        # A 3840px monitor at 250% keeps all 3 columns (1280px each clears
        # the DPI-scaled minimum).
        monitors = [_mon(0, 0, 3840, 2040, scale=2.5)]
        slots = compute_grid(monitors, cols=3, rows=1)
        assert len(slots) == 3
        assert [s.w for s in slots] == [1280, 1280, 1280]

    def test_mixed_dpi_monitors_get_independent_column_counts(self):
        # Real setup: two 250% 4K panels (3 cols) + one 175% 1080p (2 cols).
        monitors = [
            _mon(-3840, 0, 3840, 2040, scale=2.5),
            _mon(0, 0, 3840, 2040, scale=2.5),
            _mon(3840, 0, 1920, 996, scale=1.75),
        ]
        slots = compute_grid(monitors, cols=3, rows=1)
        per_mon = {i: [s for s in slots if s.monitor_index == i] for i in range(3)}
        assert len(per_mon[0]) == 3
        assert len(per_mon[1]) == 3
        assert len(per_mon[2]) == 2
