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
import re
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


# The alternate-screen entry the stand-in TUI writes as its first act on EVERY
# dial. Nothing else in a pane ever emits it: the supervisor deliberately never
# enters or leaves the alternate screen (DESIGN.md, "the status line owns the
# bottom row"), so a SECOND occurrence means "the next connection has started
# drawing", and means nothing else.
ALT_SCREEN_ON = "\x1b[?1049h"

# An ESC, or an ESC + a CSI that never got its final byte, at the very end of a
# snapshot. A cut there is not a rendering: `Screen` prints the leftovers as
# text, at the cursor, on the user's row.
_DANGLING_ESCAPE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*)?\Z")


def _during_the_outage(raw: str) -> str:
    """``raw`` cut where the NEXT connection starts drawing.

    THE SNAPSHOT'S BOUNDARY HAS TO COME FROM THE STREAM, NOT FROM LUCK. `expect`
    returns as soon as its needle lands in a chunk, but it cannot stop the child
    writing, and `Pty._raw` grows by whole 4096-byte reads -- so `pty.raw`
    straight after `expect("dialing")` holds "everything up to the dialing
    repaint" only if the reader happened to stop there. It often does not: the
    supervisor redials the instant the countdown ends, the stand-in's first act
    on dial 2 is `ESC[?1049h ESC[2J` (a full-screen CLEAR) followed by a
    row-by-row repaint, and the pane then prints its closing "detached from
    demo." line. One read can return all of it.

    Two of those over-long snapshots reproduce the reported failure EXACTLY --
    assertions 1-3 green, "a row other than the bottom one changed" red -- when
    replayed through the real `_screen.Screen` with the real byte shapes:

    * cut inside dial 2's repaint, between the typed row and the box below it:
      row 11 is blank because the clear landed and the repaint had not reached
      it yet;
    * cut past the whole repaint: the "detached" line lands at the restored
      cursor and `\\n` (ONLCR'd to `\\r\\n` by the pty) puts it on row 11.

    Why macos-latest and not the others: the POSIX driver only reads when
    `expect` asks (`read_nonblocking`), so a test descheduled for ~100ms finds
    the dialing repaint, the whole of dial 2 and the closing line waiting in one
    chunk. The Windows driver drains continuously off a daemon thread, so the
    dialing write is almost always captured on its own, and ubuntu runners are
    far less contended than the 3-core macOS ones.

    Nothing is weakened by cutting here: every byte of dial 1's frame and every
    outage repaint is still in the snapshot, so a status line that landed off
    the bottom row still fails. What is removed is only what the product wrote
    AFTER the outage it is being judged on.
    """
    parts = raw.split(ALT_SCREEN_ON)
    return _DANGLING_ESCAPE.sub("", ALT_SCREEN_ON.join(parts[:2]))


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
            mid_outage = _during_the_outage(pty.raw)
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
            mid_outage = _during_the_outage(pty.raw)
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
            frame_drawn = _render(_during_the_outage(pty.raw)).scrolls
            pty.expect("dialing", timeout=60)
            mid_outage = _during_the_outage(pty.raw)
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


