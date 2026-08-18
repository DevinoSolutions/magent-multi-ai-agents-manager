"""The outage status line, drawn onto a REAL frozen frame in a REAL terminal.

THE BUG THESE PIN. ``magent attach`` panes run ``magent-attach-client``, which
runs ssh, which runs a remote psmux hosting a full-screen agent TUI. When wi-fi
drops, ssh dies -- and the terminal is left in the ALTERNATE SCREEN showing that
TUI's last frame, cursor parked wherever the TUI put it, which for an agent pane
is inside the prompt box at the end of whatever the user had typed and not yet
sent. The supervisor then drew its reconnect status line with a bare
``\\r\\x1b[2K`` at that cursor: carriage return, erase the whole row. The row it
erased was the user's unsent sentence. They reported it as "the reconnect
warning replaces the text I typed in Claude Code".

WHY A PTY AND A SCREEN MODEL, rather than assertions on the output string. A
pty hands back what a child WROTE; "the typed text is still on screen" is a
statement about what the terminal DREW. Only a grid can answer it -- so the REAL
byte stream that the REAL supervisor writes into a REAL pty is replayed through
``_screen.Screen`` (a small VT model, not a fake of anything the product does)
and the assertions read cells. Nothing here monkeypatches a terminal write; the
only substitutions live in ``_attach_pane.py`` and are the network boundary
(which program stands in for ssh, and the host probe there is no host for).
Its real-wire counterpart, with ssh and the probe unsubstituted, is
``tests/e2e/test_ssh_real.py::test_typed_text_survives_a_real_reconnect``.
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

from tests.e2e import _attach_pane as pane_mod
from tests.e2e._pty import Budget, Pty
from tests.e2e._screen import Screen

pytestmark = [pytest.mark.e2e, pytest.mark.pty]

if sys.platform == "win32":
    pytest.importorskip("winpty", reason="pywinpty needed for the Windows PTY tests")
else:
    pytest.importorskip("pexpect", reason="pexpect needed for the POSIX PTY tests")

ROWS, COLS = 24, 80

# One wall-clock allowance per pane, clamping every stage inside it.
#
# These tests have no network in them -- the stand-in TUI and the supervisor
# both run locally and the whole thing finishes in a couple of seconds -- but
# their per-stage timeouts sum to ~360s EACH, so five of them could ask for
# half an hour inside a job that has twenty minutes. Nobody chose that number;
# it is just what five reasonable-looking stages add up to. The total is chosen,
# and it is what a hosted runner under load could plausibly need for a local
# process to paint a countdown, with room to spare.
PANE_BUDGET_S = 120.0


def _env() -> dict[str, str]:
    """A child environment that reaches the installed product, colour off."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith("MAGENT_")
        # COLUMNS/LINES are checked by `shutil.get_terminal_size` BEFORE it asks
        # the real terminal, so a shell that exported them would make the pane
        # address a bottom row the pty does not have -- and the row assertions
        # below are the whole test.
        and k.upper() not in ("PYTHONPATH", "PYTHONHOME", "COLUMNS", "LINES")
    }
    env["NO_COLOR"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["TERM"] = env.get("TERM", "xterm")
    return env


def _render(raw: str) -> Screen:
    return Screen(rows=ROWS, cols=COLS).feed(raw)


class TestTheFrozenFrameSurvivesTheStatusLine:
    """One outage, drawn on top of a real frozen alternate-screen frame."""

    def test_the_users_typed_text_is_never_erased_or_moved(self, tmp_path):
        """THE REGRESSION GUARD, stated the way the user reported it.

        The stand-in TUI dies with ``TYPED-...`` sitting in its prompt box and
        the caret at the end of it. The supervisor then paints a full outage --
        a 2s countdown at 1s granularity plus the "dialing" repaint, so at least
        three writes -- and the assertions are made on the GRID at that moment:
        the sentence is still there, still on its own row, still spelled the
        same, and the row it is on has not moved (a scroll would preserve the
        text and still shove the frame around, which is not "preserved").
        """
        token = f"TYPED-BUT-UNSENT-{uuid.uuid4().hex[:8]}"
        state = tmp_path / "state"
        pty = Pty(
            pane_mod.pane_argv("frozen", token, state),
            env=_env(),
            cwd=str(tmp_path),
            dimensions=(ROWS, COLS),
            budget=Budget(PANE_BUDGET_S),
        )
        try:
            pty.expect(pane_mod.PANE_READY, timeout=60)
            pty.expect(pane_mod.HINT, timeout=60)  # the frame is drawn
            pty.expect("dialing", timeout=60)  # the outage is fully painted
            mid_outage = pty.raw
            pty.expect(pane_mod.PANE_DONE, timeout=120)
            status = pty.wait_exit(timeout=60)
        finally:
            pty.close()

        assert status == 0, f"pane did not finish cleanly\n{pty.transcript}"
        screen = _render(mid_outage)
        report = f"\n--- screen ---\n{screen.text}\n--- raw ---\n{mid_outage!r}"

        assert token in screen.text, f"the typed text was erased{report}"
        # Row-exact: the stand-in drew it on PROMPT_ROW and nothing may move it.
        assert screen.row_of(token) == pane_mod.PROMPT_ROW - 1, (
            f"the typed text moved rows{report}"
        )
        assert screen.line(pane_mod.PROMPT_ROW - 1) == f"| > {token}", (
            f"the typed row was partially overwritten{report}"
        )
        # ...and so is every other row of the frame. Stated as "the whole screen
        # above the bottom row is exactly what the TUI drew and nothing else",
        # which is stronger than spot-checking the interesting rows: a repaint
        # that landed anywhere it should not fails here even if it missed the
        # prompt by luck.
        expected = dict.fromkeys(range(ROWS - 1), "")
        expected[pane_mod.FRAME_TOP_ROW - 1] = pane_mod.FRAME_TOP
        expected[pane_mod.FRAME_TOP_ROW] = "FRAME-BODY assistant reply text"
        expected[pane_mod.PROMPT_ROW - 2] = "+" + "-" * 38 + "+"
        expected[pane_mod.PROMPT_ROW - 1] = f"| > {token}"
        expected[pane_mod.PROMPT_ROW] = "+" + "-" * 38 + "+"
        assert {row: screen.line(row) for row in expected} == expected, (
            f"a row other than the bottom one changed{report}"
        )

    def test_the_status_line_lives_on_the_bottom_row_and_nowhere_else(self, tmp_path):
        """Where the outage IS allowed to draw: exactly one row, the last one.

        The stand-in puts a hint line on the bottom row on purpose -- that row
        is the one-row cost of this design, and pinning it here is what keeps
        the cost one row rather than "wherever the cursor was".
        """
        token = f"TYPED-BUT-UNSENT-{uuid.uuid4().hex[:8]}"
        pty = Pty(
            pane_mod.pane_argv("frozen", token, tmp_path / "state"),
            env=_env(),
            cwd=str(tmp_path),
            dimensions=(ROWS, COLS),
            budget=Budget(PANE_BUDGET_S),
        )
        try:
            pty.expect(pane_mod.PANE_READY, timeout=60)
            pty.expect("dialing", timeout=60)
            mid_outage = pty.raw
            pty.expect(pane_mod.PANE_DONE, timeout=120)
            pty.wait_exit(timeout=60)
        finally:
            pty.close()

        screen = _render(mid_outage)
        report = f"\n--- screen ---\n{screen.text}"
        assert "reconnecting" in screen.line(ROWS - 1), (
            f"the status line is not on the bottom row{report}"
        )
        # Every other row is either frame or blank -- the status text appears
        # exactly once on the whole screen.
        assert sum("reconnecting" in line for line in screen.lines) == 1, (
            f"the status line was drawn more than once{report}"
        )
        assert screen.in_alt_screen, (
            "the pane left the alternate screen, which discards the frozen "
            f"frame the user asked to keep looking at{report}"
        )

    def test_the_frame_never_scrolls(self, tmp_path):
        """No newline, ever, for the whole of an outage.

        A scroll in the alternate screen does not make a free row, it destroys
        the top one -- there is no scrollback to catch it. This is the property
        that makes "own a fresh line" the wrong answer here, so it is pinned
        rather than described.
        """
        token = f"TYPED-BUT-UNSENT-{uuid.uuid4().hex[:8]}"
        pty = Pty(
            pane_mod.pane_argv("frozen", token, tmp_path / "state"),
            env=_env(),
            cwd=str(tmp_path),
            dimensions=(ROWS, COLS),
            budget=Budget(PANE_BUDGET_S),
        )
        try:
            pty.expect(pane_mod.PANE_READY, timeout=60)
            pty.expect(pane_mod.HINT, timeout=60)
            frame_drawn = _render(pty.raw).scrolls
            pty.expect("dialing", timeout=60)
            mid_outage = pty.raw
            pty.expect(pane_mod.PANE_DONE, timeout=120)
            pty.wait_exit(timeout=60)
        finally:
            pty.close()

        assert _render(mid_outage).scrolls == frame_drawn, (
            "the outage scrolled the screen\n"
            f"--- screen ---\n{_render(mid_outage).text}"
        )

    def test_the_reattach_repaints_the_true_frame_over_our_row(self, tmp_path):
        """The other half of the contract: we clean up only what we drew.

        When the connection comes back the remote redraws everything -- so the
        status line has to be gone, and the frame (typed text included) has to
        be the REMOTE's current one rather than the ghost that was on screen a
        second ago. What the user actually asked for is not "keep the frozen
        pixels" but "my sentence is still there when I get back to it".
        """
        token = f"TYPED-BUT-UNSENT-{uuid.uuid4().hex[:8]}"
        pty = Pty(
            pane_mod.pane_argv("frozen", token, tmp_path / "state"),
            env=_env(),
            cwd=str(tmp_path),
            dimensions=(ROWS, COLS),
            budget=Budget(PANE_BUDGET_S),
        )
        try:
            pty.expect(pane_mod.PANE_READY, timeout=60)
            pty.expect("dialing", timeout=60)
            pty.expect(pane_mod.PANE_DONE, timeout=120)
            pty.wait_exit(timeout=60)
        finally:
            pty.close()

        screen = _render(pty.raw)
        report = f"\n--- screen ---\n{screen.text}"
        assert "reconnecting" not in screen.text, (
            f"the status line outlived the outage{report}"
        )
        assert f"| > {token}" in screen.text, (
            f"the reattached frame lost the typed text{report}"
        )
        # Row-agnostic on purpose: the pane's closing "detached from ..."
        # message is a deliberate permanent line and scrolls the screen the way
        # any output does. What is being pinned is that the sentence came back,
        # not that the frame is pixel-identical to the ghost that replaced it.


class TestKeystrokesTypedDuringAnOutage:
    """The bonus question, answered rather than assumed.

    stdin stays the same terminal for the whole outage and this process never
    reads it, so bytes typed while no ssh child exists sit in the TERMINAL's
    input buffer -- and the next ssh child, which inherits that same terminal,
    reads them. The user's "continue to type in the prompt section" therefore
    already works: what they type mid-outage is forwarded to the remote agent
    once the connection is back. This test exists to keep it working; the
    tempting "drain stdin so stray keys cannot corrupt the frame" would throw
    away input the user meant to send.
    """

    def test_typing_during_an_outage_reaches_the_next_connection(self, tmp_path):
        state = tmp_path / "state"
        typed = f"typed-mid-outage-{uuid.uuid4().hex[:8]}"
        pty = Pty(
            pane_mod.pane_argv("typing", typed, state),
            env=_env(),
            cwd=str(tmp_path),
            dimensions=(ROWS, COLS),
            budget=Budget(PANE_BUDGET_S),
        )
        try:
            pty.expect(pane_mod.PANE_READY, timeout=60)
            # Provably mid-outage: the countdown is on screen and no child of
            # the supervisor exists to read anything.
            pty.expect("retry in", timeout=60)
            pty.send_line(typed)
            pty.expect(pane_mod.PANE_DONE, timeout=120)
            pty.wait_exit(timeout=60)
        finally:
            pty.close()

        captured = (state / "typed.bin").read_bytes().decode("utf-8", "replace")
        assert typed in captured, (
            "keystrokes typed during the outage did not reach the reconnected "
            f"session; the next child read {captured!r}\n{pty.transcript}"
        )
