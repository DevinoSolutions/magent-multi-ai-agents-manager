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
leaves ``attach_client``, ``env``, ``log``, ``node_scripts`` and ``nodes``.
"""

from __future__ import annotations

import contextlib
import functools
import json
import math
import shlex
import shutil
import subprocess
from typing import TYPE_CHECKING

from magent import node_scripts
from magent.attach_client import SSH_MISSING_RC, TMUX_SOCKET
from magent.env import git_child_env
from magent.log import get_logger
from magent.nodes import LoadSample, LocalGitState

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

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
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """One bounded child -- the shared body of ``run`` and the local git reads
    (``ignored_paths``, ``git_state``). ``shown`` is what an error and a log line may say
    about the command; ``label`` opens every log line, naming who spawned it.
    ``env`` None is the plain inherited environment (every ssh call); only
    the local git reads pass one (``_local_git``)."""
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
        if (project_dir / ".git").exists():
            return [project_dir]
        if not project_dir.is_dir():
            return []
        return sorted(
            child
            for child in project_dir.iterdir()
            if child.is_dir() and (child / ".git").exists()
        )
    except OSError as e:
        reason = f"cannot read {project_dir}: {e.strerror or e}"
        get_logger("nodes").warning("repo lookup failed: %s", reason)
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
