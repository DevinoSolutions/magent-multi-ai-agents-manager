"""``magent watch`` -- the live fleet table -- under a REAL pseudo-terminal.

Every other test of ``watch`` (``tests/unit/test_watch.py``) is a ``CliRunner``
``--once`` against a fake platform: no tty, no loop, no clear-and-redraw and no
key handling, so ``_poll_key`` (``msvcrt`` on Windows, ``select`` elsewhere) has
never run the way a user's keyboard reaches it. This tier closes that.

What is real: the CLI process (``python -m magent watch``) under a genuine pty
(pexpect on POSIX, pywinpty/ConPTY on Windows), the state store on disk, the
WRITER of that store (the shipped ``magent.state_hook`` fed real Claude Code
lifecycle-event JSON on stdin, exactly as the hooks ``magent hooks install``
wires do), the attention engine's ordering, the redraw loop and the key reads.

What is asserted is what a human reads on the SCREEN: the byte stream is replayed
through ``_screen.Screen`` because ConPTY repaints by diff, so the child's raw
output is not the picture -- the grid is.

What is substituted: nothing but the desktop. A digit press asks the platform for
the row's window; the project names are unique to this run so nothing on the
machine's real desktop can match, and the observable is the product's own "no
window found for <name>" line -- which names the ROW the digit routed to. (The
focus call itself is a real-desktop behaviour: the CI-only platform tiers own it.)

Isolation: identical posture to ``test_pty_menu`` -- the HOME family redirected
into tmp (the store, config and logs never touch the real ``~/.magent``), every
``MAGENT_*`` stripped, the supervisors/boost/hand-off opt-outs set, no multiplexer
and no window is created. Every wait is bounded by one ``Budget``.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from tests.e2e._pty import Budget, Pty
from tests.e2e._screen import Screen
from tests.e2e.test_pty_menu import _child_env

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [pytest.mark.e2e, pytest.mark.pty]

_ROWS, _COLS = 50, 160
# Room for the worst honest case: a TCC-blocked macOS runner spends two 10s
# osascript timeouts (the window snapshot, then the title search) on EACH digit
# press before the miss line appears, twice, on top of interpreter start-up.
_BUDGET_S = 180.0
_INTERVAL_S = "1"

_ROW = re.compile(r"^\s*(\d)\s+(\S+)\s+(needs-input|error|done|working|idle|parked)\b")


def _hook(
    budget: Budget, env: dict[str, str], event: str, cwd: Path, **extra: object
) -> None:
    """One REAL lifecycle event through the shipped writer, stdin-piped."""
    payload = {
        "hook_event_name": event,
        "cwd": str(cwd),
        "session_id": uuid.uuid4().hex,
        **extra,
    }
    result = subprocess.run(
        [sys.executable, "-m", "magent.state_hook", "--source", "claude"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=budget.clamp(60.0),
        env=env,
        check=False,
    )
    assert result.returncode == 0, f"state hook failed: {result.stderr}"


def _screen(pty: Pty) -> Screen:
    pty.drain()
    return Screen(rows=_ROWS, cols=_COLS).feed(pty.raw)


def _table(screen: Screen) -> list[tuple[int, str, str]]:
    """``(row number, name, state)`` for every table row on the grid."""
    found = []
    for line in screen.lines:
        match = _ROW.match(line)
        if match:
            found.append((int(match.group(1)), match.group(2), match.group(3)))
    return found


def _wait_screen(pty: Pty, budget: Budget, predicate, what: str, timeout: float = 30):
    """Poll the replayed grid until ``predicate(screen)`` holds; bounded, and the
    failure carries the grid the user would have been looking at."""
    deadline = time.monotonic() + budget.clamp(timeout)
    while True:
        screen = _screen(pty)
        if predicate(screen):
            return screen
        if time.monotonic() >= deadline:
            pytest.fail(f"never saw {what}; the screen was:\n{screen.text}")
        time.sleep(0.05)


@pytest.fixture
def rig(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = _child_env(home)
    unique = uuid.uuid4().hex[:8]
    names = {role: f"mgw{unique}-{role}" for role in ("ask", "ship", "grind", "nap")}
    dirs = {}
    for role, name in names.items():
        dirs[role] = tmp_path / name
        dirs[role].mkdir()
    cfg = tmp_path / "magent.config.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 3,
                "projects": [
                    {"path": str(dirs[role]), "title": names[role], "tool": "probe"}
                    for role in names
                ],
                "settings": {
                    "defaultTool": "probe",
                    "tools": {"probe": "rem magent-pty-watch-test"},
                    "uploadServer": False,
                },
            }
        ),
        encoding="utf-8",
    )
    work = tmp_path / "work"
    work.mkdir()
    budget = Budget(_BUDGET_S)
    spawned: list[Pty] = []

    def spawn() -> Pty:
        pty = Pty(
            [
                sys.executable,
                "-m",
                "magent",
                "--config",
                str(cfg),
                "watch",
                "--interval",
                _INTERVAL_S,
            ],
            env=env,
            cwd=str(work),
            dimensions=(_ROWS, _COLS),
            budget=budget,
        )
        spawned.append(pty)
        return pty

    try:
        yield SimpleNamespace(
            env=env, names=names, dirs=dirs, budget=budget, spawn=spawn
        )
    finally:
        for pty in spawned:
            pty.close()


def _seed(rig) -> None:
    """Four sessions, written OUT of urgency order, by the real writer."""
    env, d = rig.env, rig.dirs
    _hook(rig.budget, env, "SessionStart", d["nap"])  # idle
    _hook(rig.budget, env, "UserPromptSubmit", d["grind"])  # working
    _hook(rig.budget, env, "Stop", d["ship"])  # done
    _hook(
        rig.budget,
        env,
        "Notification",
        d["ask"],
        message="Claude needs your permission to use Bash",
    )  # needs-input


def _expected_initial(names):
    return [
        (1, names["ask"], "needs-input"),
        (2, names["ship"], "done"),
        (3, names["grind"], "working"),
        (4, names["nap"], "idle"),
    ]


def _press(pty: Pty, digit: str) -> None:
    # POSIX stdin is line-buffered outside a raw tty ("digits need Enter" in
    # watch._poll_key's own words); Windows reads the console key directly. The
    # trailing newline after the digit is an expected extra key the watch loop
    # ignores.
    pty.send_keys(digit if sys.platform == "win32" else digit + "\n")


def test_rows_arrive_most_urgent_first_from_the_real_hook_store(rig):
    _seed(rig)
    pty = rig.spawn()

    screen = _wait_screen(
        pty,
        rig.budget,
        lambda s: len(_table(s)) == 4,
        "all four sessions in the table",
    )

    assert _table(screen) == _expected_initial(rig.names), screen.text
    assert "4 session(s)" in screen.text
    assert "1-9 focus window" in screen.text and "q quit" in screen.text


def test_a_state_change_reorders_and_a_session_end_removes_a_row_live(rig):
    _seed(rig)
    pty = rig.spawn()
    _wait_screen(pty, rig.budget, lambda s: len(_table(s)) == 4, "the first full frame")
    n = rig.names

    # The working session now needs you: equal urgency with `ask`, but newer,
    # so it sorts above it -- and `done` slides to third. No restart, no key.
    _hook(
        rig.budget,
        rig.env,
        "Notification",
        rig.dirs["grind"],
        message="Claude needs your permission to use Bash",
    )
    screen = _wait_screen(
        pty,
        rig.budget,
        lambda s: _table(s)[:1] == [(1, n["grind"], "needs-input")],
        "the working session promoted to the top",
    )
    assert _table(screen) == [
        (1, n["grind"], "needs-input"),
        (2, n["ask"], "needs-input"),
        (3, n["ship"], "done"),
        (4, n["nap"], "idle"),
    ], screen.text

    # A session ending clears its record, and the row goes with it.
    _hook(rig.budget, rig.env, "SessionEnd", rig.dirs["nap"])
    screen = _wait_screen(
        pty,
        rig.budget,
        lambda s: len(_table(s)) == 3,
        "the ended session's row to disappear",
    )
    assert n["nap"] not in screen.text
    assert "3 session(s)" in screen.text


def test_a_digit_routes_to_that_rows_session_and_q_quits_cleanly(rig):
    _seed(rig)
    pty = rig.spawn()
    n = rig.names
    _wait_screen(pty, rig.budget, lambda s: len(_table(s)) == 4, "the first frame")

    # Row 1 is the most urgent session; its name is what the miss line must carry.
    _press(pty, "1")
    _wait_screen(
        pty,
        rig.budget,
        lambda s: f"no window found for {n['ask']}" in s.text,
        "the focus miss naming row 1's session",
        timeout=45,
    )

    # ...and row 3 is a DIFFERENT session: the digit indexes the rows, it does
    # not just fire at the top one.
    _press(pty, "3")
    _wait_screen(
        pty,
        rig.budget,
        lambda s: f"no window found for {n['grind']}" in s.text,
        "the focus miss naming row 3's session",
        timeout=45,
    )

    pty.send_keys("q" if sys.platform == "win32" else "q\n")
    assert pty.wait_exit(timeout=30) == 0, pty.transcript
