"""The single owner of every subprocess aimed at a node (spec §6).

ssh argv, ``tmux -L magent`` on the node, the shipped ``node_scripts``, and the
local git reads a bring-up needs. Every function returns data or raises
``RemoteError``; nothing here prints or exits. Four laws:

- a remote command is a list, sent as ``bash -c <shlex.quote(shlex.join(argv))>``
  -- never an f-string shell, and never parsed by the node user's LOGIN shell
  (zsh expands a bare ``=word``, which is every exact tmux target ``=<sid>``);
- every call is bounded: ``timeout_s`` is a required keyword, so a call that
  forgot it is a TypeError, never a hang -- and the stdout it may hold in RAM
  is capped too (``max_stdout_bytes``, default ``MAX_REPLY_BYTES``);
- secrets travel on stdin only -- never argv, never a log line;
  ``RemoteError.command_redacted`` names stdin by its length alone;
- ``BatchMode=yes`` everywhere: a password prompt nobody can answer is a hang.

A leaf: never imports ``magent.cli`` (LS-A-001); its magent imports are the
leaves ``attach_client``, ``log``, ``node_scripts`` and ``nodes``.
"""

from __future__ import annotations

import contextlib
import functools
import io
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from magent import node_scripts
from magent.attach_client import SSH_MISSING_RC, TMUX_SOCKET
from magent.log import get_logger
from magent.nodes import LoadSample, encoded_project_dir, node_dir

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence
    from pathlib import Path
    from typing import IO

    from magent.nodes import Node

# tmux, not psmux: nodes are Linux. One server per node user (`-L magent`,
# D10). The name has one owner, attach_client, whose pane attaches to it; this
# is a re-export, never a second literal (DECISION-3).
MUX = "tmux"
SOCKET = TMUX_SOCKET

# The default bounds (spec §6): a probe is one round trip, a script is one
# connection doing real work, a bring-up clones repositories.
PROBE_TIMEOUT_S = 10.0
SCRIPT_TIMEOUT_S = 120.0
BRING_UP_TIMEOUT_S = 600.0

# The line separating a shipped script from its payload on stdin. bash -s reads
# a script from a pipe one byte at a time, so a script whose last line is
# `main "$@"; exit $?` hands the REST of stdin to main -- which skips to this
# line and reads the payload (JSON, a tarball) after it. Never a temp file on
# the node, never an argument.
PAYLOAD_SENTINEL = "__MAGENT_PAYLOAD__"

# remote_mux's OWN option set -- not attach_client.SSH_CONNECTION_OPTS, which is
# scoped to the interactive attach pane and allows a 20s connect, i.e. longer
# than a whole probe here. A connect bound strictly under PROBE_TIMEOUT_S lets a
# dead node surface as ssh's own exit 255 (unreachable) before the subprocess
# bound turns it into rc None (hung): the distinction RemoteError carries.
# ServerAlive: a link that dies mid-bring-up (600s bound) fails in ~45s.
# ssh honours the FIRST value of a repeated -o and a command-line -o beats
# ~/.ssh/config, so a node user's config cannot turn BatchMode off.
CONNECT_TIMEOUT_S = 5
SSH_BATCH_OPTS = (
    "-o",
    "BatchMode=yes",
    "-o",
    f"ConnectTimeout={CONNECT_TIMEOUT_S}",
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=3",
)

# The most stdout one call may hold in RAM (``max_stdout_bytes``'s default):
# past it the child is killed and the call is RemoteError, so a node streaming
# gigabytes inside its time bound is an error, never a MemoryError. 64 MiB
# dwarfs every control and bring-up reply, and holds ``ignored_paths``' local
# `git ls-files` output for a very large repo. A pull passes its own cap.
MAX_REPLY_BYTES = 64 * 1024 * 1024
# How much of a child's stderr is held: only its tail is ever read, so older
# bytes are dropped as new ones arrive. Drained whole all the same -- a child
# blocked on a full stderr pipe would hang.
_STDERR_KEEP_BYTES = 1024 * 1024
# One read from a child's pipe.
_READ_CHUNK_BYTES = 64 * 1024
# How much of a failed command's stderr an error carries: enough for the
# cause, never a whole log.
_STDERR_TAIL_LINES = 20
# How long a killed child gets to be reaped. The process is already dead; the
# only thing that can still take time is a pipe magent stopped caring about.
_REAP_TIMEOUT_S = 1.0
# No console window per ssh on Windows -- the flag psmux._SPAWN_FLAGS carries
# for every psmux control spawn. Read off the module rather than hand-defined,
# so this file needs no `sys.platform` branch.
_SPAWN_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class RemoteError(RuntimeError):
    """A node call that failed: ``rc``, the last lines of its stderr, and the
    argv it ran with stdin reduced to its length.

    What magent authors is clean: ``command_redacted`` is argv only, and stdin
    -- where secrets travel -- appears as its length alone. ``stderr_tail`` is
    NOT magent's to clean: it is the node's own words, verbatim. So a script
    must never echo its payload or run under xtrace (``set -x``), and must
    hand secrets to tools by stdin or a credential helper, never a URL (git
    prints a token-bearing remote URL in "fatal: unable to access").

    ``rc`` None means the call never finished, and that covers two opposite
    cases. After a spawn failure the command never ran. After a timeout the
    OUTCOME IS UNKNOWN: killing the local ssh does not stop a non-tty remote
    command, so it may have run to completion (a killed send may have landed).
    A caller must therefore never retry a mutation blindly on rc None."""

    def __init__(
        self, rc: int | None, stderr_tail: str, command_redacted: tuple[str, ...]
    ) -> None:
        self.rc = rc
        self.stderr_tail = stderr_tail
        self.command_redacted = command_redacted
        super().__init__(
            f"{shlex.join(command_redacted)} failed (rc={rc}): {stderr_tail}"
        )


