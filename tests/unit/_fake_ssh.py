"""THE fake for everything that dials a node: a real on-disk ``ssh`` (or
``gh``/``tmux``/``claude`` -- pass ``name``) that records what it was given
and answers from a script.

A structural mirror of ``_fake_psmux.py``/``_fake_ccswap.py``, for the same
reason: running a genuine executable proves the argv a process actually
RECEIVES and the bytes that actually crossed its stdin -- which is where node
secrets must travel. Each call is its OWN pair of files
(``calls/<ns>-<pid>.json`` = ``{argv, stdin_sha256, stdin_len}`` +
``.stdin`` = the raw bytes), never a shared append: concurrent appends tore
lines on CI. The base directory is baked into the recorder as a literal, so no
environment plumbing is needed.

Known limit of the ``.cmd`` launcher on Windows: ``cmd /c`` percent-expands
the line, so an argument containing a DEFINED ``%NAME%`` (``%PATH%``) is
recorded with that variable's value pasted in -- rc 0, a silently wrong
record. An undefined name is left alone, so ``date +%s`` is safe; a single
quote mixed with ``&``/``|``/``>`` round-trips fine. Exact-argv fidelity for
such an argument would need an .exe launcher rather than a .cmd.
"""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

_RECORDER = """\
import hashlib, json, os, signal, sys, time
from pathlib import Path

BASE = Path(r"<<BASE>>")
args = sys.argv[1:]
data = b"" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.buffer.read()

calldir = BASE / "calls"
calldir.mkdir(parents=True, exist_ok=True)
stem = str(time.time_ns()) + "-" + str(os.getpid())
# The bytes first, the record second: a reader globbing *.json can never find
# a record whose stdin is not on disk yet.
(calldir / (stem + ".stdin")).write_bytes(data)
(calldir / (stem + ".json")).write_text(
    json.dumps({
        # The launcher's own path, as the parent spawned it: argv[0] proper,
        # which sys.argv cannot show (it holds this recorder's path).
        "program": os.environ.get("FAKE_SSH_SELF", ""),
        "argv": args,
        "stdin_sha256": hashlib.sha256(data).hexdigest(),
        "stdin_len": len(data),
    }),
    encoding="utf-8",
)

mode = (BASE / "mode.txt").read_text(encoding="utf-8").strip() if (BASE / "mode.txt").exists() else "ok"
if mode == "timeout":
    # Outlive any test's bound without writing a byte. Short on purpose: on
    # Windows kill() reaps cmd.exe, and this python lives out the sleep as an
    # orphan.
    time.sleep(5)
    sys.exit(0)
if mode == "flood":
    # Stream stdout forever and never exit on its own. Ends only when the
    # reader closes the pipe (the write fails) -- on Windows kill() reaps
    # cmd.exe, and this python is the orphan still holding the pipe. A
    # matching reply's stderr is written first: last words before the flood.
    replies = json.loads((BASE / "replies.json").read_text(encoding="utf-8")) if (BASE / "replies.json").exists() else []
    said = next((r["stderr"] for m, r in replies if m in " ".join(args)), "")
    sys.stderr.buffer.write(said.encode("utf-8"))
    sys.stderr.flush()
    if said:
        # The words must be read before the flood reaches the cap, and stamped
        # so on any clock: Windows' before Python 3.13 ticks every 15.6 ms, and
        # a read that ties the cap counts as after it. The pause sets the
        # order, not a race.
        time.sleep(0.3)
    block = b"x" * 65536
    try:
        while True:
            sys.stdout.buffer.write(block)
            sys.stdout.flush()
    except (OSError, ValueError):
        sys.exit(0)

line = " ".join(args)
replies = json.loads((BASE / "replies.json").read_text(encoding="utf-8")) if (BASE / "replies.json").exists() else []
for match, reply in replies:
    if match in line:
        # A reply may first hang (a probe the caller must bound itself), and
        # may ignore TERM while it does (only KILL ends it).
        if reply.get("ignore_term"):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(reply.get("hang_s", 0))
        # Raw UTF-8 bytes: sys.stdout.write() on Windows encodes to the console
        # code page and mangles non-ASCII -- the fleet tier's cp1252 defect.
        sys.stdout.buffer.write(reply["stdout"].encode("utf-8"))
        sys.stderr.buffer.write(reply["stderr"].encode("utf-8"))
        sys.stdout.flush()
        sys.stderr.flush()
        sys.exit(reply["rc"])
sys.exit(0)
"""


@dataclass(frozen=True)
class FakeCall:
    """One recorded invocation: the launcher path it was spawned as
    (``program``, i.e. argv[0]), its argv without the program name, and the
    exact bytes it read from stdin."""

    program: str
    argv: list[str]
    stdin: bytes


