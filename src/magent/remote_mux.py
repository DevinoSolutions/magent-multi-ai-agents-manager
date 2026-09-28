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
leaves ``attach_client``, ``env``, ``log``, ``node_scripts``, ``nodes``,
``psmux`` and ``sessions``.
"""

from __future__ import annotations

import contextlib
import filecmp
import functools
import io
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from magent import node_scripts, psmux

# find_ssh is bound by value, not read off attach_client at call time: the
# conftest guard answers None for attach_client.find_ssh, and this module's own
# find_ssh is the seam every remote_mux test fakes (fake_ssh repoints it).
from magent.attach_client import SSH_MISSING_RC, TMUX_SOCKET
from magent.attach_client import find_ssh as _find_ssh_client
from magent.env import git_child_env
from magent.log import get_logger

# pullable_sid is re-exported: its one owner is nodes.py (a leaf that must not
# reach into this seam), and callers keep saying remote_mux.pullable_sid. The
# one load-sample parse is the leaf's too (with its _finite, which parse_pull
# also reads the node's clock and resume points through); sample() and
# parse_pull call it.
from magent.nodes import (
    READ_FLAGS,
    LoadSample,
    LocalGitState,
    NodeConfigError,
    RepoStatus,
    _finite,
    _load_sample,
    _safe_part,
    absolute_remote,
    encoded_project_dir,
    node_dir,
    parse_repo_status,
    path_exists,
    path_is_dir,
    pullable_sid,
    walk_memory,
)
from magent.sessions import build_resume_command

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence
    from typing import IO

    from magent.nodes import Node, Recipe

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

    ``rc`` None means the call never finished, and that covers three cases.
    After a spawn failure the command never ran. After a timeout, and after a
    reply over the cap, magent killed the local ssh mid-call -- and killing it
    does not stop a non-tty remote command, so it may still be running or have
    run to completion (a killed send may have landed): the OUTCOME IS UNKNOWN.
    A caller must therefore never retry a mutation blindly on rc None.

    Each question has its own answer, and a caller must never match
    ``stderr_tail`` to learn any of them:

    - ``outcome_unknown``: may the remote command have run? A read-only
      property, ``timed_out or over_cap`` -- derived, never stored, so it can
      never drift from the two facts it rests on. It is what any retry of a
      MUTATING remote call must read (Plan F's provision): retry blindly only
      when it is False. One caveat it cannot carry: rc ``SSH_TRANSPORT_RC``
      (255) is ambiguous too -- ssh cannot tell "never connected" from
      "dropped mid-call" -- so a mutation must be safe to re-run there as well.
    - ``timed_out``: did the node go silent? Only ``_spawn``'s timeout sets it.
      It is what reachability reads (node_sync counts it as unreachable). An
      over-cap reply is a node that answered, too much, so it is False there.
    - ``over_cap``: did the reply pass the stdout cap? Then ``stderr_tail``'s
      FIRST line is magent's own ``reply exceeded N bytes`` and the child's
      words follow it.

    A spawn failure sets none of them."""

    def __init__(
        self,
        rc: int | None,
        stderr_tail: str,
        command_redacted: tuple[str, ...],
        *,
        timed_out: bool = False,
        over_cap: bool = False,
    ) -> None:
        self.rc = rc
        self.stderr_tail = stderr_tail
        self.command_redacted = command_redacted
        self.timed_out = timed_out
        self.over_cap = over_cap
        super().__init__(
            f"{shlex.join(command_redacted)} failed (rc={rc}): {stderr_tail}"
        )

    @property
    def outcome_unknown(self) -> bool:
        """May the remote command have run? Retry safety of a MUTATING remote
        call (Plan F's provision) reads THIS; reachability reads ``timed_out``;
        ``over_cap`` is the size fact. Both kills end the local ssh mid-call,
        and neither stops a non-tty remote command. False does not cover an
        rc 255 transport drop, which may also have run (see the class)."""
        return self.timed_out or self.over_cap


@functools.lru_cache(maxsize=1)
def find_ssh() -> str | None:
    """The ssh client, or None -- by ``attach_client.find_ssh``'s rule
    (Windows' own OpenSSH first, PATH as the fallback), so a bring-up and the
    attach window it opens dial through the same client and the same agent.
    Cached for the process lifetime like ``psmux.find_psmux``, which also makes
    the one debug line naming the client a once-per-process line; a test that
    changes PATH clears it on the way in and out. Tests never see the real one
    (tests/conftest.py::_no_real_ssh)."""
    exe = _find_ssh_client()
    get_logger("nodes").debug("ssh client for node calls: %s", exe)
    return exe


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
        raise RemoteError(SSH_MISSING_RC, "no ssh client found", shown)
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
    pipe, so no close ever races a read. ``over`` is read once the thread has
    finished. ``data`` HANDS OVER what is held and forgets it -- a second call
    returns only what arrived since -- and may be taken before the thread has
    finished, as what has arrived so far: a grandchild can hold a pipe open
    long after the child is gone."""

    def __init__(self, pipe: IO[bytes] | None, cap: int, *, tail: bool) -> None:
        super().__init__(daemon=True)
        self._pipe = pipe
        self._cap = cap
        self._tail = tail
        # _chunks and _held are shared with a data() taken mid-read.
        self._lock = threading.Lock()
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
            with self._lock:
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
        with self._lock:
            held = b"".join(self._chunks)
            self._chunks.clear()
            self._held = 0
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


def _os_words(e: OSError) -> str:
    """An OSError's own words for a log line -- errno, winerror when set,
    strerror -- and never ``str(e)`` or ``e.filename``: for a spawn on POSIX
    those carry the client's path, and a log line names the program only."""
    winerror = getattr(e, "winerror", None)
    parts = [
        f"errno {e.errno}" if e.errno is not None else "",
        f"winerror {winerror}" if winerror is not None else "",
        e.strerror or "",
    ]
    return ", ".join(p for p in parts if p)


def os_detail(exc: BaseException) -> str:
    """The OS's words behind ``exc`` (``_os_words`` of its ``__cause__``) for
    a caller's WARNING line; "" when that cause is no OSError or has no
    words. A quiet call (``pull_node``'s) logs nothing itself, and its
    RemoteError names only the class -- the screen's -- so the callers that
    log it add this."""
    cause = exc.__cause__
    return _os_words(cause) if isinstance(cause, OSError) else ""