@functools.lru_cache(maxsize=1)
def find_ssh() -> str | None:
    """The ssh client on PATH, or None. Cached for the process lifetime like
    ``psmux.find_psmux``: a test that changes PATH clears it on the way in and
    out. Tests never see the real one (tests/conftest.py::_no_real_ssh)."""
    return shutil.which("ssh")


def _remote_string(argv: Sequence[str]) -> str:
    """The ONE remote command string (DECISION-9): sshd hands it to the node
    user's LOGIN shell, which may be zsh/fish -- so the argv is shlex-joined
    and handed to bash as a single quoted ``-c`` payload, and only bash ever
    parses it."""
    return "bash -c " + shlex.quote(shlex.join(argv))


def _ssh_tail(node: Node, remote_argv: Sequence[str], *, tty: bool) -> list[str]:
    """Everything after argv[0]: options, target, the one remote string."""
    tail = list(SSH_BATCH_OPTS)
    if tty:
        tail.append("-t")
    return [*tail, node.target, _remote_string(remote_argv)]


def _client(shown: tuple[str, ...]) -> str:
    """The client ``find_ssh`` resolves NOW -- looked up as this module's
    attribute at call time, so the conftest guard and ``fake_ssh`` both hold --
    or RemoteError rc 127. The only way an ssh argv gets its argv[0]."""
    exe = find_ssh()
    if exe is None:
        raise RemoteError(SSH_MISSING_RC, "ssh client not found on PATH", shown)
    return exe


def ssh_argv(node: Node, remote_argv: Sequence[str], *, tty: bool = False) -> list[str]:
    """``ssh`` argv running ``remote_argv`` on ``node`` as ONE ``bash -c``
    remote string. argv[0] is the client ``find_ssh`` resolved, never a bare
    ``"ssh"`` -- an argv built here and spawned elsewhere must not reach a
    client the guard never saw. Raises RemoteError rc 127 when there is none.
    The options are ``SSH_BATCH_OPTS``, this module's own set for
    non-interactive node calls; the interactive attach pane dials with
    ``attach_client``'s."""
    tail = _ssh_tail(node, remote_argv, tty=tty)
    return [_client(("ssh", *tail)), *tail]


def _tail(stderr: bytes) -> str:
    lines = stderr.decode("utf-8", "replace").splitlines()
    return "\n".join(lines[-_STDERR_TAIL_LINES:])


def _redacted(argv: list[str], input_bytes: bytes | None) -> tuple[str, ...]:
    if input_bytes is None:
        return tuple(argv)
    return (*argv, f"<stdin: {len(input_bytes)} bytes>")


def _run_shown(
    node: Node, argv_remote: Sequence[str], input_bytes: bytes | None
) -> tuple[str, ...]:
    """What an error and a log line may say about ``run(node, argv_remote,
    input_bytes=...)``: the program, not this PC's path to it; the one
    ``bash -c`` remote string; stdin by its length alone."""
    return _redacted(["ssh", *_ssh_tail(node, argv_remote, tty=False)], input_bytes)


class _Drain(threading.Thread):
    """One of a child's output pipes, read to its end on a daemon thread,
    holding at most ``cap`` bytes.

    Head mode (stdout): the first byte past the cap ends the read, drops all
    it held, sets ``over`` and closes the pipe -- so a writer still filling it
    fails instead of blocking forever. Tail mode (stderr): the oldest bytes are
    dropped instead, and the read runs to the end. Only this thread closes the
    pipe, so no close ever races a read. ``over`` and ``data`` are read once
    the thread has finished."""

    def __init__(self, pipe: IO[bytes] | None, cap: int, *, tail: bool) -> None:
        super().__init__(daemon=True)
        self._pipe = pipe
        self._cap = cap
        self._tail = tail
        self._chunks: deque[bytes] = deque()
        self._held = 0
        self.over = False

    def run(self) -> None:
        if self._pipe is None:
            return
        try:
            # A broken pipe ends the stream the way EOF does.
            with contextlib.suppress(OSError):
                self._read(self._pipe.fileno())
        finally:
            with contextlib.suppress(OSError):
                self._pipe.close()

    def _read(self, fd: int) -> None:
        # os.read, not the buffered object: it returns what is there now, so
        # a trickle is seen as it arrives.
        while chunk := os.read(fd, _READ_CHUNK_BYTES):
            if not self._tail and self._held + len(chunk) > self._cap:
                self.over = True
                self._chunks.clear()
                self._held = 0
                return
            self._chunks.append(chunk)
            self._held += len(chunk)
            while self._tail and self._held - len(self._chunks[0]) >= self._cap:
                self._held -= len(self._chunks.popleft())

    def data(self) -> bytes:
        held = b"".join(self._chunks)
        self._chunks.clear()
        return held[-self._cap :] if self._tail else held


def _feed(pipe: IO[bytes], data: bytes) -> None:
    """Write ``data`` to a child's stdin and close it -- on its own thread, so
    a child that writes before it reads cannot deadlock the call. A child that
    exits without reading it all is no error here; its exit status says what
    happened (a broken pipe, or EINVAL on Windows, is swallowed)."""
    try:
        with contextlib.suppress(OSError):
            pipe.write(data)
    finally:
        with contextlib.suppress(OSError):
            pipe.close()


