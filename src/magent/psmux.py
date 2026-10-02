"""Psmux (tmux multiplexer) lifecycle primitives.

Every subprocess interaction with the psmux binary lives here: session
creation, liveness checks, send-keys, status-line flashes, kills. Callers
(launch, upload_server, session_picker, cli/status) import tested primitives
instead of inlining ad-hoc ``subprocess.run`` calls.

The module is a pure leaf — no cli/ imports, no heavy subsystem imports at
top level. It sits alongside ``tiling.py``, ``procs.py``, and ``tailnet.py``
in the dependency graph.
"""

from __future__ import annotations

import contextlib
import functools
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from magent.config import MagentConfig
    from magent.platform import Platform

from magent.log import get_logger

# Every one-shot psmux client this module spawns is a CONTROL or PROBE command
# whose output is piped or discarded -- no human ever looks at its console. On
# Windows that console is not free: a console-subsystem child whose parent has
# no console gets a brand-new one, and on Windows 11 the default terminal is
# Windows Terminal, so each spawn materializes as a real, empty, focus-stealing
# WT window. The processes that call into this module without a console are
# exactly the supervised fleet -- `magent serve`, `attention -d`, and the
# hotkey listener are all spawned `DETACHED_PROCESS | CREATE_NO_WINDOW` (see
# `launch.spawn_detached`) -- so one Alt+V press (three narration flashes, the
# paste `send-keys`, a discovery fan-out probing every configured session)
# opened dozens of empty terminals at once and froze the desktop (observed
# live 2026-08-31). CREATE_NO_WINDOW gives each child a windowless conhost
# instead: psmux control commands are indifferent to it (verified against the
# real 3.3.8 binary -- `-V` and a bogus-socket `list-sessions` both answer
# rc 0 under the flag). The value is hand-defined because the attribute only
# exists on Windows (same pattern as `launch.spawn_detached`); POSIX passes 0,
# which `Popen` accepts as "no flags". The one psmux spawn that must NOT carry
# this lives in `platform/windows.py` (`new-session`, which deliberately
# inherits the caller's console -- see the comment there); every spawn in THIS
# module must, and a unit contract test walks the AST to enforce it.
_CREATE_NO_WINDOW = 0x08000000  # subprocess.CREATE_NO_WINDOW, a win32-only attr
_SPAWN_FLAGS = _CREATE_NO_WINDOW if sys.platform == "win32" else 0


@functools.lru_cache(maxsize=1)
def find_psmux() -> str | None:
    """Locate the psmux binary. LRU-cached for the process lifetime."""
    found = shutil.which("psmux")
    if found:
        return found
    if sys.platform == "win32":
        from magent.env import localappdata_dir

        local = localappdata_dir() / "psmux" / "psmux.exe"
        if local.is_file():
            return str(local)
    return None


def child_env() -> dict[str, str]:
    """Environment for a psmux child that CREATES a session.

    Delegates to ``env.spawn_child_env`` -- the only module allowed to touch
    ``os.environ``, and the one seam every agent-hosting spawn in the product
    routes through -- and is re-exported here so the psmux spawn site that
    needs it (``platform/windows.py``'s ``new-session``) reaches it through the
    module that owns psmux subprocess behaviour. Imported in-body because
    ``magent.env`` pulls pydantic in, and this module is a leaf that only
    imports ``magent.log`` at module level.

    This session is the one that will host the project's agent for the rest of
    the day, so the strip is wider than psmux's own concern: the multiplexer
    nesting markers, the launching agent harness's session markers, and the
    launching shell's colour overrides all go. See ``env.spawn_child_env``.

    SCOPE of the psmux half, measured rather than assumed: psmux's
    nested-session guard fires for ``new-session`` alone. Run from inside a live
    pane against a live session, ``has-session -t``, ``display-message -t`` and
    ``capture-pane -t`` return byte-identical results with the markers present
    and with them stripped -- no warning, same exit code. So every CONTROL and
    PROBE command in this module spawns with the plain inherited environment:
    cleaning it there would buy nothing and would put a rebuilt environment
    block under every psmux round-trip magent makes. (The one thing an inherited
    ``$TMUX`` could still do -- let a target-less command answer for the calling
    client's own pane -- is closed explicitly by the ``-t <session>`` every
    command here passes.) The harness/colour markers are the same story from the
    other side: a control command's environment never reaches the pane, only
    ``new-session``'s does.
    """
    from magent.env import spawn_child_env

    return spawn_child_env()


# --- Priority of the interactive path -----------------------------------------
# Every image name a psmux process can be running under, lower-cased.
#
# Checked against the real artifact rather than assumed: the Windows release zip
# (both v3.3.6 and v3.3.8) ships FIVE entries -- LICENSE, README.md, and THREE
# copies of the same binary named ``psmux.exe``, ``pmux.exe`` and ``tmux.exe``.
# ``Expand-Archive`` drops all three side by side (this box's
# ``%LOCALAPPDATA%\psmux`` has exactly that), so which name a running server
# carries is simply whichever one was invoked. magent's own ``find_psmux`` only
# ever resolves ``psmux``, and this machine's live fleet is 169 ``psmux.exe`` --
# but a user who put that directory on PATH and typed ``pmux`` gets a server
# named ``pmux.exe`` hosting exactly the same pane, and it should feel the same.
#
# ``tmux.exe`` is deliberately NOT in the set, and that is the one judgement
# call here. The name is not psmux's to claim: an MSYS2/Cygwin/Git-for-Windows
# box can carry an unrelated ``tmux.exe``, and a sweep that reached it would be
# raising the priority of a process magent never launched and knows nothing
# about. The cost of leaving it out is bounded and visible -- a user who invokes
# the tmux-named copy keeps today's Normal priority, i.e. today's behaviour.
PSMUX_IMAGE_NAMES = frozenset({"psmux.exe", "pmux.exe"})


def session0_server_pids() -> list[int]:
    """Live psmux processes running in Windows logon Session 0.

    The diagnostic half of the desktop hand-off: the hand-off stops magent from
    CREATING these, and this finds the ones already there -- started by an older
    magent, by a bare `psmux` typed over ssh, or by any other service. Empty off
    Windows and empty when the process snapshot cannot be taken, which is the
    right answer for a diagnostic that must never invent a problem.

    ``session_id_of`` needs no process handle, so this sees servers the desktop
    user could not open: a Session-0 psmux started over ssh runs at High
    integrity and an ordinary shell cannot touch it, which is exactly why the
    repair hint says "elevated".
    """
    from magent.procs import pids_by_image_name, session_id_of

    return [
        pid for pid in pids_by_image_name(PSMUX_IMAGE_NAMES) if session_id_of(pid) == 0
    ]


def session0_message(count: int) -> str:
    """The one wording `doctor` and `status` both report a Session-0 fleet in."""
    return (
        f"{count} psmux server(s) run in logon Session 0 (started over ssh?) "
        "— invisible to this desktop and blocking their names; stop them from "
        "an elevated shell"
    )


def boost_enabled() -> bool:
    """Whether ``MAGENT_PSMUX_BOOST`` permits the priority sweep.

    Same degradation doctrine as ``upload_server.supervision_enabled`` and
    ``launch.upload_supervision_enabled``: a long-lived process must never die
    of an environment variable it does not use, and every other MAGENT_*
    consumer has already failed loudly at CLI entry by the time a supervisor
    thread is running -- so an env that has gone bad underneath one degrades to
    the default (sweep) rather than taking the sweep's owner down with it.
    """
    from pydantic import ValidationError

    from magent.env import get_env

    try:
        return get_env().psmux_boost
    except ValidationError:
        get_logger("launch").warning(
            "psmux boost: environment did not validate; boosting anyway"
        )
        return True


def boost_priority() -> int:
    """Raise every live psmux process to ABOVE_NORMAL. Returns how many this
    call actually raised (0 on a fleet that is already boosted, off Windows, and
    whenever ``MAGENT_PSMUX_BOOST=0``).

    THE one seam for the whole feature -- the launch-path bring-up, the
    attention daemon's tick and the serve supervisor all call exactly this, so
    "who boosts" is a question about call sites and never about behaviour. It is
    idempotent (a process already above NORMAL is skipped) and a per-pid failure
    -- a pid that exited mid-sweep, one the OS refuses -- is skipped rather than
    aborting the rest of the fleet. Each of the three owners still wraps the
    call, on the same doctrine as ``UploadServerSupervisor``: the handling
    belongs at the boundary with the loop that has to survive.

    Rationale for ABOVE_NORMAL, for a sweep rather than a spawn flag, and for
    the no-downgrade rule: DESIGN.md §2 "The interactive path outranks the
    fleet".
    """
    from magent.procs import boost_above_normal  # in-body: keeps this leaf thin

    if not boost_enabled():
        return 0
    boosted = boost_above_normal(PSMUX_IMAGE_NAMES)
    if boosted:
        # Only the transitions are logged. A steady state that logged every 30
        # seconds would be the loudest line in the file and say nothing.
        get_logger("launch").info(
            "psmux boost: raised %d process(es) to above-normal priority", boosted
        )
    return boosted


@dataclass
class PsmuxWindowOpts:
    """One window to create inside a psmux session."""

    window_name: str
    cwd: str
    command: str
    # False for a command that must run at most ONCE per session: a cloud pane's
    # `claude --cloud` creates a new cloud session every time it is typed.
    resend: bool = True
    # The status-left brand nick (`status_left`); None = the plain brand.
    nick: str | None = None


def session_name(title: str) -> str:
    """Sanitize a window title into a valid psmux/tmux session name."""
    return title.replace(".", "-").replace(":", "-").replace(" ", "-")


def has_session(
    name: str, psmux: str | None = None, timeout: float | None = None
) -> bool:
    """True if a psmux session named ``name`` is alive.

    The explicit ``-t <name>`` is REQUIRED, exactly as it is for
    ``display-message``: a BARE ``has-session`` exits 0 on this machine's psmux
    3.3.6 for a socket that has no server at all (``psmux -L
    definitely-not-a-session-xyz has-session`` -> rc 0; psmux also keeps
    internal ``__warm__`` spare servers per socket that answer it), so every
    liveness probe in the product reported UP for dead sessions -- status, the
    menu's "already running", the bring-up creation verify, revive, the corpse
    sweeps. With ``-t`` the answer is truthful on live and dead sockets alike.

    ``-t`` prefix-matches in tmux, which is safe here by construction: magent
    runs ONE session per socket and the session name equals the socket name, so
    no second session can share ``-L <name>`` to be matched by accident.

    ``timeout`` bounds the probe and a timed-out probe answers False. Left at
    the default the call blocks exactly as before -- only the bring-up creation
    verify passes a bound, because a wedged psmux server answers nothing at all
    and would otherwise hold the whole fan-out hostage.
    """
    binary = psmux or find_psmux()
    if not binary:
        return False
    try:
        result = subprocess.run(
            [binary, "-L", name, "has-session", "-t", name],
            capture_output=True,
            timeout=timeout,
            check=False,
            creationflags=_SPAWN_FLAGS,
        )
    except subprocess.TimeoutExpired:
        return False
    else:
        return result.returncode == 0


def _probe_live(names: list[str], binary: str, timeout: float | None) -> set[str]:
    """One fan-out pass: which of ``names`` answer ``has-session -t``.

    Every probe is spawned before any is waited on, so n sessions cost roughly
    one psmux round-trip instead of n sequential ones. A probe that could not
    be spawned, or that outran ``timeout``, counts as NOT live -- the caller
    decides whether to retry it.
    """
    procs: list[tuple[str, subprocess.Popen[bytes] | None]] = []
    for name in names:
        try:
            procs.append(
                (
                    name,
                    subprocess.Popen(
                        # `-t <name>` is load-bearing -- see ``has_session``.
                        [binary, "-L", name, "has-session", "-t", name],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=_SPAWN_FLAGS,
                    ),
                )
            )
        except OSError:
            procs.append((name, None))

    live: set[str] = set()
    for name, proc in procs:
        if proc is None:
            continue
        try:
            rc = proc.wait() if timeout is None else proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            continue
        if rc == 0:
            live.add(name)
    return live


