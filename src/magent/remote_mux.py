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
leaves ``attach_client``, ``env``, ``log``, ``node_scripts``, ``nodes``,
``psmux`` and ``sessions``.
"""

from __future__ import annotations

import contextlib
import functools
import gzip
import hashlib
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
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from magent import node_scripts, nodes, psmux

# find_ssh is bound by value, not read off attach_client at call time: the
# conftest guard answers None for attach_client.find_ssh, and this module's own
# find_ssh is the seam every remote_mux test fakes (fake_ssh repoints it).
from magent.attach_client import SSH_MISSING_RC, SSH_TRANSPORT_RC, TMUX_SOCKET
from magent.attach_client import find_ssh as _find_ssh_client
from magent.env import git_child_env
from magent.log import get_logger
from magent.nodes import (
    LoadSample,
    LocalGitState,
    NodeConfigError,
    absolute_remote,
    encoded_project_dir,
    stdio_programs,
    without_missing_programs,
)
from magent.sessions import build_resume_command

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Mapping, Sequence
    from typing import IO

    from magent.config import MagentConfig
    from magent.nodes import Node, Recipe, UserScope

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

# The Claude Code lifecycle events the node's state hook is wired into: the
# same six `magent hooks install` wires on this PC. A src module may not import
# the cli package (LS-A-001), so this is a copy -- drift-pinned against
# cli/hooks_cmd._EVENTS by tests/unit/test_node_provision.py.
HOOK_EVENTS = (
    "UserPromptSubmit",
    "PostToolUse",
    "Stop",
    "Notification",
    "SessionStart",
    "SessionEnd",
)
# The node-side hook command. provision.sh installs node_scripts/state_hook.sh
# at this path; Claude Code runs hook commands through bash, so $HOME expands.
NODE_STATE_HOOK_COMMAND = '"$HOME/.magent/bin/state-hook.sh" --source claude'


def state_hook_entries(
    command: str = NODE_STATE_HOOK_COMMAND,
) -> dict[str, dict[str, object]]:
    """One settings.json hook entry per event, in exactly the shape `magent
    hooks install` writes (pinned by test) -- PostToolUse alone carries the
    ``"*"`` matcher."""
    entries: dict[str, dict[str, object]] = {}
    for event in HOOK_EVENTS:
        entry: dict[str, object] = {
            "hooks": [{"type": "command", "command": command, "timeout": 10}]
        }
        if event == "PostToolUse":
            entry = {"matcher": "*", **entry}
        entries[event] = entry
    return entries


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
    A caller must therefore never retry a mutation blindly on rc None.
    ``timed_out`` tells the two apart: True only on the timeout path (outcome
    unknown), False on every other construction (a spawn failure never ran)."""

    def __init__(
        self,
        rc: int | None,
        stderr_tail: str,
        command_redacted: tuple[str, ...],
        *,
        timed_out: bool = False,
    ) -> None:
        self.rc = rc
        self.timed_out = timed_out
        self.stderr_tail = stderr_tail
        self.command_redacted = command_redacted
        super().__init__(
            f"{shlex.join(command_redacted)} failed (rc={rc}): {stderr_tail}"
        )


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
) -> subprocess.CompletedProcess[bytes]:
    """One bounded child -- the shared body of ``run`` and the local git reads
    (``ignored_paths``, ``git_state``). ``shown`` is what an error and a log line may say
    about the command; ``label`` opens every log line, naming who spawned it.
    ``env`` None is the plain inherited environment (every ssh call); only
    the local git reads pass one (``_local_git``). ``quiet`` drops all three
    of those log lines; the RemoteError is raised exactly the same."""
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
        if not quiet:
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
        if not quiet:
            get_logger("nodes").warning(
                "%s timed out after %.1fs: %s", label, timeout_s, shlex.join(shown)
            )
        raise RemoteError(
            None, f"timed out after {timeout_s:g}s", shown, timed_out=True
        ) from None
    if check and proc.returncode != 0:
        if not quiet:
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
    quiet: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """Run ``argv_remote`` on ``node`` over ssh, as ONE ``bash -c`` remote
    string (``_remote_string``). Raises RemoteError on a spawn failure, a
    missing client (rc 127), a timeout (rc None), or -- with ``check`` -- a
    non-zero exit. With ``check=False`` every exit code comes back for the
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
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """Run the packaged ``node_scripts/<script>.sh`` on ``node`` as
    ``bash -s -- <SOCKET> <args>``: the script on stdin, then -- when
    ``stdin`` is given -- the sentinel line and that payload. The socket is
    added here, on every call; ``args`` never carry it. Secrets belong in
    ``stdin``; ``args`` are argv, visible to the node's process table and to
    logs. A failure's ``stderr_tail`` is the script's own words (see
    ``RemoteError``): a script must never echo its payload. ``check=False``
    hands a non-zero exit back instead of raising: a script that reports its
    own failures in rows exits 1 and still has rows to read.

    Refused before any ssh: ValueError for a script in
    ``node_scripts.NON_ENTRY_SCRIPTS`` (it would read the socket as its own
    first argument) or for a name that is not a plain script name
    (``./lib``); FileNotFoundError for an unknown script."""
    if f"{script}.sh" in node_scripts.NON_ENTRY_SCRIPTS:
        raise ValueError(f"{script}.sh is not a run_script entry point")
    argv_remote, framed = _script_call(script, args, stdin)
    return run(node, argv_remote, timeout_s=timeout_s, input_bytes=framed, check=check)


# The row vocabulary every provisioning script prints: status<TAB>item<TAB>
# detail, one per line. Any other stdout line is a tool's chatter, ignored.
REPORT_STATUSES = frozenset({"ok", "did", "skip", "drop", "warn", "fail", "key"})


@dataclass(frozen=True)
class ScriptLine:
    status: str
    item: str
    detail: str


@dataclass(frozen=True)
class ProvisionReport:
    """What a node script said, row by row, in order."""

    lines: tuple[ScriptLine, ...]

    @property
    def failed(self) -> bool:
        return any(line.status == "fail" for line in self.lines)

    @property
    def changed(self) -> bool:
        return any(line.status in ("did", "drop") for line in self.lines)

    def keys(self) -> dict[str, str]:
        """``key`` rows (setup.sh): Unix user -> that user's node public key."""
        return {line.item: line.detail for line in self.lines if line.status == "key"}


