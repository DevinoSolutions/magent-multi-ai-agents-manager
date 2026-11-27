"""The launch checklist, driven under a REAL pseudo-terminal.

The state machine is pinned key by key in ``tests/unit/test_checklist.py``; what
only a terminal can prove is the half around it: that the keys a person presses
reach the reader (Tab, Ctrl+A, Backspace, Esc, PgDn), that the frame is
rewritten IN PLACE inside a small window without scrolling it, and that what was
checked is what a launch then acts on.

Assertions read a GRID, not a string: ``tests/e2e/_screen.py`` replays the real
byte stream into a small VT model, because a repainting list leaves every
earlier frame in the raw transcript and "the row is on screen NOW" is a
statement about the drawn terminal. What was launched is read from the real
``--dry-run`` preview, which prints one ``o <name>  new`` row per project the
launch phase was handed -- no terminal, psmux or agent is ever started.

Isolation is the same as every pty tier: HOME and the config bases redirected
into tmp, every ``MAGENT_*`` stripped and the isolation opt-outs set.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import TYPE_CHECKING

import pytest

from tests.e2e._pty import Budget, Pty
from tests.e2e._screen import Screen

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

pytestmark = [pytest.mark.e2e, pytest.mark.pty]

if sys.platform == "win32":
    pytest.importorskip("winpty", reason="pywinpty needed for the Windows PTY tests")
else:
    pytest.importorskip("pexpect", reason="pexpect needed for the POSIX PTY tests")

CHECKLIST_BUDGET_S = 120.0
ROWS, COLS = 20, 100

ENTER = "\r"
ESC = "\x1b"
TAB = "\t"
BACKSPACE = "\x7f"
CTRL_A = "\x01"
CTRL_N = "\x0e"  # Down, as a single byte (see picker._CTRL_ALIASES)
CTRL_P = "\x10"  # Up

_GROUPS = {"web": 14, "data": 13, "tool": 12}
ODD_ONE = "zephyr-service"


def _child_env(home: Path) -> dict[str, str]:
    """A clean child environment: real PATH etc. preserved, every ``MAGENT_*``
    stripped, HOME + config bases redirected into tmp, colour disabled."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith("MAGENT_")
        and k.upper() not in ("PYTHONPATH", "PYTHONHOME")
    }
    home_s = str(home)
    drive, tail = os.path.splitdrive(home_s)
    env["USERPROFILE"] = home_s
    env["HOMEDRIVE"] = drive
    env["HOMEPATH"] = tail or "\\"
    env["HOME"] = home_s
    # The opt-outs every pty tier sets: no keyboard hook, no real upload
    # server, no priority sweep over the real fleet, no Session-0 hand-off.
    env["MAGENT_HOTKEY_SUPERVISOR"] = "0"
    env["MAGENT_UPLOAD_SUPERVISOR"] = "0"
    env["MAGENT_ATTENTION_SUPERVISOR"] = "0"
    env["MAGENT_PSMUX_BOOST"] = "0"
    env["MAGENT_NODE_SYNC"] = "0"
    env["MAGENT_IDLE_REAP"] = "0"
    env["MAGENT_SESSION0_POLICY"] = "allow"
    env["APPDATA"] = home_s
    env["LOCALAPPDATA"] = home_s
    env["XDG_CONFIG_HOME"] = home_s
    env["NO_COLOR"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["TERM"] = env.get("TERM", "xterm")
    return env


def _fleet_config(tmp_path: Path) -> Path:
    """~40 generic projects in three groups, one of them out of pattern."""
    projects = []
    names = [
        f"{prefix}-{n:02d}"
        for prefix, count in _GROUPS.items()
        for n in range(1, count + 1)
    ]
    for name in [*names, ODD_ONE]:
        (tmp_path / "proj" / name).mkdir(parents=True)
    for prefix, count in _GROUPS.items():
        for n in range(1, count + 1):
            name = f"{prefix}-{n:02d}"
            projects.append({"path": f"proj/{name}", "title": name, "group": prefix})
    projects.append({"path": f"proj/{ODD_ONE}", "title": ODD_ONE, "group": "tool"})
    cfg = tmp_path / "magent.config.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 4,
                "baseDir": str(tmp_path),
                "settings": {"uploadServer": False},
                "projects": projects,
            }
        ),
        encoding="utf-8",
    )
    return cfg


