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
"""

from __future__ import annotations

import re
import sys
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

IS_WIN = sys.platform == "win32"

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


class Pty:
    """One real terminal running one child process."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        env: dict[str, str],
        cwd: str,
        dimensions: tuple[int, int] = (50, 160),
    ) -> None:
        self._raw = ""  # everything read so far (with escapes), for transcripts
        self._seen = ""  # cleaned stream already consumed past
        self._pending = ""  # cleaned stream not yet matched/consumed
        self._eof = False
        rows, cols = dimensions
        if IS_WIN:
            from winpty import PtyProcess

            self._win = PtyProcess.spawn(
                list(argv), cwd=cwd, env=env, dimensions=(rows, cols)
            )
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

    def _read_some(self) -> str:
        if IS_WIN:
            try:
                return self._win.read(4096)
            except EOFError:
                self._eof = True
                return ""
        import pexpect

        try:
            return self._child.read_nonblocking(size=4096, timeout=0.2)
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
        if IS_WIN:
            return self._win.isalive()
        return self._child.isalive()

    # -- matching ------------------------------------------------------------

    def expect(self, needle: str, timeout: float = 30.0) -> None:
        """Block until ``needle`` (a plain substring, escapes already stripped)
        appears in the stream, then consume everything up to and including it."""
        deadline = time.monotonic() + timeout
        while needle not in self._pending:
            if self._pump():
                continue
            # On EOF/dead child, one last drain then give up if still unmatched.
            if (self._eof or not self.is_alive()) and not (
                self._pump() or needle in self._pending
            ):
                self._fail(needle)
            if time.monotonic() > deadline:
                self._fail(needle)
            time.sleep(0.05)
        cut = self._pending.index(needle) + len(needle)
        self._seen += self._pending[:cut]
        self._pending = self._pending[cut:]

    def _fail(self, needle: str) -> None:
        raise PtyTimeout(
            f"timed out waiting for {needle!r}\n"
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

    # -- lifecycle -----------------------------------------------------------

    def wait_exit(self, timeout: float = 30.0) -> int:
        """Wait for the child to exit; return its real exit status."""
        deadline = time.monotonic() + timeout
        while self.is_alive():
            self._pump()
            if time.monotonic() > deadline:
                raise PtyTimeout(
                    "child never exited\n--- cleaned transcript ---\n"
                    + strip_ansi(self._raw)
                )
            time.sleep(0.05)
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