def parse_report(text: str) -> ProvisionReport:
    """The rows in ``text``, in order. A line is a row when it splits on its
    first two tabs into a known status and a non-empty item; the detail keeps
    any further tabs. Everything else is a tool's chatter and is dropped."""
    lines: list[ScriptLine] = []
    for raw in text.splitlines():
        parts = raw.rstrip("\r").split("\t", 2)
        if len(parts) >= 2 and parts[0] in REPORT_STATUSES and parts[1]:
            lines.append(
                ScriptLine(parts[0], parts[1], parts[2] if len(parts) == 3 else "")
            )
    return ProvisionReport(tuple(lines))


def _report_of(
    result: subprocess.CompletedProcess[bytes],
    script: str,
    node: Node,
    *,
    args: Sequence[str],
    stdin: bytes | None,
) -> ProvisionReport:
    """A finished script's rows. Exit 255 is ssh's own failure, not the
    script's, and raises; any other non-zero exit with no ``fail`` row gets
    one, so a script that died mid-step can never read as a success.

    ``args`` and ``stdin`` are the ones the ``run_script`` call was given, so
    the error names exactly what ran (``--force``, the probed programs) the
    way ``run`` does (``_run_shown`` over ``_script_call``): the program, not
    this PC's path to it, stdin by its length alone, and no client lookup --
    a lookup here could turn a transport failure into "ssh not installed"."""
    if result.returncode == SSH_TRANSPORT_RC:
        raise RemoteError(
            SSH_TRANSPORT_RC,
            _tail(result.stderr),
            _run_shown(node, *_script_call(script, args, stdin)),
        )
    report = parse_report(result.stdout.decode("utf-8", "replace"))
    if result.returncode != 0 and not report.failed:
        err = result.stderr.decode("utf-8", "replace").strip().splitlines()
        detail = f"exited {result.returncode}" + (f": {err[-1][:200]}" if err else "")
        report = ProvisionReport((*report.lines, ScriptLine("fail", script, detail)))
    return report