def _spawn(tmp_path: Path) -> Pty:
    home = tmp_path / "home"
    home.mkdir()
    cfg = _fleet_config(tmp_path)
    return Pty(
        [sys.executable, "-m", "magent", "--config", str(cfg), "--go", "--dry-run"],
        env=_child_env(home),
        cwd=str(tmp_path),
        dimensions=(ROWS, COLS),
        budget=Budget(CHECKLIST_BUDGET_S),
    )


def _screen(pty: Pty) -> Screen:
    """The terminal as drawn right now: the real byte stream, replayed."""
    pty.drain()
    return Screen(ROWS, COLS).feed(pty.raw)


def _await(pty: Pty, what: str, ok: Callable[[Screen], bool]) -> Screen:
    """Wait, BOUNDED, until the drawn screen satisfies ``ok``. Draining on every
    poll is load-bearing: a repaint that fills the kernel's pty buffer blocks
    the child until someone reads it."""
    end = time.monotonic() + (pty._budget.clamp(30.0) if pty._budget else 30.0)
    while True:
        screen = _screen(pty)
        if ok(screen):
            return screen
        if time.monotonic() >= end:
            pytest.fail(f"never saw {what}\n--- screen ---\n{screen.text}")
        time.sleep(0.05)


def _launched(pty: Pty) -> list[str]:
    """Project names the dry-run launch preview listed, in order."""
    return re.findall(r"^\s*o (\S+)\s+new\b", pty.transcript, flags=re.MULTILINE)


def _finish(pty: Pty) -> int:
    try:
        return pty.wait_exit()
    finally:
        pty.close()


def test_a_forty_project_checklist_fits_a_small_terminal_and_filters(tmp_path):
    pty = _spawn(tmp_path)
    try:
        first = _await(pty, "the first frame", lambda s: "40 of 40 selected" in s.text)

        # The list is taller than the window, and says so instead of silently
        # cutting off: pinned header, a "more" counter, the footer -- and the
        # frame never scrolled the terminal.
        assert "Launch which projects?" in first.text
        assert "filter: _" in first.text
        assert any(
            line.strip().startswith("v ") and "more" in line for line in first.lines
        )
        assert "web-01" in first.text
        assert "tool-12" not in first.text
        assert "type to filter" in first.text
        assert first.scrolls == 0

        # Typing narrows by the picker's ranking. The first character also
        # drops the all-checked default, so the footer starts from nothing.
        pty.send_keys("zeph")
        found = _await(
            pty,
            "the filtered list",
            lambda s: "filter: zeph_" in s.text and "1 shown" in s.text,
        )
        assert ODD_ONE in found.text
        assert "web-01" not in found.text
        assert "0 of 40 selected" in found.text
        assert "enter launch zephyr-service" in found.text

        # Space checks it through the filter; Esc clears the filter and the
        # check survives, with the whole list back on screen.
        pty.send_keys(" ")
        _await(pty, "one selected", lambda s: "1 of 40 selected" in s.text)
        pty.send_keys(ESC)
        back = _await(
            pty,
            "the unfiltered list",
            lambda s: "filter: _" in s.text and "web-01" in s.text,
        )
        assert "1 of 40 selected" in back.text

        # Ctrl+A checks only the VISIBLE rows: "tool-1" shows tool-10..12 and,
        # as an in-order subsequence, tool-01 -- four rows, nothing else...
        pty.send_keys("tool-1")
        _await(pty, "four matches", lambda s: "4 shown" in s.text)
        pty.send_keys(CTRL_A)
        _await(pty, "five selected", lambda s: "5 of 40 selected" in s.text)
        # ...and pressed again it clears those four, leaving the row the filter
        # was hiding exactly as it was.
        pty.send_keys(CTRL_A)
        _await(pty, "one selected", lambda s: "1 of 40 selected" in s.text)
        pty.send_keys(ESC)
        _await(pty, "list restored", lambda s: "filter: _" in s.text)

        # Tab walks to the next section: the cursor lands on its first row, with
        # that section's heading in view.
        pty.send_keys(TAB)
        jumped = _await(
            pty,
            "the second section",
            lambda s: any(
                line.strip().startswith(">") and "data-01" in line for line in s.lines
            ),
        )
        assert any(line.strip() == "data" for line in jumped.lines)

        pty.send_keys(ENTER)
        assert _finish(pty) == 0
    finally:
        pty.close()

    # The dry run lists at most one row per tile slot, so the launched set is
    # kept small enough to be read back whole.
    assert _launched(pty) == [ODD_ONE], pty.transcript