@dataclass
class FakeSsh:
    """Handle onto a fake client binary."""

    path: str
    base: Path

    def set_reply(
        self,
        match: str,
        *,
        stdout: str = "",
        stderr: str = "",
        rc: int = 0,
        hang_s: float = 0.0,
        ignore_term: bool = False,
    ) -> None:
        """Answer every call whose space-joined argv contains ``match``, after
        sleeping ``hang_s`` (one hung call, where ``set_mode("timeout")`` hangs
        them all) -- deaf to SIGTERM while it does if ``ignore_term``. The first
        registered match wins; an unmatched call exits 0, silent."""
        replies_path = self.base / "replies.json"
        replies = (
            json.loads(replies_path.read_text(encoding="utf-8"))
            if replies_path.exists()
            else []
        )
        replies.append(
            [
                match,
                {
                    "stdout": stdout,
                    "stderr": stderr,
                    "rc": rc,
                    "hang_s": hang_s,
                    "ignore_term": ignore_term,
                },
            ]
        )
        replies_path.write_text(json.dumps(replies), encoding="utf-8")

    def set_mode(self, mode: str) -> None:
        """``"timeout"``: every call hangs silently for 5s. ``"flood"``: every
        call writes a matching reply's stderr (if any, then pauses 0.3s), then
        streams stdout without end and never exits on its own."""
        (self.base / "mode.txt").write_text(mode, encoding="utf-8")

    def calls(self) -> list[FakeCall]:
        calldir = self.base / "calls"
        if not calldir.exists():
            return []
        records = sorted(
            calldir.glob("*.json"), key=lambda p: int(p.name.split("-")[0])
        )
        out = []
        for p in records:
            record = json.loads(p.read_text(encoding="utf-8"))
            out.append(
                FakeCall(
                    program=record["program"],
                    argv=record["argv"],
                    stdin=p.with_suffix(".stdin").read_bytes(),
                )
            )
        return out


# A flood pin's cap under SteppedClock: over one read of a pipe
# (remote_mux._READ_CHUNK_BYTES), so the read that passes it is never the
# stdout drain's first.
FLOOD_CAP = 256 * 1024


class SteppedClock:
    """remote_mux's monotonic clock for a flood pin, ordering by thread, not
    by time: each thread's first reading is 0.0 and every later one 1000.0.
    A drain stamps each read, so what a child said first -- stderr's first
    read -- comes before the cap, and what a drain reads later (the stream's
    end, a complaint at the pipe the cap closed) ties it, which counts as
    after. No clock's tick, however coarse, and no scheduling can reorder
    that -- nor can the fake's pause, so a pin of the pause needs the real
    clock.

    Keyed on the thread OBJECT, held for the clock's life: an ident is reused
    once its thread ends, and a tick's executor worker and the drains its
    call starts come and go. The cap must exceed one read (``FLOOD_CAP``): a
    cap the stdout drain's FIRST read passed is stamped 0.0 too, and ties the
    words."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: set[threading.Thread] = set()

    def monotonic(self) -> float:
        me = threading.current_thread()
        with self._lock:
            if me in self._seen:
                return 1000.0
            self._seen.add(me)
            return 0.0


def make_fake_ssh(tmp_path: Path, *, name: str = "ssh") -> FakeSsh:
    base = tmp_path / f"fake{name}"
    base.mkdir(parents=True, exist_ok=True)
    recorder = base / "recorder.py"
    recorder.write_text(_RECORDER.replace("<<BASE>>", str(base)), encoding="utf-8")
    # Each launcher hands the recorder its OWN path (%~f0 / $0) so a call
    # records the argv[0] it was actually spawned as.
    if sys.platform == "win32":
        launcher = base / f"{name}.cmd"
        launcher.write_text(
            '@echo off\r\nset "FAKE_SSH_SELF=%~f0"\r\n'
            f'"{sys.executable}" "{recorder}" %*\r\n',
            encoding="utf-8",
        )
    else:
        launcher = base / name
        launcher.write_text(
            '#!/bin/sh\nFAKE_SSH_SELF="$0"\nexport FAKE_SSH_SELF\n'
            f'exec "{sys.executable}" "{recorder}" "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(0o755)
    return FakeSsh(path=str(launcher), base=base)


def gh_auth_status(
    login: str | None,
    scopes: str = "",
    *,
    accounts: Sequence[tuple[str, bool, str] | tuple[str, bool, str, str]] = (),
    token_source: str = "keyring",
) -> str:
    """The stdout of ``gh auth status --json hosts`` (gh 2.88) -- the reply a
    fake ``gh`` gives.

    ``accounts`` are ``(login, active, state[, error])`` entries, listed
    FIRST and in order; ``login``, when not None, is then appended as the
    active, verified (``state: success``) account -- so the common case stays
    ``gh_auth_status("demo", "repo")`` and a test can still put the active
    one anywhere but first. ``state`` is gh's own vocabulary: ``success``,
    ``error``, ``timeout``; ``error`` is gh's own words for a failed check
    (``HTTP 401: Bad credentials ...``, a DNS failure), empty by default. No
    entries at all is gh's not-logged-in shape under ``--json``:
    ``{"hosts": {}}``, exit 0."""
    rows = [*accounts, *([(login, True, "success")] if login is not None else [])]
    entries = [
        {
            "active": row[1],
            "error": row[3] if len(row) == 4 else "",
            "host": "github.com",
            "login": row[0],
            "scopes": scopes,
            "state": row[2],
            "tokenSource": token_source,
        }
        for row in rows
    ]
    return json.dumps({"hosts": {"github.com": entries} if entries else {}})
