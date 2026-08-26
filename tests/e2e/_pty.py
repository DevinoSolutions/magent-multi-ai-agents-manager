"""Cross-platform real-PTY driver for the interactive-menu e2e tests.

Not a test module (no ``test_`` prefix, so pytest never collects it): a thin
uniform wrapper over a REAL pseudo-terminal so one test body drives ``magent``
the same way on every OS.

* POSIX -> ``pexpect`` (a real pty via ``os.forkpty``).
* Windows -> ``pywinpty`` (a real ConPTY / pseudo-console).

Both back ends are given the SAME contract: spawn a child under a real terminal,
``expect`` plain-text substrings out of the live stream, ``send_line`` a reply
the way a user's Enter key would, and ``wait_exit`` for the real process exit
code. Matching strips ANSI/ConPTY control sequences first — ConPTY in particular
interleaves cursor-move and erase codes with the app's text — so the assertions
key off what a human reads on screen, not the raw byte soup. Children are always
launched with ``NO_COLOR=1`` so click emits no colour of its own, leaving only
the terminal layer's own sequences to strip.

EVERY WAIT HERE IS WALL-CLOCK BOUNDED, on both back ends, and that is a
correctness property of the harness rather than a nicety. A test that hangs
does not fail — it burns the whole CI job's ``timeout-minutes`` and the job is
CANCELLED, which throws away the transcript, the pytest summary and every
result after it. That happened: a Windows ``needs_ssh`` leg sat inside one test
for 13m39s, printed the test's name and nothing else, and took the job with it.
Two independent holes let a wait outlive its ``timeout``, and both are closed
below:

* **A silent child blocked the read forever.** ``pywinpty``'s
  ``PtyProcess.read`` is a plain ``socket.recv`` with no timeout (its reader
  thread only forwards bytes the child actually produced), so a child that says
  nothing parks the caller inside ``recv`` and the deadline underneath is never
  reached. Windows reads therefore go through ``_win_reader`` — our own daemon
  thread draining that blocking call into a queue — and the test thread only
  ever does a ``get(timeout=...)``. The POSIX side was already bounded
  (``read_nonblocking(timeout=...)``).
* **A CHATTY child skipped the deadline check.** ``expect`` used to ``continue``
  on every successful read, so a child producing bytes that never contain the
  needle looped without ever consulting the clock. An attach pane in its
  reconnect backoff repaints a status line once a second forever — output, and
  never the needle. The clock is now read on every iteration, full stop.

A wait that runs out raises ``PtyTimeout`` carrying the whole cleaned
transcript, the elapsed time and whether the child is still alive. That
diagnostic is the entire difference between a 30-second fix and a cancelled job
with zero information.

``Budget`` adds the other half: per-stage timeouts answer "how long may THIS
step take", never "how long may the test take", and on CI only the second
question has a real answer — the job's own timeout.
"""

from __future__ import annotations

import queue
import re
import sys
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

IS_WIN = sys.platform == "win32"

# How long a single read attempt may block. Small enough that a wall-clock
# deadline is honored to well inside a second; large enough not to spin a core.
READ_SLICE_S = 0.2

# Idle poll between reads that returned nothing, on the POSIX path where the
# read itself already returns promptly.
POLL_S = 0.05

# Reads allowed in the final drain after the child is known dead. Bounded so a
# child that exited having written megabytes cannot turn "give up" into a
# second unbounded loop.
FINAL_DRAIN_READS = 50

# CSI (``ESC[ ... final``), OSC (``ESC] ... BEL``) and lone two-char escapes
# (``ESC(B`` etc.) — enough to reduce ConPTY/click output to readable text.
_ANSI = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|\x1b\[[0-9;?]*[ -/]*[@-~]"
)


def strip_ansi(text: str) -> str:
    """Drop escape sequences so plain on-screen text is left to match against."""
    return _ANSI.sub("", text)


class PtyTimeout(AssertionError):
    """Raised when an expected substring never appears in time. Carries the full
    cleaned transcript so a CI failure is self-diagnosing."""