def test_enter_with_nothing_selected_launches_the_highlighted_project(tmp_path):
    pty = _spawn(tmp_path)
    try:
        _await(pty, "the first frame", lambda s: "40 of 40 selected" in s.text)
        pty.send_keys("tool-03")
        _await(
            pty,
            "the match highlighted",
            lambda s: "enter launch tool-03" in s.text,
        )

        pty.send_keys(ENTER)
        assert _finish(pty) == 0
    finally:
        pty.close()

    assert _launched(pty) == ["tool-03"], pty.transcript


def test_backspace_edits_the_filter_and_arrows_move_within_it(tmp_path):
    pty = _spawn(tmp_path)
    try:
        _await(pty, "the first frame", lambda s: "40 of 40 selected" in s.text)
        pty.send_keys("web-0x")
        _await(pty, "no match", lambda s: "no match for web-0x" in s.text)
        pty.send_keys(BACKSPACE)
        _await(
            pty,
            "ten matches",
            lambda s: "filter: web-0_" in s.text and "10 shown" in s.text,
        )

        pty.send_keys(CTRL_N + CTRL_N)  # web-01 -> web-03
        pty.send_keys(" ")
        _await(pty, "one selected", lambda s: "1 of 40 selected" in s.text)
        pty.send_keys(ENTER)
        assert _finish(pty) == 0
    finally:
        pty.close()

    assert _launched(pty) == ["web-03"], pty.transcript


def test_esc_on_an_empty_filter_cancels_and_launches_nothing(tmp_path):
    pty = _spawn(tmp_path)
    try:
        _await(pty, "the first frame", lambda s: "40 of 40 selected" in s.text)
        pty.send_keys("web")
        _await(pty, "filtered", lambda s: "filter: web_" in s.text)
        pty.send_keys(ESC)  # clears the filter, does not cancel
        _await(pty, "cleared", lambda s: "filter: _" in s.text)
        pty.send_keys(ESC)  # now it cancels
        pty.expect("Nothing launched.")
        assert _finish(pty) == 0
    finally:
        pty.close()

    assert _launched(pty) == []


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="ConPTY re-encodes escape sequences on input; the Windows console "
    "codes are pinned against the real reader in tests/unit/test_picker.py",
)
class TestEscapeSequenceKeys:
    PGDN = "\x1b[6~"
    PGUP = "\x1b[5~"
    HOME = "\x1b[H"
    END = "\x1b[F"
    BTAB = "\x1b[Z"

    def test_end_pgup_home_and_shift_tab_reach_the_reader(self, tmp_path):
        pty = _spawn(tmp_path)
        try:
            _await(pty, "the first frame", lambda s: "40 of 40 selected" in s.text)

            pty.send_keys(self.END)
            at_end = _await(
                pty,
                "the last row",
                lambda s: any(
                    line.strip().startswith(">") and ODD_ONE in line for line in s.lines
                ),
            )
            assert any(line.strip().startswith("^ ") for line in at_end.lines)
            assert not any(line.strip().startswith("v ") for line in at_end.lines)

            pty.send_keys(self.PGUP)
            _await(
                pty,
                "a page up",
                lambda s: (
                    not any(
                        line.strip().startswith(">") and ODD_ONE in line
                        for line in s.lines
                    )
                ),
            )

            pty.send_keys(self.HOME)
            _await(
                pty,
                "the first row",
                lambda s: any(
                    line.strip().startswith(">") and "web-01" in line
                    for line in s.lines
                ),
            )

            pty.send_keys(self.BTAB)  # wraps to the last section's start
            _await(
                pty,
                "the last section",
                lambda s: any(
                    line.strip().startswith(">") and "tool-01" in line
                    for line in s.lines
                ),
            )
            pty.send_keys(ESC)
            pty.expect("Nothing launched.")
            assert _finish(pty) == 0
        finally:
            pty.close()
