"""Offline half of doctor-replay: parse + plan + slot math, everywhere.

Runs in the normal unit gate on every OS (no virtual-display driver, no real
windows). Two layers:

* the committed sample doctor reports (``fixtures/doctor_reports/*.json``) parse
  into monitors and, fed through ``grid.compute_grid``, reproduce the committed
  golden slots byte-for-byte -- this is where the negative-origin
  "left-of-primary" arrangement math is regression-locked (the live lab replays
  resolution+DPI only; see ``tests/platform/doctor_replay.py``);
* parse/plan edge cases: a report with no ``monitors`` key, an absurd scale, the
  720p@125 -> 100 physical-floor snap, and negative origins surviving a
  round-trip.

The live materialization + real ``--go`` tiling assertions are
``tests/platform/test_doctor_replay.py`` (monitor_lab CI tier only).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from magent.grid import MonitorRect, compute_grid
from tests.platform.doctor_replay import (
    DoctorReportError,
    parse_doctor_report,
    plan_replay,
)

_REPORTS = Path(__file__).parent.parent / "platform" / "fixtures" / "doctor_reports"
_GOLDENS = _REPORTS / "_goldens.json"


def _report_names() -> list[str]:
    return sorted(p.stem for p in _REPORTS.glob("*.json") if not p.stem.startswith("_"))


def _text(name: str) -> str:
    return (_REPORTS / name).with_suffix(".json").read_text()


def _goldens() -> dict[str, object]:
    return json.loads(_GOLDENS.read_text())


# --------------------------------------------------------------------------- #
# fixtures parse + reproduce the golden slot math
# --------------------------------------------------------------------------- #


def test_fixtures_present() -> None:
    """The golden index lists exactly the committed sample reports."""
    slots = _goldens()["slots"]
    assert isinstance(slots, dict)
    assert set(_report_names()) == set(slots)


@pytest.mark.parametrize("name", _report_names())
def test_report_parses_to_monitors(name: str) -> None:
    monitors = parse_doctor_report(_text(name))
    assert monitors
    assert all(isinstance(m, MonitorRect) for m in monitors)


@pytest.mark.parametrize("name", _report_names())
def test_golden_slots_match_compute_grid(name: str) -> None:
    """compute_grid over the parsed report reproduces the committed slots
    byte-for-byte -- the exact-coordinate pin, negative origins included."""
    golden = _goldens()
    layout = golden["layout"]
    assert isinstance(layout, dict)
    monitors = parse_doctor_report(_text(name))
    slots = compute_grid(monitors, int(layout["cols"]), int(layout["rows"]))
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
    slots_by_name = golden["slots"]
    assert isinstance(slots_by_name, dict)
    assert got == slots_by_name[name]


def test_left_of_primary_fixture_has_negative_origin() -> None:
    """The flagship arrangement fixture really does place a monitor left of the
    primary (x < 0) -- the classic tiling bug class this coverage exists for."""
    monitors = parse_doctor_report(_text("laptop_plus_external_left"))
    assert min(m.x for m in monitors) < 0
    assert any(m.is_primary and m.x == 0 for m in monitors)


# --------------------------------------------------------------------------- #
# parse edge cases
# --------------------------------------------------------------------------- #


def test_parse_bare_monitors_list() -> None:
    monitors = parse_doctor_report(json.dumps([{"x": 0, "y": 0, "w": 1920, "h": 1080}]))
    assert len(monitors) == 1
    assert monitors[0].scale_factor == 1.0  # default when omitted
    assert monitors[0].is_primary is False


def test_parse_missing_monitors_key_is_clear_error() -> None:
    blob = json.dumps({"ok": True, "checks": [], "failures": 0})
    with pytest.raises(DoctorReportError, match="no 'monitors' key"):
        parse_doctor_report(blob)


def test_parse_empty_monitors_is_error() -> None:
    with pytest.raises(DoctorReportError, match="zero monitors"):
        parse_doctor_report(json.dumps({"monitors": []}))


def test_parse_malformed_geometry_is_error() -> None:
    with pytest.raises(DoctorReportError, match="missing 'h'"):
        parse_doctor_report(json.dumps({"monitors": [{"x": 0, "y": 0, "w": 1920}]}))


def test_parse_non_numeric_field_is_error() -> None:
    with pytest.raises(DoctorReportError, match="not a number"):
        parse_doctor_report(
            json.dumps({"monitors": [{"x": 0, "y": 0, "w": "wide", "h": 1080}]})
        )


def test_parse_rejects_non_json() -> None:
    with pytest.raises(DoctorReportError, match="not valid JSON"):
        parse_doctor_report("<not json>")


# --------------------------------------------------------------------------- #
# plan edge cases
# --------------------------------------------------------------------------- #


def _mon(
    w: int, h: int, scale: float, x: int = 0, primary: bool = False
) -> MonitorRect:
    return MonitorRect(x=x, y=0, w=w, h=h, is_primary=primary, scale_factor=scale)


def test_plan_orders_left_to_right() -> None:
    plan = plan_replay([_mon(1920, 1080, 1.0, x=1920), _mon(2560, 1440, 1.0, x=-2560)])
    assert [pm.x for pm in plan.monitors] == [-2560, 1920]


def test_plan_snaps_nonstandard_scale() -> None:
    # 1.33 -> 133% -> nearest standard step is 125%.
    plan = plan_replay([_mon(3840, 2160, 1.33)])
    pm = plan.monitors[0]
    assert pm.dpi_percent == 125
    assert any("snapped" in d for d in pm.deviations)


def test_plan_caps_absurd_scale_to_physical_max() -> None:
    # 999% is impossible on any panel; 3840x2160 is height-capped at
    # 2160*100/768 = 281.25 -> largest standard step <= 281 is 250%.
    plan = plan_replay([_mon(3840, 2160, 9.99)])
    pm = plan.monitors[0]
    assert pm.dpi_percent == 250
    assert any("unachievable" in d for d in pm.deviations)


def test_plan_720p_at_125_snaps_down_to_100() -> None:
    # 1280x720 @125% -> effective 1024x576, height below the 768 floor -> the
    # only achievable step is 100%.
    plan = plan_replay([_mon(1280, 720, 1.25)])
    pm = plan.monitors[0]
    assert pm.dpi_percent == 100
    assert any("unachievable" in d for d in pm.deviations)


def test_plan_standard_topology_has_no_scale_deviation() -> None:
    # 2560x1440 @150% is physically fine and a standard step: no DPI deviation.
    plan = plan_replay([_mon(2560, 1440, 1.5, primary=True)])
    pm = plan.monitors[0]
    assert pm.dpi_percent == 150
    assert not any("snapped" in d or "unachievable" in d for d in pm.deviations)


def test_plan_records_arrangement_and_primary_deviations() -> None:
    plan = plan_replay(
        [_mon(2560, 1440, 1.0, x=-2560), _mon(1920, 1200, 1.5, x=0, primary=True)]
    )
    joined = " ".join(plan.deviations)
    assert "arrangement" in joined
    assert "primary" in joined
    # lab_specs are the (w, h, dpi) add-order triples, left-to-right.
    assert plan.lab_specs() == [(2560, 1440, 100), (1920, 1200, 150)]


def test_plan_flags_nonstandard_resolution() -> None:
    plan = plan_replay([_mon(1234, 987, 1.0)])
    assert any(
        "not a standard parsec-vdd mode" in d for d in plan.monitors[0].deviations
    )


def test_plan_empty_is_error() -> None:
    with pytest.raises(DoctorReportError):
        plan_replay([])


def test_all_deviations_flattens_per_monitor() -> None:
    plan = plan_replay([_mon(1280, 720, 1.25, primary=True)])
    flat = plan.all_deviations()
    assert any("monitor 0" in d for d in flat)