def _spawn(
    argv: list[str],
    *,
    timeout_s: float,
    input_bytes: bytes | None,
    check: bool,
    shown: tuple[str, ...],
    label: str,
    env: Mapping[str, str] | None = None,
    quiet: bool = False,
    max_stdout_bytes: int = MAX_REPLY_BYTES,
) -> subprocess.CompletedProcess[bytes]:
    """One bounded child -- the shared body of ``run`` and the local git reads
    (``ignored_paths``, ``git_state``). ``shown`` is what an error and a log line may say
    about the command; ``label`` opens every log line, naming who spawned it.
    ``env`` None is the plain inherited environment (every ssh call); only
    the local git reads pass one (``_local_git``). ``quiet`` drops every one
    of those log lines; the RemoteError is raised exactly the same.

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
            env=env,
        )
    except OSError as e:
        # A FileNotFoundError is the client vanishing between find_ssh and the
        # spawn (or its cached path going stale): the same "not installed" as
        # no client at all. The program and the class: strerror is the OS's
        # words (localized, per platform), not ours, and CPython's POSIX
        # _execute_child puts the client's path in str(e) -- an error or a log
        # line names the program only.
        rc = SSH_MISSING_RC if isinstance(e, FileNotFoundError) else None
        reason = f"could not start {shown[0]} ({type(e).__name__})"
        if not quiet:
            # The OS's words and codes are the log's (a quiet caller adds them
            # to its own line through os_detail).
            words = _os_words(e)
            get_logger("nodes").warning(
                "%s %s: %s",
                label,
                f"{reason}: {words}" if words else reason,
                shlex.join(shown),
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
        raise RemoteError(
            None,
            f"timed out after {timeout_s:g}s",
            shown,
            timed_out=True,
        )
    if out.over:
        _kill(proc)
        if not quiet:
            get_logger("nodes").warning(
                "%s reply exceeded %d bytes: %s",
                label,
                max_stdout_bytes,
                shlex.join(shown),
            )
        reason = f"reply exceeded {max_stdout_bytes} bytes"
        # What the child said before it flooded is the likely cause. stderr
        # gets the reap bound to end -- time too for words already in the pipe
        # to be read -- and what it delivered is reported either way: a
        # grandchild may hold it open long after the child is gone, and the
        # words already read are no less the child's. A stream still open may
        # end mid-line, though, and a fragment is no reason: only whole lines
        # are kept. Asked BEFORE the handover, so a stream that ends between
        # the two is trimmed, never a half-line kept.
        err.join(_REAP_TIMEOUT_S)
        ended = not err.is_alive()
        held = err.data()
        said = _tail(held if ended else held[: held.rfind(b"\n") + 1])
        # Killed mid-call, so the remote may still be running; but the node
        # answered, so it is not unreachable (timed_out stays False).
        raise RemoteError(
            None,
            f"{reason}\n{said}" if said else reason,
            shown,
            over_cap=True,
        )
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


def tmux_argv(*args: str) -> list[str]:
    """``tmux -L magent <args>``: the one node server (DECISION-3)."""
    return [MUX, "-L", SOCKET, *args]


def list_sessions(node: Node) -> list[str] | None:
    """Every session on ``node``'s magent server. ``[]`` when tmux said there
    are none (exit 1: no server). None when the PROBE failed (unreachable, no
    tmux, a timeout): "I could not look" is never "nothing is running"."""
    try:
        result = run(
            node,
            tmux_argv("list-sessions", "-F", "#{session_name}"),
            timeout_s=PROBE_TIMEOUT_S,
            check=False,
        )
    except RemoteError:
        return None
    if result.returncode == 1:
        return []
    if result.returncode != 0:
        return None
    return [
        line for line in result.stdout.decode("utf-8", "replace").splitlines() if line
    ]


def kill_session(node: Node, sid: str) -> bool | None:
    """Kill ``sid`` on ``node``: True killed, False it was not there, None the
    call failed and the session may still be running."""
    try:
        result = run(
            node,
            tmux_argv("kill-session", "-t", f"={sid}"),
            timeout_s=PROBE_TIMEOUT_S,
            check=False,
        )
    except RemoteError:
        return None
    return {0: True, 1: False}.get(result.returncode)


def decoration_args(sid: str, nick: str, code_hint: bool) -> list[list[str]]:
    """The ten decoration commands of a node session: the SAME vocabulary as
    ``psmux.decoration_argv`` (status hints, the F1/F2 bindings, the window
    name rule), with the brand naming the node. One server hosts every node
    session, so the options are scoped with ``-t =sid:`` rather than ``-g``,
    where psmux's server-per-session model allows a global. The colon is
    load-bearing: ``set``/``setw``/``rename-window`` take a PANE or WINDOW
    target, where a bare ``=sid`` is no session ("no such session: =sid") --
    ``=sid:`` is the exact session and its current window. Only session-target
    commands (``has-session``, ``kill-session``, ``attach``) take ``=sid``."""
    hints, hints_len = psmux.status_hints(code_hint)
    brand, brand_len = psmux.status_left(nick)
    target = f"={sid}:"
    fmt = psmux.WINDOW_STATUS_FORMAT
    return [
        tmux_argv("bind", "-n", "F1", "detach-client"),
        tmux_argv("set", "-t", target, "status-right", hints),
        tmux_argv("set", "-t", target, "status-right-length", hints_len),
        tmux_argv("set", "-t", target, "status-left", brand),
        tmux_argv("set", "-t", target, "status-left-length", brand_len),
        psmux.f2_binding_argv(tmux_argv(), code_hint),
        tmux_argv("rename-window", "-t", target, psmux.window_display_name(sid)),
        tmux_argv("setw", "-t", target, "automatic-rename", "off"),
        tmux_argv("setw", "-t", target, "window-status-format", fmt),
        tmux_argv("setw", "-t", target, "window-status-current-format", fmt),
    ]


def decoration_script(sid: str, nick: str, code_hint: bool) -> str:
    """``decoration_args`` as a bash script, one line per command, each allowed
    to fail: a cosmetic option must never fail the bring-up it rides."""
    return "".join(
        shlex.join(argv) + " || true\n"
        for argv in decoration_args(sid, nick, code_hint)
    )


def decorate(node: Node, sid: str, nick: str) -> bool:
    """(Re)apply the decoration to a session already running on ``node``, in
    one connection. ``code_hint`` is THIS machine's answer: F2 is caught by the
    listener on this PC, not by anything on the node."""
    script = decoration_script(sid, nick, psmux.code_on_path())
    try:
        result = run(
            node,
            ["bash", "-s"],
            timeout_s=SCRIPT_TIMEOUT_S,
            input_bytes=script.encode("utf-8"),
            check=False,
        )
    except RemoteError:
        return False
    return result.returncode == 0


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
    # nodes._finite/_integral.) RecursionError is json.loads' answer to deep
    # nesting, which fits easily inside the reply cap: the node's bad answer
    # too.
    except (ValueError, KeyError, TypeError, OverflowError, RecursionError) as e:
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
"""The NODE's cap on one whole pull.sh reply. ``pull_node`` sends it as
``max_total_bytes``, and pull.sh stops adding members before its reply --
header, metadata, every tar header and pad, the trailer -- would pass it. What
did not fit is named under ``truncated`` (``NodeSnapshot.truncated``), with
where to resume under ``resume``. Without this cap a first ``since=0`` pull
whose history sums past ``PULL_MAX_REPLY_BYTES`` is killed on every tick, its
watermark never moves, and the node reads unreachable forever although ssh
works. Also checked here: members summing past it are RemoteError before
anything is written."""
PULL_MAX_REPLY_BYTES = PULL_MAX_TOTAL_BYTES + 4 * 1024 * 1024
"""THIS PC's cap on the stdout one pull may hold in RAM: ``pull_node`` passes
it as ``max_stdout_bytes``, and a reply past it is RemoteError rc None. The
margin over ``PULL_MAX_TOTAL_BYTES`` is for what pull.sh cannot count: bytes
a node user's shell rc file prints before the script runs."""
PULL_COPY_CHUNK_BYTES = 1024 * 1024
"""A member is streamed to disk in chunks of this size, never read whole."""
PULL_TEMP_PREFIX = "."
PULL_TEMP_SUFFIX = ".part"
"""The ONE shape of a pulled file's in-flight temp: ``_write_file`` hands this
pair to mkstemp and ``_pull_temp`` recognises it, so the writer and the tar
filter that must never ship a stranded temp cannot drift apart."""
# The newest mtime believed: ~36,800 years of Unix time, far past any real
# clock yet inside every platform's time_t, so os.utime cannot overflow. A
# member outside [0, _MAX_MTIME] (or NaN, or inf) is stored without its mtime.
_MAX_MTIME = 2**40
_TRAILER_COUNT = re.compile(rb"([0-9]{1,9})\n")
# The next watermark is the NODE's clock when its scan began, minus this: a
# file written in the same second as the scan is asked for again, never lost.
WATERMARK_OVERLAP_S = 1.0
_PULL_KINDS = frozenset({"transcripts", "state"})


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
      every tick and freeze its watermark forever. The caller reports them;
    - ``unreadable``: the same shape, for files (or transcript directories)
      the node user could not read -- any errno but ENOENT. Not a failure
      either, for the same reason: a file that stays unreadable would freeze
      the watermark. Named rather than dropped in silence;
    - ``truncated``: the same shape, for files left out because the reply
      reached ``PULL_MAX_TOTAL_BYTES``. They are still owed: see ``resume``;
    - ``resume``: per truncated session, the mtime of its oldest file left
      out. pull.sh ships oldest first, so every older one arrived.
      ``next_since`` turns it into the watermark that continues from there --
      holding the old one instead would ask for the same files, which fit
      the same way, on every tick."""

    now: float
    sessions: tuple[str, ...]
    sample: LoadSample | None
    realpaths: Mapping[str, str]
    state_files: Mapping[str, tuple[str, ...]]
    files: tuple[Path, ...]
    failed_sids: frozenset[str]
    skipped: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    unreadable: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    truncated: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    resume: Mapping[str, float] = field(default_factory=dict)


def next_since(snap: NodeSnapshot, sid: str, since: float) -> float:
    """The watermark to ask for ``sid`` with next, after ``snap`` answered a
    request made with ``since`` -- the ONE watermark rule, which ``pull`` and
    the sync daemon share:
    - a session with a file that could not be stored holds ``since``;
    - a complete one moves to the node's clock minus ``WATERMARK_OVERLAP_S``;
    - a truncated one moves to just under its ``resume`` mtime, so that file
      and every newer one are asked for again -- but never past the complete
      rule's value, so a file rewritten after the scan began is never lost.
      Without a usable ``resume`` it holds ``since``;
    - any of them whose node clock, less the overlap, is now BEHIND ``since``
      starts over from 0.0. A node clock that jumped forward and came back
      left ``since`` in its future, and a file stamped before it would never
      be asked for again.
    ``skipped`` and ``unreadable`` files never hold it: they would on every
    tick."""
    if sid in snap.failed_sids:
        return since
    after_scan = snap.now - WATERMARK_OVERLAP_S
    if after_scan < since:
        return 0.0
    if sid not in snap.truncated:
        return after_scan
    stop = snap.resume.get(sid)
    if stop is None:
        return since
    # nextafter: pull.sh wants `mtime > since`, and the file AT `stop` is owed.
    return max(since, min(math.nextafter(stop, -math.inf), after_scan))


class NotAPull(RemoteError):
    """rc 0: the node answered, and the answer was not a pull this PC can read
    -- another framing version, a truncated or damaged reply, an archive over
    its cap -- so NOTHING of it was stored. Its own type so a caller can tell
    it from a ``PullRefused`` (rc 0 as well): a recall must stop on this one,
    because the node still holds whatever it could not send (cq-G14 C-R3-1).
    Every other caller sees the same RemoteError(0) as before."""

    def __init__(self, message: str) -> None:
        super().__init__(0, message, ("pull.sh",))


class PullRefused(RemoteError):
    """rc 0: a pull refused on this PC before any ssh -- a session name this PC
    cannot store, an empty remote root. Nothing was asked of the node, and no
    re-run changes the answer."""

    def __init__(self, message: str, command_redacted: tuple[str, ...]) -> None:
        super().__init__(0, message, command_redacted)


def _pull_error(message: str) -> NotAPull:
    return NotAPull(message)


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


def _report_dict(raw: object) -> dict[str, tuple[str, ...]]:
    """pull.sh's ``skipped`` and ``unreadable`` maps: ``{sid: [archive name,
    ...]}``. Only its strings are kept; anything else there is ignored, never
    an error -- the names are the node's report, read to be logged, never a
    path to open."""
    if not isinstance(raw, dict):
        return {}
    kept = {
        k: tuple(n for n in v if isinstance(n, str))
        for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, list)
    }
    return {k: names for k, names in kept.items() if names}


def _clock_dict(raw: object) -> dict[str, float]:
    """pull.sh's ``resume`` map: ``{sid: mtime}``. Only finite numbers are
    kept -- json.loads accepts NaN, Infinity and a 309-digit int -- and a
    session without a usable one holds its watermark (``next_since``)."""
    if not isinstance(raw, dict):
        return {}
    kept: dict[str, float] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        try:
            kept[k] = _finite(v)
        except (TypeError, ValueError, OverflowError):
            continue
    return kept


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
    """Store one pulled file whole (sibling ``.part`` + fsync + ``os.replace``)
    with the node's mtime when it has a usable one, so a reader never sees half
    a transcript. Streamed in ``PULL_COPY_CHUNK_BYTES`` chunks, never whole.

    The temp name is ``mkstemp``'s short ``.<random>.part``, never derived from
    the target's name: a name-derived temp outgrows NAME_MAX on a node filename
    the target itself fits in, and that failure would hold the sid's watermark
    on every tick. The ``.part`` suffix stays (the tar walker skips it); the
    temp is unlinked on any failure, so none is ever left behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, part = tempfile.mkstemp(
        dir=path.parent, prefix=PULL_TEMP_PREFIX, suffix=PULL_TEMP_SUFFIX
    )
    try:
        with open(fd, "wb") as out:
            shutil.copyfileobj(reader, out, length=PULL_COPY_CHUNK_BYTES)
            out.flush()
            os.fsync(out.fileno())
        if mtime is not None:
            os.utime(part, (mtime, mtime))
        os.replace(part, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(part)
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
        # The class only: `node sync --once` prints this, and an OSError here
        # can name a path on this PC. Its words are nodes.log's.
        log.warning("node pull: unreadable pull archive: %s", e)
        raise _pull_error(f"unreadable pull archive ({type(e).__name__})") from e
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
    warning. NotAPull (a RemoteError, rc 0) when the reply is not a pull at all -- no
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
    # RecursionError: json.loads' answer to deep nesting (200k '[' fit well
    # inside the reply cap). The node's bad answer, not a bug on this PC.
    except (ValueError, RecursionError) as e:
        # By class, as the archive's error is; the parser's words are logged.
        get_logger("nodes").warning("node pull: unreadable pull metadata: %s", e)
        raise _pull_error(f"unreadable pull metadata ({type(e).__name__})") from e
    if not isinstance(meta, dict):
        raise _pull_error("pull metadata is not an object")
    # json.loads accepts NaN and Infinity (a NaN watermark, which
    # write_json_atomic refuses) and ints of any length (float() of a 309-digit
    # one raises OverflowError). None of them is a clock.
    try:
        now = _finite(meta.get("now"))
    except (TypeError, ValueError, OverflowError) as e:
        raise _pull_error("pull metadata has no clock") from e
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
            k: v for k, v in _report_dict(meta.get("skipped")).items() if k in wanted
        },
        unreadable={
            k: v for k, v in _report_dict(meta.get("unreadable")).items() if k in wanted
        },
        truncated={
            k: v for k, v in _report_dict(meta.get("truncated")).items() if k in wanted
        },
        resume={
            k: v for k, v in _clock_dict(meta.get("resume")).items() if k in wanted
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
        # The node stops adding files before its whole reply passes this and
        # names the rest under `truncated`, so a big backlog arrives over
        # several ticks instead of failing every one (NodeSnapshot.resume).
        "max_total_bytes": PULL_MAX_TOTAL_BYTES,
    }
    return _script_call(
        "pull", [], json.dumps(payload).encode("utf-8")
    )  # `bash -s -- <SOCKET>`: the socket is always $1


