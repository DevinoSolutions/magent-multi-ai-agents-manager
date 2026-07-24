"""Window-title grammar — the single source for every magent window title.

    title := "magent:" [ "[" glyph "]" " " ] name
    glyph := "!" (needs-input) | "x" (error) | "+" (done)

The badge sits at the FRONT (right after the prefix) because taskbars truncate
title tails; a clean ``magent:name`` title is the quiet working/idle state. Every
consumer that reads titles goes through ``parse_title`` and every producer
through ``make_title`` — nothing else may hand-build an ``magent:`` string.
Constraint: project names must not themselves start with the ``[?] `` badge
shape; ``parse_title`` would strip it (accepted — enforced by convention).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from magent.config import WindowConfig

MAGENT_TITLE_PREFIX = "magent:"

# Attention states worth a badge; keys match magent.agent_state values.
# working/idle deliberately have no badge — quiet title = nothing needs you.
STATE_BADGES: dict[str, str] = {
    "needs-input": "!",
    "error": "x",
    "done": "+",
}
_GLYPH_TO_STATE: dict[str, str] = {v: k for k, v in STATE_BADGES.items()}


def get_leaf_name(path: str) -> str:
    normalized = path.replace("\\", "/").rstrip("/")
    return normalized.rsplit("/", 1)[-1] if "/" in normalized else normalized


def make_title(name: str, state: str | None = None, *, prefix: bool = True) -> str:
    """Render a window title for ``name``, badged when ``state`` warrants one.

    With ``prefix=False`` (the ``windowTitlePrefix`` setting off) the title is
    the bare ``name`` — no ``magent:`` prefix and no state badge, since a badge
    without the prefix would leave ``parse_title`` unable to recognize it. The
    caller opts out per its config; this module never imports config itself.
    """
    if not prefix:
        return name
    glyph = STATE_BADGES.get(state) if state else None
    if glyph is not None:
        return f"{MAGENT_TITLE_PREFIX}[{glyph}] {name}"
    return f"{MAGENT_TITLE_PREFIX}{name}"


def parse_title(title: str) -> tuple[str, str | None] | None:
    """Split a window title into ``(name, state)``.

    Returns None for titles that are not magent's (no ``magent:`` prefix).
    An unrecognized badge glyph is treated as part of the name rather than
    dropped, so a future glyph added by a newer writer degrades readably.
    """
    if not title.startswith(MAGENT_TITLE_PREFIX):
        return None
    rest = title[len(MAGENT_TITLE_PREFIX) :]
    if len(rest) >= 4 and rest[0] == "[" and rest[2] == "]" and rest[3] == " ":
        state = _GLYPH_TO_STATE.get(rest[1])
        if state is not None:
            return rest[4:], state
    return rest, None


def generate_titles(
    title: str | None,
    path: str,
    windows: list[WindowConfig] | None,
) -> list[str]:
    base = title or get_leaf_name(path)
    if not windows:
        return [base]
    titles: list[str] = []
    for i, w in enumerate(windows):
        titles.append(w.name if w.name else (base if i == 0 else f"{base}-{i + 1}"))
    return titles