class Budget:
    """A wall-clock allowance shared by a whole sequence of stages.

    A test made of five ``expect`` calls has five per-stage timeouts and NO
    total, so its real worst case is their sum — a number nobody chose and
    which, in the incident this class was written for, exceeded the CI job's
    own ``timeout-minutes``. Stage timeouts are therefore clamped to what is
    left of one total: whichever stage is slow, the test as a whole still lands
    inside a budget the job can afford, and it lands as a FAILURE with a
    transcript rather than as a cancellation.

    Deliberately not a thread or a signal: the clamp is applied where a stage
    starts, so the failure is attributed to the stage that ran out of road.
    """

    def __init__(self, total: float) -> None:
        self.total = total
        self._end = time.monotonic() + total

    def remaining(self) -> float:
        """Seconds left in the total budget (may be negative)."""
        return self._end - time.monotonic()

    def clamp(self, want: float) -> float:
        """``want`` seconds, or what is left of the total — whichever is less.

        Never negative: an exhausted budget yields 0.0, which makes the next
        stage fail immediately (with its transcript) instead of raising a bare
        arithmetic error nobody can diagnose.
        """
        return max(0.0, min(want, self.remaining()))


class Pty:
    """One real terminal running one child process."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        env: dict[str, str],
        cwd: str,
        dimensions: tuple[int, int] = (50, 160),
        budget: Budget | None = None,
    ) -> None:
        # A whole-test allowance, applied to every wait this terminal serves.
        # Set here rather than passed per call so the guarantee is structural:
        # a stage added later inherits the total instead of quietly extending
        # it, which is how the stage timeouts came to out-sum their CI job.
        self._budget = budget
        self._raw = ""  # everything read so far (with escapes), for transcripts
        self._seen = ""  # cleaned stream already consumed past
        self._pending = ""  # cleaned stream not yet matched/consumed
        self._eof = False
        rows, cols = dimensions
        if IS_WIN:
            import threading

            from winpty import PtyProcess

            self._win = PtyProcess.spawn(
                list(argv), cwd=cwd, env=env, dimensions=(rows, cols)
            )
            # pywinpty's read() is an untimed socket recv (see the module
            # docstring): drain it from a thread so the TEST thread's waits can
            # be bounded. Daemon, so a child that never speaks cannot keep the
            # interpreter alive; the thread ends on its own when `close()`
            # tears the pty down and the recv reports EOF.
            self._queue: queue.Queue[str | None] = queue.Queue()
            self._reader = threading.Thread(target=self._win_reader, daemon=True)
            self._reader.start()
        else:
            import pexpect

            self._child = pexpect.spawn(
                argv[0],
                list(argv[1:]),
                env=env,
                cwd=cwd,
                encoding="utf-8",
                codec_errors="replace",
                timeout=30,
                dimensions=(rows, cols),
            )

    # -- reading -------------------------------------------------------------

    def _win_reader(self) -> None:
        """Drain pywinpty's blocking read into the queue, forever.

        Runs off the test thread on purpose: the blocking call is allowed to
        park here indefinitely because nothing is waiting on it. ``None`` is
        the end-of-stream token — pushed on EOF and on a torn-down pty
        (``close()`` shuts the socket under us, which surfaces as OSError).
        """
        while True:
            try:
                chunk = self._win.read(4096)
            except (EOFError, OSError, ValueError):
                self._queue.put(None)
                return
            if chunk:
                self._queue.put(chunk)

    def _read_some(self) -> str:
        """At most one chunk, waiting at most ``READ_SLICE_S`` for it."""
        if IS_WIN:
            try:
                item = self._queue.get(timeout=READ_SLICE_S)
            except queue.Empty:
                return ""
            if item is None:
                self._eof = True
                return ""
            return item
        import pexpect

        try:
            return self._child.read_nonblocking(size=4096, timeout=READ_SLICE_S)
        except pexpect.TIMEOUT:
            return ""
        except pexpect.EOF:
            self._eof = True
            return ""

    def _pump(self) -> bool:
        """Read one chunk into the buffers. Returns True if bytes were read."""
        chunk = self._read_some()
        if not chunk:
            return False
        self._raw += chunk
        self._pending += strip_ansi(chunk)
        return True

    def is_alive(self) -> bool:
        try:
            if IS_WIN:
                return bool(self._win.isalive())
            return bool(self._child.isalive())
        except (OSError, ValueError, EOFError):
            # A closed/torn-down pty is not a live child, and asking must never
            # be the thing that raises out of a timeout path.
            return False

    # -- matching ------------------------------------------------------------

    def expect(
        self, needle: str, timeout: float = 30.0, *, budget: Budget | None = None
    ) -> None:
        """Block until ``needle`` (a plain substring, escapes already stripped)
        appears in the stream, then consume everything up to and including it.

        Returns within ``timeout`` seconds (plus one read slice) or raises
        ``PtyTimeout`` — no input from the child, and no amount of the WRONG
        input, can make this outlast its deadline. The stage is additionally
        clamped to whatever is left of ``budget`` (or of the terminal's own
        whole-test allowance, when one was given at construction).
        """
        allowance = budget or self._budget
        limit = timeout if allowance is None else allowance.clamp(timeout)
        started = time.monotonic()
        deadline = started + limit
        while needle not in self._pending:
            got = self._pump()
            if needle in self._pending:
                break
            # The clock is read on EVERY iteration, including one that read
            # bytes. A child stuck in a loop that prints something other than
            # the needle is exactly as timed out as a silent one.
            if time.monotonic() >= deadline:
                self._fail(needle, limit, time.monotonic() - started)
            if got:
                continue
            if self._eof or not self.is_alive():
                # Dead child: drain what it left behind, then decide once.
                for _ in range(FINAL_DRAIN_READS):
                    if not self._pump():
                        break
                if needle not in self._pending:
                    self._fail(needle, limit, time.monotonic() - started)
                break
            time.sleep(POLL_S)
        cut = self._pending.index(needle) + len(needle)
        self._seen += self._pending[:cut]
        self._pending = self._pending[cut:]

    def _fail(self, needle: str, timeout: float, elapsed: float) -> None:
        state = "still running" if self.is_alive() else "exited"
        raise PtyTimeout(
            f"timed out waiting for {needle!r} after {elapsed:.1f}s "
            f"(budget {timeout:.1f}s); child {state}, eof={self._eof}\n"
            f"--- cleaned transcript ---\n{strip_ansi(self._raw)}\n"
            f"--- raw (repr, tail) ---\n{self._raw[-1200:]!r}"
        )

    # -- writing -------------------------------------------------------------

    def send_line(self, text: str) -> None:
        """Type ``text`` and press Enter, the way a real keyboard would."""
        if IS_WIN:
            self._win.write(text + "\r\n")
        else:
            self._child.send(text + "\n")

    def send_keys(self, text: str) -> None:
        """Type raw characters with NO Enter appended.

        The type-to-filter picker reads KEYS, not lines: every character it is
        sent must land as its own keystroke, and an Enter it did not ask for
        would commit the very prompt the test is still typing into. Escape
        sequences (``\\x1b[B`` for Down) go through here too — this is the only
        way to press a key that is not a character.
        """
        if IS_WIN:
            self._win.write(text)
        else:
            self._child.send(text)

    # -- lifecycle -----------------------------------------------------------

    def wait_exit(self, timeout: float = 30.0, *, budget: Budget | None = None) -> int:
        """Wait for the child to exit; return its real exit status.

        Bounded on the same terms as ``expect``: every read inside is capped at
        one slice, so the deadline below is the real worst case.
        """
        allowance = budget or self._budget
        limit = timeout if allowance is None else allowance.clamp(timeout)
        started = time.monotonic()
        deadline = started + limit
        while self.is_alive():
            self._pump()
            if time.monotonic() >= deadline:
                raise PtyTimeout(
                    f"child never exited within {limit:.1f}s "
                    f"(waited {time.monotonic() - started:.1f}s)\n"
                    "--- cleaned transcript ---\n" + strip_ansi(self._raw)
                )
            time.sleep(POLL_S)
        # Drain any trailing output for the transcript.
        for _ in range(5):
            if not self._pump():
                break
        if IS_WIN:
            return int(self._win.wait())
        self._child.close()
        return int(self._child.exitstatus or 0)

    def close(self) -> None:
        try:
            if IS_WIN:
                if self._win.isalive():
                    self._win.terminate(force=True)
            else:
                self._child.close(force=True)
        except (OSError, EOFError):
            pass

    @property
    def transcript(self) -> str:
        return strip_ansi(self._raw)

    @property
    def raw(self) -> str:
        """Everything read so far, escapes INTACT.

        The menu tests want ``transcript`` -- what a human reads. The status-line
        tests want this: whether an erase landed on the user's half-typed prompt
        is a question about the escape sequences themselves, and about the grid
        they drive (see ``_screen.Screen``), not about the text that survived.
        """
        return self._raw
