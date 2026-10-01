"""Live-session value types shared by the idle probe and the reaper.

A stdlib-only leaf, deliberately SEPARATE from ``sessions/__init__.py`` (which
imports ``sessions/claude``): a probe implementation in ``claude.py`` needs
these types, and importing them from the package ``__init__`` would cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

# A conservative session-id shape: a leading alnum then up to 127 more of
# alnum/underscore/hyphen. ALWAYS used with fullmatch, so a value with a space,
# a path separator, or 128+ chars can never reach a shell as a resume argument.
SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


class LiveSession(NamedTuple):
    """One agent process the probe verified is alive. ``created`` is the FILETIME
    creation time (identity), ``quiet`` is the tool's own 'idle' verdict,
    ``status_ts`` is when the tool last set that status (epoch seconds)."""

    pid: int
    created: int
    image: str
    session_id: str
    cwd: str
    status: str
    status_ts: float
    kind: str
    quiet: bool


class SessionScan(NamedTuple):
    """One read of a tool's session store. ``sessions`` maps each live agent pid
    to its LiveSession. ``unusable`` holds the pids named by files that are there
    but could not be used: an agent nobody can read, so unknown, never absent --
    the reaper vetoes a pane tree that holds one."""

    sessions: Mapping[int, LiveSession]
    unusable: frozenset[int]


@dataclass(frozen=True)
class IdleProbe:
    """The only agent-specific code the reaper calls. ``sessions_by_pid`` reads
    the tool's session store (None = the store could not be read);
    ``last_activity`` is the newest transcript mtime for a session (None = no
    main transcript / unreadable). A frozen dataclass, not a NamedTuple, so a
    tool cannot accidentally iterate or unpack it as a pair of callables."""

    sessions_by_pid: Callable[[Path], SessionScan | None]
    last_activity: Callable[[LiveSession, Path], float | None]