def live_sessions(
    names: list[str],
    psmux: str | None = None,
    *,
    timeout: float | None = None,
    retries: int = 1,
) -> list[str]:
    """THE liveness enumeration: which of ``names`` are live, in input order.

    Every surface that answers "which sessions are running" goes through this
    one function -- ``psmux_status`` (and so ``magent status``, ``magent
    down``, the menu), the session picker's sweep, and the upload server's
    ``discover_sessions``. They used to each roll their own: same probe, three
    different retry policies, and therefore three different answers on the same
    machine at the same moment. The picker retried flapping probes and the
    other two did not, so ``status``/``down`` could call a session stopped that
    the picker was happily attaching to -- and ``down`` then never stopped it
    and never mentioned it (the "these stay always" bug).

    ``retries`` re-probes only the misses: under the load of many running
    agents an individual probe flaps, and a dropped probe silently HIDES a live
    session, which is the dangerous direction for a shutdown. ``retries=0`` is
    for the bring-up creation verify, which owns its own respawn-and-re-probe
    cycle and must not have a second retry folded into it.

    ``timeout`` bounds each probe; a timed-out probe counts as not live. Left
    at the default the wait is unbounded, which is what the status/down/picker
    surfaces want: a psmux server that is merely SLOW (measured at ~19s for one
    46-socket fan-out on a loaded host) must not be reported dead.
    """
    binary = psmux or find_psmux()
    if not binary or not names:
        return []
    live = _probe_live(names, binary, timeout)
    for _ in range(max(0, retries)):
        missing = [n for n in names if n not in live]
        if not missing:
            break
        live |= _probe_live(missing, binary, timeout)
    return [n for n in names if n in live]


# What the bring-up's dedupe learns about one session. "live" and "absent" are
# ANSWERS -- has-session exited 0, or exited non-zero -- and mean exactly what
# ``has_session`` means by True and False. "unknown" is the third state that
# ``has_session``/``live_sessions`` fold into "not live": the client never
# answered (or could not even be started). For a status table that fold is the
# right call; for a bring-up it is not, because "not live" leads to kill-server
# and a fresh new-session, and in the 2026-08-18 wedge the sessions that stopped
# answering were FROZEN live agents, not dead ones.
SessionState = Literal["live", "absent", "unknown"]

# The ONE exit code ``has-session`` answers "no such session" with: tmux's own
# (1), measured on psmux 3.3.x (``has-session -t <name>`` -> rc 1 after the
# session or its server is gone; every fake in the suite exits 1 the same way).
# Its other answer is 0. Anything else is not an answer about the session at
# all: a client that died abnormally (STATUS_DLL_INIT_FAILED 0xC0000142, an
# access violation, a signal), a usage error, a launcher that could not start
# it.
HAS_SESSION_ABSENT_RC = 1


def session_state_from_rc(rc: int | None) -> SessionState:
    """What one ``has-session -t`` client's exit code says about its session.

    ``None`` (the client never answered) and every code that is neither
    ``0`` nor ``HAS_SESSION_ABSENT_RC`` are ``unknown``: only a positive "no
    such session" may lead the bring-up to kill the socket and create it afresh,
    and a crashed client has said nothing about the session it was asked about
    -- on the 2026-08-18 wedge the ones that did not answer were FROZEN live
    agents. The code is compared as reported, so the unsigned NTSTATUS
    (3221225794) and its signed twin (-1073741502) are both ``unknown``.
    """
    if rc is None:
        return "unknown"
    if rc == 0:
        return "live"
    if rc == HAS_SESSION_ABSENT_RC:
        return "absent"
    return "unknown"


# How long a killed client gets to be collected. A kill cannot be refused
# (TerminateProcess / SIGKILL), so this is only the OS's own teardown -- bounded
# anyway, so the reap can never become the unbounded wait it exists to end.
_REAP_TIMEOUT_S = 5.0


def _kill_and_reap(proc: subprocess.Popen[bytes]) -> None:
    """Kill a client that outran its budget, and collect it. Never raises."""
    with contextlib.suppress(OSError):
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=_REAP_TIMEOUT_S)


