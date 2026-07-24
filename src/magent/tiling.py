"""The single window-resolve-and-place helper shared by run_magent's
launch-path tiling and cli._tile_titles's attach-path tiling (R13 residual --
see audit/stage2/E9.md). Both call sites used to hand-roll their own
snapshot/retry loop with no shared helper; this is now the one place that
logic lives, so a fix here reaches both callers.
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
    mode: str  # "magent-name" (legacy alias: "md-name") | "exact" | "contains"
    slot: TileSlot  # destination rect (carries monitor_index for screen labelling)
    name: str = ""  # display label for callbacks; defaults to key

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.key


def _lookup(snap: dict[str, object], key: str, mode: str) -> object | None:
    # "md-name" is the pre-rename spelling, still accepted as a deprecated alias.
    if mode in ("magent-name", "md-name"):
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
    settle_s: float = 0.0,
    on_placed: Callable[[Placement], None] | None = None,
    on_missing: Callable[[Placement], None] | None = None,
) -> tuple[list[Placement], list[Placement]]:
    """Resolve each placement's window and move it into its slot.

    Takes one snapshot and places everything already visible, then retries
    the rest on a shared poll loop -- bounded by the slowest mode among the
    still-pending placements -- before giving up. Returns ``(placed,
    missing)``; every still-missing placement is logged as a WARNING via
    ``get_logger("launch")`` before ``on_missing`` runs for it.
    """
    if settle_s:
        time.sleep(settle_s)

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
        deadline = max(
            RETRY_SECS_CONTAINS if p.mode == "contains" else RETRY_SECS_EXACT
            for p in pending
        )
        for _ in range(deadline):
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