def _kill(proc: subprocess.Popen[bytes]) -> None:
    proc.kill()
    # Reap the direct child only, bounded. Waiting on the pipes -- the drain
    # threads, or a communicate() -- would wait for every write end, and the
    # interpreter behind a .cmd/sh shim is a GRANDCHILD still holding one --
    # the 90s-for-a-5s timeout defect psmux.probe_control_plane documents.
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=_REAP_TIMEOUT_S)


def _finish(
    proc: subprocess.Popen[bytes], out: _Drain, err: _Drain, timeout_s: float
) -> bool:
    """Wait, within ``timeout_s``, for stdout to end (EOF or over its cap),
    then stderr, then the exit. False if the bound ran out first."""
    deadline = time.monotonic() + timeout_s
    out.join(timeout_s)
    if out.over:
        return True
    err.join(max(0.0, deadline - time.monotonic()))
    if out.is_alive() or err.is_alive():
        return False
    try:
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return False
    return True


def _spawn(
    argv: list[str],
    *,
    timeout_s: float,
    input_bytes: bytes | None,
    check: bool,
    shown: tuple[str, ...],
    label: str,
    quiet: bool = False,
    max_stdout_bytes: int = MAX_REPLY_BYTES,
) -> subprocess.CompletedProcess[bytes]:
    """One bounded child -- the shared body of ``run`` and the local git reads
    (``ignored_paths``). ``shown`` is what an error and a log line may say
    about the command; ``label`` opens every log line, naming who spawned it.
    ``quiet`` drops every one of those log lines; the RemoteError is raised
    exactly the same.

    Bounded in time AND in memory: stdout is read as it arrives, and a child
    whose stdout passes ``max_stdout_bytes`` is killed and raised as
    RemoteError rc None ("reply exceeded N bytes") -- nothing past the cap is
    held. stderr is drained too, keeping only its last ``_STDERR_KEEP_BYTES``."""
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_SPAWN_FLAGS,
        )
    except OSError as e:
        # A FileNotFoundError is the client vanishing between find_ssh and the
        # spawn (or its cached path going stale): the same "not installed" as
        # no client at all. strerror, not str(e): CPython's POSIX
        # _execute_child puts the client's path in str(e), and an error or a
        # log line names the program only.
        rc = SSH_MISSING_RC if isinstance(e, FileNotFoundError) else None
        reason = e.strerror or str(e)
        if not quiet:
            get_logger("nodes").warning(
                "%s could not start (%s): %s", label, reason, shlex.join(shown)
            )
        raise RemoteError(rc, reason, shown) from e
    if input_bytes is not None and proc.stdin is not None:
        threading.Thread(
            target=_feed, args=(proc.stdin, input_bytes), daemon=True
        ).start()
    out = _Drain(proc.stdout, max_stdout_bytes, tail=False)
    err = _Drain(proc.stderr, _STDERR_KEEP_BYTES, tail=True)
    out.start()
    err.start()
    if not _finish(proc, out, err, timeout_s):
        _kill(proc)
        if not quiet:
            get_logger("nodes").warning(
                "%s timed out after %.1fs: %s", label, timeout_s, shlex.join(shown)
            )
        raise RemoteError(None, f"timed out after {timeout_s:g}s", shown)
    if out.over:
        _kill(proc)
        if not quiet:
            get_logger("nodes").warning(
                "%s reply exceeded %d bytes: %s",
                label,
                max_stdout_bytes,
                shlex.join(shown),
            )
        raise RemoteError(None, f"reply exceeded {max_stdout_bytes} bytes", shown)
    stderr = err.data()
    if check and proc.returncode != 0:
        if not quiet:
            get_logger("nodes").warning(
                "%s failed (rc=%s): %s", label, proc.returncode, shlex.join(shown)
            )
        raise RemoteError(proc.returncode, _tail(stderr), shown)
    return subprocess.CompletedProcess(argv, proc.returncode, out.data(), stderr)


def run(
    node: Node,
    argv_remote: Sequence[str],
    *,
    timeout_s: float,
    input_bytes: bytes | None = None,
    check: bool = True,
    quiet: bool = False,
    max_stdout_bytes: int = MAX_REPLY_BYTES,
) -> subprocess.CompletedProcess[bytes]:
    """Run ``argv_remote`` on ``node`` over ssh, as ONE ``bash -c`` remote
    string (``_remote_string``). Raises RemoteError on a spawn failure, a
    missing client (rc 127), a timeout (rc None), a reply over
    ``max_stdout_bytes`` (rc None; the child is killed), or -- with ``check``
    -- a non-zero exit. With ``check=False`` every exit code comes back for the
    caller to classify. The returned ``CompletedProcess.args`` is the real
    argv, this PC's client path included: a caller must not log it.
    ``quiet`` drops the per-call log line, for a caller that reports the
    outcome itself (the sync daemon logs once per state change, not once per
    tick)."""
    tail = _ssh_tail(node, argv_remote, tty=False)
    shown = _run_shown(node, argv_remote, input_bytes)
    return _spawn(
        [_client(shown), *tail],
        timeout_s=timeout_s,
        input_bytes=input_bytes,
        check=check,
        shown=shown,
        label="node call",
        quiet=quiet,
        max_stdout_bytes=max_stdout_bytes,
    )


def _script_argv(args: Sequence[str]) -> list[str]:
    """The remote argv of every script run: the socket is ALWAYS ``$1``
    (DECISION-26 ii) -- ``lib.sh`` reads and shifts it -- then the caller's
    own args."""
    return ["bash", "-s", "--", SOCKET, *args]


