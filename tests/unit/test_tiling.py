"""Behavior pins for the two duplicated tiling code paths (R13 residual) plus
direct unit tests for the shared magent.tiling.place_windows helper that
replaces them.

The TestTileTitlesPin/TestRunMagentTilingPin classes below are written
BEFORE the dedup (E9.md Step 1) and must keep passing UNCHANGED after it
(Steps 3-4): they assert observable behavior (which handles move to which
slot rects, which lines echo) -- not which lookup method is used -- so the
same assertions hold pre-conversion (find_window / inline retry) and post
(snapshot_windows / magent.tiling.place_windows). That is the whole
point: a behavior pin that survives the refactor is the drift tripwire.
"""

from __future__ import annotations

import time

import pytest

from magent import cli
from magent.config import load_config
from magent.grid import MonitorRect, Rect, TileSlot
from magent.launch import RunOpts, run_magent
from magent.tiling import (
    RETRY_SECS_CONTAINS,
    RETRY_SECS_EXACT,
    Placement,
    place_windows,
)


class _FakeTilePlat:
    """Self-contained Platform double for tiling tests -- does not reuse
    conftest's FakePlatform so these tests don't hard-depend on E2's landing
    order (E9.md S5). ``windows`` is a single {title: handle} dict (the
    common case), or a list of dicts consumed one-per-snapshot_windows() call
    to simulate a window that only appears after N retries. find_window and
    snapshot_windows always agree on the same underlying dict.
    """

    def __init__(self, windows=None, monitors=None):
        self._sequence = windows if isinstance(windows, list) else [windows or {}]
        self._snap_calls = 0
        self._monitors = (
            monitors
            if monitors is not None
            else [
                MonitorRect(x=0, y=0, w=1920, h=1080, is_primary=True, scale_factor=1.0)
            ]
        )
        self.moved: list[tuple] = []

    def set_dpi_aware(self) -> None:
        pass

    def list_monitors(self):
        return self._monitors

    def _latest(self) -> dict:
        idx = min(self._snap_calls, len(self._sequence) - 1)
        return self._sequence[idx]

    def snapshot_windows(self) -> dict:
        snap = self._latest()
        self._snap_calls += 1
        return snap

    def find_window(self, title, mode="exact"):
        snap = self._latest()
        if mode == "exact":
            return snap.get(title)
        low = title.lower()
        for t, h in snap.items():
            if low in t.lower():
                return h
        return None

    def move_window(self, handle, rect) -> None:
        self.moved.append((handle, rect))


@pytest.fixture
def fake_sleep(monkeypatch):
    """Patches the real time.sleep function object. cli._tile_titles does a
    LOCAL `import time` (pre-conversion), so there is no module-level
    `magent.cli.time` attribute to monkeypatch; `import time` anywhere
    always binds the same sys.modules['time'] object, so patching it here
    reliably intercepts every caller (cli.py pre-conversion, tiling.py/
    launch.py post-conversion) regardless of import style. Records each
    sleep's duration so retry/deadline tests can assert on call count."""
    calls: list[float] = []

    def _sleep(seconds):
        calls.append(seconds)

    monkeypatch.setattr(time, "sleep", _sleep)
    return calls


class TestTileTitlesPin:
    """Pins cli._tile_titles's CURRENT observable behavior. Must pass before
    AND after the Step 3/4 conversion (E9.md Step 1 verify)."""

    def test_moves_known_windows_to_slots(self, monkeypatch, fake_sleep, capsys):
        fp = _FakeTilePlat(windows={"A": 1, "B": 2})
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)

        cli._tile_titles(["A", "B"])

        assert (1, Rect(x=0, y=0, w=960, h=1080)) in fp.moved
        assert (2, Rect(x=960, y=0, w=960, h=1080)) in fp.moved
        out = capsys.readouterr().out
        assert "A" in out and "B" in out

    def test_reports_missing(self, monkeypatch, fake_sleep, capsys):
        fp = _FakeTilePlat(windows={"A": 1})
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)

        cli._tile_titles(["A", "B"])

        assert len(fp.moved) == 1
        out = capsys.readouterr().out
        assert "not found" in out

    def test_no_monitors_returns(self, monkeypatch, capsys, caplog):
        fp = _FakeTilePlat(windows={}, monitors=[])
        monkeypatch.setattr("magent.platform.get_platform", lambda: fp)

        with caplog.at_level("ERROR", logger="magent.launch"):
            result = cli._tile_titles(["A"])

        assert result is None
        assert fp.moved == []
        out = capsys.readouterr().out
        assert "No monitors detected" in out
        assert "no monitors detected" in caplog.text


class TestRunMagentTilingPin:
    """E2's fake_platform/tmp_config fixtures are present (RULING S3-5-3:
    mandatory here, not skipped). Drives the full run_magent orchestrator
    and pins that a newly-launched window lands in slot 0's rect. Must keep
    passing unchanged through Steps 3-4."""

    def test_tiles_new_windows(self, fake_platform, tmp_config, tmp_path, fake_sleep):
        HANDLE = 99

        def _launch_terminal(opts):
            fake_platform.launched_terminals.append(opts)
            fake_platform._windows[opts.title] = HANDLE  # simulate the window appearing

        fake_platform.launch_terminal = _launch_terminal

        config_path = tmp_config(
            {
                "layout": {"columns": 2, "rows": 1},
                "settings": {
                    "tools": {"claude": "claude --continue"},
                    "defaultTool": "claude",
                },
                "projects": [
                    {
                        "path": str(tmp_path),
                        "tool": "claude",
                        "title": "proj",
                        "color": "#336699",
                    }
                ],
            }
        )
        cfg = load_config(config_path)

        rc = run_magent(cfg, RunOpts())

        assert rc == 0
        assert (HANDLE, Rect(x=0, y=0, w=960, h=1080)) in fake_platform.moved