def refused_pull(node: Node, sids: Mapping[str, SidPull], message: str) -> PullRefused:
    """A pull of ``sids`` refused on this PC before any ssh: rc 0, and the
    command it would have run, shown the way every node error shows one
    (``_run_shown``)."""
    argv, input_bytes = _pull_call(sids)
    return PullRefused(message, _run_shown(node, argv, input_bytes))


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
    RemoteError on a transport failure (255), a timeout (None), a reply over
    ``PULL_MAX_REPLY_BYTES`` (None too -- pull.sh keeps its
    own reply under ``PULL_MAX_TOTAL_BYTES``, so this means a node that did
    not), a node without python3 (3), or a reply that is not a pull (NotAPull, 0).
    ValueError, after the ssh, when ``sids`` names a session that is not
    ``pullable_sid`` (``parse_pull`` refuses it: the caller's bug, not the
    node's)."""
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

    Stateless, so two calls whenever the node reports the session's real
    path: the first asks for its state records alone and learns that path,
    the second asks again with ``~/.claude/projects/<encoded real path>`` --
    the PC encodes, never the node (DECISION-11f). One call when it reports
    none. Up to 2 x ``timeout_s`` in all; a RemoteError from either call
    propagates. The watermark holds when either call failed to store a file;
    otherwise it is ``next_since`` of the second call, which asked for
    everything -- so a reply truncated at ``PULL_MAX_TOTAL_BYTES`` resumes
    where it stopped. A file the node skipped as over the cap or could not
    read does not hold it (``NodeSnapshot.skipped``/``unreadable``, which
    ``PullResult`` does not carry)."""
    roots = tuple(remote_dirs)
    # One spec for the refusal and the first call: a refusal names exactly
    # the pull it would have sent.
    first_spec = SidPull(roots=roots, project_dir=None, since=since_epoch)
    # Before any ssh: parse_pull refuses the same name with ValueError, which
    # would mean THIS caller's bug, not the node's.
    if not pullable_sid(sid):
        raise refused_pull(
            node, {sid: first_spec}, f"not a pullable session name: {sid!r}"
        )
    first = pull_node(node, {sid: first_spec}, timeout_s=timeout_s)
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
    since = (
        since_epoch if sid in first.failed_sids else next_since(snap, sid, since_epoch)
    )
    # Both calls ship the session's state records: each path is listed once.
    files = tuple(dict.fromkeys(first.files + snap.files))
    return PullResult(files=files, since=since)


# A local git read is a local process, but it can still hang (a credential
# prompt, a network filesystem); bounded like every other child.
GIT_TIMEOUT_S = 30.0


def _local_git(
    path: Path, args: Sequence[str], *, timeout_s: float, check: bool, label: str
) -> subprocess.CompletedProcess[bytes]:
    """One bounded, READ-ONLY ``git -C <path> --no-optional-locks <args>`` --
    the shared body of ``ignored_paths`` and every ``git_state`` read. Three
    things make it safe to aim at a repo an agent is working in right now:

    - ``--no-optional-locks`` (a global option, so before the subcommand):
      ``git status`` otherwise refreshes the stat cache under ``index.lock``
      and rewrites ``.git/index`` (measured), and the agent's own git call
      then fails "index.lock: File exists";
    - ``env.git_child_env()``: an inherited GIT_DIR / GIT_WORK_TREE -- a git
      hook exports them, absolute in a worktree -- would answer for another
      repo than the one ``-C`` names;
    - a missing ``git`` is RemoteError rc None ("git not found on PATH"):
      ``_spawn`` alone would call it the missing ssh client (rc 127)."""
    argv = ["git", "-C", str(path), "--no-optional-locks", *args]
    shown = tuple(argv)
    try:
        return _spawn(
            argv,
            timeout_s=timeout_s,
            input_bytes=None,
            check=check,
            shown=shown,
            label=label,
            env=git_child_env(),
        )
    except RemoteError as e:
        if isinstance(e.__cause__, FileNotFoundError):
            raise RemoteError(None, "git not found on PATH", shown) from e.__cause__
        raise


def ignored_paths(repo: Path, *, timeout_s: float, label: str) -> tuple[str, ...]:
    """What git ignores in the LOCAL ``repo``: ``git ls-files --others --ignored
    --exclude-standard --directory -z`` -- repo-relative, '/'-separated, and a
    wholly ignored directory as ONE ``dir/`` entry (``node_modules`` is one
    line, not a hundred thousand). Read-only (``_local_git``). The raw material
    for ``nodes.push_set``; ``git_state`` carries it as
    ``LocalGitState.ignored``. ``label`` names the caller in the log line a
    failure writes.

    A ``repo`` that is not a git repository is ``RemoteError(rc=128, <git's
    stderr tail>)`` -- git's own "fatal: not a git repository" exit. A missing
    ``git`` is RemoteError rc None ("git not found on PATH"): the command never
    ran."""
    result = _local_git(
        repo,
        [
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
            "-z",
        ],
        timeout_s=timeout_s,
        check=True,
        label=label,
    )
    return tuple(p for p in result.stdout.decode("utf-8", "replace").split("\0") if p)


def _git(
    path: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    """One ``git_state`` read (``_local_git``), bounded by ``GIT_TIMEOUT_S``."""
    return _local_git(
        path, args, timeout_s=GIT_TIMEOUT_S, check=check, label="git state read"
    )


def _out(result: subprocess.CompletedProcess[bytes]) -> str:
    return result.stdout.decode("utf-8", "replace").strip()


def repo_paths(project_dir: Path) -> list[Path]:
    """The git repos a node project is made of: the project itself when it is
    a repo, else each DIRECT child that is one (a workspace of repos), in name
    order. Empty when there is none -- which the caller refuses, because a
    node clones the project from its origin. A folder that cannot be read (a
    permission, a vanished network drive) is RemoteError rc None naming it,
    like every other failure this module reports."""
    try:
        # nodes.path_*, not Path.*: from Python 3.14 those read an unreadable
        # entry as absent -- a workspace would ship without that repo.
        if path_exists(project_dir / ".git"):
            return [project_dir]
        if not path_is_dir(project_dir):
            return []
        return sorted(
            child
            for child in project_dir.iterdir()
            if path_is_dir(child) and path_exists(child / ".git")
        )
    except OSError as e:
        # The folder is the user's own configured project, so it is named;
        # strerror is the OS's words (localized, per platform), not ours, so
        # only the class is. The whole error is logged.
        reason = f"cannot read {project_dir} ({type(e).__name__})"
        get_logger("nodes").warning(
            "repo lookup failed: cannot read %s: %s", project_dir, e
        )
        raise RemoteError(None, reason, ("repo_paths", str(project_dir))) from e


def git_state(path: Path) -> LocalGitState:
    """What D7 needs to know about the LOCAL repo at ``path``, read-only: every
    read goes through ``_local_git`` (no optional lock, no inherited GIT_DIR).

    - ``url`` is origin's, "" when there is no origin.
    - ``detached`` when HEAD names no branch (``branch`` is then "").
    - ``no_commits`` when HEAD is unborn: a repo with no commits yet names its
      branch (not detached) but has nothing to push -- D7 says "make a first
      commit" instead of a ``git push`` that would fail.
    - ``dirty`` counts untracked files too -- origin never saw them, so the
      node would not have them -- whatever the user's
      ``status.showUntrackedFiles`` or submodule-ignore config says.
    - ``unpushed`` is "HEAD has commits origin's copy of this branch lacks";
      a branch origin has never seen counts, and so does an unborn HEAD (never
      a pass). The comparison is against ``refs/remotes/origin/<branch>``, not
      ``@{u}``: the node fetches by branch NAME from origin, whatever this
      branch tracks. It is only as fresh as the last fetch here -- the
      read-only rule forbids a fetch or an ``ls-remote``.

    Raises RemoteError when ``path`` is not a repo or git itself fails."""
    _git(path, "rev-parse", "--git-dir")
    origin = _git(path, "remote", "get-url", "origin", check=False)
    url = _out(origin) if origin.returncode == 0 else ""
    head = _git(path, "symbolic-ref", "-q", "--short", "HEAD", check=False)
    detached = head.returncode != 0
    branch = "" if detached else _out(head)
    verify = _git(path, "rev-parse", "-q", "--verify", "HEAD", check=False)
    no_commits = verify.returncode != 0
    status = _git(
        path,
        "status",
        "--porcelain",
        "--untracked-files=normal",
        "--ignore-submodules=none",
    )
    dirty = bool(_out(status))
    unpushed = False
    if url and not detached:
        if no_commits:
            unpushed = True
        else:
            ahead = _git(
                path,
                "rev-list",
                "--count",
                f"refs/remotes/origin/{branch}..HEAD",
                check=False,
            )
            unpushed = ahead.returncode != 0 or _out(ahead) != "0"
    return LocalGitState(
        path=path,
        url=url,
        branch=branch,
        dirty=dirty,
        unpushed=unpushed,
        detached=detached,
        ignored=ignored_paths(path, timeout_s=GIT_TIMEOUT_S, label="git state read"),
        no_commits=no_commits,
    )


@dataclass(frozen=True)
class BringUpResult:
    """What ``bring_up.sh`` reported. ``cwd`` is the node's ABSOLUTE project
    folder (``Recipe.remote_root`` keeps ``~``); ``commits`` maps each repo's
    absolute folder to the commit it now has checked out; ``shipped`` lists the
    project-relative files written beside the clone. ``dirty`` maps a repo
    folder to whether ``git status --porcelain`` listed anything once the
    files were shipped -- untracked files included, ``repo_status.sh``'s
    rule, though only tracked changes refuse a bring-up. A folder missing
    from it was not read (an attach, ``--allow-dirty``, a status that
    failed): unknown, never clean."""

    sid: str
    attached_existing: bool
    commits: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    shipped: tuple[str, ...] = ()
    dirty: dict[str, bool] = field(default_factory=dict)


# bring_up.sh's header format version; the script refuses anything else.
_HEADER_MAGIC = "MAGENT1"


def _has_control(text: str) -> bool:
    """Any control character (C0, DEL, C1): a newline, a tab, an ESC."""
    return any(unicodedata.category(ch) == "Cc" for ch in text)


def _clean_absolute(value: str) -> bool:
    """``value`` is a node path magent can hand on: absolute (so it cannot
    start with ``-``) and free of any control character."""
    return value.startswith("/") and not _has_control(value)


def _node_path(path: str, home: str) -> str:
    """``path`` expanded against the node's ``home`` and checked where it
    first enters a remote command: an absolute POSIX path, so it can neither
    be read relative to wherever the script happens to run nor start with
    ``-`` and be taken for an option. The EXPANDED value is what is checked
    -- the default root ``~/magent`` is only absolute after expansion.
    NodeConfigError (a ValueError): the path comes from
    ``settings.nodes.<n>.root``."""
    expanded = absolute_remote(path, home)
    if not expanded.startswith("/"):
        raise NodeConfigError(
            f"node folder {expanded!r} is not an absolute path on the node"
        )
    return expanded


def _login_argv(cmd: str) -> list[str]:
    """``cmd`` run by the node user's LOGIN bash (so ~/.profile's PATH, where
    ``claude`` is usually installed, applies) and exec'd, so the agent IS the
    pane's process. Empty for no command."""
    return ["bash", "-lc", f"exec {cmd}"] if cmd else []


