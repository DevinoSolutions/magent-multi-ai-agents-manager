"""The real-PTY driver's own guarantee: a wait ENDS.

Why this tier exists at all. Everything else under ``tests/e2e`` that drives a
pty is testing magent; this file tests ``tests/e2e/_pty.py``, because that
module is the one place where a bug does not produce a failing test — it
produces NO test result at all. A blocked ``expect`` is not slow, it is silent:
the CI job burns its ``timeout-minutes``, GitHub CANCELS it, and the transcript,
the pytest summary and every test after it are thrown away. That is not a
hypothetical. A Windows ``needs_ssh`` leg sat inside one test for 13m39s,
printed the test's name and nothing else, and took its job down with it — with
zero diagnostics to work from.

The two holes that allowed it, both pinned below, both driven against a REAL
pty (ConPTY on Windows, ``forkpty`` on POSIX) rather than a mock, because a
mocked read is exactly the thing that cannot reproduce either bug:

* **the silent child.** ``pywinpty``'s ``read`` is an untimed ``socket.recv``,
  so a child that produces nothing parks the caller inside it forever and the
  deadline below is never consulted. Measured before the fix: an
  ``expect(timeout=5)`` was still blocked at 45s.
* **the chatty child.** ``expect`` used to ``continue`` on every successful
  read, so a child emitting bytes that never contain the needle looped without
  ever reading the clock. An attach pane in reconnect backoff repaints a status
  line once a second, forever — output, and never the needle. Measured before
  the fix: same 45s, still going.

The assertions are deliberately about the CLOCK and about the MESSAGE, not
about the exact bytes: a driver may take a read slice longer than asked, but it
may never take an order of magnitude longer, and whatever it captured before
giving up has to be in the exception — that transcript is the difference
between a thirty-second fix and another cancelled job.

No ssh, no magent, no network: the children are stock ``python -c`` one-liners,
so this runs in the ordinary e2e suite on every OS and on a dev box.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

from tests.e2e._pty import Budget, Pty, PtyTimeout

pytestmark = [pytest.mark.e2e, pytest.mark.pty]

if sys.platform == "win32":
    pytest.importorskip("winpty", reason="pywinpty needed for the Windows PTY tests")
else:
    pytest.importorskip("pexpect", reason="pexpect needed for the POSIX PTY tests")

# The deadline every case below asks for. Long enough that a slow runner's
# process spawn is not mistaken for a hang, short enough that the whole module
# is a few seconds.
TIMEOUT_S = 5.0

# How far over its own deadline a wait may land and still count as bounded. One
# read slice plus generous room for a loaded hosted runner. The bug this pins
# overshot by 9x and climbing, so the gap between "honored" and "hung" is not
# subtle and this threshold does not need to be tight to catch it.
SLACK_S = 10.0


def _child_env() -> dict[str, str]:
    """A plain child environment, minus the two vars that would lie about the
    terminal size (same reason the other pty tiers strip them)."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() not in ("PYTHONPATH", "PYTHONHOME", "COLUMNS", "LINES")
    }
    env["NO_COLOR"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["TERM"] = env.get("TERM", "xterm")
    return env


def _spawn(body: str, tmp_path) -> Pty:
    return Pty(
        [sys.executable, "-c", body],
        env=_child_env(),
        cwd=str(tmp_path),
        dimensions=(24, 80),
    )


# A child that says NOTHING and stays alive well past the deadline.
_SILENT = "import time; time.sleep(120)"

# A child that never stops talking and never says the needle -- the shape of an
# attach pane repainting its reconnect status line once a second.
_CHATTY = (
    "import sys, time\n"
    "while True:\n"
    "    sys.stdout.write('noise, but not the needle\\r\\n')\n"
    "    sys.stdout.flush()\n"
    "    time.sleep(0.05)\n"
)


class TestExpectAlwaysEnds:
    """``expect`` returns or raises. It never does neither."""

    def test_a_silent_child_times_out_instead_of_hanging(self, tmp_path):
        """THE headline pin. Before the fix this call was still blocked inside
        a single untimed read after nine deadlines had passed."""
        pty = _spawn(_SILENT, tmp_path)
        started = time.monotonic()
        try:
            with pytest.raises(PtyTimeout):
                pty.expect("NEVER-APPEARS", timeout=TIMEOUT_S)
            elapsed = time.monotonic() - started
        finally:
            pty.close()
        assert elapsed < TIMEOUT_S + SLACK_S, (
            f"expect(timeout={TIMEOUT_S}) took {elapsed:.1f}s -- the deadline is "
            "not being enforced, which turns a test failure into a cancelled job"
        )

    def test_a_chatty_child_times_out_instead_of_hanging(self, tmp_path):
        """Output is not progress. A child looping on the WRONG bytes is
        exactly as timed out as a silent one -- the old loop `continue`d on
        every successful read and never reached its own deadline check."""
        pty = _spawn(_CHATTY, tmp_path)
        started = time.monotonic()
        try:
            with pytest.raises(PtyTimeout):
                pty.expect("NEVER-APPEARS", timeout=TIMEOUT_S)
            elapsed = time.monotonic() - started
        finally:
            pty.close()
        assert elapsed < TIMEOUT_S + SLACK_S, (
            f"expect(timeout={TIMEOUT_S}) took {elapsed:.1f}s against a child "
            "that was producing output the whole time"
        )

    def test_a_dead_child_fails_without_waiting_out_the_deadline(self, tmp_path):
        """An already-exited child has nothing left to say, so the wait ends on
        EOF rather than sitting out the full budget -- that is what keeps a
        crashed child a FAST failure instead of a slow one."""
        pty = _spawn("raise SystemExit(3)", tmp_path)
        started = time.monotonic()
        try:
            with pytest.raises(PtyTimeout):
                pty.expect("NEVER-APPEARS", timeout=60.0)
            elapsed = time.monotonic() - started
        finally:
            pty.close()
        assert elapsed < 30.0, (
            f"a dead child took {elapsed:.1f}s to report -- EOF should end the wait"
        )

    def test_the_failure_carries_what_the_child_did_say(self, tmp_path):
        """The whole point of failing instead of hanging: the message has to be
        enough to diagnose from, without a rerun. Cancelled jobs carry none of
        this, which is what made the original incident cost a whole day."""
        pty = _spawn(
            "import sys, time\n"
            "sys.stdout.write('BREADCRUMB-9f3a\\r\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(120)\n",
            tmp_path,
        )
        try:
            with pytest.raises(PtyTimeout) as caught:
                pty.expect("NEVER-APPEARS", timeout=TIMEOUT_S)
        finally:
            pty.close()
        message = str(caught.value)
        assert "BREADCRUMB-9f3a" in message, (
            f"the transcript the child DID produce is missing:\n{message}"
        )
        assert "NEVER-APPEARS" in message, f"the needle is not named:\n{message}"
        assert "still running" in message, (
            f"the child's liveness -- silent vs crashed -- is not reported:\n{message}"
        )

    def test_a_match_still_returns_promptly(self, tmp_path):
        """The guard rail must not cost the happy path anything: a child that
        answers is matched, and the stream is consumed past the needle so a
        second expect sees only what came after."""
        pty = _spawn(
            "import sys\n"
            "sys.stdout.write('FIRST-MARK\\r\\nSECOND-MARK\\r\\n')\n"
            "sys.stdout.flush()\n"
            "import time; time.sleep(5)\n",
            tmp_path,
        )
        started = time.monotonic()
        try:
            pty.expect("FIRST-MARK", timeout=30.0)
            pty.expect("SECOND-MARK", timeout=30.0)
            elapsed = time.monotonic() - started
        finally:
            pty.close()
        assert elapsed < 20.0, f"a matching child took {elapsed:.1f}s"


class TestWaitExitAlwaysEnds:
    def test_a_child_that_never_exits_times_out(self, tmp_path):
        """``wait_exit`` pumps the same reads ``expect`` does, so it inherited
        the same unbounded-read bug and needs the same pin."""
        pty = _spawn(_CHATTY, tmp_path)
        started = time.monotonic()
        try:
            with pytest.raises(PtyTimeout):
                pty.wait_exit(timeout=TIMEOUT_S)
            elapsed = time.monotonic() - started
        finally:
            pty.close()
        assert elapsed < TIMEOUT_S + SLACK_S, (
            f"wait_exit(timeout={TIMEOUT_S}) took {elapsed:.1f}s"
        )

    def test_a_real_exit_code_still_comes_back(self, tmp_path):
        pty = _spawn("raise SystemExit(3)", tmp_path)
        try:
            assert pty.wait_exit(timeout=30.0) == 3
        finally:
            pty.close()


class TestBudget:
    """The whole-test allowance, which is the half that makes stage timeouts
    safe: five reasonable-looking stages still sum to something no CI job
    agreed to pay."""

    def test_a_stage_is_clamped_to_what_is_left(self):
        budget = Budget(10.0)
        # A stage asking for less than the total gets its ask, untouched.
        assert budget.clamp(2.0) == pytest.approx(2.0, abs=0.5)
        # A stage asking for more gets the total, not its ask. Compared with a
        # float tolerance rather than `<= 10.0`: `remaining()` is a difference
        # of two `time.monotonic()` readings, and on a Windows runner whose
        # clock has not ticked between them that lands a few ULPs ABOVE the
        # total (measured: 10.000000000000057). The guarantee is "the ask does
        # not survive", not "the arithmetic is exact".
        assert budget.clamp(600.0) == pytest.approx(10.0, abs=0.5)

    def test_an_exhausted_budget_yields_zero_not_a_negative(self):
        budget = Budget(0.0)
        time.sleep(0.05)
        assert budget.remaining() < 0
        assert budget.clamp(30.0) == 0.0

    def test_an_exhausted_budget_fails_the_next_stage_at_once(self, tmp_path):
        """A spent budget must produce an immediate, transcript-carrying
        failure -- not a bare arithmetic error, and not one more full stage."""
        pty = _spawn(_SILENT, tmp_path)
        budget = Budget(0.0)
        started = time.monotonic()
        try:
            with pytest.raises(PtyTimeout):
                pty.expect("NEVER-APPEARS", timeout=300.0, budget=budget)
            elapsed = time.monotonic() - started
        finally:
            pty.close()
        assert elapsed < SLACK_S, (
            f"an exhausted budget still spent {elapsed:.1f}s on the next stage"
        )