def _frame_script(text: str, payload: bytes | None) -> bytes:
    """The stdin of one script run. No payload: the script text alone. With
    one: ``<text>\\n<PAYLOAD_SENTINEL>\\n<payload>``. The leading ``\\n``
    guards a script text without a final newline, whose last line would
    otherwise swallow the sentinel; when the text does end in one, the result
    is a harmless blank line before the sentinel."""
    body = text.encode("utf-8")
    if payload is None:
        return body
    return body + b"\n" + PAYLOAD_SENTINEL.encode("ascii") + b"\n" + payload


def _script_call(
    script: str, args: Sequence[str], stdin: bytes | None
) -> tuple[list[str], bytes]:
    """The remote argv and the stdin bytes of one ``run_script`` call -- built
    here once, so an error raised after the call (``sample``) names exactly
    what ran."""
    return _script_argv(args), _frame_script(node_scripts.script(script), stdin)


def run_script(
    node: Node,
    script: str,
    args: Sequence[str],
    *,
    timeout_s: float,
    stdin: bytes | None = None,
    max_stdout_bytes: int = MAX_REPLY_BYTES,
) -> subprocess.CompletedProcess[bytes]:
    """Run the packaged ``node_scripts/<script>.sh`` on ``node`` as
    ``bash -s -- <SOCKET> <args>``: the script on stdin, then -- when
    ``stdin`` is given -- the sentinel line and that payload. The socket is
    added here, on every call; ``args`` never carry it. Secrets belong in
    ``stdin``; ``args`` are argv, visible to the node's process table and to
    logs. A failure's ``stderr_tail`` is the script's own words (see
    ``RemoteError``): a script must never echo its payload.
    ``max_stdout_bytes`` goes to ``run`` as is.

    Refused before any ssh: ValueError for a script in
    ``node_scripts.NON_ENTRY_SCRIPTS`` (it would read the socket as its own
    first argument) or for a name that is not a plain script name
    (``./lib``); FileNotFoundError for an unknown script."""
    if f"{script}.sh" in node_scripts.NON_ENTRY_SCRIPTS:
        raise ValueError(f"{script}.sh is not a run_script entry point")
    argv_remote, framed = _script_call(script, args, stdin)
    return run(
        node,
        argv_remote,
        timeout_s=timeout_s,
        input_bytes=framed,
        max_stdout_bytes=max_stdout_bytes,
    )


def has_session(node: Node, sid: str) -> bool | None:
    """Is ``sid`` alive on ``node``? Exit 0 is True, a live session. Exit 1 is
    False, meant as tmux's own "no" (no such session, or no server at all) --
    but ANY exit 1 in the chain reads the same: a ``nologin`` shell, a
    ForceCommand, tmux's "error connecting to socket (Permission denied)", a
    client/server version mismatch. So False means "no session named ``sid``
    was found by whatever answered exit 1", and a caller that respawns on False
    must be prepared for that. Anything else -- ssh's 255, a missing tmux, a
    timeout -- is None: the PROBE failed, which says nothing about the session.
    The target is ``=sid`` because tmux otherwise prefix-matches, and ``api``
    would answer for ``api-2``."""
    try:
        result = run(
            node,
            [MUX, "-L", SOCKET, "has-session", "-t", f"={sid}"],
            timeout_s=PROBE_TIMEOUT_S,
            check=False,
        )
    except RemoteError:
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def _finite(value: object) -> float:
    """``value`` as a float. Three refusals:

    - TypeError for a non-number. A bool and a numeric string both count:
      json's ``true`` is a Python bool (an int subclass), and ``sample.sh``
      prints bare numbers, so ``"1.5"`` is not a reading. The isinstance
      guard is also what narrows ``object`` for ty.
    - ValueError for NaN or an infinity: json accepts them, the snapshot
      writer does not.
    - OverflowError for an int too large for a float (json has no bound on
      an integer's digits)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"not a number: {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"non-finite reading: {number}")
    return number


def _integral(value: object) -> int:
    """``value`` as an int, as strict as ``_finite``: TypeError for a
    non-number (a bool and a str included), ValueError for a float that is
    not finite or not whole (``16.9``). A whole float (``16.0``) is taken."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"not a number: {value!r}")
    if isinstance(value, int):
        return value
    if not math.isfinite(value) or not value.is_integer():
        raise ValueError(f"not a whole reading: {value}")
    return int(value)


def _load_sample(raw: object) -> LoadSample:
    """``magent_sample``'s JSON object as a LoadSample -- the ONE parse, shared
    by ``sample()`` and ``parse_pull``. KeyError, TypeError, ValueError or
    OverflowError when it is not one: a JSON list or string is a TypeError,
    and every field goes through ``_finite``/``_integral``, whose refusals
    (non-number, bool, string, NaN, infinity, fractional count, an integer too
    large for a float) are those exceptions."""
    if not isinstance(raw, dict):
        raise TypeError(f"expected an object, got {type(raw).__name__}")
    return LoadSample(
        ts=_finite(raw["ts"]),
        nproc=_integral(raw["nproc"]),
        load1=_finite(raw["load1"]),
        load5=_finite(raw["load5"]),
        load15=_finite(raw["load15"]),
        mem_total_mb=_integral(raw["mem_total_mb"]),
        mem_avail_mb=_integral(raw["mem_avail_mb"]),
        my_sessions=_integral(raw["my_sessions"]),
    )