def _start_argvs(recipe: Recipe, resume_id: str | None) -> tuple[list[str], list[str]]:
    """``(start, fresh)``: the argv to run, and the one to run instead when the
    node has no transcript for this folder. An explicit resume has no fresh
    alternative -- the user named a session."""
    if resume_id:
        resume = build_resume_command(recipe.tool, recipe.command, resume_id)
        return _login_argv(resume), []
    return _login_argv(recipe.command), _login_argv(recipe.fresh_command or "")


def _header(
    recipe: Recipe, *, allow_dirty: bool, home: str, resume_id: str | None
) -> bytes:
    """The payload's ``header`` member: NUL-terminated tokens, read by
    bring_up.sh with ``read -d ''``. A NUL is the one byte a token cannot
    carry, so one is refused rather than silently splitting a token."""
    start, fresh = _start_argvs(recipe, resume_id)
    tokens = [_HEADER_MAGIC, "1" if allow_dirty else "0", str(len(recipe.repos))]
    for repo in recipe.repos:
        # git on the node would take `--upload-pack=...` as an option. The
        # script also passes `--` before them; this is the PC-side half.
        for value, what in ((repo.url, "url"), (repo.branch, "branch")):
            if value.startswith("-"):
                raise ValueError(f"repo {what} {value!r} would read as an option")
        tokens += [repo.url, repo.branch, _node_path(repo.remote_dir, home)]
    tokens += [str(len(start)), *start, str(len(fresh)), *fresh]
    if any("\0" in token for token in tokens):
        raise ValueError("a bring-up header token contains a NUL byte")
    return b"".join(token.encode("utf-8") + b"\0" for token in tokens)