class TestTheOutageSnapshotIsBoundedByTheStream:
    """The harness property the frame assertions above rest on.

    No pty and no child: these replay the byte shapes the pane really writes
    through the real screen model, at the read boundaries a loaded runner can
    really produce. They exist because the alternative -- discovering the
    boundary is wrong via an intermittent macOS failure -- costs a CI job and
    tells you nothing about which byte did it. Same reasoning as
    ``test_pty_driver.py``: a harness that can lie has to be pinned like
    product code.
    """

    _FRAME_TOP_1BASED = pane_mod.FRAME_TOP_ROW
    _PROMPT_1BASED = pane_mod.PROMPT_ROW

    def _frame(self, token: str) -> str:
        """One dial's worth of stand-in TUI output, in one flush.

        Written as one string on purpose: the stand-in's stdout is a tty and
        none of its writes carries a newline, so all of them reach the pty in a
        single write -- which is why an over-long snapshot cuts either at a
        frame boundary or inside one, never at a half-written escape.
        """
        typed = f"| > {token}"
        box = "+" + "-" * 38 + "+"
        return (
            f"{ALT_SCREEN_ON}\x1b[2J"
            f"\x1b[{self._FRAME_TOP_1BASED};1H{pane_mod.FRAME_TOP}"
            f"\x1b[{self._FRAME_TOP_1BASED + 1};1HFRAME-BODY assistant reply text"
            f"\x1b[{self._PROMPT_1BASED - 1};1H{box}"
            f"\x1b[{self._PROMPT_1BASED};1H{typed}"
            f"\x1b[{self._PROMPT_1BASED + 1};1H{box}"
            f"\x1b[{pane_mod.HINT_ROW};1H{pane_mod.HINT}"
            f"\x1b[{self._PROMPT_1BASED};{len(typed) + 1}H"
        )

    def _outage(self) -> str:
        """Three real ``StatusLine.show`` writes: save, bottom row, erase,
        yellow text, restore."""
        return "".join(
            f"\x1b7\x1b[{ROWS};1H\x1b[2K\x1b[33m{text}\x1b[0m\x1b8"
            for text in (
                "reconnecting to user@stand-in (attempt 1) -- retry in 2s",
                "reconnecting to user@stand-in (attempt 1) -- retry in 1s",
                "reconnecting to user@stand-in (attempt 1) -- dialing",
            )
        )

    def _frame_rows(self, token: str) -> dict[int, str]:
        """Every row but the bottom one, as the frozen frame has them."""
        box = "+" + "-" * 38 + "+"
        rows = dict.fromkeys(range(ROWS - 1), "")
        rows[pane_mod.FRAME_TOP_ROW - 1] = pane_mod.FRAME_TOP
        rows[pane_mod.FRAME_TOP_ROW] = "FRAME-BODY assistant reply text"
        rows[pane_mod.PROMPT_ROW - 2] = box
        rows[pane_mod.PROMPT_ROW - 1] = f"| > {token}"
        rows[pane_mod.PROMPT_ROW] = box
        return rows

    def _overruns(self, token: str) -> dict[str, str]:
        """The streams a single over-long read can hand back."""
        outage = self._frame(token) + self._outage()
        redraw = self._frame(token)
        # `_echo` writes text + "\n" and this one's text OPENS with "\n"; the
        # pty's ONLCR makes both of them "\r\n". That leading newline is why
        # the line lands one row BELOW the restored cursor -- on the prompt
        # box's bottom border rather than on the sentence itself.
        detached = "\r\n  \x1b[32m+\x1b[0m detached from \x1b[1mdemo\x1b[0m.\r\n"
        return {
            "the outage alone": outage,
            "plus all of the next dial's repaint": outage + redraw,
            "plus the pane's closing line": outage + redraw + detached,
            "cut inside the next dial's repaint": outage
            + redraw[: redraw.index(f"\x1b[{self._PROMPT_1BASED + 1};1H")],
        }

    def test_every_over_long_read_still_shows_the_frozen_frame(self):
        token = "TYPED-BUT-UNSENT-deadbeef"
        expected = self._frame_rows(token)
        for label, raw in self._overruns(token).items():
            screen = _render(_during_the_outage(raw))
            assert {row: screen.line(row) for row in expected} == expected, (
                f"{label}: a row other than the bottom one changed\n{screen.text}"
            )
            assert screen.line(ROWS - 1).endswith("dialing"), (
                f"{label}: the outage's own status line was cut away\n{screen.text}"
            )

    def test_the_cut_is_what_makes_the_difference(self):
        # The counter-proof: without it, two of those same four streams fail
        # the frame assertion exactly the way macos-latest reported it. If this
        # ever goes green, the race is gone and `_during_the_outage` can go too.
        token = "TYPED-BUT-UNSENT-deadbeef"
        expected = self._frame_rows(token)
        broken = [
            label
            for label, raw in self._overruns(token).items()
            if {row: _render(raw).line(row) for row in expected} != expected
        ]
        assert broken == [
            "plus the pane's closing line",
            "cut inside the next dial's repaint",
        ]

    def test_a_snapshot_that_ends_mid_escape_prints_no_garbage(self):
        # The other way a live-stream snapshot lies: cut between the ESC and
        # the final byte of a CSI and the model prints the leftovers as text,
        # at the cursor -- which is parked on the user's sentence.
        token = "TYPED-BUT-UNSENT-deadbeef"
        raw = self._frame(token) + self._outage()
        for cut in range(1, 8):
            screen = _render(_during_the_outage(raw + "\x1b[?1049h"[:cut]))
            assert screen.line(pane_mod.PROMPT_ROW - 1) == f"| > {token}", (
                f"a {cut}-byte partial escape reached the grid\n{screen.text}"
            )


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