def sample(node: Node) -> LoadSample:
    """One load reading from ``node`` (``sample.sh``). RemoteError when the node
    can't be reached, or answers something that is not a sample -- rc 0 on
    that error: the node answered; the answer was malformed. Its message
    carries a bounded head of what came back (a ``.bashrc`` banner on stdout
    is the likely cause). A non-finite, fractional-count, bool or string
    number is not a sample either."""
    result = run_script(node, "sample", [], timeout_s=PROBE_TIMEOUT_S)
    try:
        reading = _load_sample(json.loads(result.stdout.decode("utf-8", "replace")))
    # OverflowError is an ArithmeticError, not a ValueError: float() of a
    # 401-digit integer overflows. (`1e400` parses to inf, a ValueError from
    # _finite/_integral.)
    except (ValueError, KeyError, TypeError, OverflowError) as e:
        shown = _run_shown(node, *_script_call("sample", [], None))
        raise RemoteError(
            result.returncode,
            f"not a load sample: {e}; got {result.stdout[:200]!r}",
            shown,
        ) from e
    return reading


# --- The pull (node_sync's one ssh per node per tick) -------------------------
# How long one pull may take: one connection streaming every changed file of
# every session on the node. `pull` waits this long per phase.
PULL_TIMEOUT_S = 120.0
# The wire format of one pull.sh reply, in order:
#   PULL_HEADER, one JSON metadata line, a PLAIN (uncompressed) tar archive,
#   then the trailer line `PULL_TRAILER <member count>\n`, last.
# The first line of every pull.sh reply. Anything before it (a banner some rc
# file printed) is ignored; a reply without it is not a pull.
PULL_HEADER = b"MAGENT-PULL/1\n"
PULL_TRAILER = b"MAGENT-PULL-END "
"""The last line of every pull.sh reply: this prefix, the number of archive
members as ASCII digits, and ``\\n``. tarfile reads a cut or garbage header
past the first as end-of-archive, so without it a reply cut after member 1
parses as a SUCCESS holding one file -- and since ``now`` becomes the next
watermark, the lost members are never asked for again. The count is every
member pull.sh's tar writer added. pull.sh writes the line ONLY after that
writer closed without error: an exception mid-archive exits non-zero with no
trailer, so the reply reads as truncated."""
PULL_MAX_MEMBER_BYTES = 64 * 1024 * 1024
"""The largest file a pull ships. ``pull_node`` sends it to pull.sh as
``max_member_bytes``, and the node leaves a bigger file out and names it under
``skipped`` (``NodeSnapshot.skipped``) -- not a failure, or a file that stays
over the cap would fail its session every tick and freeze its watermark
forever. Defense in depth: a member that still declares more is not stored
here, and its session fails. A transcript is the largest file a pull carries."""
PULL_MAX_TOTAL_BYTES = 512 * 1024 * 1024
"""The most one reply may ask this PC to write, summed over the members it
would store; more is RemoteError before anything is written."""
PULL_MAX_REPLY_BYTES = PULL_MAX_TOTAL_BYTES + 4 * 1024 * 1024
"""The most stdout one pull may hold in RAM: ``pull_node`` passes it as
``max_stdout_bytes``. Over ``PULL_MAX_TOTAL_BYTES`` because the header, the
metadata line, tar's per-member headers and padding, and the trailer all ride
on top of the members' own bytes."""
PULL_COPY_CHUNK_BYTES = 1024 * 1024
"""A member is streamed to disk in chunks of this size, never read whole."""
# The newest mtime believed: ~36,800 years of Unix time, far past any real
# clock yet inside every platform's time_t, so os.utime cannot overflow. A
# member outside [0, _MAX_MTIME] (or NaN, or inf) is stored without its mtime.
_MAX_MTIME = 2**40
_TRAILER_COUNT = re.compile(rb"([0-9]{1,9})\n")
# The next watermark is the NODE's clock when its scan began, minus this: a
# file written in the same second as the scan is asked for again, never lost.
WATERMARK_OVERLAP_S = 1.0
_PULL_KINDS = frozenset({"transcripts", "state"})
# A session directory sits beside these per-node files; no sid may take a name.
_RESERVED_NAMES = frozenset(
    {"sessions.json", "load.jsonl", "pull.json", "node-map.json"}
)
# Every path part must be a legal file name on THIS PC, which may be Windows.
_UNSAFE_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
# ntpath's reserved set on 3.13 (ntpath.isreserved is 3.13+, so it is copied):
# the superscript digits count as COM/LPT numbers too.
_DEVICE_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"COM{c}" for c in "123456789¹²³"),
        *(f"LPT{c}" for c in "123456789¹²³"),
    }
)


@dataclass(frozen=True)
class SidPull:
    """What a pull asks a node for, per session:
    - ``roots``: the session's cwd as the node map records it (``~`` unexpanded;
      pull.sh expands it);
    - ``project_dir``: the finished ``~/.claude/projects`` name for that cwd.
      It is None until the node has reported its real path once, because nodes
      never encode (DECISION-11f);
    - ``since``: the node-clock watermark that a file must be newer than."""

    roots: tuple[str, ...]
    project_dir: str | None
    since: float


@dataclass(frozen=True)
class NodeSnapshot:
    """One pull.sh reply, parsed, with its files stored:
    - ``now``: the node's clock when its scan began (the next watermark);
    - ``files``: what landed on this PC;
    - ``failed_sids``: sessions with a file that could not be stored. Their
      watermark must not move;
    - ``skipped``: per session, the archive names pull.sh left out because
      they were over ``PULL_MAX_MEMBER_BYTES`` on the node. NOT a failure:
      a file that stays over the cap would otherwise fail its session on
      every tick and freeze its watermark forever. The caller reports them."""

    now: float
    sessions: tuple[str, ...]
    sample: LoadSample | None
    realpaths: Mapping[str, str]
    state_files: Mapping[str, tuple[str, ...]]
    files: tuple[Path, ...]
    failed_sids: frozenset[str]
    skipped: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