def _archive_name(rel: str) -> str:
    """``rel`` as a payload member name: ``/``-separated whatever the local
    separator, so a Windows ``config\\.env`` never reaches the node as one
    file name with a literal backslash in it. Mapping ``\\`` is also what
    makes a POSIX file literally named ``a\\..\\..\\x`` climb, so the mapped
    name is refused (ValueError) when it is absolute or has an empty, ``.``
    or ``..`` segment. A control character is refused too: the node's shell
    strips a trailing newline in ``$(...)``, so ``.env\\n`` would resolve as
    ``.env`` (the node refuses it as well)."""
    name = rel.replace("\\", "/")
    if any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError(f"{rel!r} cannot name a file inside the project")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        raise ValueError(f"{rel!r} has a control character in its name")
    return name


def _push_name(path: Path, local_root: Path) -> tuple[str, Path]:
    """``(member name, real path)`` of push file ``path``. The name is its
    place relative to ``local_root``, lexically -- a link keeps the name it
    has here. The real path is what ``_read_regular`` must open: the file
    that was vetted, not whatever ``path`` names by the time it is read.
    ValueError when ``path`` RESOLVES outside ``local_root``: B's config check
    on the entry is a string check, and a symlink inside the project can
    point at ``~/.ssh``.

    Two refusals here are belt-and-braces, and a mutant that drops either
    survives (equivalent): ``real == real_root`` (the root is a folder, which
    ``_read_regular`` refuses anyway) and the lexical ``relative_to``'s own
    message (it raises ValueError either way)."""
    real, real_root = Path(os.path.realpath(path)), Path(os.path.realpath(local_root))
    if real == real_root or not real.is_relative_to(real_root):
        raise ValueError(f"push file {path} resolves outside the project {local_root}")
    try:
        rel = path.relative_to(local_root)
    except ValueError as e:
        raise ValueError(f"push file {path} is outside the project {local_root}") from e
    return _archive_name(str(rel)), real