def await_clients(
    procs: Sequence[subprocess.Popen[bytes] | None], timeout: float
) -> list[int | None]:
    """Wait on a fan-out of psmux clients under ONE deadline.

    Returns each client's exit code in order, or None for one that never
    answered (killed and reaped here, never left running) or was never spawned
    (a None slot). One deadline for the whole set, not one per client: the
    clients run concurrently, so a fresh timeout per ``wait`` would make N hung
    clients cost N budgets -- the ``_display_fan_out`` lesson. Past the deadline
    a client that already exited still hands over its code (a zero-length wait
    reads it); only one still running counts as unanswered.

    Output is not read here, and callers spawn with DEVNULL on purpose: a piped
    client that is killed can leave a grandchild holding the pipe, and draining
    it is exactly the unbounded wait ``probe_control_plane`` documents.
    """
    deadline = time.monotonic() + timeout
    codes: list[int | None] = []
    for proc in procs:
        if proc is None:
            codes.append(None)
            continue
        try:
            codes.append(proc.wait(timeout=max(0.0, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            _kill_and_reap(proc)
            codes.append(None)
    return codes


def probe_sessions(
    names: list[str], psmux: str, *, timeout: float
) -> dict[str, SessionState]:
    """The bring-up's dedupe: live, absent or UNKNOWN for each of ``names``.

    The same ``has-session -t`` probe as ``has_session`` -- ``-t`` is
    load-bearing, see there -- fanned out and bounded by ``await_clients``. A
    probe that times out, that could not be spawned, or that exited with
    anything but 0 or ``HAS_SESSION_ABSENT_RC`` (a crashed client) is
    "unknown", never "absent": only a positive answer that the session is not
    there may lead to killing its socket and creating it afresh.
    """
    procs: list[subprocess.Popen[bytes] | None] = []
    for name in names:
        try:
            procs.append(
                subprocess.Popen(
                    [psmux, "-L", name, "has-session", "-t", name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=_SPAWN_FLAGS,
                )
            )
        except OSError:
            procs.append(None)
    states: dict[str, SessionState] = {}
    for name, rc in zip(names, await_clients(procs, timeout), strict=True):
        states[name] = session_state_from_rc(rc)
        if rc not in (None, 0, HAS_SESSION_ABSENT_RC):
            get_logger("launch").warning(
                "has-session -t %s exited %d (%#x), which is neither 'live' (0)"
                " nor 'no such session' (%d): reading it as unknown, not absent",
                name,
                rc,
                rc & 0xFFFFFFFF,
                HAS_SESSION_ABSENT_RC,
            )
    return states


# How long the control plane gets to answer one cheap command before `magent
# doctor` calls it wedged. Deliberately short: this is a diagnostic, and the
# failure it looks for is not "slow" but "never" -- a wedged psmux answers
# nothing at all, from any console, for as long as the machine stays up.
CONTROL_PROBE_TIMEOUT_S = 5.0

# A socket name no magent session can ever have (session names come from window
# titles). The probe must not aim at a project's socket: a control command
# against a live session competes with the agent using it, and the wedge is
# machine-global anyway -- the incident's own reproduction was a command on a
# FRESH socket hanging forever.
CONTROL_PROBE_SOCKET = "magent-doctor-probe"


@dataclass(frozen=True)
class ControlProbe:
    """What one bounded control-plane command did: answered, or ran out the
    clock. ``responsive`` ignores the exit code on purpose -- "no server on
    this socket" is a perfectly healthy ANSWER, and the only thing being
    measured here is whether psmux answers at all."""

    responsive: bool
    timed_out: bool
    elapsed_s: float


def probe_control_plane(
    psmux: str | None = None, *, timeout: float = CONTROL_PROBE_TIMEOUT_S
) -> ControlProbe:
    """Is the psmux control plane answering commands at all? Bounded, one shot.

    This is a RESPONSIVENESS probe and explicitly NOT a fourth liveness sweep:
    it enumerates nothing, names no configured session, and its answer is
    "psmux replies" rather than "these sessions are live". That question has
    exactly one owner -- ``live_sessions`` -- and must keep having one.

    ``list-sessions`` is the cheapest control command that reaches the server
    layer: tmux/psmux does not START a server for it (a socket with no server
    answers "no server running" and exits non-zero, which is a fine answer
    here), so a doctor run leaves nothing behind. A version flag would be
    cheaper still and would prove nothing -- ``psmux -V`` never touches the
    ConPTY plumbing that the machine-wide wedge holds.

    Never raises and never blocks past ``timeout``: a timed-out probe is the
    finding, not an error.

    The output is DISCARDED rather than captured, and that is load-bearing on
    Windows, not a style choice. ``subprocess.run(capture_output=True,
    timeout=...)`` is NOT bounded here: on expiry it kills the direct child and
    then calls ``communicate()``, which waits for the pipe write ends to close
    -- and any grandchild the wedged client left behind still holds them. Built
    that way first, this probe took 90 s (the stalled fake's whole lifetime) to
    answer a 5 s timeout. Nothing is read from a `list-sessions` here anyway:
    the answer is "it answered", not what it said.
    """
    binary = psmux or find_psmux()
    if not binary:
        return ControlProbe(responsive=False, timed_out=False, elapsed_s=0.0)
    started = time.monotonic()
    try:
        subprocess.run(
            [binary, "-L", CONTROL_PROBE_SOCKET, "list-sessions"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            creationflags=_SPAWN_FLAGS,
        )
    except subprocess.TimeoutExpired:
        return ControlProbe(
            responsive=False, timed_out=True, elapsed_s=time.monotonic() - started
        )
    except (OSError, subprocess.SubprocessError):
        return ControlProbe(
            responsive=False, timed_out=False, elapsed_s=time.monotonic() - started
        )
    return ControlProbe(
        responsive=True, timed_out=False, elapsed_s=time.monotonic() - started
    )


# `kill-server` on this machine's psmux 3.3.6 has been observed to exit 0
# without the server dying, and a wedged server answers nothing at all -- so
# the kill is bounded (one stuck socket must not hold the whole shutdown
# hostage) and the ANSWER always comes from a re-probe, never from the rc.
_KILL_TIMEOUT_S = 10.0
# `kill-server` returns before the server is fully gone; probing at t=0 would
# report a session that is on its way out as a survivor.
_STOP_SETTLE_S = 1.0


def kill_server(name: str, psmux: str | None = None) -> bool:
    """Attempt to kill the psmux server backing a single session.

    True means the command exited 0, which is NOT the same thing as the session
    being gone (psmux 3.3.6 exits 0 for kills that do not take). Nothing in the
    product is allowed to report a shutdown off this boolean -- see
    ``stop_sessions``.
    """
    binary = psmux or find_psmux()
    if not binary:
        return False
    try:
        result = subprocess.run(
            [binary, "-L", name, "kill-server"],
            capture_output=True,
            timeout=_KILL_TIMEOUT_S,
            check=False,
            creationflags=_SPAWN_FLAGS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    else:
        return result.returncode == 0


def _kill_batch(names: list[str], binary: str) -> None:
    """Fire ``kill-server`` at every name concurrently.

    Concurrent rather than sequential because the sequential sweep was itself a
    failure mode: 46 sockets x one bounded subprocess each ran long enough that
    ``down --host``'s SSH budget could guillotine the remote shutdown partway,
    leaving exactly the un-reached tail of the config alive.
    """
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda n: kill_server(n, psmux=binary), names))


def kill_servers(names: list[str]) -> list[str]:
    """Kill multiple psmux servers. Returns the names that were attempted.

    ATTEMPT-only, and deliberately so: it cannot say what actually stopped.
    Every user-facing shutdown goes through ``stop_sessions`` instead.
    """
    binary = find_psmux()
    if not binary or not names:
        return []
    _kill_batch(names, binary)
    return list(names)


def clear_stale_servers(names: list[str], psmux: str, *, timeout: float) -> list[str]:
    """``kill-server`` every name concurrently; return the ones with no answer.

    The bring-up's step between "has-session said absent" and ``new-session``:
    a socket can hold a dead server that answers "no session" while still
    squatting the name. The exit code is deliberately ignored -- "no server
    running" (rc 1) is the normal answer for an absent name, and psmux 3.3.6
    exits 0 for kills that do not take -- so the only failure is SILENCE: a
    client that never answered within ``timeout`` (one deadline for the whole
    fan-out, killed and reaped by ``await_clients``), or one that could not be
    spawned. Those names are returned so the caller can refuse to create a
    session on top of a server it could not clear.

    Not ``kill_server``/``_kill_batch``: those answer "did rc == 0", which folds
    "no server running" into the same False as "never answered", and their
    captured pipes are not a real bound on Windows.
    """
    procs: list[subprocess.Popen[bytes] | None] = []
    for name in names:
        try:
            procs.append(
                subprocess.Popen(
                    [psmux, "-L", name, "kill-server"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=_SPAWN_FLAGS,
                )
            )
        except OSError:
            procs.append(None)
    codes = await_clients(procs, timeout)
    return [n for n, rc in zip(names, codes, strict=True) if rc is None]


def stop_sessions(
    names: list[str], psmux: str | None = None
) -> tuple[list[str], list[str]]:
    """Stop every session in ``names`` and PROVE what happened.

    Returns ``(stopped, still_running)``: the sessions that were live before
    and are verifiably gone after, and the ones that survived two kill attempts.

    This exists because ``magent down`` used to report the loop it had run
    rather than the world it had changed: ``kill_servers`` threw away every
    ``kill_server`` return value and answered with the full list of names it
    had tried, so "Stopped 46 session(s)" was printed on a machine where 11 of
    them were still alive and attachable. With psmux 3.3.6 exiting 0 for kills
    that do not take, honouring the rc would not have been enough either -- the
    only truthful answer is a re-probe, and the only useful reaction to a
    survivor is to kill it again.

    Every name is killed, including ones the liveness probe called dead:
    ``kill-server`` against a socket with no server is a harmless no-op, and a
    shutdown that skips whatever a flaky probe happened to miss is exactly the
    bug. ``before`` is what keeps the REPORT honest -- a name that was already
    dead is not claimed as a session this command stopped.
    """
    binary = psmux or find_psmux()
    if not binary or not names:
        return [], []
    log = get_logger("launch")

    before = set(live_sessions(names, psmux=binary))
    _kill_batch(names, binary)
    time.sleep(_STOP_SETTLE_S)
    alive = set(live_sessions(names, psmux=binary))
    if alive:
        log.warning(
            "kill-server did not stop %s; killing again", ", ".join(sorted(alive))
        )
        retry = sorted(alive)
        _kill_batch(retry, binary)
        time.sleep(_STOP_SETTLE_S)
        alive = set(live_sessions(retry, psmux=binary))
    if alive:
        log.error(
            "session(s) still running after two kill attempts: %s",
            ", ".join(sorted(alive)),
        )
    return (
        [n for n in names if n in before and n not in alive],
        [n for n in names if n in alive],
    )


# How long one `send-keys` may take before we stop waiting on it.
#
# This was the ONE psmux call in this module with no bound at all, and it is
# the one an HTTP request handler ran inline: an Alt+V upload was measured
# taking 74 s to answer because the control command behind it stalled while the
# session's attached terminal was busy. A control command against a loaded
# socket has been measured anywhere from 3 s to past 70 s, so the default is
# generous (a paste that arrives late is still the paste the user wanted) but
# finite (a caller must never be hostage to a wedged socket forever). Callers
# with their own budget pass `timeout=`.
#
# On expiry `subprocess.run` KILLS the client, so this is exactly one attempt
# and never a re-send: a killed `send-keys` may or may not have reached the
# server, and a retry on top of that is how the same image gets pasted twice.
SEND_KEYS_TIMEOUT_S = 20.0


def send_keys(
    name: str,
    *keys: str,
    target: str | None = None,
    literal: bool = False,
    psmux: str | None = None,
    timeout: float = SEND_KEYS_TIMEOUT_S,
) -> bool:
    """Send keystrokes to a psmux session. Returns True on success.

    Bounded and non-raising, like every other probe here: a timeout, a psmux
    that will not launch, or a socket that answers nothing all come back as
    ``False`` with a WARNING in launch.log, never as an exception on a caller
    fanning this out (or, worse, as an unbounded wait on a request handler).

    ``literal=True`` adds ``-l``, so ``keys`` are pasted as verbatim text and
    key names like ``Enter`` are NOT looked up. This is how ``magent send``
    types a prompt into an agent's input line -- and why a prompt that begins
    with ``/model`` reaches the agent as the literal slash-command it is: the
    argv is a list handed straight to psmux, never a shell, so no MSYS/Git-Bash
    path rewrite can turn ``/model`` into ``C:/Program Files/Git/model``.
    """
    binary = psmux or find_psmux()
    if not binary:
        return False
    cmd: list[str] = [binary, "-L", name, "send-keys"]
    if target:
        cmd += ["-t", target]
    if literal:
        cmd.append("-l")
    cmd.append("--")
    cmd.extend(keys)
    started = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            check=False,
            creationflags=_SPAWN_FLAGS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        get_logger("launch").warning(
            "send-keys to project=%s gave up after %.1fs: %s",
            name,
            time.monotonic() - started,
            exc,
        )
        return False
    else:
        return result.returncode == 0


def pane_cwd(name: str, psmux: str | None = None) -> str:
    """Return the current working directory of the active pane, or ``""``.

    The explicit ``-t <name>`` is REQUIRED: without it ``display-message``
    answers for the *calling client's own* pane, so a magent run from inside a
    psmux session reports its own cwd for every session it probes --
    ``capture_pane`` passes ``-t`` for the same reason.

    Guarded like the inline closure it replaced (P1-06): a 3s timeout, utf-8
    decode with ``errors="replace"``, and any OSError/SubprocessError swallowed
    to ``""`` -- a hung, unlaunchable, or non-utf-8 psmux must never propagate
    to a caller fanning this across every live session.
    """
    binary = psmux or find_psmux()
    if not binary:
        return ""
    try:
        result = subprocess.run(
            [
                binary,
                "-L",
                name,
                "display-message",
                "-t",
                name,
                "-p",
                "#{pane_current_path}",
            ],
            capture_output=True,
            timeout=3,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=_SPAWN_FLAGS,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    else:
        return (result.stdout or "").strip() if result.returncode == 0 else ""


# How long one `capture-pane` gets to answer. The bound is there for a WEDGED
# psmux, which answers nothing at all for as long as the machine stays up --
# unbounded, `peek` / `sessions --json` / `send` would hang with it. It is NOT
# a liveness verdict: a merely slow control command on a loaded box has been
# measured past 3s (see ``SEND_KEYS_TIMEOUT_S``), so running out this clock
# says "unread", never "no pane". Read at call time, so a test can widen it.
CAPTURE_PANE_TIMEOUT_S = 3.0


@dataclass(frozen=True)
class PaneCapture:
    """One ``capture-pane``: the text, and whether the clock ran out first.

    ``timed_out`` is the distinction ``capture_pane``'s bare string cannot
    carry. ``text == ""`` with ``timed_out=False`` is an ANSWER (an empty pane,
    a dead socket, an unlaunchable binary); with ``timed_out=True`` nothing is
    known about the pane at all -- the session may be live and busy.
    """

    text: str
    timed_out: bool


def read_pane(name: str, psmux: str | None = None) -> PaneCapture:
    """Capture the active pane's visible text, telling a timeout apart.

    Same guards as ``pane_cwd``: bounded (``CAPTURE_PANE_TIMEOUT_S``),
    decode-tolerant, and never raises.
    """
    binary = psmux or find_psmux()
    if not binary:
        return PaneCapture(text="", timed_out=False)
    try:
        result = subprocess.run(
            [binary, "-L", name, "capture-pane", "-p", "-t", name],
            capture_output=True,
            timeout=CAPTURE_PANE_TIMEOUT_S,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=_SPAWN_FLAGS,
        )
    except subprocess.TimeoutExpired:
        return PaneCapture(text="", timed_out=True)
    except (OSError, subprocess.SubprocessError):
        return PaneCapture(text="", timed_out=False)
    text = (result.stdout or "") if result.returncode == 0 else ""
    return PaneCapture(text=text, timed_out=False)


def capture_pane(name: str, psmux: str | None = None) -> str:
    """Return the active pane's visible text, or ``""``.

    For callers that only POLL for text to appear (a timed-out read is simply
    "not yet"). Anything that REPORTS on a pane -- a state, a delivery
    verdict, a tail -- must use ``read_pane``, because here a timeout and an
    empty pane are the same ``""``.
    """
    return read_pane(name, psmux).text


# Foreground commands that mean "this pane is sitting at a prompt with no
# agent running". ``cmd`` is deliberately NOT in this set: on Windows the agent
# launchers are .cmd shims, so cmd.exe is the foreground interpreter for the
# seconds an agent takes to boot -- calling that idle would type a second
# command into a live agent. A genuinely dead pane rests at pwsh (Windows,
# where sessions are created with a pwsh default shell) or a POSIX shell.
_IDLE_SHELLS: frozenset[str] = frozenset(
    {"pwsh", "powershell", "bash", "zsh", "fish", "sh", "dash", "nu", "ksh", "tcsh"}
)

# What magent wraps every command it types into a pane in: ``cmd /c <command>``
# (``platform/windows.py::_send_argv`` and ``revive_sessions`` here). ``cmd /c``
# exits exactly when its command does, so a live one under the pane's shell IS
# the launched command, whatever that command's own image is called -- which is
# what keeps a shipped tool with no registry image (agy, cursor-agent) from
# reading idle while it runs. The agent images still matter: a human who typed
# ``claude`` at the prompt has no cmd above it.
_LAUNCHER_IMAGES: frozenset[str] = frozenset({"cmd"})


def pane_current_commands(names: list[str], psmux: str | None = None) -> dict[str, str]:
    """Each session's pane foreground command (``pwsh``, ``claude``, ...), for
    many sessions in ONE process fan-out.

    Every probe is spawned before any is read -- the shape the picker's
    liveness sweep already uses -- so a caller building a table over 40 live
    sessions pays roughly one psmux round-trip instead of 40 sequential ones.
    Bounded and decode-tolerant: a failed, hung, or unlaunchable probe degrades
    to ``""`` for that session rather than propagating.
    """
    return _display_fan_out(names, "#{pane_current_command}", psmux)


def pane_pids(names: list[str], psmux: str | None = None) -> dict[str, int | None]:
    """``#{pane_pid}`` -- the pane's OWN process, not its foreground -- for many
    sessions in one fan-out; None where it could not be read.

    Same fan-out and guards as ``pane_current_commands`` (surrounding
    whitespace is stripped). Anything that is not then a positive integer is
    None: a caller must never walk a process tree from a pid it guessed.
    """
    out: dict[str, int | None] = {}
    for name, raw in _display_fan_out(names, "#{pane_pid}", psmux).items():
        try:
            pid = int(raw)
        except ValueError:
            pid = 0
        out[name] = pid if pid > 0 else None
    return out


# The whole pane-probe fan-out's wait budget, and how long a probe that has
# already exited may take to hand over its output once that budget is spent.
# Paid once per batch, so it is sized for a loaded host: under a spawn storm a
# single display-message runs past 3 s (see FLASH_TIMEOUT_S), and a spawn storm
# is exactly when the bring-up's send-verify reads this. Ceiling: idle_sessions
# runs two of these fan-outs back to back, so 2x this bounds idle_sessions'
# SHARE of attach's 30 s `up --json --revive` ssh read, not the read itself.
# The rest of that path has no finite bound to sum: live_sessions' sweep
# before it is unbounded on purpose (a slow server must not read dead),
# revive_sessions' has_session pool runs ceil(n/16) waves in series, and each
# send_keys after it may take SEND_KEYS_TIMEOUT_S (20 s) per pane.
_FAN_OUT_TIMEOUT_S = 10.0
_FAN_OUT_DRAIN_S = 0.1


def _display_fan_out(names: list[str], fmt: str, psmux: str | None) -> dict[str, str]:
    """``display-message -p <fmt>`` against each session's own pane, every
    probe spawned before any is read; ``""`` for any that failed.

    The explicit ``-t <name>`` is REQUIRED: without it ``display-message``
    answers for the *calling client's own* pane, and magent commands are often
    run from inside a psmux session -- ``capture_pane`` passes ``-t`` for the
    same reason.
    """
    binary = psmux or find_psmux()
    if not binary or not names:
        return dict.fromkeys(names, "")
    procs: dict[str, subprocess.Popen[str] | None] = {}
    for name in names:
        try:
            procs[name] = subprocess.Popen(
                [binary, "-L", name, "display-message", "-t", name, "-p", fmt],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
                creationflags=_SPAWN_FLAGS,
            )
        except OSError:
            procs[name] = None

    # ONE deadline for the whole fan-out, not one timeout per probe: the probes
    # all run at once, so waiting a fresh timeout on each made a hung server
    # cost N x timeout. Past it, a probe that already exited still hands over
    # its output (a zero read budget can time out before the pipe is drained);
    # one still running is unknown and killed unread.
    deadline = time.monotonic() + _FAN_OUT_TIMEOUT_S
    out: dict[str, str] = {}
    for name, proc in procs.items():
        if proc is None:
            out[name] = ""
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0 and proc.poll() is None:
            proc.kill()
            out[name] = ""
            continue
        try:
            stdout, _ = proc.communicate(timeout=max(remaining, _FAN_OUT_DRAIN_S))
        except subprocess.SubprocessError:
            proc.kill()
            out[name] = ""
        else:
            out[name] = (stdout or "").strip() if proc.returncode == 0 else ""
    return out


def image_stem(raw: str) -> str:
    """``C:\\x\\PWSH.EXE`` -> ``pwsh``: the leaf name, lower-cased, ``.exe``
    dropped -- the one spelling foreground readings and process image names are
    both compared in. Public because ``reap.py`` (and the real-psmux platform
    test) compare image names in exactly this spelling."""
    leaf = raw.strip().replace("\\", "/").rsplit("/", 1)[-1].lower()
    return leaf.removesuffix(".exe")


def is_idle_command(raw: str) -> bool:
    """True when a ``#{pane_current_command}`` reading (or a process image
    name) is a bare shell.

    A HINT, never a verdict: psmux reports the pane's foreground DESCENDANT, so
    a live agent running its Bash tool reads ``bash`` -- ``idle_sessions`` is
    the only place a pane is called idle, and this is one of its conditions.
    An empty or unreadable reading is False on purpose.
    """
    stripped = raw.strip()
    if not stripped:
        return False
    return image_stem(stripped) in _IDLE_SHELLS


def pane_trees(
    names: list[str], psmux: str | None = None
) -> dict[str, list[tuple[str, int, int]] | None]:
    """Each session's pane process SUBTREE (root first), or None when unknown.

    ONE ``pane_pids`` fan-out and ONE ``snapshot_processes`` for the whole
    call -- the shape ``idle_sessions`` needs, factored out so the reaper (R4)
    reads a pane's tree the same single way. None for a name means its pid was
    unreadable, or the snapshot failed, or the pid was not in the snapshot; a
    caller must treat None as unknown, never as 'nothing runs there'.
    """
    if not names:
        return {}
    # In-body, like every procs/sessions use in this module: keeps this leaf
    # importing only magent.log at load time.
    from magent.procs import process_tree, snapshot_processes

    pids = pane_pids(names, psmux=psmux)
    snapshot = snapshot_processes()
    out: dict[str, list[tuple[str, int, int]] | None] = {}
    for name in names:
        pid = pids.get(name)
        out[name] = (
            process_tree(pid, snapshot)
            if pid is not None and snapshot is not None
            else None
        )
    return out


def _console_veto(
    tree: list[tuple[str, int, int]], members: frozenset[int] | None
) -> str | None:
    """Why a pane whose tree looked idle must NOT be typed into, or None when
    its console holds exactly its own subtree. ``members`` is the pane root's
    console-client set from ``procs.console_clients``; ``tree`` is the pane's
    subtree (root first)."""
    if members is None:
        return "console clients unreadable"
    subtree = {pid for _img, pid, _ppid in tree}
    if tree[0][1] not in members:
        return "the pane shell is not on its own console"
    outside = sorted(members - subtree)
    if outside:
        joined = ", ".join(str(p) for p in outside)
        return f"process(es) {joined} outside the pane's tree share its console"
    return None


# pane name -> the (pane pid, console-veto reason) last logged for it. The veto
# is logged ONCE per episode within one process: a long-lived caller asks again
# (the menu redraws its status table), and a pane whose console can never be
# read (a higher integrity level) would otherwise append the same WARNING
# forever. The pane reading idle ends its episode; a changed reason (a
# different outside pid) or a pane recreated under the same name is news.
_console_vetoes_logged: dict[str, tuple[int, str]] = {}


def idle_sessions(
    names: list[str],
    psmux: str | None = None,
    *,
    foreground: Mapping[str, str] | None = None,
    images: frozenset[str] | None = None,
    vetoed: dict[str, str] | None = None,
) -> set[str]:
    """The sessions among ``names`` whose agent is POSITIVELY gone: the pane
    rests at its shell with no agent anywhere under it.

    THE one answer to "is this session's agent alive". ``revive_sessions``,
    the bring-up's send-keys verification and status's idle column all read
    it, because on a yes each of them types into the pane or tells the user
    they may.

    ``#{pane_current_command}`` cannot say so on its own. psmux reports the
    pane's foreground DESCENDANT, so while Claude Code runs a tool the reading
    is ``bash`` (its Bash tool), ``pwsh``, ``grep`` or an MCP server -- with
    claude.exe alive under the pane (measured live: 4 of 31 sessions read that
    way, and revive would have typed ``cmd /c claude --continue`` + Enter into
    each). A yes therefore needs all four of:

    1. the foreground reading is a bare shell (``is_idle_command``) -- still a
       necessary condition, since a pane in the user's own program is not a
       pane at its prompt either, and a cheap filter: a session that fails it
       costs no further probe;
    2. the pane's own process (``#{pane_pid}``) was read, is in the process
       snapshot, and is itself a shell;
    3. nothing in that process's subtree is an agent image
       (``sessions.agent_image_names``) or a live launcher
       (``_LAUNCHER_IMAGES`` -- the ``cmd /c`` magent typed, alive exactly as
       long as the tool it started, registry image or not);
    4. the pane's CONSOLE holds exactly that subtree (``procs.console_clients``,
       checked last and only for the panes that passed 1-3): an agent orphaned
       out of the pane's process tree can still read the pane's console, and
       anything typed into the pane would land in it.

    Anything unknown is a no -- an unreadable pid, a failed snapshot (always,
    off Windows), a pane process gone by the time of the snapshot: never inject
    keystrokes into a pane whose state we could not establish.

    Batched: one ``pane_pids`` fan-out and ONE process snapshot for the whole
    call, paid only when some reading is a shell, and ONE console-helper spawn,
    paid only when some pane passed the tree stages. A caller that already
    holds the foreground readings (status's table) passes them as
    ``foreground`` instead of paying for that fan-out twice; ``images`` ADDS
    image names, in any spelling (``image_stem`` normalizes them), to the
    registry's agent-image set and the launcher -- so it can only make the
    verdict safer, never hide a registry agent. A caller that must report WHY a
    shell-resting pane was refused passes ``vetoed``, which receives
    ``{name: reason}`` for every console-stage veto.
    """
    readings = (
        foreground
        if foreground is not None
        else pane_current_commands(names, psmux=psmux)
    )
    shells = [name for name in names if is_idle_command(readings.get(name, ""))]
    if not shells:
        return set()

    from magent.sessions import agent_image_names

    running = (
        agent_image_names()
        | {image_stem(image) for image in images or ()}
        | _LAUNCHER_IMAGES
    )
    trees = pane_trees(shells, psmux=psmux)
    candidates: list[tuple[str, list[tuple[str, int, int]]]] = []
    for name in shells:
        tree = trees.get(name)
        if not tree or not is_idle_command(tree[0][0]):
            continue
        if any(image_stem(image) in running for image, _pid, _ppid in tree):
            continue
        candidates.append((name, tree))
    if not candidates:
        return set()

    # The image check on console clients is dropped on purpose: every client
    # inside the subtree already passed the running-image check above, and the
    # veto rejects any client outside it.
    from magent.procs import console_clients

    roots = [tree[0][1] for _name, tree in candidates]
    clients = console_clients(roots)
    idle: set[str] = set()
    for name, tree in candidates:
        reason = _console_veto(tree, clients.get(tree[0][1]))
        if reason is None:
            idle.add(name)
            _console_vetoes_logged.pop(name, None)
            continue
        if vetoed is not None:
            vetoed[name] = reason
        if _console_vetoes_logged.get(name) != (tree[0][1], reason):
            _console_vetoes_logged[name] = (tree[0][1], reason)
            get_logger("launch").warning("pane %s is not proven idle: %s", name, reason)
    return idle


# How long one status-line flash may take before we give up on it.
#
# Measured, not guessed: on an idle socket a `display-message` costs 60-130 ms
# and the attached client repaints within another ~80 ms. Under real load
# (dozens of live sessions, a discovery fan-out and a spawn storm competing for
# Cygwin process creation) the SAME command routinely ran past 3 s -- and the
# old 3 s bound did not merely time the wait out, it KILLED the child, so the
# message never reached the bar at all. Every "status-line flash failed ...
# timed out after 3 seconds" line in upload.log is one press whose feedback the
# product threw away on purpose. The wait is affordable: it happens on an HTTP
# handler thread, never on the press itself, and it is what keeps a project's
# phase messages in the order they were sent.
FLASH_TIMEOUT_S = 20.0


def flash_message(
    name: str,
    message: str,
    duration_ms: int,
    *,
    style: str | None = None,
    psmux: str | None = None,
) -> None:
    """Flash a transient message in the session's psmux status line.

    Non-disruptive — ``display-message`` repaints the status bar (immediately:
    it sets the client's message and marks the status line for redraw, it does
    not wait for the `status-interval` tick), not the agent pane. Never raises.

    Returns only when the message has actually been handed to psmux, so callers
    that flash a SEQUENCE get it on screen in order.
    """
    binary = psmux or find_psmux()
    if not binary:
        return
    cmd: list[str] = [binary, "-L", name]
    if style:
        cmd += ["set", "-g", "message-style", style, ";"]
    cmd += ["display-message", "-d", str(duration_ms), message]
    started = time.monotonic()
    try:
        subprocess.run(
            cmd,
            capture_output=True,
            timeout=FLASH_TIMEOUT_S,
            check=False,
            creationflags=_SPAWN_FLAGS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        get_logger("upload").warning(
            "status-line flash failed for project=%s after %.1fs: %s",
            name,
            time.monotonic() - started,
            exc,
        )


# The hint line every magent session advertises in its psmux status bar.
# Defined once here so the launch path and the `up` path can never drift.
# Each key name is its own badge (bold + accent, same `#[...]` idiom as
# _STATUS_BRAND below) so "F1"/"F2" read as keys rather than as stray words, and
# each label says what the key actually does: the old " F1 picker  F2 code " was
# four bare tokens with nothing marking which half was the key. Both labels are
# capitalized so the two halves match register.
#
# The hint is deliberately pure ASCII. An earlier revision fronted the picker
# label with U+2630 (the menu hamburger), and its East-Asian *ambiguous* width
# bit for real: psmux counts it as one cell, Windows Terminal draws it as two,
# so every cell after it shifted right -- a stray highlighted cell inside the
# bar and, when the shift spilled the last column, a wrapped phantom row under
# the status line. A status bar is exactly the place where the renderer's and
# the multiplexer's width arithmetic must agree, so: no ambiguous-width glyphs
# here, ever. `</>` is the universal "code" mark for the VS Code half -- three
# plain ASCII cells, zero font support required.
#
# The two halves are separate literals because only F1 is unconditionally
# true. F1 is a psmux binding this module installs on the session's own
# server, so it works for any viewer of that session. F2 is handled by the
# magent hotkey listener, which shells out to `code` -- on a machine with no
# VS Code that half advertises a key that does nothing, so it is only emitted
# when `code` resolves (see `status_hints`).
_STATUS_HINTS_F1 = "#[bold,fg=cyan] F1 #[default]Proj. Picker "

_STATUS_HINTS_F2 = "#[bold,fg=cyan] F2 #[default]</> VS Code "

# Two more spaces between the halves: each half already carries one trailing
# space, so the seam reads as the same 3-column gap it always has.
_STATUS_HINTS_GAP = "  "

_STATUS_HINTS = _STATUS_HINTS_F1 + _STATUS_HINTS_GAP + _STATUS_HINTS_F2

# ...and the width budget has to travel with it, exactly like the brand's below.
# tmux truncates status-right at `status-right-length` (default 40, but a
# personal conf may set it far tighter), so the now-wider hint can render
# mid-label. Style directives don't count toward the limit; what's left --
# " F1 ", "Proj. Picker", the gap, " F2 ", "</>" and " VS Code " -- is
# 4 + 12 + 3 + 4 + 3 + 9 = 35 columns, every one a single unambiguous cell.
# 40 carries that plus headroom for a label tweak.
_STATUS_HINTS_LEN = "40"

# The F1-only variant needs its own budget: leaving 40 here would be harmless
# on paper but wrong in spirit -- the number is documentation of the text it
# guards. Visible cells are " F1 " + "Proj. Picker" + the half's own trailing
# space = 4 + 12 + 1 = 17, and 22 carries that plus the same 5 columns of
# headroom the full hint's 40 gives its 35.
_STATUS_HINTS_F1_LEN = "22"

# The product's own status-left brand, same plainness as the hints: one word,
# one accent. magent *owns* this per session rather than inheriting whatever a
# personal ~/.tmux.conf set, so every magent window reads the same.
_STATUS_BRAND = "#[bold,fg=green] magent #[default]"

# ...and the width budget has to travel with it. tmux truncates status-left at
# `status-left-length` (default 10, but a personal conf may set it far tighter),
# so setting the brand without the length can render it mid-word. Style
# directives don't count toward the limit; " magent " is 8 cells, and the
# length carries 2 more of headroom -- the 10 every local session has always
# been given.
_STATUS_BRAND_CELLS = 8
_STATUS_LEFT_HEADROOM = 2

# What a raw F2 says when it actually reaches psmux. See `decoration_argv` for
# why this can never double-fire on a Windows attach window. Pure ASCII for the
# same reason the hints are (this text lands in the status line via
# display-message), and one line: display-message truncates at the bar's width.
_F2_FALLBACK_MSG = (
    "F2 opens VS Code only from a magent window on Windows"
    " (hotkey listener not running in this client)"
)


# The status-bar window entry is the NAME alone: the default tmux format is
# `#I:#W#F`, and with one window per session (magent's invariant) the `0:`
# index is pure noise stealing bar columns from the name. Verified live on
# psmux 3.3.8: `set -g window-status-format "#W"` renders exactly the name.
WINDOW_STATUS_FORMAT = "#W"

# ...and the name itself is width-budgeted like every other bar element. A
# 30-char project name eats the whole bar; longer than this renders as the
# first 13 chars + "..." (ASCII-only, same law as the hints -- an
# ambiguous-width glyph desyncs psmux's and the terminal's cell arithmetic).
_WINDOW_NAME_MAX = 16


def window_display_name(name: str) -> str:
    """The status-bar window name for session ``name``.

    Whole when it fits ``_WINDOW_NAME_MAX`` columns; otherwise truncated with
    a trailing ``...`` so the bar SHOWS it was cut rather than silently
    clipping mid-word. Display-only: the session name, socket name, window
    titles and every probe keep the full name -- nothing matches on the psmux
    window name (magent owns it precisely so nothing has to).
    """
    if len(name) <= _WINDOW_NAME_MAX:
        return name
    return name[: _WINDOW_NAME_MAX - 3] + "..."


def code_on_path() -> bool:
    """True when VS Code's ``code`` launcher resolves on THIS machine.

    The single owner of that probe, so the ``"code"`` literal and the hotkey
    listener's own ``shutil.which("code")`` in ``hotkey.py::_do_open_code``
    can't drift into advertising a key the listener would then refuse.
    """
    return shutil.which("code") is not None


def status_hints(code_hint: bool) -> tuple[str, str]:
    """The status-right text and its width budget, as a pair.

    Returned together because they are one decision: the budget documents the
    text it guards, and setting one without the other lets a personal
    ``~/.tmux.conf`` truncate the hint mid-label.

    ``code_hint`` is whether the F2 half is truthful here -- see
    ``decoration_argv``. Every variant is pure ASCII by construction (both
    halves are), which is load-bearing: an ambiguous-width glyph in a status
    bar desyncs psmux's and Windows Terminal's cell arithmetic.
    """
    if code_hint:
        return _STATUS_HINTS, _STATUS_HINTS_LEN
    return _STATUS_HINTS_F1, _STATUS_HINTS_F1_LEN


def _check_brand_nick(nick: str) -> None:
    """Refuse a nick the status line cannot carry verbatim. The brand is a tmux
    FORMAT string, so a ``#`` would be expanded on every redraw (``#(cmd)`` runs
    a command, ``#[...]`` restyles the bar), and a non-ASCII or non-printable
    glyph (a raw newline, a tab, a control character) breaks the "cells ==
    len" law. Config validates nicks, but the typed view is lenient and not
    every caller's nick went through ``settings.nodes``."""
    if not nick or not nick.isascii() or not nick.isprintable() or "#" in nick:
        msg = (
            f"status brand nick must be non-empty printable ASCII without '#': {nick!r}"
        )
        raise ValueError(msg)


def _brand_cells(nick: str | None) -> int:
    """The brand's visible width in cells for ``nick`` (see ``status_brand``)."""
    if nick is None:
        return _STATUS_BRAND_CELLS
    _check_brand_nick(nick)
    return _STATUS_BRAND_CELLS + len(f"@{nick} ")


def status_brand(nick: str | None) -> tuple[str, str]:
    """The status-left brand and its width in cells. ``None`` is a session on
    THIS machine: today's brand, byte for byte. A nick is a session running on
    that pool machine (the nodes feature), branded ``magent @<nick>`` so a
    window says where its agent actually is. ASCII only, same law as the hints:
    the cell count is ``len``, and a wide glyph here would desync the bar.

    Raises ``ValueError`` for an empty, non-ASCII or ``#``-bearing nick."""
    cells = _brand_cells(nick)
    if nick is None:
        return _STATUS_BRAND, str(cells)
    return _STATUS_BRAND + f"@{nick} ", str(cells)


def status_left(nick: str | None) -> tuple[str, str]:
    """``status-left`` and ``status-left-length`` for a session: the brand and
    its cells plus the headroom every session gets. The one place both
    multiplexers read it from, so a psmux bar and a node's tmux bar cannot
    budget the brand differently."""
    brand, _ = status_brand(nick)
    return brand, str(_brand_cells(nick) + _STATUS_LEFT_HEADROOM)


def f2_binding_argv(prefix: list[str], code_hint: bool) -> list[str]:
    """The F2 half of a decoration, after ``prefix`` (``[psmux, "-L", name]``
    here, ``[tmux, "-L", "magent"]`` on a node). See ``decoration_argv`` for why
    an advertised F2 binds a fallback message and an unadvertised one is
    unbound."""
    if code_hint:
        return [*prefix, "bind", "-n", "F2", "display-message", _F2_FALLBACK_MSG]
    return [*prefix, "unbind-key", "-n", "F2"]


def decoration_argv(
    name: str, psmux: str, code_hint: bool, *, nick: str | None = None
) -> list[list[str]]:
    """The psmux commands that brand ``name`` and advertise its window hotkeys.

    Ten of them. The first six: magent *owns* F1 -> detach-client per session
    (the hint has to be truthful on a machine with no personal ``bind -n F1``
    in ~/.tmux.conf, and owning the binding keeps the existing "back to the
    picker" semantics rather than changing them), the status-right carries
    the hint text plus the width budget it needs, the status-left carries
    the product brand plus the width budget *it* needs, and the sixth is the
    F2 fallback below. The last four own the window name and its status-bar
    entry (see the inline comments). Each half sets its text and its length
    together or neither: a personal conf with
    a tighter ``status-*-length`` would truncate the other half mid-label. All
    are ``-L <name>``-scoped, so they land on that session's own server and
    override whatever its tmux.conf set at start-up.

    Split out from ``decorate_session`` so the launch path can fan the same
    argvs out as raw Popens while callers with one session run them inline.

    ``code_hint`` gates the F2 half of the status-right and has no default:
    every call site has to decide. F1 (detach -> back to the picker) is a
    host-side psmux binding installed right here, so it is true for any viewer
    of the session; F2 is handled by the magent hotkey listener, which needs
    ``code`` on PATH, so its half is advertised only when ``code`` resolves on
    the machine doing the decorating. tmux's status line is session-scoped, not
    per-client, so a single answer per session is all the protocol allows --
    a per-viewer hint is out of scope, not an oversight.

    ...which is exactly why the sixth command exists. The hint is one answer for
    every viewer, but F2 itself is NOT: it is handled by the Windows hotkey
    listener, which intercepts the key in a magent-titled window and swallows it
    (``hotkey.py::_hook_decide`` returns 1, so the keystroke never reaches the
    terminal). Any other viewer of the same session -- Termius, a phone SSH app,
    a plain ``ssh`` from another box -- has no listener, so F2 fell through to
    the pane and died silently while the bar still advertised it. So when the F2
    half is advertised, magent also binds F2 on the session's own server to a
    ``display-message`` explaining the situation: that binding can only fire for
    a viewer where the key was going to be lost anyway, because the listener
    swallows it first everywhere else. When the F2 half is NOT advertised the
    sixth command is the matching ``unbind-key``, so a session that was
    decorated back when ``code`` resolved on this host doesn't keep answering a
    key nothing advertises any more.

    ``nick`` brands the status-left ``magent @<nick>`` (``status_left``); ``None``
    is the plain brand, byte for byte, so no caller that has no nick changes.
    """
    hints, hints_len = status_hints(code_hint)
    brand, brand_len = status_left(nick)
    f2 = f2_binding_argv([psmux, "-L", name], code_hint)
    return [
        [psmux, "-L", name, "bind", "-n", "F1", "detach-client"],
        [psmux, "-L", name, "set", "-g", "status-right", hints],
        [psmux, "-L", name, "set", "-g", "status-right-length", hints_len],
        [psmux, "-L", name, "set", "-g", "status-left", brand],
        [psmux, "-L", name, "set", "-g", "status-left-length", brand_len],
        f2,
        # The window NAME is magent's too (same doctrine as window titles):
        # psmux's automatic-rename shows the pane's current command, so the bar
        # read "0:claude.exe.old" after Claude Code's self-update renamed its
        # own binary -- an implementation detail of the pane's process, not
        # what the user is working on. The rename is idempotent, sticks across
        # command changes (verified live on psmux 3.3.8), and self-repairs on
        # every decoration pass; the explicit automatic-rename off is belt and
        # braces for a psmux that ever starts re-renaming. The rename target
        # stays the SESSION name (`-t name` resolves the session's current
        # window whatever it is called), so re-decorating an already-truncated
        # window still lands.
        [psmux, "-L", name, "rename-window", "-t", name, window_display_name(name)],
        [psmux, "-L", name, "set", "-g", "automatic-rename", "off"],
        # ...and the entry renders as the name alone: no `0:` index (one
        # window per session makes it noise), no flags suffix.
        [psmux, "-L", name, "set", "-g", "window-status-format", WINDOW_STATUS_FORMAT],
        [
            psmux,
            "-L",
            name,
            "set",
            "-g",
            "window-status-current-format",
            WINDOW_STATUS_FORMAT,
        ],
    ]


def decorate_session(
    name: str,
    psmux: str | None = None,
    code_hint: bool | None = None,
    nick: str | None = None,
) -> None:
    """Brand one session's status line and advertise its F1/F2 hints.

    ``code_hint=None`` means "probe here": this machine is decorating, so
    whether ``code`` resolves here is exactly the question. Callers that
    already probed (``decorate_sessions``) pass the answer down instead.
    ``nick`` is the brand nick (see ``decoration_argv``).

    Best-effort and guarded exactly like ``flash_message``: a status bar is
    cosmetic, so a missing binary, a hung psmux, or a non-zero exit is logged
    and swallowed -- never propagated into a bring-up.
    """
    binary = psmux or find_psmux()
    if not binary:
        return
    if code_hint is None:
        code_hint = code_on_path()
    for cmd in decoration_argv(name, binary, code_hint, nick=nick):
        try:
            subprocess.run(
                cmd,
                capture_output=True,
                timeout=3,
                check=False,
                creationflags=_SPAWN_FLAGS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            get_logger("launch").warning(
                "status-line decoration failed for session=%s: %s", name, exc
            )


def decorate_sessions(
    names: list[str],
    code_hint: bool | None = None,
    *,
    nicks: Mapping[str, str] | None = None,
) -> list[str]:
    """Decorate many sessions concurrently. Returns the names attempted.

    Each session is its own psmux server, so the round-trips per name
    would otherwise serialize across a large config -- same fan-out shape as
    ``revive_sessions``.

    The ``code`` probe is resolved ONCE for the whole batch and passed down:
    the answer is a property of this machine, not of a session, so a
    per-session ``shutil.which`` would be one filesystem sweep per name for
    one shared answer.

    ``nicks`` maps a session name to its brand nick; a name it does not list
    gets the plain brand.
    """
    from concurrent.futures import ThreadPoolExecutor

    binary = find_psmux()
    if not binary or not names:
        return []
    hint = code_on_path() if code_hint is None else code_hint
    brand = nicks or {}
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(
            pool.map(
                lambda n: decorate_session(
                    n, psmux=binary, code_hint=hint, nick=brand.get(n)
                ),
                names,
            )
        )
    return list(names)


# Runtime state, not config: the stamp lives beside the agent-state store and
# the logs under ~/.magent/ (same home as log.LOG_DIR / agent_state.STATE_DIR).
# It is a cache -- deleting it only costs one extra decoration pass.
DECOR_STAMP = Path.home() / ".magent" / "state" / "decor.stamp"

# How long a fired decoration pass counts as fresh. `magent attach` polls
# `up --json` up to ~20 times while it waits for a bring-up to stabilize, and
# each poll would otherwise fire six psmux commands per session (240+ processes
# against a host that is already busy). A minute is far longer than any single
# stabilization loop and far shorter than "the user changed something and
# re-attached", and newborn sessions never depend on it -- the launch/revive
# path decorates those directly at creation.
DECOR_TTL_S = 60.0


# How far "in the future" a stamp may read before the clock is called bad.
#
# The stamp's age is a difference between two DIFFERENT readings of the same
# wall clock -- `time.time()` and a filesystem mtime -- so it can come out
# slightly negative for an instant that is genuinely in the past. Measured on
# Windows/CPython 3.10, 3000 create-then-read cycles: 10.2% of them read the
# stamp as 2.384185791015625e-07s (exactly one float ULP at the current epoch)
# in the FUTURE, because `os.stat` builds st_mtime as `sec + 1e-9*nsec` while
# `time.time()` divides an integer nanosecond count -- two roundings of one
# instant. Under CPython 3.13+ the same loop never goes negative (`time.time()`
# moved to GetSystemTimePreciseAsFileTime, so the read is microseconds LATER
# than the 15.625ms-granular mtime instead of exactly equal to it).
#
# Two seconds also covers the coarse end of the real spread -- one Windows
# clock tick is 15.625ms, and FAT/exFAT store mtimes at 2s granularity -- while
# staying 1/30 of the TTL, so a clock that really is wrong still cannot switch
# decoration off for meaningfully longer than one TTL.
_STAMP_FUTURE_SLOP_S = 2.0


def _decor_stamp_fresh() -> bool:
    """True when a decoration pass ran within ``DECOR_TTL_S``.

    A missing/unreadable stamp answers False (decorate), and so does a stamp
    dated in the future by more than ``_STAMP_FUTURE_SLOP_S``: a bad clock must
    not be able to switch decoration off for longer than the TTL.
    """
    try:
        age = time.time() - DECOR_STAMP.stat().st_mtime
    except OSError:
        return False
    return -_STAMP_FUTURE_SLOP_S <= age < DECOR_TTL_S


def _touch_decor_stamp() -> None:
    """Mark a decoration pass as just-fired. Best-effort: a read-only home
    costs an un-throttled decoration, never an error."""
    with contextlib.suppress(OSError):
        DECOR_STAMP.parent.mkdir(parents=True, exist_ok=True)
        DECOR_STAMP.touch()


def decorate_sessions_async(
    names: list[str],
    code_hint: bool | None = None,
    *,
    nicks: Mapping[str, str] | None = None,
) -> list[str]:
    """Fire the decoration commands and return WITHOUT waiting for any of them.

    The status-query variant of ``decorate_sessions``. That one runs each
    session's argvs serially under ``subprocess.run(..., timeout=3)``, so a host
    whose psmux servers are busy enough to time out pays 15s per session -- and
    `up --json` (the host side of `magent attach`) called it synchronously, so
    a 40-session config could spend ~45s decorating a status bar before printing
    a byte of JSON. The attach client's status timeout fired at 30s and retried
    with a 120s one, re-running the whole thing. Decoration is cosmetic and has
    always been best-effort, so the status path must never wait on it at all.

    The argvs for one session are order-independent (a ``bind``, four
    ``set -g``, and the F2 bind/unbind), so everything goes out at once as raw
    Popens -- same shape the launch path already uses for a fresh batch
    (``platform/windows.py::WindowsPlatform._decorate_batch``), minus the wait.
    All three stdio handles go to DEVNULL, which is load-bearing rather than
    tidy: this command is usually running under ``ssh``, and a child holding an
    inherited pipe open would keep that channel open after the JSON was printed.

    Throttled by ``DECOR_STAMP``: returns ``[]`` without firing anything when a
    pass ran less than ``DECOR_TTL_S`` ago, so attach's repeated status polls
    can't pile up hundreds of orphan processes against a wedged psmux server.

    ``nicks`` maps a session name to its brand nick, as in ``decorate_sessions``.

    Returns the names it fired for (``[]`` when throttled or unable to run).
    """
    binary = find_psmux()
    if not binary or not names:
        return []
    if _decor_stamp_fresh():
        return []
    hint = code_on_path() if code_hint is None else code_hint
    brand = nicks or {}
    log = get_logger("launch")
    fired: list[str] = []
    for name in names:
        try:
            for cmd in decoration_argv(name, binary, hint, nick=brand.get(name)):
                subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=_SPAWN_FLAGS,
                )
        except OSError as exc:
            # Same posture as decorate_session's: cosmetic, so a session whose
            # commands can't even be spawned is logged and skipped.
            log.warning(
                "status-line decoration could not be spawned for %s: %s", name, exc
            )
        else:
            fired.append(name)
    _touch_decor_stamp()
    log.info("fired status-line decoration for %d session(s)", len(fired))
    return fired


def detach_client(name: str, psmux: str | None = None) -> bool:
    """Detach the client attached to ``name``. Returns True on success."""
    binary = psmux or find_psmux()
    if not binary:
        return False
    try:
        result = subprocess.run(
            [binary, "-L", name, "detach-client"],
            capture_output=True,
            timeout=3,
            check=False,
            creationflags=_SPAWN_FLAGS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    else:
        return result.returncode == 0


def socket_id(session_dict: dict[str, object]) -> str:
    """The psmux socket id for a session dict: ``session`` key when present,
    else ``name``."""
    return str(session_dict.get("session") or session_dict.get("name") or "")


def _field_str(d: dict[str, object], key: str) -> str:
    """A descriptor dict's string field (narrows dict[str, object] to str)."""
    value = d.get(key, "")
    return value if isinstance(value, str) else ""


def eligible_projects(
    config: MagentConfig,
    group: str | None = None,
    *,
    config_dirs: Mapping[str, Path] | None = None,
) -> list[dict[str, object]]:
    """Projects that map to a persistent psmux session.

    A project is eligible when it is enabled, runs a CLI agent (not an IDE),
    and is local (no ``host``, no pool ``node``). When ``group`` is given,
    only projects tagged with that group (case-insensitive) are returned.

    The ``cmd`` each entry carries is fresh-start aware: a project directory
    with no stored session for its tool gets the configured command WITHOUT its
    implicit-resume flag, because ``claude --continue`` in such a directory
    errors out and leaves the pane at a dead shell. Every consumer of that key
    -- ``bring_up``, ``revive_sessions``, and the ``up --json`` payload the
    attach client spawns no-mux windows from -- inherits the decision, and all
    of them run the command on THIS machine, the one just probed (remote
    projects are excluded above, so the probe never answers for a foreign
    filesystem).

    ``config_dirs`` names, per psmux session id, WHICH of the tool's stores
    that project's probe must read -- the config directory its pane will run
    under. It is keyed by session id (not by path) because that is the key the
    rest of the product already uses for a project. A session absent from the
    mapping, and the default None, both mean the tool's own default store,
    which is byte-for-byte today's probe for every project.

    A cloud project's ``cmd`` is the one ``claude --cloud "<task>"`` it runs
    (never a resume command, never ``build_start_command``'s). One that cannot
    start a session -- no task, a tool or wrapper that is not claude, an
    executable that cannot be typed -- carries ``cmd == ""`` and the reason in
    ``cmd_why``, so every consumer that already reads an empty command as
    "nothing to run" stays correct and the surfaces that can name the reason do.
    Each entry carries ``node`` (``"cloud"`` or None).
    """
    from magent.config import is_cloud, runs_on_node
    from magent.launch import _expand_base_dir, _resolve_path
    from magent.sessions import build_start_command, is_ide_tool
    from magent.titles import get_leaf_name

    base_dir = config.base_dir
    if base_dir:
        base_dir = _expand_base_dir(base_dir)

    out: list[dict[str, object]] = []
    seen: set[str] = set()
    for proj in config.projects:
        if not proj.enabled:
            continue
        if group and (not proj.group or proj.group.lower() != group.lower()):
            continue
        tool = proj.tool or config.settings.default_tool
        if is_ide_tool(tool):
            continue
        if proj.host:
            continue
        # A pool-node project runs on that node's tmux, never in a local psmux
        # session (PR-D). A cloud project is a local pane and stays eligible
        # (DECISION-15).
        if runs_on_node(proj):
            continue
        leaf = proj.title or get_leaf_name(proj.path)
        sid = session_name(leaf)
        # One session id, one entry: duplicate config entries for a project
        # produced two identical status rows, hence two identically-titled
        # attach windows -- and the second could never be tiled, since both
        # resolve to the same window handle. First occurrence wins.
        if sid in seen:
            continue
        seen.add(sid)
        resolved = _resolve_path(proj.path, base_dir)
        cmd_why = ""
        if is_cloud(proj):
            cmd, cmd_why = _cloud_command(
                tool, config.settings.tools.get(tool, ""), proj.cloud_task
            )
        else:
            cmd = build_start_command(
                tool,
                config.settings.tools.get(tool, ""),
                resolved,
                config_dir=config_dirs.get(sid) if config_dirs else None,
            )
        row: dict[str, object] = {
            "name": leaf,
            "session": sid,
            "path": proj.path,
            "tool": tool,
            "group": proj.group,
            "resolved": resolved,
            "cmd": cmd,
            "color": proj.color,
            "node": proj.node,
        }
        if cmd_why:
            row["cmd_why"] = cmd_why
        out.append(row)
    return out


def _cloud_command(tool: str, base_cmd: str, task: str | None) -> tuple[str, str]:
    """``(command, "")`` for a cloud pane, or ``("", why)`` when it has none.

    The same three refusals, in the same order, as the create gate and the
    launch path (``launch.cloud_tool_refusal``, then the task, then the typing
    check), so an empty command is never a bare ``bash --cloud "t"`` and no
    surface words the reason differently."""
    from magent.launch import NO_CLOUD_TASK, cloud_tool_refusal
    from magent.sessions.claude import cloud_pane_command

    refusal = cloud_tool_refusal(tool, base_cmd)
    if refusal:
        return "", refusal
    if not task:
        return "", NO_CLOUD_TASK
    try:
        return cloud_pane_command(base_cmd, task), ""
    except ValueError as exc:
        return "", str(exc)


def _down_reason(binary: str | None, project: dict[str, object]) -> str:
    """Name the one thing that keeps ``project`` from ever being probed."""
    if not binary:
        return "psmux not installed"
    if not project["resolved"]:
        return "folder not found"
    # A cloud pane's command is withheld for a specific reason; "no agent
    # command" would send the user after a setting that is not the problem.
    cmd_why = project.get("cmd_why")
    if isinstance(cmd_why, str) and cmd_why:
        return cmd_why
    return "no agent command"


def psmux_status(
    config: MagentConfig, group: str | None = None
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    """Return ``(up, down, all_projects)`` for eligible projects.

    A project that never gets probed at all (no psmux binary, a path that does
    not resolve on this machine, or no agent command) carries a ``reason`` on
    its down entry. Without it such a project reports down forever with zero
    explanation -- ``bring_up`` skips exactly the same projects silently, so
    "it says down and `up` does nothing" was the only symptom. Sessions that
    DID get probed and simply failed ``has-session`` stay reason-less: that is
    ordinary down, and nothing to explain.

    Precedence is binary-first: a missing psmux is a machine-wide blocker that
    makes every other reason moot, so naming it once beats telling the user
    about a folder they would still not be able to launch.

    Liveness comes from ``live_sessions`` -- the same call the session picker
    and the upload server make -- so the three surfaces can no longer answer
    differently. It used to run its own single-shot fan-out with no retry while
    the picker retried its misses, which is how ``status``/``down`` came to
    report sessions stopped that the picker was still attaching to.
    """
    binary = find_psmux()
    projects = eligible_projects(config, group)
    up: list[dict[str, object]] = []
    down: list[dict[str, object]] = []

    probeable: list[dict[str, object]] = []
    for p in projects:
        info: dict[str, object] = {
            "name": p["name"],
            "session": p["session"],
            "path": p["path"],
            "tool": p["tool"],
            "group": p.get("group"),
        }
        if binary and p["resolved"] and p["cmd"]:
            probeable.append(info)
        else:
            info["reason"] = _down_reason(binary, p)
            down.append(info)

    live = set(
        live_sessions([_field_str(i, "session") for i in probeable], psmux=binary)
    )
    for info in probeable:
        (up if _field_str(info, "session") in live else down).append(info)

    return up, down, projects


def _cloud_create_refusal(
    config: MagentConfig, row: dict[str, object], sid: str
) -> str | None:
    """Why the cloud pane ``row`` (session ``sid``) must not be created now.

    Three questions, cheapest first. (1) Does this session name belong to the
    project the row was built from? The gate answers by NAME, for the first
    ENABLED project that owns it, and ``eligible_projects`` skips an IDE
    project: an IDE project listed ahead of a cloud one for the same folder
    would make the gate read the IDE project (not cloud: waved through) while
    the pane created is the cloud one, skipping the git and ``.env`` checks.
    (2) Can its command be built at all? (3) The gate itself."""
    # in-body: launch is a heavy subsystem that itself imports psmux in-body, so
    # by call time both are loaded and this is not a cycle.
    from magent import launch
    from magent.config import is_cloud

    owner = launch.project_for_session(config, sid)
    if owner is None or not is_cloud(owner):
        return launch.twin_session_refusal(sid)
    cmd_why = row.get("cmd_why")
    if isinstance(cmd_why, str) and cmd_why:
        return cmd_why
    return launch.cloud_refusal(config, sid)


def bring_up(
    config: MagentConfig,
    only: list[str] | None = None,
    group: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Create detached psmux sessions for eligible projects.

    ``only`` restricts creation to the given session names; ``group``
    restricts to a single project group.

    Returns ``(created, failed)``: the sessions the creation verify PROVED are
    up, and the ones still missing after ``launch_verified``'s one respawn --
    each mapped to the reason it is down (see ``launch_verified``).
    The casualties used to be discarded here -- ``launch_verified`` logged
    "session never came up after respawn" while this function answered with
    every name it had attempted, so both callers printed "Brought up N
    session(s)" for sessions that were never created. A caller cannot report
    honestly on a list that never distinguished the two.

    A cloud pane is typed ONCE (``resend=False``) and branded ``@cloud``: every
    ``claude --cloud`` is a new billed cloud session the CLI can neither list
    nor stop. So a cloud create the gate (``launch.cloud_refusal``) refuses --
    or one whose command cannot be built, or whose session name another project
    owns -- is not attempted: it lands in the second item with its reason, the
    same place a creation casualty does, and so reaches every printer of it. A
    cloud session that is already live is never gated (nothing is created for
    it) and is left to ``launch_verified``, whose launch dedupes it.
    """
    from magent.platform import get_platform

    plat = get_platform()
    windows: list[PsmuxWindowOpts] = []
    refused: dict[str, str] = {}
    rows = [
        p
        for p in eligible_projects(config, group)
        if (only is None or _field_str(p, "session") in only) and p["resolved"]
    ]
    # THE liveness answer, once for every cloud project (a dropped probe reads
    # "not live", which only asks the gate: the safe direction).
    cloud_sids = [_field_str(p, "session") for p in rows if p.get("node") == "cloud"]
    live_cloud = set(live_sessions(cloud_sids)) if cloud_sids else set()
    for p in rows:
        sid = _field_str(p, "session")
        cloud = p.get("node") == "cloud"
        if cloud and sid not in live_cloud:
            reason = _cloud_create_refusal(config, p, sid)
            if reason:
                get_logger("launch").warning(
                    "cloud session %s not created: %s", sid, reason
                )
                refused[sid] = reason
                continue
        if not p["cmd"]:
            continue
        windows.append(
            PsmuxWindowOpts(
                window_name=sid,
                cwd=_field_str(p, "resolved"),
                command=_field_str(p, "cmd"),
                resend=not cloud,
                nick="cloud" if cloud else None,
            )
        )
    names = [w.window_name for w in windows]
    if not windows:
        return [], refused
    failed = launch_verified(plat, windows)
    stuck = set(failed)
    return [n for n in names if n not in stuck], {**failed, **refused}


# Mirrors ``platform/windows.py::_SEND_VERIFY_SETTLE_S`` on purpose: a freshly
# created psmux server needs a beat before it answers control commands, and the
# two verifies run back to back in the same bring-up, so a different pause here
# would only be a second number to reason about.
_CREATE_VERIFY_SETTLE_S = 2.0
# A wedged psmux server answers nothing at all -- in the incident below every
# control command against its socket timed out. Bound each probe so one wedged
# server costs its own timeout instead of the whole fan-out's.
_CREATE_PROBE_TIMEOUT_S = 3.0


def _missing_sessions(names: list[str], binary: str) -> list[str]:
    """The subset of ``names`` whose session does not answer ``has-session``.

    Concurrent fan-out, the shape ``revive_sessions`` already uses: one bounded
    probe per session, all in flight together, so a 40-session bring-up pays
    roughly one round-trip rather than 40 sequential ones.

    Deliberately NOT ``live_sessions``: that seam answers the user-facing
    question "what is running" and retries flapping misses, while this one
    answers "what did creation fail to produce", where a probe that timed out
    against a wedged server must count as MISSING and be re-CREATED --
    ``launch_verified`` owns that retry, and folding a second probe retry in
    here would only delay its respawn.
    """
    from concurrent.futures import ThreadPoolExecutor

    def _up(name: str) -> bool:
        return has_session(name, psmux=binary, timeout=_CREATE_PROBE_TIMEOUT_S)

    with ThreadPoolExecutor(max_workers=16) as pool:
        flags = list(pool.map(_up, names))
    return [n for n, ok in zip(names, flags, strict=True) if not ok]


# Appended to a refusal whose session the verify then found live: the refused
# client did its job after all, too late for this bring-up to start the agent.
# `magent up` revives a live session that rests at its shell.
_LATE_LIVE = (
    "; it answers now, but this bring-up typed no agent command into it"
    " -- run `magent up` to revive it"
)


def _late_live_once(sid: str) -> str:
    """``_LATE_LIVE`` for a ``resend=False`` window (a cloud pane). "Revive it"
    is wrong twice over there: revive never re-types a cloud pane (a second
    ``claude --cloud`` is a new billed session), and an ``up`` that finds the
    session live creates nothing. The advice that works is down, then up --
    after the user has looked at what the first attempt did."""
    return (
        "; it answers now, but this bring-up cannot tell whether its command was"
        " typed, and a cloud pane is never re-typed -- check claude.ai/code, then"
        f" run `magent down {sid}` and `magent up` to start it afresh"
    )


# The reason for a ``resend=False`` window the creation verify found missing and
# deliberately did NOT respawn (see ``launch_verified``).
_NOT_RETYPED = (
    "its command runs at most once per session (a cloud pane's `claude --cloud`"
    " starts a new cloud session each time it is typed), and the first attempt"
    " may already have typed it, so it was not re-created; check claude.ai/code,"
    " then run `magent up` to try again"
)


def launch_verified(plat: Platform, windows: list[PsmuxWindowOpts]) -> dict[str, str]:
    """Create ``windows`` through the platform, then prove each session exists.

    ``launch_psmux_session`` reports success the moment its ``new-session``
    processes exit 0, which is not the same thing as a live psmux server.
    During a ~40-session attach bring-up storm one project's server wedged --
    every control command against its socket timed out -- and NOTHING detected
    it: creation had no verify at all, so the picker showed that project down
    forever and only a second, storm-free bring-up recreated it. This is the
    creation-level twin of ``platform/windows.py::_verify_sends_landed``.

    ASYMMETRY WITH THE SEND VERIFIER, deliberately: that one treats an
    empty/unreadable pane reading as "not a casualty", because re-sending into
    a pane whose state is unknown would type the agent command into a live
    agent. Here the remedy is ``new-session``, which ``launch_psmux_session``
    already skips for any session that answers ``has-session`` -- so re-running
    it is safe, and an unknown state (including a probe that TIMED OUT against
    a wedged server) counts as MISSING and is handed back for the respawn.
    That is safe only because the respawn goes through ``launch_psmux_session``
    again, whose own dedupe is tri-state: a name IT cannot read is never
    killed and never re-created (psmux.probe_sessions). "Unknown" is safe to
    re-ask about; it is not safe to kill, re-create, or inject into.

    A name the platform REFUSED (its dedupe or its kill-server got no answer,
    or its new-session outran the budget) is not respawned when the verify
    also misses it: the respawn would only repeat the wait that failed, and on
    a wedged socket double it. It is reported with the platform's reason.

    A refusal is final even when the verify finds that session LIVE. A
    new-session killed at its deadline can still have created the session
    late, and then nothing ever typed the agent command into it: counted as
    brought up, it is a bare shell under a success line (``--go`` never
    revives). So it stays in the report, its reason extended by what the
    verify saw (``_LATE_LIVE``).

    Never raises out of the verify -- and, since v3.10.10, never raises out of
    the CREATION either: one stuck session must not cost the wave its remaining
    ones. The first ``launch_psmux_session`` was the one call here left
    unguarded, so a single window psmux refused to create ("failed to create
    session 'X'", rc 1) escaped as a ``CalledProcessError`` traceback out of
    `magent up` and out of the interactive menu's "u", aborting every remaining
    session in the wave. A raise is now logged and falls through to the probe
    below, which is the component that already knows how to respawn what is
    missing and report what stayed down.

    A window marked ``resend=False`` (a command that may run only once) is the
    one exception to the respawn: it is never re-created, because the respawn
    types the command again. When it is missing it is reported with that
    reason instead (``_NOT_RETYPED``), exactly like a refusal.

    Returns the sessions still missing after the one retry, plus every name
    the platform refused, in input order, each mapped to why: the platform's
    refusal reason, or ``""`` when the log is the only account (the Session-0
    refusal is named by the printers via ``launch.session0_note``).
    """
    if not windows:
        return {}
    names = [w.window_name for w in windows]
    log = get_logger("launch")

    # THE choke point's safety net. Every session this product creates goes
    # through here, so this is the one place that can guarantee no path -- not
    # `--go`, not the menu's "u", not `revive` -- ever creates a psmux server in
    # a logon session the user cannot see. The command shells hand off BEFORE
    # reaching this function, so anything that arrives here in Session 0 is a
    # path that did not, and the honest outcome is a loud, named failure rather
    # than a silent second hand-off from inside a subsystem.
    #
    # In-body import, the same way `eligible_projects` reaches launch: launch
    # imports this module, so neither side may import the other at top level.
    from magent.launch import session0_disposition, session0_refusal

    if session0_disposition(plat) != "run":
        log.error(
            "%s (would have created: %s)", session0_refusal(plat), ", ".join(names)
        )
        return dict.fromkeys(names, "")

    refused: dict[str, str] = {}
    try:
        refused.update(plat.launch_psmux_session(windows))
    except (OSError, subprocess.SubprocessError):
        # Same handling the respawn below has always had. Deliberately NOT a
        # re-raise: the probe decides what actually came up, and a partial
        # batch is exactly the case worth verifying.
        log.exception("bring-up raised while creating %s", ", ".join(names))

    binary = find_psmux()
    if not binary:
        # Nothing can be probed, so nothing can be claimed. Every name is
        # reported missing rather than silently passed off as created -- with
        # no psmux binary the creation above cannot have succeeded either.
        return dict.fromkeys(names, "")

    # Windows whose command may run only once (a cloud pane: each `claude
    # --cloud` is a new, billed cloud session).
    once = {w.window_name for w in windows if not w.resend}

    def _report(down: list[str]) -> dict[str, str]:
        gone = set(down)
        late = [n for n in names if n in refused and n not in gone]
        if late:
            log.warning(
                "refused %s, which answers now: left without its agent command",
                ", ".join(late),
            )
        return {
            n: refused[n] + (_late_live_once(n) if n in once else _LATE_LIVE)
            if n in refused and n not in gone
            # ...and the missing ones, refused or not, as the platform left them.
            else refused.get(n, "")
            for n in names
            if n in gone or n in refused
        }

    # Settle first: the storm's timeouts were transient churn, and probing at
    # t=0 would misclassify slow-but-fine servers on a loaded host.
    time.sleep(_CREATE_VERIFY_SETTLE_S)
    missing = _missing_sessions(names, binary)
    # The respawn re-runs the launch path, which TYPES the command again. A
    # window whose command may run only once (a cloud pane: each `claude
    # --cloud` is a new, billed cloud session) is therefore never respawned,
    # and "missing" cannot tell a session that never started from one that
    # started, typed, and then wedged or died -- the first attempt may already
    # have typed it. It is refused instead, and the next `magent up` is the
    # user's informed retry.
    for n in missing:
        if n in once and n not in refused:
            refused[n] = _NOT_RETYPED
            log.warning(
                "%s is missing after bring-up and its command runs at most once;"
                " not respawning it",
                n,
            )
    respawn = [n for n in missing if n not in refused]
    if not respawn:
        return _report(missing)
    log.warning(
        "session did not come up after bring-up; respawning: %s", ", ".join(respawn)
    )
    stuck = set(respawn)
    try:
        # Back through the full launch path on purpose -- a hand-rolled
        # ``new-session`` here would diverge from the original recipe (batch
        # pacing, send-keys verification, status-line decoration).
        refused.update(
            plat.launch_psmux_session([w for w in windows if w.window_name in stuck])
        )
    except (OSError, subprocess.SubprocessError):
        log.exception("respawn failed for %s", ", ".join(respawn))
        return _report(missing)

    time.sleep(_CREATE_VERIFY_SETTLE_S)
    still_missing = set(_missing_sessions(respawn, binary))
    if still_missing:
        log.error(
            "session never came up after respawn; left down: %s",
            ", ".join(n for n in respawn if n in still_missing),
        )
    return _report([n for n in missing if n in still_missing or n not in stuck])


def _parked_session_id(rec: dict[str, object]) -> str | None:
    """The conversation id a ``parked`` record resumes, or None when it holds
    none usable. The id is typed into a shell, so only the shape a session file
    may hold passes -- a truthiness test would type ``x & calc`` or a flag."""
    from magent.sessions.live import SESSION_ID_RE

    sid = rec.get("session_id")
    if isinstance(sid, str) and SESSION_ID_RE.fullmatch(sid):
        return sid
    return None


def revive_sessions(
    config: MagentConfig,
    only: list[str] | None = None,
    group: str | None = None,
    *,
    resume_parked: bool = False,
    vetoed: dict[str, str] | None = None,
) -> list[str]:
    """Re-launch the agent in live sessions whose pane fell back to a shell.

    A session whose agent was Ctrl-C'ed (or whose original send-keys died)
    still answers ``has-session``, so ``up``/``attach`` reuse it and hand the
    user a window parked at a bare prompt forever. Liveness is probed
    concurrently (a large config would otherwise serialize a round-trip per
    session), then the live ones get ONE ``idle_sessions`` verdict -- the only
    thing that may put keystrokes into a pane, since typed into a LIVE agent
    the resume command is a submitted prompt. Returns the session ids revived.

    A pane the idle reaper parked (its record says ``parked``) is left alone
    unless ``resume_parked`` -- status's ``r<n>``, a human asking for that pane
    back. Then it resumes by the record's id, never ``--continue``, and the
    record is cleared once the resume is sent.

    A caller that must say WHY a session was not revived passes ``vetoed``,
    which receives ``{name: reason}`` for every eligible session in scope it
    did not revive, and for every name in ``only`` it could not consider.
    Without a psmux binary nothing is read, so only the names in ``only`` get
    a reason, and a call with no ``only`` leaves ``vetoed`` empty.
    """
    from concurrent.futures import ThreadPoolExecutor

    from magent import agent_state
    from magent.sessions import build_resume_command

    why: dict[str, str] = {} if vetoed is None else vetoed
    binary = find_psmux()
    if not binary:
        why.update(dict.fromkeys(only or (), "psmux not found"))
        return []

    candidates: list[dict[str, object]] = []
    eligible = eligible_projects(config, group)
    for p in eligible:
        if only is not None and _field_str(p, "session") not in only:
            continue
        if p.get("node") == "cloud":
            # Every re-type -- a resume command, a parked resume, a fresh start
            # -- would be a NEW cloud session (spec §18.5), in every mode:
            # vetoed before the pane is read or anything is sent.
            why[_field_str(p, "session")] = (
                "a cloud pane is never re-typed: that would start a second cloud"
                " session"
            )
            continue
        if not p["cmd"]:
            why[_field_str(p, "session")] = "its tool has no command configured"
            continue
        candidates.append(p)
    known = {_field_str(p, "session") for p in eligible}
    for sid in set(only or ()) - known:
        why[sid] = "not an enabled local agent session in the config"
    if not candidates:
        return []

    def _live(p: dict[str, object]) -> bool:
        return has_session(_field_str(p, "session"), psmux=binary)

    with ThreadPoolExecutor(max_workers=16) as pool:
        flags = list(pool.map(_live, candidates))
    live = [p for p, ok in zip(candidates, flags, strict=True) if ok]
    for p, ok in zip(candidates, flags, strict=True):
        if not ok:
            why[_field_str(p, "session")] = "its psmux session did not answer"
    console: dict[str, str] = {}
    idle = idle_sessions(
        [_field_str(p, "session") for p in live], psmux=binary, vetoed=console
    )

    revived: list[str] = []
    for p in live:
        sid = _field_str(p, "session")
        if sid not in idle:
            # An unreadable pane reads as not idle too: neither is claimed alone.
            # "not proven idle" holds for every console reason, the unreadable
            # one included.
            why[sid] = (
                f"its pane is not proven idle: {console[sid]}"
                if sid in console
                else "its agent is still running, or its pane could not be read"
            )
            continue
        cwd = _field_str(p, "resolved")
        rec = agent_state.state_for(cwd) if cwd else None
        if rec is not None and rec.get("state") == agent_state.PARKED:
            # A bulk revive resuming it would undo the memory the park freed,
            # on every up and every attach.
            if not resume_parked:
                why[sid] = "the idle reaper parked it"
                continue
            resume_id = _parked_session_id(rec)
            if resume_id is None:
                # Never --continue instead: with another agent in this
                # directory it can open that agent's conversation.
                get_logger("launch").warning(
                    "revive: %s is parked without a resumable session id; leaving it",
                    sid,
                )
                why[sid] = "it is parked without a resumable session id"
                continue
            resume = build_resume_command(
                _field_str(p, "tool"), _field_str(p, "cmd"), resume_id
            )
            keys = f"cmd /c {resume}" if sys.platform == "win32" else resume
            if send_keys(sid, keys, "Enter", target=sid, psmux=binary):
                revived.append(sid)
                agent_state.clear_state(cwd)  # its SessionStart hook writes anew
            else:
                why[sid] = "the resume could not be sent (see launch.log)"
            continue
        # The configured command already IS the resume command -- claude's
        # registry default is ``claude --continue``, which picks the dead
        # pane's conversation back up. ``sessions.build_resume_command`` is
        # deliberately NOT used here: with no session id claude's builder
        # *strips* ``--continue`` unconditionally, starting a fresh chat -- the
        # opposite of reviving. ``eligible_projects`` has already dropped that
        # flag for the one case where keeping it cannot work (a directory with
        # no stored conversation, where ``--continue`` would only re-kill the
        # pane the revive is trying to rescue), and left it alone everywhere
        # else. Injection shape mirrors ``launch_psmux_session``.
        resume = _field_str(p, "cmd")
        keys = f"cmd /c {resume}" if sys.platform == "win32" else resume
        if send_keys(sid, keys, "Enter", target=sid, psmux=binary):
            revived.append(sid)
        else:
            why[sid] = "the relaunch could not be sent (see launch.log)"
    return revived


def config_sessions(config_path: str | None) -> list[dict[str, object]]:
    """Eligible psmux sessions from config — no psmux binary calls, fast path
    for the upload server's session list."""
    import json
    from pathlib import Path

    # Same machinery `eligible_projects` uses, imported in-body in the same
    # style: a configured ``path`` may be relative to ``baseDir``, and every
    # consumer of this list that has to *act* on the folder (the F2 "open in
    # VS Code" hotkey) needs an absolute one.
    from magent.launch import _expand_base_dir, _resolve_path
    from magent.paths import find_config
    from magent.sessions import is_ide_tool

    config_file = find_config(config_path)
    if not config_file.exists():
        return []

    data = json.loads(config_file.read_text(encoding="utf-8"))
    default_tool = data.get("settings", {}).get("defaultTool", "claude")
    raw_base = data.get("baseDir")
    base_dir = (
        _expand_base_dir(raw_base) if isinstance(raw_base, str) and raw_base else None
    )
    out: list[dict[str, object]] = []
    for p in data.get("projects", []):
        if not p.get("enabled", True):
            continue
        tool = p.get("tool", default_tool)
        if isinstance(tool, str) and is_ide_tool(tool):
            continue
        # Raw dict: same rule as eligible_projects' node skip (DECISION-15).
        if p.get("node") not in (None, "cloud"):
            continue
        proj_name = p.get("title") or Path(p["path"]).name
        out.append(
            {
                "name": proj_name,
                "session": session_name(proj_name),
                "path": p["path"],
                # "" (never None) when the folder can't be resolved, so a JSON
                # consumer can treat it as a plain string field.
                "resolved": _resolve_path(p["path"], base_dir) or "",
                "node": p.get("node"),
            }
        )
    return out


def discover_sessions(config_path: str | None) -> list[dict[str, object]]:
    """Active psmux sessions from config — through the one liveness seam."""
    candidates = config_sessions(config_path)
    binary = find_psmux()
    if not candidates or not binary:
        return []
    live = set(live_sessions([socket_id(c) for c in candidates], psmux=binary))
    return [c for c in candidates if socket_id(c) in live]
