"""Fake pane probes and process snapshots for the idle-pane pins.

A pane's agent counts as gone only on POSITIVE proof: the pane's own process
(``#{pane_pid}``) is readable, and no agent runs anywhere under it in the
process snapshot. ``#{pane_current_command}`` alone cannot say so -- psmux
reports the pane's foreground DESCENDANT, which is ``bash`` (or ``grep``, or an
MCP server, or ``pwsh``) whenever Claude Code is running a tool.

These fakes stand in for every part of that answer -- the foreground reading,
the pane pid, the Toolhelp snapshot, and the pane console's client list
(``procs.console_clients``) -- so no test reads a real psmux server, this
machine's real process list (which, on the box this was written on, holds a
live fleet of 31 agents), or attaches a helper to a real console.
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
    taken, the session names of every ``pane_pids`` fan-out, and the pid list
    of every ``console_clients`` call."""

    snapshots: list[int] = field(default_factory=list)
    pid_probes: list[list[str]] = field(default_factory=list)
    console_probes: list[list[int]] = field(default_factory=list)


def fake_process_side(
    monkeypatch,
    *,
    pids: Mapping[str, int | None],
    snapshot: list[tuple[str, int, int]] | None,
    consoles: Mapping[int, frozenset[int] | None] | None = None,
) -> PaneProbes:
    """Answer ``psmux.pane_pids`` from ``pids`` (None = unreadable),
    ``procs.snapshot_processes`` with ``snapshot`` (None = it failed), and
    ``procs.console_clients`` from ``consoles``: a pid absent from the map
    reports ``frozenset({pid})`` (its own console, alone), an explicit None
    stays None, and every call's pid list is recorded."""
    probes = PaneProbes()

    def _pane_pids(names, psmux=None):
        probes.pid_probes.append(list(names))
        return {n: pids.get(n) for n in names}

    def _snapshot():
        probes.snapshots.append(1)
        return snapshot

    def _console_clients(query_pids, *, timeout=5.0):
        probes.console_probes.append(list(query_pids))
        table = consoles or {}
        return {
            pid: table[pid] if pid in table else frozenset({pid}) for pid in query_pids
        }

    monkeypatch.setattr("magent.psmux.pane_pids", _pane_pids)
    monkeypatch.setattr("magent.procs.snapshot_processes", _snapshot)
    monkeypatch.setattr("magent.procs.console_clients", _console_clients)
    # The once-per-episode veto latch is process state: every test starts with
    # no episode open, whatever ran before it.
    monkeypatch.setattr("magent.psmux._console_vetoes_logged", {})
    return probes


def fake_panes(
    monkeypatch,
    *,
    foreground: Mapping[str, str],
    pids: Mapping[str, int | None],
    snapshot: list[tuple[str, int, int]] | None,
    consoles: Mapping[int, frozenset[int] | None] | None = None,
) -> PaneProbes:
    """Every pane probe answered from tables: ``foreground`` is each session's
    ``#{pane_current_command}`` ("" = unreadable), plus ``fake_process_side``.
    """
    monkeypatch.setattr(
        "magent.psmux.pane_current_commands",
        lambda names, psmux=None: {n: foreground.get(n, "") for n in names},
    )
    return fake_process_side(
        monkeypatch, pids=pids, snapshot=snapshot, consoles=consoles
    )
