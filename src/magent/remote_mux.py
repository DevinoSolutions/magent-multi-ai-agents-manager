"""The single owner of every subprocess aimed at a node (spec §6).

ssh argv, ``tmux -L magent`` on the node, the shipped ``node_scripts``, and the
local git reads a bring-up needs. Every function returns data or raises
``RemoteError``; nothing here prints or exits. Four laws:

- a remote command is a list, sent as ``bash -c <shlex.quote(shlex.join(argv))>``
  -- never an f-string shell, and never parsed by the node user's LOGIN shell
  (zsh expands a bare ``=word``, which is every exact tmux target ``=<sid>``);
- every call is bounded: ``timeout_s`` is a required keyword, so a call that
  forgot it is a TypeError, never a hang;
- secrets travel on stdin only -- never argv, never a log line;
  ``RemoteError.command_redacted`` names stdin by its length alone;
- ``BatchMode=yes`` everywhere: a password prompt nobody can answer is a hang.

A leaf: never imports ``magent.cli`` (LS-A-001); its magent imports are the
leaves ``attach_client``, ``log``, ``node_scripts``, ``nodes``, ``psmux`` and
``sessions``.
"""

from __future__ import annotations

import contextlib
import functools
import io
import json
import math
import os
import shlex
import shutil
import stat
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from magent import node_scripts, psmux
from magent.attach_client import SSH_MISSING_RC, TMUX_SOCKET
from magent.log import get_logger
from magent.nodes import (
    LoadSample,
    LocalGitState,
    NodeConfigError,
    absolute_remote,
    encoded_project_dir,
)
from magent.sessions import build_resume_command

if TYPE_CHECKING:
    from collections.abc import Sequence

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