# The size bounds of what one bring-up ships (the node reads the payload from
# stdin into tar; nothing here is streamed). A push file over its cap, or a
# push set over the payload's, is refused before any ssh. Memory is skipped,
# never refused. PAYLOAD_MAX_BYTES bounds the shipped FILE bytes; the tar's
# own framing and the header ride on top.
PUSH_FILE_MAX_BYTES = 16 * 1024 * 1024
PAYLOAD_MAX_BYTES = 64 * 1024 * 1024


def _read_regular(path: Path, *, cap: int, what: str) -> bytes:
    """The bytes of ``path``, which must be a REGULAR file of at most ``cap``
    bytes, or ValueError naming ``what``. Checked three times, because each
    check alone has a hole: ``lstat`` before opening (a FIFO or a device is
    never opened, an oversize file never read), ``fstat`` on what was opened
    (the path may have been swapped in between), and a read of at most
    ``cap + 1`` bytes (the file may have grown). ``fstat`` must also name the
    SAME file the ``lstat`` sized -- ``(st_dev, st_ino)`` -- or a regular file
    swapped in under the name would be read on the old one's vetting. OSError
    when it cannot be opened -- ``O_NOFOLLOW`` makes a final-component link
    swapped in after the ``lstat`` one of those."""
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{what} is not a regular file")
    if before.st_size > cap:
        raise ValueError(f"{what} is {before.st_size} bytes; the cap is {cap}")
    fd = os.open(path, READ_FLAGS)
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"{what} is not a regular file")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError(f"{what} changed between its check and its open")
        data = handle.read(cap + 1)
    if len(data) > cap:
        raise ValueError(f"{what} grew past the cap of {cap} bytes")
    return data


def _files(recipe: Recipe, *, memory: bool) -> list[tuple[str, bytes]]:
    """The payload's file members, vetted and READ -- before any ssh, so every
    refusal costs no connection. ``project/<rel>``: the push set, each file
    contained in the project and bounded (ValueError otherwise, naming it);
    ``memory/<rel>`` for a bring-up: Claude's memory, where anything that
    fails a check is skipped and logged instead (a bring-up never fails
    because of memory)."""
    if recipe.push_files and recipe.local_root is None:
        raise ValueError("a recipe with push files needs its local_root")
    root = recipe.local_root
    vetted = (
        [(*_push_name(p, root), p) for p in recipe.push_files]
        if root is not None
        else []
    )
    out: list[tuple[str, bytes]] = []
    total = 0
    for name, real, path in vetted:
        data = _read_regular(real, cap=PUSH_FILE_MAX_BYTES, what=f"push file {path}")
        total += len(data)
        if total > PAYLOAD_MAX_BYTES:
            raise ValueError(
                f"the push set passes the payload cap of {PAYLOAD_MAX_BYTES} "
                f"bytes at push file {path}"
            )
        out.append((f"project/{name}", data))
    if memory and recipe.memory_dir is not None:
        logger = get_logger("nodes")
        for rel, path in _memory_files(recipe.memory_dir):
            cap = min(PUSH_FILE_MAX_BYTES, PAYLOAD_MAX_BYTES - total)
            try:
                data = _read_regular(path, cap=cap, what=f"memory file {path}")
            except (ValueError, OSError) as e:
                logger.warning("memory file %s skipped: %s", path, e)
                continue
            total += len(data)
            out.append((f"memory/{rel}", data))
    return out


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o600
    info.mtime = 0
    tar.addfile(info, io.BytesIO(data))