def _pull_error(message: str) -> RemoteError:
    # rc 0: the node answered, and the answer was not a pull.
    return RemoteError(0, message, ("pull.sh",))


def _safe_part(part: str) -> bool:
    # A part ending in "." or " " is refused outright (Windows drops them), so
    # the device check needs only ntpath's: the stem before the FIRST dot,
    # trailing spaces dropped -- "CON .jsonl" opens the console.
    return (
        part not in ("", ".", "..")
        and _UNSAFE_CHARS.search(part) is None
        and not part.endswith((".", " "))
        and part.split(".", 1)[0].rstrip(" ").upper() not in _DEVICE_NAMES
    )


def pullable_sid(sid: str) -> bool:
    """Can ``sid`` name a directory under ``~/.magent/nodes/<nick>/`` here?"""
    return _safe_part(sid) and sid not in _RESERVED_NAMES


def _str_dict(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def _names_dict(raw: object) -> dict[str, tuple[str, ...]]:
    if not isinstance(raw, dict):
        return {}
    return {
        k: tuple(
            n for n in v if isinstance(n, str) and n.endswith(".json") and _safe_part(n)
        )
        for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, list)
    }


def _skipped_dict(raw: object) -> dict[str, tuple[str, ...]]:
    """pull.sh's ``skipped`` map: ``{sid: [archive name, ...]}``. Only its
    strings are kept; anything else there is ignored, never an error -- the
    names are the node's report, read to be logged, never a path to open."""
    if not isinstance(raw, dict):
        return {}
    kept = {
        k: tuple(n for n in v if isinstance(n, str))
        for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, list)
    }
    return {k: names for k, names in kept.items() if names}


def _member_parts(
    member: tarfile.TarInfo, sids: frozenset[str]
) -> tuple[str, ...] | None:
    """``<sid>/transcripts/<any depth>`` or ``<sid>/state/<name>.json`` for a
    requested sid, every part a legal name here, regular files only -- or None."""
    if not member.isfile():
        return None
    # tarfile calls a GNU sparse member a file, and its size is whatever the
    # header claims. pull.sh never passes `-S`, so a sparse member is never ours.
    if member.issparse():
        return None
    parts = tuple(member.name.split("/"))
    if len(parts) < 3 or parts[0] not in sids or parts[1] not in _PULL_KINDS:
        return None
    if parts[1] == "state" and (len(parts) != 3 or not parts[2].endswith(".json")):
        return None
    if not all(_safe_part(p) for p in parts):
        return None
    return parts


def _usable_mtime(value: object) -> float | None:
    """A member's mtime as ``os.utime`` can take it, or None. A PAX header
    can say ``nan`` (ValueError from utime) or ``1e400``, and GNU base-256
    can say ``10**20`` (OverflowError): the node's word, never a crash here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    if not math.isfinite(number) or not 0 <= number <= _MAX_MTIME:
        return None
    return number


def _write_file(path: Path, reader: IO[bytes], mtime: float | None) -> None:
    """Store one pulled file whole (sibling ``.part`` + ``os.replace``) with the
    node's mtime when it has a usable one, so a reader never sees half a
    transcript. Streamed in ``PULL_COPY_CHUNK_BYTES`` chunks, never whole."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.part")
    try:
        with part.open("wb") as out:
            shutil.copyfileobj(reader, out, length=PULL_COPY_CHUNK_BYTES)
        if mtime is not None:
            os.utime(part, (mtime, mtime))
        os.replace(part, path)
    except BaseException:
        with contextlib.suppress(OSError):
            part.unlink()
        raise


def _newness(member: tarfile.TarInfo) -> float:
    mtime = _usable_mtime(member.mtime)
    return -1.0 if mtime is None else mtime


def _select(
    members: Sequence[tarfile.TarInfo], sids: frozenset[str]
) -> tuple[dict[tuple[str, ...], tarfile.TarInfo], frozenset[str]]:
    """What to store: one member per path -- the newer mtime wins a duplicate,
    a tie goes to the later one, as tar itself would leave it -- and the sids
    that hold a member over ``PULL_MAX_MEMBER_BYTES`` (never read). Nothing is
    read here; only headers are looked at."""
    log = get_logger("nodes")
    chosen: dict[tuple[str, ...], tarfile.TarInfo] = {}
    oversized: set[str] = set()
    skipped = 0
    for member in members:
        parts = _member_parts(member, sids)
        if parts is None:
            skipped += 1
            continue
        if member.size > PULL_MAX_MEMBER_BYTES:
            oversized.add(parts[0])
            log.warning(
                "node pull: %s declares %d bytes, over the %d-byte cap; not stored",
                "/".join(parts),
                member.size,
                PULL_MAX_MEMBER_BYTES,
            )
            continue
        held = chosen.get(parts)
        if held is None or _newness(member) >= _newness(held):
            chosen[parts] = member
    if skipped:
        log.warning(
            "node pull: skipped %d archive member(s) outside the requested sessions",
            skipped,
        )
    return chosen, frozenset(oversized)


