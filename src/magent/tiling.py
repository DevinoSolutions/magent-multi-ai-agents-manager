"""The single window-resolve-and-place helper shared by run_magent's
launch-path tiling and cli._tile_titles's attach-path tiling (R13 residual --
see audit/stage2/E9.md). Both call sites used to hand-roll their own
snapshot/retry loop with no shared helper; this is now the one place that
logic lives, so a fix here reaches both callers.

Timing model: there is no up-front wait anywhere in this module. Placement
starts with an immediate snapshot and every window is moved the instant it
shows up in one; ``deadline_s`` only bounds how long we keep polling for
windows that have not appeared yet. A caller whose windows are all already
open therefore finishes in a single sweep, no matter how large its deadline.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from magent.grid import Rect, TileSlot
from magent.log import get_logger
from magent.titles import parse_title

if TYPE_CHECKING:
    from collections.abc import Callable

    from magent.platform import Platform

RETRY_SECS_CONTAINS = 20  # contains-mode windows are slow to appear (e.g. VS Code)
RETRY_SECS_EXACT = 6
POLL_INTERVAL_S = 1.0


@dataclass
class Placement:
    key: str  # match string: bare name for magent-name, exact title, or substring for contains
    mode: str  # "magent-name" | "exact" | "contains"
    slot: TileSlot  # destination rect (carries monitor_index for screen labelling)
    name: str = ""  # display label for callbacks; defaults to key

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.key


def _lookup(snap: dict[str, object], key: str, mode: str) -> object | None:
    if mode == "magent-name":
        # Match magent-owned windows by parsed name so a state badge in
        # the title (titles.make_title) never breaks resolution.
        for title, handle in snap.items():
            parsed = parse_title(title)
            if parsed is not None and parsed[0] == key:
                return handle
        return None
    if mode == "exact":
        return snap.get(key)
    key_lower = key.lower()
    for title, handle in snap.items():
        if key_lower in title.lower():
            return handle
    return None


def place_windows(
    plat: Platform,
    placements: list[Placement],
    *,
    deadline_s: float | None = None,
    on_placed: Callable[[Placement], None] | None = None,
    on_missing: Callable[[Placement], None] | None = None,
) -> tuple[list[Placement], list[Placement]]:
    """Resolve each placement's window and move it into its slot.

    Takes an immediate snapshot and places everything already visible, then
    polls the still-missing set once per ``POLL_INTERVAL_S`` until nothing is
    pending or the budget runs out. ``deadline_s`` is that budget in seconds
    and is a deadline for latecomers, not an up-front wait: windows are placed
    the moment they appear, so a caller whose windows are all up already
    returns after the first sweep having slept zero times. When it is None the
    budget is the slowest mode among the still-pending placements
    (``RETRY_SECS_CONTAINS``/``RETRY_SECS_EXACT``). Returns ``(placed,
    missing)``; every still-missing placement is logged as a WARNING via
    ``get_logger("launch")`` before ``on_missing`` runs for it.
    """
    placed: list[Placement] = []
    pending = list(placements)

    def _sweep() -> None:
        nonlocal pending
        snap = plat.snapshot_windows()
        still_pending = []
        for p in pending:
            handle = _lookup(snap, p.key, p.mode)
            if handle is None:
                still_pending.append(p)
                continue
            plat.move_window(
                handle, Rect(x=p.slot.x, y=p.slot.y, w=p.slot.w, h=p.slot.h)
            )
            placed.append(p)
            if on_placed is not None:
                on_placed(p)
        pending = still_pending

    _sweep()

    if pending:
        # Only the windows that are still missing cost anything here: each
        # poll sweeps and places whatever has since appeared, so the budget is
        # a deadline for latecomers rather than a wait the whole set pays.
        budget_s = (
            deadline_s
            if deadline_s is not None
            else max(
                RETRY_SECS_CONTAINS if p.mode == "contains" else RETRY_SECS_EXACT
                for p in pending
            )
        )
        for _ in range(int(budget_s / POLL_INTERVAL_S)):
            if not pending:
                break
            time.sleep(POLL_INTERVAL_S)
            _sweep()

    if pending:
        log = get_logger("launch")
        for p in pending:
            log.warning(
                "tiling: window not found after retries: key=%r mode=%s", p.key, p.mode
            )
            if on_missing is not None:
                on_missing(p)

    return placed, pending