GH_TIMEOUT_S = 20.0


@functools.lru_cache(maxsize=1)
def find_gh() -> str | None:
    """This PC's ``gh``. Only provisioning uses it: to share the PC's GitHub
    login with a node and to register a node's key."""
    return shutil.which("gh")


def _gh(
    args: list[str], *, input_bytes: bytes | None = None
) -> subprocess.CompletedProcess[bytes] | None:
    """One bounded local ``gh`` call; None when gh is missing or could not
    run. Only argv is ever logged -- a token read's stdout never is."""
    exe = find_gh()
    if exe is None:
        return None
    try:
        return _spawn(
            [exe, *args],
            timeout_s=GH_TIMEOUT_S,
            input_bytes=input_bytes,
            check=False,
            shown=("gh", *args),
            label="local gh",
        )
    except RemoteError:
        return None


@dataclass(frozen=True)
class GhAccount:
    login: str
    scopes: frozenset[str]


def local_gh_account() -> GhAccount | None:
    """The active, logged-in github.com account of this PC's gh, or None."""
    result = _gh(["auth", "status", "--json", "hosts"])
    if result is None or result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    hosts = data.get("hosts") if isinstance(data, dict) else None
    entries = hosts.get("github.com") if isinstance(hosts, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        login = entry.get("login")
        if (
            entry.get("active") is True
            and entry.get("state") == "success"
            and isinstance(login, str)
            and login
        ):
            raw = entry.get("scopes")
            scopes = raw if isinstance(raw, str) else ""
            return GhAccount(
                login=login,
                scopes=frozenset(s.strip() for s in scopes.split(",") if s.strip()),
            )
    return None


def local_gh_token() -> str | None:
    """This PC's github.com token, or None. It leaves this process only on a
    node call's stdin (``build_payload``) -- never argv, never a log."""
    result = _gh(["auth", "token", "--hostname", "github.com"])
    if result is None or result.returncode != 0:
        return None
    token = result.stdout.decode("utf-8", "replace").strip()
    if not token or any(ch.isspace() for ch in token):
        return None
    return token


# node_apply refuses a manifest of another version (its MANIFEST_VERSION is
# pinned equal to this by test).
PAYLOAD_VERSION = 1


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_payload(
    scope: UserScope,
    *,
    gh_token: str | None,
    gh_login: str | None,
    state_hook: str,
) -> bytes:
    """What follows the sentinel on provision.sh's stdin: the gh token (or an
    empty line) and a gzip tar of the user scope + manifest. Deterministic --
    identical input, identical bytes. The token is in the first line ONLY."""
    entries = state_hook_entries()
    digests = scope.digests()
    digests["gh"] = _sha(f"{gh_login}\n{gh_token}") if gh_token else ""
    digests["state_hook"] = _sha(state_hook + _canonical(entries))
    manifest = {
        "version": PAYLOAD_VERSION,
        "digests": digests,
        "gh_login": gh_login if gh_token else None,
        "plugins": list(scope.plugins),
        "marketplaces": scope.marketplaces,
        "hook_entries": entries,
    }
    members: list[tuple[str, bytes, int]] = [
        ("manifest.json", _canonical(manifest).encode("utf-8"), 0o600),
        ("mcp_oauth.json", _canonical(scope.mcp_oauth).encode("utf-8"), 0o600),
        ("mcp_servers.json", _canonical(scope.mcp_servers).encode("utf-8"), 0o600),
        ("node_apply.py", node_scripts.source("node_apply.py").encode("utf-8"), 0o600),
        ("settings.json", _canonical(scope.settings).encode("utf-8"), 0o600),
        ("state-hook.sh", state_hook.encode("utf-8"), 0o700),
    ]
    members += [
        (f"skills/{f.path}", f.data, 0o700 if f.executable else 0o600)
        for f in scope.skills
    ]
    raw = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for name, data, mode in sorted(members):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    return (gh_token or "").encode("utf-8") + b"\n" + raw.getvalue()


# A provision applies the whole user scope (plugins install, marketplaces
# clone); the program probe ahead of it is one `command -v` per name.
PROVISION_TIMEOUT_S = 300.0
PROGRAMS_TIMEOUT_S = 30.0


class ProgramsProbeFailed(Exception):
    """The programs probe reached the node but did not answer for every name
    it was asked: it exited non-zero, or left a name without an ``ok`` or
    ``skip`` row. ``lines`` holds its ``fail`` row(s), one naming
    ``programs``; the probe proved nothing about the node either way."""

    def __init__(self, lines: tuple[ScriptLine, ...]) -> None:
        super().__init__("; ".join(line.detail for line in lines))
        self.lines = lines


def node_programs(
    node: Node, programs: Iterable[str], *, timeout_s: float
) -> frozenset[str]:
    """The subset of ``programs`` the node resolves (an executable file on its
    PATH, ~/.local/bin first, as provision.sh runs). Only program NAMES cross
    -- never a server's env or args. Raises RemoteError when the node is
    unreachable, and ProgramsProbeFailed when the probe ran but did not
    answer for every name: a failed probe is not "not on the node"."""
    wanted = sorted(set(programs))
    if not wanted:
        return frozenset()
    result = run_script(node, "programs", wanted, timeout_s=timeout_s, check=False)
    report = _report_of(result, "programs", node, args=wanted, stdin=None)
    answered = {line.item for line in report.lines if line.status in ("ok", "skip")}
    unanswered = [program for program in wanted if program not in answered]
    if report.failed or unanswered:
        failed = tuple(line for line in report.lines if line.status == "fail")
        raise ProgramsProbeFailed(
            failed
            or (
                ScriptLine(
                    "fail", "programs", "no answer for " + ", ".join(unanswered)
                ),
            )
        )
    return frozenset(
        line.item
        for line in report.lines
        if line.status == "ok" and line.item in wanted
    )


def provision(
    node: Node, user_scope: UserScope, *, timeout_s: float, force: bool = False
) -> ProvisionReport:
    """Lay ``user_scope`` onto ``node`` in ONE apply call: provision.sh unpacks
    the payload and node_apply applies it. This PC's gh token is shared only
    when gh names the account it belongs to, and it rides stdin. ``force``
    re-applies unchanged items (``magent node setup`` sends it).

    A stdio MCP candidate ships only if the node resolves its program: when
    there is one, a ``programs.sh`` probe comes first, and what the node lacks
    is dropped BEFORE the payload exists, so its env never leaves this PC.
    A probe that fails drops every candidate the same way, and its ``fail``
    row rides the report (the rest of the scope still applies).
    The report opens with one verdict line per server: the scope's notes as
    ``skip`` rows (what stayed behind, and why), then ``ok`` per shipped one."""
    programs = stdio_programs(user_scope)
    probe_failed: tuple[ScriptLine, ...] = ()
    if programs:
        try:
            found = node_programs(
                node, programs.values(), timeout_s=min(timeout_s, PROGRAMS_TIMEOUT_S)
            )
        except ProgramsProbeFailed as exc:
            probe_failed = exc.lines
            user_scope = without_missing_programs(
                user_scope, found=frozenset(), unprobed=True
            )
        else:
            user_scope = without_missing_programs(user_scope, found=found)
    account = local_gh_account()
    token = local_gh_token() if account is not None else None
    login = account.login if account is not None and token else None
    payload = build_payload(
        user_scope,
        gh_token=token if login else None,
        gh_login=login,
        state_hook=node_scripts.script("state_hook"),
    )
    args = ["--force"] if force else []
    result = run_script(
        node, "provision", args, timeout_s=timeout_s, stdin=payload, check=False
    )
    report = _report_of(result, "provision", node, args=args, stdin=payload)
    notes = tuple(ScriptLine("skip", "scope", note) for note in user_scope.notes)
    shipped = tuple(
        ScriptLine("ok", "scope", f"mcp {name}: shipped")
        for name in sorted(user_scope.mcp_servers)
    )
    return ProvisionReport((*notes, *shipped, *probe_failed, *report.lines))


def provision_node(
    node: Node,
    config: MagentConfig,
    *,
    home: Path,
    timeout_s: float,
    force: bool = False,
) -> ProvisionReport:
    """THE provisioning body (DECISION-24): ``magent node setup`` and
    ``launch._provision_once`` both call it. Builds the user scope from
    ``home`` -- the one place src builds one; plan K swaps that line for its
    relay seam and drops the ``del`` above it -- then hands it to
    ``provision``, which probes the node's programs only when a stdio server
    is in the scope. ``config`` is unread until K lands. Raises RemoteError
    when the node is unreachable; a failed step is a ``fail`` row."""
    del config  # plan K's relay swap reads config.settings
    return provision(node, nodes.user_scope(home), timeout_s=timeout_s, force=force)


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
member tar wrote (pull.sh hands tar an explicit file list with
``--no-recursion``, so it is that list's length). pull.sh must emit
``tar ... ; printf 'MAGENT-PULL-END %d\\n' "$count"`` -- the printf ONLY after
tar exits 0, so a tar that failed leaves the reply without a trailer."""
PULL_MAX_MEMBER_BYTES = 64 * 1024 * 1024
"""A member declaring more than this is not stored (its session fails, so its
watermark holds). A transcript is the largest file a pull carries."""
PULL_MAX_TOTAL_BYTES = 512 * 1024 * 1024
"""The most one reply may ask this PC to write, summed over the members it
would store; more is RemoteError before anything is written."""
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
      watermark must not move."""

    now: float
    sessions: tuple[str, ...]
    sample: LoadSample | None
    realpaths: Mapping[str, str]
    state_files: Mapping[str, tuple[str, ...]]
    files: tuple[Path, ...]
    failed_sids: frozenset[str]


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


def _member_parts(
    member: tarfile.TarInfo, sids: frozenset[str]
) -> tuple[str, ...] | None:
    """``<sid>/transcripts/<any depth>`` or ``<sid>/state/<name>.json`` for a
    requested sid, every part a legal name here, regular files only -- or None."""
    if not member.isfile():
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
    )


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
    fd = os.open(path, _READ_FLAGS)
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
    """``(member name, path)`` for every REGULAR file under ``memory_dir``, in
    name order. A link is never followed -- not a file link, not a folder
    link, and not ``memory_dir`` itself being one: the folder is Claude's,
    and a link in it can name ``~/.ssh``. What is skipped is logged, never
    raised: a bring-up never fails because of memory.

    "A link" is decided by ``realpath``, not ``is_symlink``: a Windows
    junction -- which any standard user can make -- is not a symlink to
    pathlib, and ``os.walk(followlinks=False)`` descends into one. An entry is
    kept only when resolving it changes nothing but its parent's own
    resolution, so a link ABOVE ``memory_dir`` (a dotfiles ``~/.claude``)
    still ships, and every file must resolve inside the resolved folder."""
    logger = get_logger("nodes")
    real_mem = Path(os.path.realpath(memory_dir))
    if real_mem != Path(os.path.realpath(memory_dir.parent)) / memory_dir.name:
        logger.warning("memory folder %s is a link; no memory shipped", memory_dir)
        return []
    found: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(memory_dir):
        base = Path(dirpath)
        real_base = Path(os.path.realpath(base))
        kept: list[str] = []
        for name in dirnames:
            if Path(os.path.realpath(base / name)) == real_base / name:
                kept.append(name)
            else:
                logger.warning("memory link %s skipped", base / name)
        dirnames[:] = kept  # os.walk descends only into what is left
        for name in filenames:
            path = base / name
            if path.is_symlink() or not path.is_file():
                logger.warning("memory entry %s is not a regular file; skipped", path)
                continue
            if not Path(os.path.realpath(path)).is_relative_to(real_mem):
                logger.warning("memory entry %s resolves outside memory; skipped", path)
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