def _payload(*, header: bytes, decorate: str, files: list[tuple[str, bytes]]) -> bytes:
    """ONE uncompressed PAX tar: ``header``, ``decorate``, then ``files``
    (``_files``' members, in its order). Bytes stay bytes -- a secret file is
    never re-encoded."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        _add_bytes(tar, "header", header)
        _add_bytes(tar, "decorate", decorate.encode("utf-8"))
        for name, data in files:
            _add_bytes(tar, name, data)
    return buf.getvalue()


def _memory_files(memory_dir: Path) -> list[tuple[str, Path]]:
    """``(member name, path)`` for every file ``nodes.walk_memory`` lets ship
    from ``memory_dir``, in name order. That walk is the recipe's too, so its
    rules (never a link, every file inside the resolved folder, every skip
    logged and never raised) are one rule for both. A file that cannot be
    named on the node is skipped and logged as well."""
    logger = get_logger("nodes")
    found: list[tuple[str, Path]] = []
    for path in walk_memory(memory_dir):
        try:
            rel = _archive_name(str(path.relative_to(memory_dir)))
        except ValueError:
            logger.warning("memory file %s cannot be named on the node", path)
            continue
        found.append((rel, path))
    return sorted(found)


def _remote_home(node: Node) -> str:
    """The node user's ``$HOME``. The PC expands ``~`` itself and computes the
    Claude project name from the absolute path, so it must be absolute."""
    probe = ["printenv", "HOME"]
    result = run(node, probe, timeout_s=PROBE_TIMEOUT_S)
    # printenv's one trailing newline is framing; anything else is the
    # answer. A second line (a login banner) or any other control character
    # is refused, never trimmed away: the value becomes argv and a path.
    home = result.stdout.decode("utf-8", "replace").removesuffix("\n")
    if not home.startswith("/") or _has_control(home):
        raise RemoteError(
            result.returncode,
            f"unusable $HOME on the node: {home!r}",
            _run_shown(node, probe, None),
        )
    return home


def _parse_result(
    result: subprocess.CompletedProcess[bytes],
) -> dict[str, object] | None:
    """bring_up.sh's last non-empty stdout line as a JSON object, or None
    when it is not one -- which the caller raises: a result that cannot be
    read is not a success."""
    text = result.stdout.decode("utf-8", "replace")
    lines = [line for line in text.splitlines() if line.strip()]
    try:
        parsed = json.loads(lines[-1]) if lines else None
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _deliver(
    node: Node, mode: str, recipe: Recipe, root: str, payload: bytes
) -> dict[str, object]:
    args = [mode, recipe.sid, root, encoded_project_dir(root)]
    result = run_script(
        node, "bring_up", args, timeout_s=BRING_UP_TIMEOUT_S, stdin=payload
    )
    parsed = _parse_result(result)
    if parsed is None:
        # What RAN, as run() itself would name it (the ``sample`` precedent).
        # Built on this path only: the frame is a copy of the whole payload.
        shown = _run_shown(node, *_script_call("bring_up", args, payload))
        raise RemoteError(result.returncode, "not a bring-up result", shown)
    return parsed


def bring_up(
    node: Node,
    recipe: Recipe,
    *,
    allow_dirty: bool = False,
    resume_id: str | None = None,
) -> BringUpResult:
    """Bring ``recipe`` up on ``node`` in one script run (after a ``$HOME``
    probe): clone or fast-forward every repo, ship the push set and seed the
    memory, then start the agent in tmux session ``recipe.sid`` -- or, when it
    is already running there, attach to it and touch nothing but its
    decoration. Raises RemoteError (bring_up.sh exit codes: 2 bad input, 3 a
    dirty node tree without ``allow_dirty``, 4 tmux, 5 git or a write);
    ValueError for a recipe that cannot be framed (NodeConfigError for a node
    folder that is not absolute); OSError for a push file that cannot be
    read. Every push-file refusal lands before any ssh."""
    files = _files(recipe, memory=True)
    home = _remote_home(node)
    root = _node_path(recipe.remote_root, home)
    header = _header(recipe, allow_dirty=allow_dirty, home=home, resume_id=resume_id)
    decorate_text = decoration_script(recipe.sid, node.nick, psmux.code_on_path())
    payload = _payload(header=header, decorate=decorate_text, files=files)
    raw = _deliver(node, "up", recipe, root, payload)
    commits, cwd, shipped = raw.get("commits"), raw.get("cwd"), raw.get("shipped")
    dirty = raw.get("dirty")
    return BringUpResult(
        sid=recipe.sid,
        attached_existing=raw.get("attached_existing") is True,
        commits=(
            {str(k): str(v) for k, v in commits.items()}
            if isinstance(commits, dict)
            else {}
        ),
        cwd=cwd if isinstance(cwd, str) and _clean_absolute(cwd) else root,
        shipped=tuple(str(s) for s in shipped) if isinstance(shipped, list) else (),
        dirty=(
            {str(k): v for k, v in dirty.items() if isinstance(v, bool)}
            if isinstance(dirty, dict)
            else {}
        ),
    )


def push_files(node: Node, recipe: Recipe) -> list[str]:
    """Re-ship ``recipe``'s push set into its EXISTING folder on ``node`` (no
    git, no session, no memory). Returns the project-relative paths written.
    Raises RemoteError -- exit 5 when the folder is not there yet -- and the
    same ValueError/OSError refusals as ``bring_up``."""
    files = _files(recipe, memory=False)
    home = _remote_home(node)
    root = _node_path(recipe.remote_root, home)
    header = _header(recipe, allow_dirty=True, home=home, resume_id=None)
    payload = _payload(header=header, decorate="", files=files)
    raw = _deliver(node, "push", recipe, root, payload)
    shipped = raw.get("shipped")
    return [str(s) for s in shipped] if isinstance(shipped, list) else []


def _session_root(remote_root: str) -> str:
    """``remote_root`` as it may be sent to a node script: ``~``, ``~/...`` or
    absolute. G sends the root UNEXPANDED (the script expands ``~``), so D's
    check on its own expanded value never sees it (plan G Task 9's forward
    correction). Anything else -- a relative path, a leading ``-`` read as an
    option, another user's ``~user`` -- raises NodeConfigError before a
    connection is opened. So does a ``..`` segment (it walks out of the root
    it names) and any C0/DEL/C1 control character (a newline or TAB splits
    the node's report rows; ESC drives the terminal the root is echoed on).
    A trailing ``/`` is fine."""
    if not (remote_root == "~" or remote_root.startswith(("~/", "/"))):
        raise NodeConfigError(
            f"session root {remote_root!r} is not ~, ~/... or an absolute path "
            "on the node"
        )
    parts = remote_root.split("/")
    # Per component, not a substring: "~/a..b" is a fine directory name.
    if ".." in parts:
        raise NodeConfigError(f"session root {remote_root!r} has a '..' segment")
    if remote_root.startswith("/") and all(p in ("", ".") for p in parts):
        raise NodeConfigError(
            f"session root {remote_root!r} is the node's whole filesystem"
        )
    if any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in remote_root):
        raise NodeConfigError(f"session root {remote_root!r} holds a control character")
    return remote_root


def _stdout_text(done: subprocess.CompletedProcess[bytes]) -> str:
    return done.stdout.decode("utf-8", "replace")


def repo_status(node: Node, remote_root: str, *, timeout_s: float) -> list[RepoStatus]:
    """Each repo under a node session's cwd, as it is right now (read-only,
    ``repo_status.sh``). RemoteError when the node does not answer;
    NodeConfigError, before any dial, for a root ``_session_root`` refuses."""
    done = run_script(
        node, "repo_status", [_session_root(remote_root)], timeout_s=timeout_s
    )
    return parse_repo_status(_stdout_text(done))


# A recall ships a whole conversation directory; the spec's script default.
INSTALL_TIMEOUT_S = 120.0


def _raise(err: OSError) -> None:
    raise err


def _pull_temp(name: str) -> bool:
    """The pull writer's in-flight temp: mkstemp with ``PULL_TEMP_PREFIX`` and
    ``PULL_TEMP_SUFFIX`` beside its target (E8), stranded only by a kill
    mid-write. That exact shape, case included (mkstemp never writes upper
    case), and nothing wider: a real ``notes.part`` is the user's file."""
    return name.startswith(PULL_TEMP_PREFIX) and name.endswith(PULL_TEMP_SUFFIX)


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(a) == os.path.normcase(b)


def _is_its_own_place(path: str, real_parent: str) -> bool:
    """Does ``path`` physically sit where its name says -- inside
    ``real_parent`` (already resolved) -- rather than being a symlink or a
    Windows junction to somewhere else? A junction (``mklink /J``, no admin
    needed) is not ``is_symlink()`` and ``os.walk`` descends it, but
    ``realpath`` resolves it; that is the only test that sees both."""
    return _same_path(
        os.path.realpath(path), os.path.join(real_parent, os.path.basename(path))
    )


def _within(path: str, real_root: str) -> bool:
    """Is ``path``'s real location ``real_root`` (resolved, normcased) or
    under it? A different drive is never under it."""
    real = os.path.normcase(os.path.realpath(path))
    try:
        return os.path.commonpath([real, real_root]) == real_root
    except ValueError:
        return False


class MirrorIsALink(OSError):
    """A pulled transcripts dir that is itself a symlink or junction to
    somewhere else: nothing is taken from it."""


