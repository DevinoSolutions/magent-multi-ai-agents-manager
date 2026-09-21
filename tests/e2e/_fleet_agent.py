"""A stand-in agent pane for the real-multiplexer fleet tier.

Not a test module (no ``test_`` prefix, so pytest never collects it), and the
same posture as ``_attach_pane.py``: a small stdlib-only program that runs
INSIDE a real tmux/psmux pane and imitates Claude Code's *on-screen contract*
closely enough for magent to drive it. Everything between this program and
``magent send`` / ``model`` / ``peek`` -- the ``send-keys -l`` literal paste,
the separate ``Enter``, ``capture-pane``, the footer -- is the real product
against a real multiplexer.

What it imitates, taken from a real Claude Code pane's last six captured lines:

    ------------------------------------------------------------
    <caret>                                  <- the INPUT line
    ------------------------------------------------------------
      Fable 5.1 * high * <name>              <- the FOOTER (U+00B7 separators)
      >> bypass permissions on (shift+tab to cycle) * <- for agents

...where ``<caret>`` is U+276F and ``*`` is U+00B7. The footer is what
``fleet.parse_footer`` reads; the hints row being LAST is what made
``fleet.looks_unsent`` unable to see an unsent prompt on the pane's last line,
so the caret line's position relative to it is load-bearing, not decoration.

Two properties are deliberate and both were measured on this repo's Windows
box against real psmux 3.3.8:

* **This program owns the input line, in raw mode.** Claude Code is a
  full-screen TUI: the terminal's own echo is off and the app paints what you
  typed. Imitating that is not cosmetic here -- measured on real psmux, the
  platform echo is useless for this purpose in BOTH directions. A detached pane
  echoes nothing as characters arrive (paste + Enter into a sleeping child left
  ``capture-pane`` byte-identical), and then the Windows console echoes the
  WHOLE line at read completion, at the cursor, which landed a duplicate copy
  across the rule line under the caret. So :func:`_own_the_input_line` clears
  ``ENABLE_LINE_INPUT``/``ENABLE_ECHO_INPUT`` (Windows) or ``ECHO``/``ICANON``
  (POSIX), and a reader thread writes incoming characters at the parked cursor
  -- which is exactly where the caret line ends. That is also what lets
  ``/hold`` reproduce the real "prompt pasted, never submitted" state honestly.
* **Output goes through ``sys.stdout.buffer``.** A bare ``sys.stdout.write``
  encodes to the console code page on Windows (cp1252), which mangles U+00B7
  and makes the footer unparseable -- the same trap ``tests/unit/_fake_psmux``
  documents.

Every line received on stdin is appended to ``--log`` as one JSON record, so
the test's ground truth for "the text arrived VERBATIM" is the bytes this
program read, not a screen scrape.

The first record is different: it is this process's own view of the two
environment variables account routing turns on (``startup``, written by
:meth:`Pane.record_startup`). Nothing else can answer that question. A routed
pane's account is set exactly once, on the ``new-session`` client, and the psmux
SERVER that ends up hosting the agent is a grandchild that client forks -- the
same boundary Windows refuses to inherit a priority class across, which is why
``psmux.boost_priority`` has to exist. So whether the environment survives it is
a measurement, not an inference, and the only witness is the program at the far
end. The credential variable is recorded as a PRESENCE, never a value: this log
is a CI artifact.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import threading
import time
from pathlib import Path

# Spelled as escapes rather than pasted glyphs so this file is diffable and
# encoding-proof; what reaches the pane is the real character either way.
CARET = "\u276f"  # the input-line caret
MIDDOT = "\u00b7"  # the footer separator
ELLIPSIS = "\u2026"
HINTS = (
    "  \u23f5\u23f5 bypass permissions on (shift+tab to cycle) "
    + MIDDOT
    + " \u2190 for agents"
)

# An 80x24 pane. Rules are ASCII: a run of 60 box-drawing characters is a run of
# 60 EAST-ASIAN-AMBIGUOUS cells, and this repo has already been bitten once by
# a multiplexer and a terminal disagreeing about that arithmetic.
WIDTH = 78
RULE = "-" * 60
TRANSCRIPT_LINES = 5

# Model CLI alias -> the display name its footer shows, mirroring the real
# thing closely enough for ``fleet.verify_switch`` to check a switch landed.
MODELS = {
    "fable": "Fable 5.1",
    "opus": "Opus 5",
    "sonnet": "Sonnet 4.5",
    "haiku": "Haiku 4.5",
}

# How often the main loop looks for a complete line. Small enough that a send
# is acted on well inside magent's own post-send settle, cheap enough to idle in.
POLL_S = 0.05


def _own_the_input_line() -> None:
    """Switch the terminal out of line-editing/echo, like a real TUI does.

    Best-effort by design: a platform that refuses (or a stdin that is not a
    terminal at all) leaves this program's own echo as the only painter, which
    is the behaviour the tests assert on anyway. The Windows branch is the
    load-bearing one -- see the module docstring's duplicate-echo finding.
    """
    if os.name == "nt":
        import ctypes

        enable_line_input = 0x0002
        enable_echo_input = 0x0004
        with contextlib.suppress(OSError, AttributeError, ValueError):
            # `windll` exists only on Windows, which the branch above has
            # already established; AttributeError is suppressed regardless.
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(
                    handle, mode.value & ~(enable_line_input | enable_echo_input)
                )
        return
    import termios

    with contextlib.suppress(ImportError, OSError, termios.error, ValueError):
        attrs = termios.tcgetattr(0)
        attrs[3] &= ~(termios.ECHO | termios.ICANON)  # lflags
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(0, termios.TCSANOW, attrs)


def _clip(text: str) -> str:
    """One pane line, never wider than the pane -- a wrapped line would add a
    row and move the footer/hints rows the tests read by position."""
    return text[:WIDTH]


class Pane:
    """The stand-in's whole state: what is on screen and what stdin has said."""

    def __init__(
        self, log: Path, model: str, effort: str, name: str, compact_seconds: float
    ) -> None:
        self.log = log
        self.model = model
        self.effort = effort
        self.name = name
        self.compact_seconds = compact_seconds
        self.transcript: list[str] = ["  stand-in agent ready."]
        self.busy: str | None = None
        self.hold_until = 0.0
        self._buf = b""
        self._eof = False
        self._lock = threading.Lock()  # serializes every write to the pane

    # -- output ---------------------------------------------------------------

    def _write(self, text: str) -> None:
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.flush()

    def paint(self) -> None:
        """Repaint the whole screen and park the cursor on the caret line.

        Parking matters: the reader thread's echo is a plain write at the
        cursor, so wherever the cursor rests IS the input line.
        """
        lines = [_clip(line) for line in self.transcript[-TRANSCRIPT_LINES:]]
        if self.busy:
            lines.append(_clip("  " + self.busy))
        lines.append(RULE)
        caret_row = len(lines)
        lines.append(CARET + " ")
        lines.append(RULE)
        lines.append(
            _clip(f"  {self.model} {MIDDOT} {self.effort} {MIDDOT} {self.name}")
        )
        lines.append(_clip(HINTS))
        with self._lock:
            self._write("\x1b[2J\x1b[H" + "\r\n".join(lines))
            # Back up to the caret row, then to column 3 -- just past "caret ".
            up = (len(lines) - 1) - caret_row
            self._write(f"\x1b[{up}A\r\x1b[2C" if up else "\r\x1b[2C")

    def echo(self, text: str) -> None:
        """Draw received characters where the cursor is: the caret line."""
        if not text:
            return
        with self._lock:
            self._write(text)

    # -- input ----------------------------------------------------------------

    def read_forever(self) -> None:
        """Drain stdin into the buffer and echo it, forever.

        Off the main thread on purpose, and the reason ``/hold`` can work: a
        held pane must still SHOW the prompt that was pasted into it (that is
        the production failure ``magent send``'s exit 4 detects) while nothing
        consumes it.
        """
        while True:
            try:
                chunk = os.read(0, 4096)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            with self._lock:
                self._buf += chunk
            self.echo(
                chunk.replace(b"\r", b"").replace(b"\n", b"").decode("utf-8", "replace")
            )
        self._eof = True

    def take_line(self) -> str | None:
        """The next complete line from the buffer, or None."""
        with self._lock:
            index = min(
                (i for i in (self._buf.find(b"\r"), self._buf.find(b"\n")) if i >= 0),
                default=-1,
            )
            if index < 0:
                return None
            raw, self._buf = self._buf[:index], self._buf[index + 1 :]
            # A CRLF must not also yield an empty second line.
            self._buf = self._buf.lstrip(b"\r\n")
        return raw.decode("utf-8", "replace")

    def record(self, line: str) -> None:
        """Append the received line to the log -- the test's ground truth."""
        with self.log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"line": line, "ts": time.time()}) + "\n")

    def record_startup(self) -> None:
        """Append what THIS process sees of the routing environment.

        ``CLAUDE_CONFIG_DIR`` is recorded verbatim -- it is a directory path and
        the value under test. ``ANTHROPIC_API_KEY`` is recorded as a presence
        flag only: a real key must never reach a log, and "was it stripped?" is
        the entire question anyway.
        """
        with self.log.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "startup": {
                            "CLAUDE_CONFIG_DIR": os.environ.get("CLAUDE_CONFIG_DIR"),
                            "anthropic_api_key_set": bool(
                                os.environ.get("ANTHROPIC_API_KEY")
                            ),
                        },
                        "ts": time.time(),
                    }
                )
                + "\n"
            )

    # -- behaviour ------------------------------------------------------------

    def run_busy(self, message: str, seconds: float, done: str) -> None:
        """Show a mid-turn screen for ``seconds``, then go idle with ``done``.

        A real sleep, and the queued keystrokes that arrive during it are read
        afterwards -- that ordering is part of what the tier proves.
        """
        self.busy = message
        self.paint()
        time.sleep(seconds)
        self.busy = None
        self.transcript.append(done)
        self.paint()

    def handle(self, line: str) -> None:
        text = line.strip()
        if not text:
            self.paint()
            return
        head, _, rest = text.partition(" ")
        arg = rest.strip()
        if head == "/model":
            self.model = MODELS.get(arg.lower(), arg.capitalize() or self.model)
            self.transcript.append(f"  {MIDDOT} model set to {self.model}")
            self.paint()
        elif head == "/effort":
            self.effort = arg or self.effort
            self.transcript.append(f"  {MIDDOT} effort set to {self.effort}")
            self.paint()
        elif head == "/compact":
            self.run_busy(
                f"Compacting conversation{ELLIPSIS} (1s {MIDDOT} esc to interrupt)",
                self.compact_seconds,
                "\u25cf Compacted.",
            )
        elif head == "/busy":
            self.run_busy(
                f"* Working{ELLIPSIS} (1s {MIDDOT} esc to interrupt)",
                _seconds(arg, 3.0),
                "\u25cf Done working.",
            )
        elif head == "/hold":
            # Submit this command (so its own echo is cleared), then stop
            # consuming stdin WITHOUT repainting: whatever is pasted next is
            # echoed onto the caret line and stays there, unsent.
            self.transcript.append(f"\u25cf holding {arg or '?'}s")
            self.paint()
            self.hold_until = time.monotonic() + _seconds(arg, 5.0)
        else:
            self.transcript.append(_clip(f"\u25cf ack: {text}"))
            self.paint()

    def serve(self) -> None:
        self.paint()
        while True:
            if time.monotonic() < self.hold_until:
                time.sleep(POLL_S)
                continue
            line = self.take_line()
            if line is None:
                if self._eof:
                    return
                time.sleep(POLL_S)
                continue
            self.record(line)
            self.handle(line)


def _seconds(raw: str, default: float) -> float:
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="stand-in agent pane")
    parser.add_argument("--log", required=True)
    parser.add_argument("--model", default="Fable 5.1")
    parser.add_argument("--effort", default="high")
    parser.add_argument("--name", default="standin")
    parser.add_argument("--compact-seconds", type=float, default=3.0)
    args = parser.parse_args(argv)

    _own_the_input_line()
    pane = Pane(
        log=Path(args.log),
        model=args.model,
        effort=args.effort,
        name=args.name,
        compact_seconds=args.compact_seconds,
    )
    # Before the first paint: a test that is only asking "what environment did
    # this pane get?" must not have to wait for a terminal to render.
    pane.record_startup()
    reader = threading.Thread(target=pane.read_forever, daemon=True)
    reader.start()
    pane.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
