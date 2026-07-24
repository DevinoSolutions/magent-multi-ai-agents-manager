"""Doctor-report -> monitor-lab replay planner (pure, import-harmless on POSIX).

``magent doctor --json`` captures a user's exact monitor topology under the
top-level ``monitors`` key (see ``cli/doctor.py``). This module turns one of
those reports into a :class:`ReplayPlan` the virtual-monitor lab
(``monitor_lab.py``) can materialize, so a bug-report topology can be replayed
on a hosted ``windows-latest`` runner and driven through magent's real
``--go`` tiling assertions.

Everything here is PURE (no ctypes, no Win32, no driver) so the whole planner
is unit-testable on every OS in the normal gate
(``tests/unit/test_doctor_replay_offline.py``). The live materialization +
real-window assertions live in ``tests/platform/test_doctor_replay.py`` and run
only under the ``monitor_lab`` CI tier.

Physical constraints baked into :func:`plan_replay` (learned in the Wave-1
spike; mirrored from ``monitor_lab.DPI_VALS`` and the ZOO comment in
``test_monitor_lab_tiling.py``):

* parsec-vdd DPI is set through the CCD SetDPI port, which only accepts the
  standard Windows scale steps in :data:`STANDARD_SCALES` -- a report's exact
  ``scale_factor`` is snapped to the nearest step.
* Windows refuses a scale whose *effective* resolution would drop below
  ~1024x768, so e.g. 1280x720 cannot exceed 100%. :func:`plan_replay` caps each
  monitor's scale to what its resolution physically allows.
* The live lab lays displays out left-to-right to the right of the runner's own
  (immovable) primary, so a report's exact origins -- negative-x
  "left-of-primary" monitors especially -- are NOT reproduced positionally in
  the live lab; that origin math is pinned OFFLINE via ``compute_grid`` over the
  parsed monitors instead. Every such divergence is recorded as a deviation, so
  the replay is never a silent approximation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from magent.grid import MonitorRect

# Standard Windows display-scale steps the CCD SetDPI port accepts (mirrors
# ``monitor_lab.DPI_VALS``). A report scale is snapped to the nearest of these.
STANDARD_SCALES: tuple[int, ...] = (
    100,
    125,
    150,
    175,
    200,
    225,
    250,
    300,
    350,
    400,
    450,
    500,
)

# Windows refuses a scale whose effective (logical) resolution drops below this.
MIN_EFFECTIVE_W = 1024
MIN_EFFECTIVE_H = 768

# Resolutions parsec-vdd reliably advertises (the lab can only attach a driver-
# enumerated mode). A report resolution outside this set still becomes the plan
# target, but is flagged: the lab may snap it and the live assert would surface
# the mismatch loudly.
KNOWN_RESOLUTIONS: frozenset[tuple[int, int]] = frozenset(
    {
        (1280, 720),
        (1366, 768),
        (1440, 900),
        (1600, 900),
        (1680, 1050),
        (1920, 1080),
        (1920, 1200),
        (2048, 1152),
        (2560, 1080),
        (2560, 1440),
        (2560, 1600),
        (3440, 1440),
        (3840, 1600),
        (3840, 2160),
    }
)


class DoctorReportError(ValueError):
    """The blob is not a usable doctor report (no monitors, malformed geometry)."""


@dataclass
class PlannedMonitor:
    """One report monitor mapped to what the lab can actually materialize."""

    # Requested geometry (verbatim from the report), left-to-right ordered.
    x: int
    y: int
    w: int
    h: int
    is_primary: bool
    requested_scale_pct: int
    # Achievable in the lab: same resolution (target), snapped/capped DPI.
    dpi_percent: int
    deviations: list[str] = field(default_factory=list)

    @property
    def scale_factor(self) -> float:
        return self.dpi_percent / 100.0


@dataclass
class ReplayPlan:
    """The full plan: per-monitor achievable specs + every recorded deviation."""

    monitors: list[PlannedMonitor]
    deviations: list[str] = field(default_factory=list)

    def all_deviations(self) -> list[str]:
        """Flatten per-monitor deviations + plan-level ones for loud logging."""
        out = list(self.deviations)
        for i, pm in enumerate(self.monitors):
            out.extend(f"monitor {i} ({pm.w}x{pm.h}): {d}" for d in pm.deviations)
        return out

    def lab_specs(self) -> list[tuple[int, int, int]]:
        """``(width, height, dpi_percent)`` triples in the order the lab should
        ``add`` them -- one virtual display per report monitor, left-to-right."""
        return [(pm.w, pm.h, pm.dpi_percent) for pm in self.monitors]


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #


def _as_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DoctorReportError(f"monitor {field_name!r} is not a number: {value!r}")
    return int(value)


def _monitor_from_dict(raw: object) -> MonitorRect:
    if not isinstance(raw, dict):
        raise DoctorReportError(f"monitor entry is not an object: {raw!r}")
    for required in ("x", "y", "w", "h"):
        if required not in raw:
            raise DoctorReportError(f"monitor entry missing {required!r}: {raw!r}")
    scale_raw = raw.get("scale_factor", 1.0)
    if isinstance(scale_raw, bool) or not isinstance(scale_raw, (int, float)):
        raise DoctorReportError(f"monitor scale_factor is not a number: {scale_raw!r}")
    return MonitorRect(
        x=_as_int(raw["x"], "x"),
        y=_as_int(raw["y"], "y"),
        w=_as_int(raw["w"], "w"),
        h=_as_int(raw["h"], "h"),
        is_primary=bool(raw.get("is_primary", False)),
        scale_factor=float(scale_raw),
    )


def parse_doctor_report(text: str) -> list[MonitorRect]:
    """Parse a ``magent doctor --json`` blob (or a bare monitors list) into
    :class:`MonitorRect`\\ s.

    Accepts either the full doctor envelope ``{"ok", "checks", ..., "monitors"}``
    or a bare ``[{...}, ...]`` monitors list. Raises :class:`DoctorReportError`
    with an actionable message when the blob has no ``monitors`` key (an old
    doctor build) or a monitor entry is malformed.
    """
    try:
        doc: object = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DoctorReportError(f"not valid JSON: {exc}") from exc

    if isinstance(doc, list):
        raw_monitors: object = doc
    elif isinstance(doc, dict):
        if "monitors" not in doc:
            raise DoctorReportError(
                "doctor report has no 'monitors' key -- it was produced by a "
                "magent build that predates topology capture. Re-run "
                "`magent doctor --json` on a current build."
            )
        raw_monitors = doc["monitors"]
    else:
        raise DoctorReportError(f"top-level JSON is neither list nor object: {doc!r}")

    if not isinstance(raw_monitors, list):
        raise DoctorReportError(f"'monitors' is not a list: {raw_monitors!r}")
    if not raw_monitors:
        raise DoctorReportError(
            "doctor report lists zero monitors -- nothing to replay"
        )
    return [_monitor_from_dict(m) for m in raw_monitors]


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #


def _snap_scale(percent: int) -> int:
    """Nearest standard Windows scale step (ties resolve to the lower step)."""
    return min(STANDARD_SCALES, key=lambda s: (abs(s - percent), s))


def _max_scale(w: int, h: int) -> int:
    """Largest standard scale whose effective resolution clears the OS minimum."""
    ceiling = min(w * 100 / MIN_EFFECTIVE_W, h * 100 / MIN_EFFECTIVE_H)
    achievable = [s for s in STANDARD_SCALES if s <= ceiling]
    return max(achievable) if achievable else STANDARD_SCALES[0]


def plan_replay(monitors: list[MonitorRect]) -> ReplayPlan:
    """Map every report monitor to the closest topology the lab can achieve.

    Deterministic and pure. Monitors are ordered left-to-right by requested x
    (the order the lab must ``add`` them). Every snapped DPI, capped DPI,
    non-standard resolution, and the positional arrangement gap is recorded --
    no silent approximation.
    """
    if not monitors:
        raise DoctorReportError("cannot plan a replay for zero monitors")

    ordered = sorted(monitors, key=lambda m: (m.x, m.y))
    planned: list[PlannedMonitor] = []
    for m in ordered:
        requested_pct = round(m.scale_factor * 100)
        devs: list[str] = []

        snapped = _snap_scale(requested_pct)
        if snapped != requested_pct:
            devs.append(
                f"scale {requested_pct}% snapped to nearest standard step {snapped}%"
            )

        cap = _max_scale(m.w, m.h)
        final = min(snapped, cap)
        if final != snapped:
            devs.append(
                f"scale {snapped}% unachievable at {m.w}x{m.h} "
                f"(effective < {MIN_EFFECTIVE_W}x{MIN_EFFECTIVE_H}); capped to {final}%"
            )

        if (m.w, m.h) not in KNOWN_RESOLUTIONS:
            devs.append(
                f"resolution {m.w}x{m.h} is not a standard parsec-vdd mode; the lab "
                "may snap it to the nearest advertised resolution"
            )

        planned.append(
            PlannedMonitor(
                x=m.x,
                y=m.y,
                w=m.w,
                h=m.h,
                is_primary=m.is_primary,
                requested_scale_pct=requested_pct,
                dpi_percent=final,
                deviations=devs,
            )
        )

    plan_devs: list[str] = []
    nontrivial_arrangement = len(ordered) > 1 or any(
        m.x != 0 or m.y != 0 for m in ordered
    )
    if nontrivial_arrangement:
        plan_devs.append(
            "arrangement: monitors are replayed left-to-right (in report x-order) "
            "to the right of the runner's own primary; exact report origins "
            "(negative-x left-of-primary and non-zero y offsets included) are NOT "
            "reproduced positionally in the live lab. That origin math is pinned "
            "offline via compute_grid over the parsed monitors."
        )
    if any(m.is_primary for m in ordered):
        plan_devs.append(
            "the report's primary monitor is materialized as a NON-primary virtual "
            "display; the runner's own screen remains monitor 0 (primary)."
        )
    return ReplayPlan(monitors=planned, deviations=plan_devs)