def _mirror_members(source: Path, *, who: str) -> list[Path]:
    """What may leave the pulled transcripts dir ``source``, sorted: its
    regular files and directories -- the ONE rule for both ways a mirror
    leaves it (``_tar_dir`` to a node, ``copy_mirror`` into this PC's Claude
    dir). A symlink could name anything on this PC, and a ``.<rand>.part``
    file is a pull temp (``_pull_temp``). Nothing reached through a link is a
    member either: ``source`` itself being a symlink or junction raises
    MirrorIsALink, a linked directory inside it is not descended (logged), and
    a file whose real path leaves ``source`` is skipped -- two independent
    layers. OSError when ``source`` cannot be read."""
    log = get_logger("nodes")
    if not _is_its_own_place(str(source), os.path.realpath(source.parent)):
        log.warning("%s: %s is a link (symlink or junction); refused", who, source)
        raise MirrorIsALink(
            f"the pulled transcripts dir {source} is a link (symlink or junction)"
            " to somewhere else"
        )
    root = os.path.normcase(os.path.realpath(source))
    entries = []
    for dirpath, dirnames, filenames in os.walk(source, onerror=_raise):
        real_base = os.path.realpath(dirpath)
        inside = []
        for name in dirnames:
            if _is_its_own_place(os.path.join(dirpath, name), real_base):
                inside.append(name)
            else:
                log.warning(
                    "%s: %s is a link (symlink or junction); not descended",
                    who,
                    os.path.join(dirpath, name),
                )
        dirnames[:] = inside
        base = source / os.path.relpath(dirpath, source)
        entries.extend(base / name for name in (*dirnames, *filenames))
    return [
        path
        for path in sorted(entries)
        if not path.is_symlink()
        and (
            path.is_dir()
            or (
                path.is_file()
                and not _pull_temp(path.name)
                and _within(str(path), root)
            )
        )
    ]


def _owner_only(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """``_tar_dir``'s filter: every member owner-only (dirs 0700, files
    0600) and owned by no one in particular, as ``_add_bytes``' members
    are. The node's tar opens each member with its ARCHIVE mode, and a
    default ACL there makes the kernel ignore the umask (audit-acl S2),
    so this is the mode that holds -- and the PC's own would depend on
    its OS: a Windows stat is 0666/0777. The mtime still travels: the
    newest transcript is found by it."""
    info.mode = 0o700 if info.isdir() else 0o600
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _tar_dir(source: Path) -> bytes:
    """An uncompressed tar of ``source``'s CONTENTS (paths relative to it):
    its ``_mirror_members``, nothing else, every one ``_owner_only``. A
    source that cannot be read, or is a link, raises RemoteError with rc
    None (nothing ran on a node)."""
    buf = io.BytesIO()
    try:
        members = _mirror_members(source, who="_tar_dir")
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for path in members:
                arcname = path.relative_to(source).as_posix()
                tar.add(path, arcname=arcname, recursive=False, filter=_owner_only)
    except MirrorIsALink as err:
        raise RemoteError(
            None, f"{err}; nothing was sent", ("tar", str(source))
        ) from err
    except OSError as err:
        # The OS's words (and the file's path) go to nodes.log; the error a
        # recall shows is ours plus the class.
        get_logger("nodes").warning(
            "_tar_dir: could not read the pulled transcripts in %s: %s", source, err
        )
        raise RemoteError(
            None,
            f"could not read the pulled transcripts ({type(err).__name__})",
            ("tar", str(source)),
        ) from err
    return buf.getvalue()


def copy_mirror(source: Path, dest: Path) -> tuple[str, ...]:
    """Install the pulled transcripts dir ``source`` into ``dest`` on THIS PC
    by the rule ``_tar_dir`` sends it to a node by (``_mirror_members``): the
    ``recall --local`` twin of ``install_transcripts``. What ``dest`` already
    holds is kept; a same-name file is replaced by the node's copy, and the
    names (relative, '/'-separated, sorted) of those whose content DIFFERED
    are returned for the caller to report. MirrorIsALink when ``source`` is
    itself a link; any other OSError propagates, possibly after a partial copy
    that a re-run overwrites."""
    members = set(_mirror_members(source, who="copy_mirror"))
    replaced: list[str] = []

    def _not_members(folder: str, names: list[str]) -> list[str]:
        return [name for name in names if Path(folder, name) not in members]

    def _copy(src: str, dst: str) -> object:
        if os.path.isfile(dst) and not filecmp.cmp(src, dst, shallow=False):
            replaced.append(Path(dst).relative_to(dest).as_posix())
        return shutil.copy2(src, dst)

    shutil.copytree(
        source, dest, ignore=_not_members, copy_function=_copy, dirs_exist_ok=True
    )
    return tuple(sorted(replaced))


def node_realpath(node: Node, path: str, *, timeout_s: float) -> str:
    """The physical path ``path`` names on ``node`` (``~`` expanded, symlinks
    resolved) -- the string Claude Code there keys its project dir by.
    NodeConfigError, before any dial, for a path ``_session_root`` refuses."""
    done = run_script(node, "node_realpath", [_session_root(path)], timeout_s=timeout_s)
    return _stdout_text(done).strip()


@dataclass(frozen=True)
class InstalledTranscripts:
    """Where a recall's conversation landed on a node, and the items (files,
    or a directory where the node has a file) the node already had its own
    newer or diverged copy of -- those were KEPT, not overwritten
    (``install_transcripts.sh``). Informational, not a failure."""

    landed: str
    kept: tuple[str, ...] = ()

    @property
    def note(self) -> str:
        """One line naming what was kept, or "" when nothing was. An item, not
        a file: a directory is KEPT too when the node has a file of that name
        where the payload has a directory."""
        if not self.kept:
            return ""
        return (
            f"kept the node's newer/diverged copy of {len(self.kept)} item(s): "
            + ", ".join(self.kept)
        )


# install_transcripts.sh's own refusals, by exit code. Any other failure (ssh's
# 255, a timeout, a command that died under `set -e`) passes through as is.
INSTALL_REFUSALS = {
    2: "the encoded project dir name is outside the encoder's alphabet",
    3: "the transcript payload arrived missing or broken; nothing was installed",
    4: "the node's project dir is a symlink; nothing was installed through it",
    5: (
        "the node could not make a folder the conversation goes in, so no file"
        " was installed; check that the node user has a home it can write to"
    ),
}


def _installed(text: str) -> InstalledTranscripts:
    kept: list[str] = []
    landed = ""
    for line in text.splitlines():
        if line.startswith("KEPT\t"):
            kept.append(line.removeprefix("KEPT\t"))
        elif line.strip():
            landed = line.strip()
    return InstalledTranscripts(landed=landed, kept=tuple(kept))


def install_transcripts(
    node: Node, remote_root: str, source: Path, *, timeout_s: float
) -> InstalledTranscripts:
    """Put a pulled Claude project directory where a session started in
    ``remote_root`` on ``node`` will look for it (recall --to, spec §12 step
    4). The name is encoded HERE, by the one encoder, from the node's own
    physical path; the node only places files, never overwriting work it has
    that this PC lacks. Returns where it landed and what the node KEPT.
    NodeConfigError (a root ``_session_root`` refuses) and RemoteError rc None
    (``source`` cannot be read) both come before any dial; RemoteError when
    the node refuses -- its reason named from ``INSTALL_REFUSALS`` -- or does
    not answer."""
    _session_root(remote_root)
    payload = _tar_dir(source)
    name = encoded_project_dir(node_realpath(node, remote_root, timeout_s=timeout_s))
    try:
        done = run_script(
            node, "install_transcripts", [name], timeout_s=timeout_s, stdin=payload
        )
    except RemoteError as err:
        reason = INSTALL_REFUSALS.get(err.rc) if err.rc is not None else None
        if reason is None:
            raise
        raise RemoteError(
            err.rc, f"{reason}\n{err.stderr_tail}".rstrip(), err.command_redacted
        ) from err
    return _installed(_stdout_text(done))
