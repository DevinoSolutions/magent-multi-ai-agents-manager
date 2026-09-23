"""The two halves of an attach pane, for the real-pty status-line tests.

Not a test module (no ``test_`` prefix, so pytest never collects it). It is run
as a SCRIPT, inside a real pseudo-terminal, by
``tests/e2e/test_pty_attach_status.py``:

* ``_attach_pane.py frozen <token> <state-dir>`` is the stand-in for what lives
  on the far end of the connection -- a full-screen agent TUI. It enters the
  alternate screen, draws a frame with a prompt box containing ``<token>`` as
  the user's typed-but-unsent text, parks the cursor at the end of that text,
  and then DIES WITHOUT LEAVING THE ALTERNATE SCREEN (exit 255), which is
  precisely what ssh does when wi-fi drops mid-session. On its second
  invocation it plays the reattach instead: psmux's full redraw, typed text and
  all, then a clean exit.
* ``_attach_pane.py pane-<scenario> <...>`` is the supervisor itself -- the REAL
  ``magent.attach_client.supervise`` loop, with the REAL ``_run_ssh``, the REAL
  ``StatusLine``, and the REAL terminal writes. Nothing about the rendering is
  simulated; every escape sequence the test's screen model consumes was written
  by product code into a genuine pty.

WHAT IS SUBSTITUTED, and why that is the whole of it: ``ssh_argv`` (so the
child is the stand-in above instead of a real ssh -- there is no host in this
tier) and ``_probe_session`` (the out-of-band question asked of a host that
does not exist). Both are the NETWORK boundary. ``_run_ssh`` still does the real
``subprocess.Popen`` with the console inherited exactly as a real pane does, so
the frozen frame on screen is a real frozen frame. The real-wire counterpart,
where none of this is substituted, is ``tests/e2e/test_ssh_real.py``'s
``test_typed_text_survives_a_real_reconnect``.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

# Rows the stand-in TUI draws on, 1-based, in a 24-row pane. The prompt box sits
# where a real agent puts it -- above the bottom, with a hint line below it --
# because "does the status line land on the user's typed text?" is only a real
# question when something else is on the bottom row.
FRAME_TOP_ROW = 1
PROMPT_ROW = 11
HINT_ROW = 24

FRAME_TOP = "FRAME-TOP oldest visible conversation line"
HINT = "HINT-ROW ? for shortcuts"
PANE_READY = "<<<PANE-READY>>>"
PANE_DONE = "<<<PANE-DONE rc="


def _dial_number(state: Path) -> int:
    """How many times the stand-in has been run, recorded on disk.

    ``supervise`` computes its ssh argv ONCE and reuses it for every dial, so
    "which connection am I?" cannot come from the command line -- and driving it
    from the argv would mean patching ``_run_ssh``, which is the one function
    this tier most wants to leave real.
    """
    dial = len(list(state.glob("dial-*"))) + 1
    (state / f"dial-{dial}").write_text("x", encoding="utf-8")
    return dial


def _frozen_tui(token: str, state: Path) -> int:
    """Draw the frame; die frozen the first time, redraw and detach the next."""
    write = sys.stdout.write
    write("\x1b[?1049h\x1b[2J")  # the alternate screen, as psmux/an agent enters it
    write(f"\x1b[{FRAME_TOP_ROW};1H{FRAME_TOP}")
    write(f"\x1b[{FRAME_TOP_ROW + 1};1HFRAME-BODY assistant reply text")
    write(f"\x1b[{PROMPT_ROW - 1};1H+--------------------------------------+")
    typed = f"| > {token}"
    write(f"\x1b[{PROMPT_ROW};1H{typed}")
    write(f"\x1b[{PROMPT_ROW + 1};1H+--------------------------------------+")
    write(f"\x1b[{HINT_ROW};1H{HINT}")
    # The caret where the user left it: end of their unsent sentence.
    write(f"\x1b[{PROMPT_ROW};{len(typed) + 1}H")
    sys.stdout.flush()
    if _dial_number(state) == 1:
        # No `\x1b[?1049l`, no cleanup, no goodbye -- the connection died.
        return 255
    return 0


def _typing_tui(state: Path) -> int:
    """Keystroke probe: dial 1 dies, dial 2 reports what stdin had waiting.

    This process never gets its stdin redirected (``_run_ssh`` pipes fd 2 and
    nothing else), so what it reads is what the TERMINAL buffered while no
    child existed -- which is the whole question.
    """
    if _dial_number(state) == 1:
        return 255
    box: list[bytes] = []

    def read() -> None:
        try:
            box.append(os.read(sys.stdin.fileno(), 512))
        except OSError as exc:  # pragma: no cover - probe only
            box.append(f"ERR:{exc}".encode())

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    thread.join(timeout=5.0)
    (state / "typed.bin").write_bytes(box[0] if box else b"")
    return 0


def _pane(scenario: str, token: str, state: Path) -> int:
    """Run the REAL supervisor with the stand-in in ssh's place."""
    from magent import attach_client

    child = [sys.executable, os.path.abspath(__file__), scenario, token, str(state)]
    attach_client.shutil.which = lambda _name: "/stand-in/ssh"
    attach_client.ssh_argv = lambda _target, _remote: list(child)
    attach_client._probe_session = lambda _t, _s: attach_client.SESSION_ALIVE
    if scenario == "typing":
        # A longer first backoff so the test has room to type into a pane that
        # is provably mid-outage rather than racing the redial.
        attach_client.BACKOFF_BASE_S = 6.0

    sys.stdout.write(PANE_READY)
    sys.stdout.flush()
    rc = attach_client.supervise("user@stand-in", "psmux -L demo attach", "demo")
    sys.stdout.write(f"{PANE_DONE}{rc}>>>")
    sys.stdout.flush()
    return 0


def pane_argv(scenario: str, token: str, state: Path) -> list[str]:
    """The argv that runs the supervisor half of this file.

    Built here rather than in the test so the two files cannot drift on the
    spelling of a mode that only this file understands.
    """
    return [
        sys.executable,
        os.path.abspath(__file__),
        f"pane-{scenario}",
        token,
        str(state),
    ]


def main(argv: list[str]) -> int:
    mode, token, state = argv[0], argv[1], Path(argv[2])
    state.mkdir(parents=True, exist_ok=True)
    if mode.startswith("pane-"):
        return _pane(mode.removeprefix("pane-"), token, state)
    if mode == "typing":
        return _typing_tui(state)
    return _frozen_tui(token, state)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