def _spawn(
    argv: list[str],
    *,
    timeout_s: float,
    input_bytes: bytes | None,
    check: bool,
    shown: tuple[str, ...],
    label: str,
) -> subprocess.CompletedProcess[bytes]:
    """One bounded child -- the shared body of ``run`` and the local git reads
    (``ignored_paths``, ``git_state``). ``shown`` is what an error and a log line may say
    about the command; ``label`` opens every log line, naming who spawned it."""
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
        get_logger("nodes").warning(
            "%s could not start (%s): %s", label, reason, shlex.join(shown)
        )
        raise RemoteError(rc, reason, shown) from e
    try:
        out, err = proc.communicate(input=input_bytes, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        # Reap the direct child only, bounded. A second communicate() would
        # wait for every pipe's write end, and the interpreter behind a
        # .cmd/sh shim is a GRANDCHILD still holding one -- the 90s-for-a-5s
        # timeout defect psmux.probe_control_plane documents.
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            proc.wait(timeout=_REAP_TIMEOUT_S)
        get_logger("nodes").warning(
            "%s timed out after %.1fs: %s", label, timeout_s, shlex.join(shown)
        )
        raise RemoteError(None, f"timed out after {timeout_s:g}s", shown) from None
    if check and proc.returncode != 0:
        get_logger("nodes").warning(
            "%s failed (rc=%s): %s", label, proc.returncode, shlex.join(shown)
        )
        raise RemoteError(proc.returncode, _tail(err), shown)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def run(
    node: Node,
    argv_remote: Sequence[str],
    *,
    timeout_s: float,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """Run ``argv_remote`` on ``node`` over ssh, as ONE ``bash -c`` remote
    string (``_remote_string``). Raises RemoteError on a spawn failure, a
    missing client (rc 127), a timeout (rc None), or -- with ``check`` -- a
    non-zero exit. With ``check=False`` every exit code comes back for the
    caller to classify. The returned ``CompletedProcess.args`` is the real
    argv, this PC's client path included: a caller must not log it."""
    tail = _ssh_tail(node, argv_remote, tty=False)
    shown = _run_shown(node, argv_remote, input_bytes)
    return _spawn(
        [_client(shown), *tail],
        timeout_s=timeout_s,
        input_bytes=input_bytes,
        check=check,
        shown=shown,
        label="node call",
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
) -> subprocess.CompletedProcess[bytes]:
    """Run the packaged ``node_scripts/<script>.sh`` on ``node`` as
    ``bash -s -- <SOCKET> <args>``: the script on stdin, then -- when
    ``stdin`` is given -- the sentinel line and that payload. The socket is
    added here, on every call; ``args`` never carry it. Secrets belong in
    ``stdin``; ``args`` are argv, visible to the node's process table and to
    logs. A failure's ``stderr_tail`` is the script's own words (see
    ``RemoteError``): a script must never echo its payload.

    Refused before any ssh: ValueError for a script in
    ``node_scripts.NON_ENTRY_SCRIPTS`` (it would read the socket as its own
    first argument) or for a name that is not a plain script name
    (``./lib``); FileNotFoundError for an unknown script."""
    if f"{script}.sh" in node_scripts.NON_ENTRY_SCRIPTS:
        raise ValueError(f"{script}.sh is not a run_script entry point")
    argv_remote, framed = _script_call(script, args, stdin)
    return run(node, argv_remote, timeout_s=timeout_s, input_bytes=framed)


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
    session, so the per-session options are scoped with ``-t =sid`` (and the
    window ones with ``=sid:``) rather than ``-g``, where psmux's
    server-per-session model allows a global."""
    hints, hints_len = psmux.status_hints(code_hint)
    brand, brand_len = psmux.status_left(nick)
    target, window = f"={sid}", f"={sid}:"
    fmt = psmux.WINDOW_STATUS_FORMAT
    return [
        tmux_argv("bind", "-n", "F1", "detach-client"),
        tmux_argv("set", "-t", target, "status-right", hints),
        tmux_argv("set", "-t", target, "status-right-length", hints_len),
        tmux_argv("set", "-t", target, "status-left", brand),
        tmux_argv("set", "-t", target, "status-left-length", brand_len),
        psmux.f2_binding_argv(tmux_argv(), code_hint),
        tmux_argv("rename-window", "-t", window, psmux.window_display_name(sid)),
        tmux_argv("setw", "-t", window, "automatic-rename", "off"),
        tmux_argv("setw", "-t", window, "window-status-format", fmt),
        tmux_argv("setw", "-t", window, "window-status-current-format", fmt),
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


def sample(node: Node) -> LoadSample:
    """One load reading from ``node`` (``sample.sh``). RemoteError when the node
    can't be reached, or answers something that is not a sample -- rc 0 on
    that error: the node answered; the answer was malformed. Its message
    carries a bounded head of what came back (a ``.bashrc`` banner on stdout
    is the likely cause). A non-finite, fractional-count, bool or string
    number is not a sample either."""
    result = run_script(node, "sample", [], timeout_s=PROBE_TIMEOUT_S)
    try:
        raw = json.loads(result.stdout.decode("utf-8", "replace"))
        reading = LoadSample(
            ts=_finite(raw["ts"]),
            nproc=_integral(raw["nproc"]),
            load1=_finite(raw["load1"]),
            load5=_finite(raw["load5"]),
            load15=_finite(raw["load15"]),
            mem_total_mb=_integral(raw["mem_total_mb"]),
            mem_avail_mb=_integral(raw["mem_avail_mb"]),
            my_sessions=_integral(raw["my_sessions"]),
        )
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


# A local git read is a local process, but it can still hang (a credential
# prompt, a network filesystem); bounded like every other child.
GIT_TIMEOUT_S = 30.0


def _git(
    path: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    """One bounded, read-only ``git -C <path> <args>``. A missing ``git`` is
    RemoteError rc None ("git not found on PATH"), as in ``ignored_paths``:
    ``_spawn`` alone would call it the missing ssh client (rc 127)."""
    argv = ["git", "-C", str(path), *args]
    shown = tuple(argv)
    try:
        return _spawn(
            argv,
            timeout_s=GIT_TIMEOUT_S,
            input_bytes=None,
            check=check,
            shown=shown,
            label="git state read",
        )
    except RemoteError as e:
        if isinstance(e.__cause__, FileNotFoundError):
            raise RemoteError(None, "git not found on PATH", shown) from e.__cause__
        raise


def _out(result: subprocess.CompletedProcess[bytes]) -> str:
    return result.stdout.decode("utf-8", "replace").strip()


def repo_paths(project_dir: Path) -> list[Path]:
    """The git repos a node project is made of: the project itself when it is
    a repo, else each DIRECT child that is one (a workspace of repos), in name
    order. Empty when there is none -- which the caller refuses, because a
    node clones the project from its origin."""
    if (project_dir / ".git").exists():
        return [project_dir]
    if not project_dir.is_dir():
        return []
    return sorted(
        child
        for child in project_dir.iterdir()
        if child.is_dir() and (child / ".git").exists()
    )


def git_state(path: Path) -> LocalGitState:
    """What D7 needs to know about the LOCAL repo at ``path``, read-only.

    ``url`` is origin's, "" when there is no origin. ``detached`` when HEAD
    names no branch (``branch`` is then ""). A repo with no commits yet is
    not detached: HEAD still names its unborn branch. ``dirty`` counts
    untracked files too -- origin never saw them, so the node would not have
    them. ``unpushed`` is "HEAD has commits origin's copy of this branch
    lacks", and a branch origin has never seen counts. Raises RemoteError
    when ``path`` is not a repo or git itself fails."""
    _git(path, "rev-parse", "--git-dir")
    origin = _git(path, "remote", "get-url", "origin", check=False)
    url = _out(origin) if origin.returncode == 0 else ""
    head = _git(path, "symbolic-ref", "-q", "--short", "HEAD", check=False)
    detached = head.returncode != 0
    branch = "" if detached else _out(head)
    dirty = bool(_out(_git(path, "status", "--porcelain")))
    unpushed = False
    if url and not detached:
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
        ignored=ignored_paths(path, timeout_s=PROBE_TIMEOUT_S, label="git state read"),
    )


@dataclass(frozen=True)
class BringUpResult:
    """What ``bring_up.sh`` reported. ``cwd`` is the node's ABSOLUTE project
    folder (``Recipe.remote_root`` keeps ``~``); ``commits`` maps each repo's
    absolute folder to the commit it now has checked out; ``shipped`` lists the
    project-relative files written beside the clone."""

    sid: str
    attached_existing: bool
    commits: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    shipped: tuple[str, ...] = ()


# bring_up.sh's header format version; the script refuses anything else.
_HEADER_MAGIC = "MAGENT1"


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
    or ``..`` segment."""
    name = rel.replace("\\", "/")
    if any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError(f"{rel!r} cannot name a file inside the project")
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

# How a vetted file is opened: never through a final-component link, never
# blocking on a FIFO. Read off the module, so Windows (which has neither
# flag, and wants O_BINARY) needs no `sys.platform` branch.
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_BINARY", 0)
)


def _read_regular(path: Path, *, cap: int, what: str) -> bytes:
    """The bytes of ``path``, which must be a REGULAR file of at most ``cap``
    bytes, or ValueError naming ``what``. Checked three times, because each
    check alone has a hole: ``lstat`` before opening (a FIFO or a device is
    never opened, an oversize file never read), ``fstat`` on what was opened
    (the path may have been swapped in between), and a read of at most
    ``cap + 1`` bytes (the file may have grown). OSError when it cannot be
    opened -- ``O_NOFOLLOW`` makes a final-component link swapped in after the
    ``lstat`` one of those."""
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{what} is not a regular file")
    if before.st_size > cap:
        raise ValueError(f"{what} is {before.st_size} bytes; the cap is {cap}")
    fd = os.open(path, _READ_FLAGS)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError(f"{what} is not a regular file")
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
    """``(member name, path)`` for every REGULAR file under ``memory_dir``, in
    name order. A link is never followed -- not a file link, not a folder
    link, and not ``memory_dir`` itself being one: the folder is Claude's,
    and a link in it can name ``~/.ssh``. What is skipped is logged, never
    raised: a bring-up never fails because of memory."""
    logger = get_logger("nodes")
    if memory_dir.is_symlink():
        logger.warning("memory folder %s is a link; no memory shipped", memory_dir)
        return []
    found: list[tuple[str, Path]] = []
    # os.walk never descends into a linked folder (followlinks=False); each
    # one is named so the skip is visible.
    for dirpath, dirnames, filenames in os.walk(memory_dir):
        base = Path(dirpath)
        for name in dirnames:
            if (base / name).is_symlink():
                logger.warning("memory link %s skipped", base / name)
        for name in filenames:
            path = base / name
            if path.is_symlink() or not path.is_file():
                logger.warning("memory entry %s is not a regular file; skipped", path)
                continue
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
    home = result.stdout.decode("utf-8", "replace").strip()
    if not home.startswith("/"):
        raise RemoteError(
            result.returncode,
            f"unusable $HOME on the node: {home!r}",
            _run_shown(node, probe, None),
        )
    return home


def _parse_result(
    result: subprocess.CompletedProcess[bytes], shown: tuple[str, ...]
) -> dict[str, object]:
    """bring_up.sh's last non-empty stdout line, a JSON object. Anything else
    is a RemoteError: a result that cannot be read is not a success."""
    text = result.stdout.decode("utf-8", "replace")
    lines = [line for line in text.splitlines() if line.strip()]
    try:
        parsed = json.loads(lines[-1]) if lines else None
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        raise RemoteError(result.returncode, "not a bring-up result", shown)
    return parsed


def _deliver(
    node: Node, mode: str, recipe: Recipe, root: str, payload: bytes
) -> dict[str, object]:
    args = [mode, recipe.sid, root, encoded_project_dir(root)]
    result = run_script(
        node, "bring_up", args, timeout_s=BRING_UP_TIMEOUT_S, stdin=payload
    )
    # What RAN, as run() itself would name it (the ``sample`` precedent).
    shown = _run_shown(node, *_script_call("bring_up", args, payload))
    return _parse_result(result, shown)


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
    return BringUpResult(
        sid=recipe.sid,
        attached_existing=raw.get("attached_existing") is True,
        commits=(
            {str(k): str(v) for k, v in commits.items()}
            if isinstance(commits, dict)
            else {}
        ),
        cwd=cwd if isinstance(cwd, str) and cwd else root,
        shipped=tuple(str(s) for s in shipped) if isinstance(shipped, list) else (),
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