def _slot(x=0, y=0, w=960, h=1080, monitor_index=0):
    return TileSlot(x=x, y=y, w=w, h=h, monitor_index=monitor_index, label="r1c1")


class TestPlaceWindows:
    """Direct unit tests for the new shared helper (E9.md Step 2)."""

    def test_places_found_on_first_pass(self, fake_sleep):
        fp = _FakeTilePlat(windows={"X": 1})
        placed_cb: list[Placement] = []
        placements = [Placement(key="X", mode="exact", slot=_slot())]

        placed, missing = place_windows(fp, placements, on_placed=placed_cb.append)

        assert placed == placements
        assert missing == []
        assert placed_cb == placements
        assert fake_sleep == []  # found immediately -> zero sleeps, no retry
        assert fp.moved == [(1, Rect(x=0, y=0, w=960, h=1080))]

    def test_retries_until_window_appears(self, fake_sleep):
        fp = _FakeTilePlat(windows=[{}, {}, {"X": 42}])
        placements = [Placement(key="X", mode="exact", slot=_slot())]

        placed, missing = place_windows(fp, placements)

        assert [p.key for p in placed] == ["X"]
        assert missing == []
        assert len(fake_sleep) == 2  # appears on the 3rd snapshot -> 2 poll waits
        assert all(s == 1.0 for s in fake_sleep)

    def test_never_found_logs_warning_and_calls_on_missing(self, fake_sleep, caplog):
        fp = _FakeTilePlat(windows={})
        missing_cb: list[Placement] = []
        placements = [Placement(key="ghost", mode="exact", slot=_slot())]

        with caplog.at_level("WARNING", logger="magent.launch"):
            placed, missing = place_windows(
                fp, placements, on_missing=missing_cb.append
            )

        assert placed == []
        assert missing == placements
        assert missing_cb == placements
        assert "ghost" in caplog.text
        assert len(fake_sleep) == RETRY_SECS_EXACT

    def test_settle_sleeps_before_first_snapshot(self, fake_sleep):
        fp = _FakeTilePlat(windows={"X": 1})
        placements = [Placement(key="X", mode="exact", slot=_slot())]

        place_windows(fp, placements, settle_s=3)

        assert fake_sleep[0] == 3

    def test_exact_vs_contains_lookup(self, fake_sleep):
        fp = _FakeTilePlat(windows={"Visual Studio Code - foo": 7})
        exact = Placement(key="Visual Studio Code - foo", mode="exact", slot=_slot())
        contains = Placement(key="foo", mode="contains", slot=_slot())
        no_match = Placement(key="Visual Studio Code", mode="exact", slot=_slot())

        placed, missing = place_windows(fp, [exact, contains, no_match])

        assert {p.key for p in placed} == {"Visual Studio Code - foo", "foo"}
        assert [p.key for p in missing] == ["Visual Studio Code"]

    def test_deadline_uses_contains_when_any_contains(self, fake_sleep):
        fp = _FakeTilePlat(windows={})
        placements = [
            Placement(key="exact-ghost", mode="exact", slot=_slot()),
            Placement(key="contains-ghost", mode="contains", slot=_slot()),
        ]

        place_windows(fp, placements)

        assert len(fake_sleep) == RETRY_SECS_CONTAINS

    def test_magent_name_lookup_matches_badged_and_plain_titles(self, fake_sleep):
        fp = _FakeTilePlat(windows={"magent:[!] api": 7, "magent:web": 8, "api": 9})
        badged = Placement(key="api", mode="magent-name", slot=_slot())
        plain = Placement(key="web", mode="magent-name", slot=_slot())

        placed, missing = place_windows(fp, [badged, plain])

        assert {p.key for p in placed} == {"api", "web"}
        assert missing == []
        # The bare "api" window (handle 9) is NOT magent's; the badged
        # magent: window (handle 7) must win the "api" placement.
        assert (7, Rect(x=0, y=0, w=960, h=1080)) in fp.moved
        assert all(h != 9 for h, _ in fp.moved)

    def test_legacy_md_name_alias_still_resolves(self, fake_sleep):
        # "md-name" is the pre-rename spelling; it must keep resolving so old
        # producers/configs still tile. Canonical value is "magent-name".
        fp = _FakeTilePlat(windows={"magent:api": 7})
        placements = [Placement(key="api", mode="md-name", slot=_slot())]

        placed, missing = place_windows(fp, placements)

        assert [p.key for p in placed] == ["api"]
        assert missing == []

    def test_magent_name_requires_exact_name_not_substring(self, fake_sleep):
        fp = _FakeTilePlat(windows={"magent:api-v2": 7})
        placements = [Placement(key="api", mode="magent-name", slot=_slot())]

        placed, missing = place_windows(fp, placements)

        assert placed == []
        assert [p.key for p in missing] == ["api"]