def _extract(
    archive: bytes, count: int, *, dest: Path, sids: frozenset[str]
) -> tuple[tuple[Path, ...], frozenset[str]]:
    """Check the archive against its trailer's ``count`` and its size caps,
    THEN store what was asked for -- a damaged or oversized archive writes
    nothing. A session with a file that cannot be stored fails alone."""
    if not archive:
        if count:
            raise _pull_error(
                f"pull archive damaged: expected {count} member(s), saw 0"
            )
        return (), frozenset()
    log = get_logger("nodes")
    files: list[Path] = []
    failed: set[str] = set()
    try:
        # "r:" -- a plain tar only. pull.sh never compresses (ssh can), and a
        # compressed archive would decompress past every cap below.
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            members = tar.getmembers()
            if len(members) != count:
                raise _pull_error(
                    f"pull archive damaged: expected {count} member(s), "
                    f"saw {len(members)}"
                )
            chosen, oversized = _select(members, sids)
            total = sum(m.size for m in chosen.values())
            if total > PULL_MAX_TOTAL_BYTES:
                raise _pull_error(
                    f"pull archive holds {total} bytes, "
                    f"over the {PULL_MAX_TOTAL_BYTES}-byte cap"
                )
            for parts, member in chosen.items():
                if parts[0] in failed:
                    continue
                reader = tar.extractfile(member)
                if reader is None:  # _member_parts admits regular files only
                    continue
                name = "/".join(parts)
                mtime = _usable_mtime(member.mtime)
                if mtime is None:
                    log.warning(
                        "node pull: %s has an unusable mtime; stored without it", name
                    )
                target = dest.joinpath(*parts)
                try:
                    _write_file(target, reader, mtime)
                except OSError as e:
                    failed.add(parts[0])
                    log.warning("node pull: cannot store %s: %s", name, e)
                    continue
                files.append(target)
    # ValueError/OverflowError: a header field this module did not foresee
    # still ends as a pull error, never an escape past the RemoteError contract.
    except (tarfile.TarError, EOFError, OSError, ValueError, OverflowError) as e:
        raise _pull_error(f"unreadable pull archive: {e}") from e
    return tuple(files), frozenset(failed | oversized)


def _split_trailer(rest: bytes) -> tuple[bytes, int]:
    """``rest`` (everything after the header) without its trailer line, and
    the member count that line claims. The trailer is the LAST line, so it is
    looked for from the end: a file holding the same text sits before it."""
    at = rest.rfind(PULL_TRAILER)
    found = _TRAILER_COUNT.fullmatch(rest, at + len(PULL_TRAILER)) if at >= 0 else None
    if found is None:
        raise _pull_error("reply truncated: no MAGENT-PULL-END line at its end")
    return rest[:at], int(found.group(1))


def parse_pull(stdout: bytes, *, dest: Path, sids: Collection[str]) -> NodeSnapshot:
    """Parse a pull.sh reply and store its files under ``dest`` (a node's
    mirror dir). Only the requested ``sids`` are believed: their metadata, and
    archive members shaped ``<sid>/transcripts/...`` or ``<sid>/state/<x>.json``
    whose every part is a legal name here. Everything else is dropped with one
    warning. RemoteError (rc 0) when the reply is not a pull at all -- no
    header, no ``PULL_TRAILER`` last line (truncated), a member count that
    disagrees with the trailer, a compressed or unreadable archive, or one
    over ``PULL_MAX_TOTAL_BYTES``. ValueError when a requested sid is not
    ``pullable_sid``: that is the caller's bug, not the node's.

    A member lands at ``dest/<its own archive path>`` -- nothing here maps a
    path back to a project directory."""
    wanted = frozenset(sids)
    bad = next((s for s in sorted(wanted) if not pullable_sid(s)), None)
    if bad is not None:
        raise ValueError(f"not a pullable session name: {bad!r}")
    _, sep, rest = stdout.partition(PULL_HEADER)
    if not sep:
        raise _pull_error("no MAGENT-PULL header in the reply")
    framed, count = _split_trailer(rest)
    meta_line, _, archive = framed.partition(b"\n")
    try:
        meta = json.loads(meta_line.decode("utf-8"))
    except ValueError as e:
        raise _pull_error(f"unreadable pull metadata: {e}") from e
    if not isinstance(meta, dict):
        raise _pull_error("pull metadata is not an object")
    now = meta.get("now")
    # json.loads accepts NaN and Infinity. A non-finite clock would become a
    # NaN watermark, which write_json_atomic refuses with ValueError.
    if (
        isinstance(now, bool)
        or not isinstance(now, (int, float))
        or not math.isfinite(now)
    ):
        raise _pull_error("pull metadata has no clock")
    raw_sessions = meta.get("sessions")
    # A non-string entry is corruption, never a name to skip: dropping it would
    # write a snapshot without that session, and D would read it as dead.
    if not isinstance(raw_sessions, list) or not all(
        isinstance(s, str) for s in raw_sessions
    ):
        raise _pull_error("pull metadata's sessions is not a list of names")
    sessions = tuple(s for s in raw_sessions if s)
    try:
        reading: LoadSample | None = _load_sample(meta.get("sample"))
    except (ValueError, KeyError, TypeError, OverflowError):
        reading = None
    files, failed = _extract(archive, count, dest=dest, sids=wanted)
    return NodeSnapshot(
        now=float(now),
        sessions=sessions,
        sample=reading,
        realpaths={
            k: v for k, v in _str_dict(meta.get("realpaths")).items() if k in wanted
        },
        state_files={
            k: v for k, v in _names_dict(meta.get("state_files")).items() if k in wanted
        },
        files=files,
        failed_sids=failed,
        skipped={
            k: v for k, v in _skipped_dict(meta.get("skipped")).items() if k in wanted
        },
    )


