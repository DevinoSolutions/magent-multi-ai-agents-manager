"""Fake pane probes and process snapshots for the idle-pane pins.

A pane's agent counts as gone only on POSITIVE proof: the pane's own process
(``#{pane_pid}``) is readable, and no agent runs anywhere under it in the
process snapshot. ``#{pane_current_command}`` alone cannot say so -- psmux
reports the pane's foreground DESCENDANT, which is ``bash`` (or ``grep``, or an
MCP server, or ``pwsh``) whenever Claude Code is running a tool.

These fakes stand in for every half of that answer -- the foreground reading,
the pane pid, and the Toolhelp snapshot -- so no test reads a real psmux server
or this machine's real process list (which, on the box this was written on,
holds a live fleet of 31 agents).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

# What every fake pane's own process hangs off: the psmux server. Any pid that
# is not a pane pid works.
SERVER_PID = 4000


def pane_tree(pane_pid: int, *chain: str) -> list[tuple[str, int, int]]:
    """One pane as snapshot entries ``(image, pid, parent pid)``: its pwsh at
    ``pane_pid`` and ``chain`` as a parent -> child line under it.

    ``pane_tree(100, "cmd.exe", "claude.exe", "bash.exe")`` is magent's own
    launch shape (``cmd /c claude ...`` typed into the pane's pwsh) with the
    agent's Bash tool running; ``pane_tree(100)`` is a pane at its prompt.
    """
    entries = [("pwsh.exe", pane_pid, SERVER_PID)]
    parent = pane_pid
    for depth, image in enumerate(chain, start=1):
        pid = pane_pid + depth
        entries.append((image, pid, parent))
        parent = pid
    return entries


@dataclass
class PaneProbes:
    """What the code under test asked for: one entry per process snapshot
    taken, and the session names of every ``pane_pids`` fan-out."""

    snapshots: list[int] = field(default_factory=list)
    pid_probes: list[list[str]] = field(default_factory=list)


def fake_process_side(
    monkeypatch,
    *,
    pids: Mapping[str, int | None],
    snapshot: list[tuple[str, int, int]] | None,
) -> PaneProbes:
    """Answer ``psmux.pane_pids`` from ``pids`` (None = unreadable) and
    ``procs.snapshot_processes`` with ``snapshot`` (None = it failed)."""
    probes = PaneProbes()

    def _pane_pids(names, psmux=None):
        probes.pid_probes.append(list(names))
        return {n: pids.get(n) for n in names}

    def _snapshot():
        probes.snapshots.append(1)
        return snapshot

    monkeypatch.setattr("magent.psmux.pane_pids", _pane_pids, raising=False)
    monkeypatch.setattr("magent.procs.snapshot_processes", _snapshot)
    return probes


def fake_panes(
    monkeypatch,
    *,
    foreground: Mapping[str, str],
    pids: Mapping[str, int | None],
    snapshot: list[tuple[str, int, int]] | None,
) -> PaneProbes:
    """Every pane probe answered from tables: ``foreground`` is each session's
    ``#{pane_current_command}`` ("" = unreadable), plus ``fake_process_side``.

    Every foreground primitive answers from the same table, so a pin states the
    VERDICT, not which primitive the code happens to call.
    """
    monkeypatch.setattr(
        "magent.psmux.pane_current_command",
        lambda name, psmux=None: foreground.get(name, ""),
    )
    monkeypatch.setattr(
        "magent.psmux.pane_current_commands",
        lambda names, psmux=None: {n: foreground.get(n, "") for n in names},
    )
    return fake_process_side(monkeypatch, pids=pids, snapshot=snapshot)
