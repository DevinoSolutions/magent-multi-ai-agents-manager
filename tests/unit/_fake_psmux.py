"""A real, on-disk fake ``psmux`` binary for the fleet tests.

The whole point of ``magent send`` / ``model`` / ``peek`` is the psmux WIRE:
the ``send-keys -l`` literal flag, the separate ``Enter``, a slash-command that
must reach the pane verbatim. Mocking ``subprocess.run`` proves the argv magent
BUILDS; running a genuine executable proves the argv a process actually
RECEIVES -- including that no shell splits or rewrites ``/model`` on the way.

``make_fake_psmux`` writes a tiny launcher (``.cmd`` on Windows, a ``sh`` script
elsewhere) that shells to a Python recorder. Every invocation appends its argv
to ``calls.jsonl``; ``capture-pane`` prints ``pane.txt`` (after sleeping
``capture_delay.txt`` seconds, if set); ``has-session`` exits 0 unless
``live.txt`` exists and omits the queried session (after sleeping
``has_session_delay.txt`` seconds, if set); ``send-keys`` swaps in
``after_send.txt`` as the pane when one is staged, and exits 1 while
``fail_send.txt`` is staged (a staged ``gone`` also empties ``live.txt``: the
session died, which is why the send failed). The base directory is baked into
the recorder as a literal, so no environment plumbing is needed.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_RECORDER = """\
import json, os, sys, time
from pathlib import Path

BASE = Path(r"{base}")
args = sys.argv[1:]

# One file per invocation, never a shared append: liveness probing fans out
# several psmux processes at once, and concurrent appends to one log tore a
# line on CI (a half-written record that then failed json.loads). A unique
# name (monotonic ns + pid) is written by exactly one process, so it can never
# be torn; the reader orders by the ns prefix.
calldir = BASE / "calls"
calldir.mkdir(parents=True, exist_ok=True)
(calldir / (str(time.time_ns()) + "-" + str(os.getpid()) + ".json")).write_text(
    json.dumps(args), encoding="utf-8"
)


def _target():
    if "-t" in args:
        i = args.index("-t")
        if i + 1 < len(args):
            return args[i + 1]
    return ""


if "send-keys" in args:
    # A send that fails: what a psmux whose server went away (or that refused
    # the key) answers. With "gone" staged the liveness probe fails from here
    # on too, the way a dead session's does.
    fail = BASE / "fail_send.txt"
    if fail.exists():
        if fail.read_text(encoding="utf-8").strip() == "gone":
            (BASE / "live.txt").write_text("", encoding="utf-8")
        sys.exit(1)
    # A pane that reacts: when after_send.txt is staged, the first send-keys
    # swaps it in as the pane (a dialog that closes on a keypress, a turn
    # that stops on Escape) -- what a capture right after the key shows.
    after = BASE / "after_send.txt"
    if after.exists():
        os.replace(after, BASE / "pane.txt")
    sys.exit(0)

if "has-session" in args:
    # A liveness probe that answers late: a loaded or frozen psmux server,
    # made deterministic. Only has-session is slowed.
    delay = BASE / "has_session_delay.txt"
    if delay.exists():
        time.sleep(float(delay.read_text(encoding="utf-8")))
    live = BASE / "live.txt"
    if live.exists():
        names = set(live.read_text(encoding="utf-8").split())
        sys.exit(0 if _target() in names else 1)
    sys.exit(0)

if "capture-pane" in args:
    # A pane that answers late: what a loaded box does to a real capture, made
    # deterministic. Only capture-pane is slowed, so liveness and send-keys
    # still answer at once.
    delay = BASE / "capture_delay.txt"
    if delay.exists():
        time.sleep(float(delay.read_text(encoding="utf-8")))
    pane = BASE / "pane.txt"
    if pane.exists():
        # Write raw UTF-8 bytes: the parent (psmux.capture_pane) decodes with
        # encoding="utf-8", but a bare sys.stdout.write() on Windows encodes to
        # the console code page (cp1252), which mangles the U+00B7 footer
        # separator and made the whole fleet-state parse fail on CI while
        # passing locally under PYTHONIOENCODING=utf-8. Real psmux emits UTF-8.
        sys.stdout.buffer.write(pane.read_text(encoding="utf-8").encode("utf-8"))
    sys.exit(0)

sys.exit(0)
"""


@dataclass
class FakePsmux:
    """Handle onto a running fake psmux binary."""

    path: str
    base: Path

    def set_pane(self, text: str) -> None:
        (self.base / "pane.txt").write_text(text, encoding="utf-8")

    def set_capture_delay(self, seconds: float) -> None:
        """Make every later ``capture-pane`` sleep ``seconds`` before answering."""
        (self.base / "capture_delay.txt").write_text(str(seconds), encoding="utf-8")

    def set_pane_after_send(self, text: str) -> None:
        """Make the NEXT ``send-keys`` replace the pane with ``text``."""
        (self.base / "after_send.txt").write_text(text, encoding="utf-8")

    def set_has_session_delay(self, seconds: float) -> None:
        """Make every later ``has-session`` sleep ``seconds`` before answering."""
        (self.base / "has_session_delay.txt").write_text(str(seconds), encoding="utf-8")

    def set_send_failure(self, *, gone: bool = False) -> None:
        """Make every later ``send-keys`` exit 1. With ``gone`` the session
        also stops answering ``has-session`` from that send on."""
        (self.base / "fail_send.txt").write_text(
            "gone" if gone else "", encoding="utf-8"
        )

    def set_live(self, names: list[str] | None) -> None:
        live = self.base / "live.txt"
        if names is None:
            live.unlink(missing_ok=True)
        else:
            live.write_text("\n".join(names), encoding="utf-8")

    def calls(self) -> list[list[str]]:
        d = self.base / "calls"
        if not d.exists():
            return []
        files = sorted(d.glob("*.json"), key=lambda p: int(p.name.split("-")[0]))
        return [json.loads(p.read_text(encoding="utf-8")) for p in files]

    def send_key_calls(self) -> list[list[str]]:
        return [c for c in self.calls() if "send-keys" in c]


def make_fake_psmux(
    tmp_path: Path, *, pane: str = "", live: list[str] | None = None
) -> FakePsmux:
    base = tmp_path / "fakepsmux"
    base.mkdir(parents=True, exist_ok=True)
    (base / "recorder.py").write_text(
        _RECORDER.format(base=str(base)), encoding="utf-8"
    )
    if pane:
        (base / "pane.txt").write_text(pane, encoding="utf-8")
    if live is not None:
        (base / "live.txt").write_text("\n".join(live), encoding="utf-8")

    if sys.platform == "win32":
        launcher = base / "psmux.cmd"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{base / "recorder.py"}" %*\r\n',
            encoding="utf-8",
        )
    else:
        launcher = base / "psmux"
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{base / "recorder.py"}" "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(0o755)
    return FakePsmux(path=str(launcher), base=base)