def _pull_call(sids: Mapping[str, SidPull]) -> tuple[list[str], bytes]:
    """The remote argv and stdin of one pull.sh call for ``sids``."""
    payload = {
        "sids": {
            sid: {
                "roots": list(s.roots),
                "project_dir": s.project_dir,
                "since": s.since,
            }
            for sid, s in sids.items()
        },
        # The node skips a bigger file and names it under `skipped`, so a file
        # that stays over the cap never fails its session (NodeSnapshot.skipped).
        "max_member_bytes": PULL_MAX_MEMBER_BYTES,
    }
    return _script_call(
        "pull", [], json.dumps(payload).encode("utf-8")
    )  # `bash -s -- <SOCKET>`: the socket is always $1


def refused_pull(node: Node, sids: Mapping[str, SidPull], message: str) -> RemoteError:
    """A pull of ``sids`` refused on this PC before any ssh: rc 0, and the
    command it would have run, shown the way every node error shows one
    (``_run_shown``)."""
    argv, input_bytes = _pull_call(sids)
    return RemoteError(0, message, _run_shown(node, argv, input_bytes))


def pull_node(
    node: Node,
    sids: Mapping[str, SidPull],
    *,
    dest: Path | None = None,
    timeout_s: float = PULL_TIMEOUT_S,
) -> NodeSnapshot:
    """ONE ssh to ``node`` running pull.sh for ``sids`` (possibly none: the
    call still returns liveness and load), with the files stored under
    ``dest`` (default: the node's mirror dir). Quiet: the caller reports.
    RemoteError on a transport failure (255), a timeout or a reply over
    ``PULL_MAX_REPLY_BYTES`` (None), a node without python3 (3), or a reply
    that is not a pull (0)."""
    argv, input_bytes = _pull_call(sids)
    result = run(
        node,
        argv,
        timeout_s=timeout_s,
        input_bytes=input_bytes,
        check=False,
        quiet=True,
        max_stdout_bytes=PULL_MAX_REPLY_BYTES,
    )
    if result.returncode != 0:
        raise RemoteError(
            result.returncode,
            _tail(result.stderr),
            _run_shown(node, argv, input_bytes),
        )
    return parse_pull(
        result.stdout,
        dest=dest if dest is not None else node_dir(node.nick),
        sids=frozenset(sids),
    )


@dataclass(frozen=True)
class PullResult:
    """``pull``'s answer (master §3): the files that landed, and the watermark
    to pass as ``since_epoch`` next time."""

    files: tuple[Path, ...]
    since: float


def pull(
    node: Node,
    sid: str,
    remote_dirs: Sequence[str],
    since_epoch: float,
    *,
    timeout_s: float = PULL_TIMEOUT_S,
) -> PullResult:
    """Pull one session's transcripts and state newer than ``since_epoch``
    into the node's mirror (master §3; G's recall calls it with 0.0).

    Two calls when the transcript dir is not known yet: the first learns the
    session's real path on the node, the second asks for
    ``~/.claude/projects/<encoded real path>`` -- the PC encodes, never the
    node (DECISION-11f). Up to 2 x ``timeout_s`` in all. The watermark only
    moves when every file was stored; a file the node skipped as over the cap
    does not hold it (``NodeSnapshot.skipped``)."""
    # Before any ssh: parse_pull refuses the same name with ValueError, which
    # would mean THIS caller's bug, not the node's.
    roots = tuple(remote_dirs)
    if not pullable_sid(sid):
        raise refused_pull(
            node,
            {sid: SidPull(roots=roots, project_dir=None, since=since_epoch)},
            f"not a pullable session name: {sid!r}",
        )
    first = pull_node(
        node,
        {sid: SidPull(roots=roots, project_dir=None, since=since_epoch)},
        timeout_s=timeout_s,
    )
    real = first.realpaths.get(sid)
    if real is None:
        return PullResult(files=first.files, since=since_epoch)
    snap = pull_node(
        node,
        {
            sid: SidPull(
                roots=roots, project_dir=encoded_project_dir(real), since=since_epoch
            )
        },
        timeout_s=timeout_s,
    )
    failed = sid in first.failed_sids or sid in snap.failed_sids
    since = since_epoch if failed else snap.now - WATERMARK_OVERLAP_S
    # Both calls ship the session's state records: each path is listed once.
    files = tuple(dict.fromkeys(first.files + snap.files))
    return PullResult(files=files, since=since)


def ignored_paths(repo: Path, *, timeout_s: float, label: str) -> tuple[str, ...]:
    """What git ignores in the LOCAL ``repo``: ``git ls-files --others --ignored
    --exclude-standard --directory -z`` -- repo-relative, '/'-separated, and a
    wholly ignored directory as ONE ``dir/`` entry (``node_modules`` is one
    line, not a hundred thousand). Read-only. The raw material for
    ``nodes.push_set``; ``git_state`` carries it as ``LocalGitState.ignored``.
    ``label`` names the caller in the log line a failure writes.

    A ``repo`` that is not a git repository is ``RemoteError(rc=128, <git's
    stderr tail>)`` -- git's own "fatal: not a git repository" exit. A missing
    ``git`` is RemoteError rc None ("git not found on PATH"): the command never
    ran. ``_spawn`` reads a FileNotFoundError as the missing ssh client (rc
    127), which is not what happened here."""
    argv = [
        "git",
        "-C",
        str(repo),
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "--directory",
        "-z",
    ]
    shown = tuple(argv)
    try:
        result = _spawn(
            argv,
            timeout_s=timeout_s,
            input_bytes=None,
            check=True,
            shown=shown,
            label=label,
        )
    except RemoteError as e:
        if isinstance(e.__cause__, FileNotFoundError):
            raise RemoteError(None, "git not found on PATH", shown) from e.__cause__
        raise
    return tuple(p for p in result.stdout.decode("utf-8", "replace").split("\0") if p)
