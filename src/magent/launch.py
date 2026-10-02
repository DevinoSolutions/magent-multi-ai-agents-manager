from __future__ import annotations

import dataclasses
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import click

from magent import attach_client, tailnet
from magent.config import NODE_AUTO, is_cloud, runs_on_node
from magent.grid import TileSlot, compute_grid
from magent.lockfile import LockHeld
from magent.log import HEARTBEAT_MAX_AGE, get_logger, heartbeat_fresh, heartbeat_mtime
from magent.platform import (
    Platform,
    PsmuxWindowOpts,
    TerminalLaunchOpts,
    TerminalNotFoundError,
    VSCodeLaunchOpts,
    get_platform,
)
from magent.procs import (
    REGISTRATION_TIMEOUT_S,
    await_registration,
    filetime_to_epoch,
    pid_alive,
    process_identity,
    spawn_unjobbed,
    terminate_verified,
)
from magent.sessions import (
    AGENT_TOOLS,
    build_resume_command,
    build_start_command,
    fresh_start_command,
    ide_command,
    is_ide_tool,
)
from magent.style import style
from magent.tiling import Placement, magent_window_names, place_windows
from magent.titles import generate_titles, get_leaf_name, make_title, parse_title

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable, Mapping

    from magent.config import MagentConfig, ProjectConfig
    from magent.env import MagentEnv
    from magent.nodes import (
        LoadSample,
        LocalGitState,
        Node,
        NodeMapEntry,
        Recipe,
    )
    from magent.nodes import Placement as NodePlacement
    from magent.procs import ProcessIdentity


def spawn_detached(args: list[str], extra_flags: int = 0) -> subprocess.Popen[bytes]:
    """Popen a process that outlives both this process and a launching SSH session.

    Two independent halves, and only one of them lives here now. The CONSOLE
    half is this function's own: ``DETACHED_PROCESS | CREATE_NO_WINDOW`` gives
    the child no console to be killed with and no window to flash. The JOB half
    -- escaping the kill-on-close job object Windows OpenSSH wraps every SSH
    session in -- is ``procs.spawn_unjobbed``, shared with the psmux
    session-creation spawn in ``platform/windows.py`` so the recipe that decides
    whether work survives a disconnect exists exactly once.
    """
    if sys.platform != "win32":
        return spawn_unjobbed(args)
    CREATE_NO_WINDOW = 0x08000000
    DETACHED_PROCESS = 0x00000008
    return spawn_unjobbed(
        args, creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS | extra_flags
    )


# --- Session-0 desktop hand-off ----------------------------------------------
# The incident, in one paragraph: a laptop ran `magent attach <desktop>`, the
# host reported sessions down, and attach ran `magent up` on the host over ssh.
# Windows OpenSSH is a SERVICE, so that bring-up -- and everything it created --
# was born in logon Session 0: 82 psmux servers and 42 agents on a desktop
# nobody can see. The desktop's own magent called them stopped, every later
# bring-up logged "session never came up after respawn" (psmux's registry under
# ~/.psmux is shared, so `new-session` for a held name just exits 1), tiling
# logged "window not found", and the Session-0 `serve` had taken 127.0.0.1:8034
# out from under the desktop's Alt+V. Clearing it needed an elevated kill of
# 1172 processes.
#
# Every wording below is shared so the host's answer reads identically whether
# it is printed locally or relayed up an ssh pipe by `magent attach`.
SESSION0_HANDOFF_LINE = (
    "hand-off: this magent runs in a non-interactive logon session (Session 0);"
    " re-running on the desktop..."
)
SESSION0_REFUSAL = (
    "refusing to start psmux sessions from a non-interactive logon session "
    "(Session 0 / ssh): they would be invisible to this host's desktop and "
    "block the same names. Run 'magent up' on the host's own desktop, or set "
    "MAGENT_SESSION0_POLICY=allow for a headless host."
)
SESSION0_SERVE_REFUSAL = (
    "refusing to start the upload server from a non-interactive logon session "
    "(Session 0 / ssh): it would take the loopback port this host's desktop "
    "needs for Alt+V. Run 'magent serve' on the host's own desktop, or set "
    "MAGENT_SESSION0_POLICY=allow for a headless host."
)
# The two other survivors a Session-0 magent could plant. Neither is a command
# `magent attach` fires, so they are refused at their spawn seams (and `attention
# -d`, a command shell like `serve --ensure`, hands off first).
SESSION0_HOTKEY_REFUSAL = (
    "refusing to start the Alt+V listener from a non-interactive logon session "
    "(Session 0 / ssh): its keyboard hook would never see a key typed at this "
    "host's desktop. Run 'magent serve' on the host's own desktop (it starts "
    "the listener), or set MAGENT_SESSION0_POLICY=allow for a headless host."
)
SESSION0_ATTENTION_REFUSAL = (
    "refusing to start the attention daemon from a non-interactive logon "
    "session (Session 0 / ssh): it would badge windows on a desktop it cannot "
    "see and revive the upload server out of this desktop's reach. Run "
    "'magent attention -d' on the host's own desktop, or set "
    "MAGENT_SESSION0_POLICY=allow for a headless host."
)
# The same refusal with a different cause, and worth its own sentence: the
# policy DID ask for a hand-off and there is simply nowhere to hand off TO.
# Telling that user to "run it on the desktop" would be advice they cannot
# take, and telling them to set a policy they already set would be noise.
SESSION0_NO_DESKTOP = (
    "no user is logged on at this host's desktop, so there is nowhere to hand "
    "the work to (and a session started here would be invisible to that "
    "desktop when someone does log in). Log in at the console and retry, or "
    "set MAGENT_SESSION0_POLICY=allow for a headless host."
)
# The hand-off inherits the budget of the command it replaces: a bring-up is a
# cold-start storm (attach allows 900s over ssh for the same work), while
# `serve --ensure` returns the moment a detached server answers.
SESSION0_UP_TIMEOUT_S = 900.0
SESSION0_SERVE_TIMEOUT_S = 60.0
SESSION0_ATTENTION_TIMEOUT_S = 60.0


def session0_disposition(plat: Platform) -> Literal["run", "handoff", "refuse"]:
    """What a session-creating command should do on THIS machine.

    ONE decision, in one place, for every caller -- the command shells that can
    hand off, and the psmux choke point that can only refuse. A second copy of
    this policy is how one of them ends up creating a Session-0 fleet again.

    An interactive logon session is always "run", before the policy is even
    read: a normal desktop launch must not be able to change behaviour because
    of an environment variable somebody set for a headless host. Off Windows
    every platform reports interactive, so nothing changes there at all.
    """
    from magent.env import get_env  # heavy subsystem: in-body per policy

    if plat.logon_session_is_interactive():
        return "run"
    policy = get_env().session0_policy
    if policy == "allow":
        return "run"
    if policy == "refuse":
        return "refuse"
    return "handoff" if plat.supports_desktop_handoff() else "refuse"


def session0_refusal(plat: Platform, base: str = SESSION0_REFUSAL) -> str:
    """The refusal wording that fits THIS machine.

    Two different situations wear the same disposition. Usually the policy said
    no. But when the policy asked for a hand-off and the platform reports no
    mechanism, the cause on Windows is specifically that nobody is logged on at
    the console -- and a user who is told to "run it on the desktop" when there
    is no desktop has been given advice they cannot take.
    """
    from magent.env import get_env  # heavy subsystem: in-body per policy

    if get_env().session0_policy == "handoff" and not plat.supports_desktop_handoff():
        return SESSION0_NO_DESKTOP
    return base


def session0_block(base: str, plat: Platform | None = None) -> str | None:
    """Why a daemon spawn seam must not spawn here, or None when it may run.

    The question every daemon spawn seam asks (``base`` names what was
    refused). A seam is not the command the user typed, so it can never hand
    off -- only refuse, exactly like the psmux choke point. The wording falls
    to ``SESSION0_NO_DESKTOP`` when there is no desktop to go to.
    """
    plat = plat or get_platform()
    if session0_disposition(plat) == "run":
        return None
    return session0_refusal(plat, base)


def session0_note() -> str | None:
    """The one-line reason a spawn is blocked here, or None when it may run.

    What the "N session(s) failed to come up" printers add so a casualty list
    carries its cause. A user staring at 40 failed names must not have to find
    launch.log to learn that nothing was even attempted.
    """
    return session0_block(SESSION0_REFUSAL)


def report_bring_up_casualties(
    failed: Mapping[str, str], *, log_hint: str = "(see ~/.magent/logs/launch.log)"
) -> None:
    """Print the "N session(s) failed to come up" block; nothing when none did.

    THE one printer for a bring-up's casualties -- `--go`, the menu's "u" and
    `magent up` (the block `magent attach` relays from the host) all report
    through it, so the three cannot drift apart again. The count and names,
    then one dimmed line per KNOWN reason (a session the bring-up deliberately
    left alone says why: "could not tell" is not "dead"), then
    ``session0_note``: a casualty list with no cause is what sent a user
    hunting through launch.log last time.
    """
    if not failed:
        return
    click.echo(
        f"  {style('x', fg='red')} {style(str(len(failed)), fg='red', bold=True)}"
        f" session(s) failed to come up: {style(', '.join(failed), fg='red')}"
        f" {style(log_hint, dim=True)}"
    )
    for why in failed.values():
        if why:
            click.echo(f"    {style(why, dim=True)}")
    note = session0_note()
    if note:
        click.echo(f"  {style(note, dim=True)}")


def relay_handoff(plat: Platform, argv: list[str], *, timeout_s: float) -> int:
    """Run ``argv`` on the desktop, relay its output verbatim, return its code.

    Verbatim and unindented on purpose: the command being handed off is the
    same command the user asked for, so its output IS this command's output.
    `magent attach` indents the whole remote stream by two spaces on the
    laptop, which is where the nesting belongs.
    """
    click.echo(SESSION0_HANDOFF_LINE)
    result = plat.run_on_desktop(argv, timeout_s=timeout_s)
    if result.stdout.strip():
        click.echo(result.stdout.rstrip())
    if result.stderr.strip():
        click.echo(result.stderr.rstrip(), err=True)
    if result.timed_out:
        click.echo(
            f"  {style('x', fg='red')} hand-off timed out after "
            f"{timeout_s:.0f}s -- the desktop copy may still be running "
            f"(see ~/.magent/logs/launch.log on this host). {result.detail}",
            err=True,
        )
        return 1
    if result.rc is None:
        # "failed", not "could not run": some of these answers are about a
        # command that DID run (it lost its child, or finished with an
        # unreadable exit code), and `detail` says which.
        click.echo(
            f"  {style('x', fg='red')} hand-off failed: {result.detail} "
            "(see ~/.magent/logs/launch.log on this host)",
            err=True,
        )
        return 1
    return result.rc


def hotkey_restart_reason(
    manifest: dict[str, str | None] | None,
    server_url: str,
    ssh_host: str | None,
) -> str | None:
    """Why the running listener can't serve ``(server_url, ssh_host)``, or None
    if it can and must be left alone.

    Pure so it is testable off Windows -- ``magent.hotkey`` raises ImportError
    at import time there, and this is the whole decision behind "keep or
    restart the listener". Two real bugs live in the two non-None branches:
    a pip upgrade leaves the OLD process running old code (an F2 handler it
    may not even have), and a locally-wired listener answers F2 for the wrong
    machine when `magent attach` wanted the remote-wired one. A missing or
    unparseable manifest is a pre-3.6.0 listener: stale by definition.
    """
    from magent import __version__  # PEP 562 lazy: skipped unless a pid is live

    if manifest is None:
        return "no manifest (listener predates self-describing listeners)"
    running = manifest.get("version")
    if running != __version__:
        return f"version skew (listener {running}, want {__version__})"
    if manifest.get("server_url") != server_url:
        return (
            f"target change (server_url {manifest.get('server_url')} -> {server_url})"
        )
    if manifest.get("ssh_host") != ssh_host:
        return f"target change (ssh_host {manifest.get('ssh_host')} -> {ssh_host})"
    return None


def start_hotkey_listener(server_url: str, ssh_host: str | None = None) -> int | None:
    """Start the window-hotkey (Alt+V paste / F2 open-in-VS-Code) listener
    detached, unless a listener matching this exact version and target is
    already running. Returns its pid, or None if the child never confirmed
    itself.

    Windows-only: the caller owns the ``supports_hotkey()`` gate (the launch
    path holds a Platform already, the CLI path resolves one), which is also
    what keeps the ``magent.hotkey`` import below reachable -- it raises
    ImportError off win32 at import time.

    Lives here, next to ``spawn_detached``, rather than in ``cli/background.py``
    where it started: ``launch.py`` must not import the cli package (cli/__init__
    imports every command module, so a reverse import cycles), and both the
    launch path and ``magent attach`` need this same recipe.

    ``ssh_host`` is forwarded to the child so its F2 handler opens projects
    through VS Code Remote-SSH; omitted, F2 opens them on this machine.

    A live listener is kept only when its manifest says it is this version and
    this exact target (see ``hotkey_restart_reason``); anything else is killed
    and respawned. Repeat calls with identical arguments are therefore a no-op,
    which matters because `magent attach` re-runs this on every attach.

    Refused in a non-interactive logon session (Session 0), before anything
    else: a keyboard hook there never sees a key typed at the desktop.
    """
    refusal = session0_block(SESSION0_HOTKEY_REFUSAL)
    if refusal:
        get_logger("hotkey").warning("%s", refusal)
        return None
    from magent.hotkey import (  # ImportError off-Windows (hotkey.py guards); must stay lazy
        listener_manifest,
        listener_pid,
        stop_listener,
    )

    existing = listener_pid()
    if existing:
        reason = hotkey_restart_reason(listener_manifest(), server_url, ssh_host)
        if reason is None:
            return existing  # same version, same target: nothing to do
        get_logger("hotkey").info("restarting listener pid=%d: %s", existing, reason)
        # Reuse the taskkill recipe stop_listener already owns; it tolerates a
        # pid that has since died (listener_pid clears the stale file and it
        # returns False), and either way the spawn below replaces it.
        stop_listener()

    args = [sys.executable, "-m", "magent", "hotkey", "-s", server_url]
    if ssh_host:
        args += ["--ssh-host", ssh_host]
    # The child writes its pid only after the keyboard hook installs, so the
    # wait both reports the pid and surfaces a hook failure (the child exits).
    # `not_pid=existing` guards the restart path: a kill that didn't take must
    # not read back as "the new listener came up".
    return await_registration(spawn_detached(args), listener_pid, not_pid=existing)


def supervised_hotkey_target(
    manifest: dict[str, str | None] | None, default_url: str
) -> tuple[str, str | None]:
    """The ``(server_url, ssh_host)`` a SUPERVISED restart must use.

    Pure so it is testable off Windows, like ``hotkey_restart_reason``.

    The distinction this encodes is the whole reason ``ensure_hotkey_listener``
    exists as a separate entry point. The launch and attach paths KNOW which
    target the listener should serve and deliberately re-aim it when that
    changes -- that is what ``hotkey_restart_reason``'s "target change" branches
    are for. A supervisor knows no such thing: ``magent attach`` aims the
    listener at a REMOTE host so F2 opens projects over VS Code Remote-SSH, and
    a supervisor that re-aimed it at its own loopback URL every interval would
    fight attach forever, silently breaking F2 on every remote fleet. So a
    listener that is already running keeps whatever target it was wired to; the
    supervisor's default is only ever used for a listener that is not there.

    A missing/unreadable manifest yields the default: that listener is getting
    restarted anyway ("no manifest" is a restart reason), and the default is
    the only target we can honestly claim to know.
    """
    if manifest is None:
        return default_url, None
    return manifest.get("server_url") or default_url, manifest.get("ssh_host")


# A listener whose pid is alive but whose heartbeat stopped is WEDGED: the
# process exists, the keyboard hook may or may not still fire, and nothing ever
# replaced it (`ensure_hotkey_listener` only asked "is there a pid?"). The
# heartbeat proves the message loop is turning, so a silent one means Alt+V and
# F2 are dead behind a live pid.
#
# 3x the stale threshold `status` already reports (HEARTBEAT_MAX_AGE, 30s) = 90s,
# nine missed 10s pulses. "Stale" is a label; this verdict ends a process, so it
# must be slower than the label and clear of every benign silence: a busy
# machine, a debugger pause, a laptop that slept (which stalls serve's own
# supervisor thread just the same, and is separately absorbed by the two-tick
# confirm on `ListenerWatch`).
WEDGED_LISTENER_GRACE_S = 3 * HEARTBEAT_MAX_AGE

# At most one replacement per five minutes. A fresh listener needs ~90s of grace
# plus a 30s confirm tick before it can be judged wedged again, so the natural
# floor on the kill/spawn cycle is already ~2 minutes; the cooldown is the real
# bound on top of it (max 12 an hour) for a listener that wedges the instant it
# starts, e.g. a broken hook install -- kill/spawn churn there would flash
# console windows and steal keyboard-hook ownership on every cycle, and a
# listener that is broken at birth is not fixed by replacing it faster.
WEDGED_REPLACE_COOLDOWN_S = 300.0


@dataclass
class ListenerWatch:
    """What one supervisor remembers between ticks about the listener it watches.

    In-memory on purpose: a serve restart is a fresh look at the world, and the
    first tick after it can only ever be a first sighting.
    """

    suspect: tuple[int, float] | None = None  # (pid, heartbeat mtime) last seen wedged
    last_replaced: float | None = None  # time.monotonic() of the last attempt
    cooldown_logged: bool = False
    unverifiable_pid: int | None = None  # the pid we already warned we cannot prove

    def confirm(self, pid: int, mtime: float) -> bool:
        """True once the SAME pid has shown the SAME last pulse on two ticks.

        A machine that slept looks wedged on the tick it wakes (old heartbeat)
        and healthy on the next (the listener pulsed in between); requiring the
        pulse to be unchanged across a tick apart turns that into a non-event.
        """
        key = (pid, mtime)
        if self.suspect == key:
            return True
        self.suspect = key
        return False

    def in_cooldown(self, mono: float) -> bool:
        return (
            self.last_replaced is not None
            and mono - self.last_replaced < WEDGED_REPLACE_COOLDOWN_S
        )


def wedged_listener_reason(
    last_pulse: float | None, now: float, identity: ProcessIdentity | None
) -> str | None:
    """Why a live-pid listener is provably wedged, or None (leave it alone).

    Pure, like ``hotkey_restart_reason``. Every clause exists to keep a kill from
    landing on the wrong thing:

    - no heartbeat file: nothing to compare against (`status` calls that off or
      crashed, and it is not ours to end);
    - a pulse inside the grace: alive, or at worst slow;
    - an unreadable identity: we cannot prove what the pid is;
    - an image that is not python: a recycled pid now owned by something else;
    - created AFTER the last pulse: also a recycled pid. This is the proof
      that survives a python-on-python reuse -- a process born after the final
      heartbeat cannot be the one that wrote it.
    """
    if last_pulse is None:
        return None
    silent_s = now - last_pulse
    if silent_s <= WEDGED_LISTENER_GRACE_S:
        return None
    if identity is None:
        return None
    if not identity.image.lower().startswith("python"):
        return None
    if filetime_to_epoch(identity.created) > last_pulse:
        return None
    return (
        f"heartbeat silent for {silent_s:.0f}s "
        f"(grace {WEDGED_LISTENER_GRACE_S:.0f}s) with the process still alive"
    )


def retire_wedged_listener(
    pid: int, watch: ListenerWatch, *, now: float, mono: float
) -> bool:
    """End ``pid`` if it is a proven, twice-confirmed, off-cooldown wedged
    listener. True only when it was actually ended and its files forgotten --
    the caller then spawns the replacement.

    The kill goes through ``procs.terminate_verified``, which re-reads the
    identity through the very handle it terminates with, so a pid recycled
    between the proof and the kill is never hit. It is NOT ``stop_listener``'s
    ``taskkill /PID /F``, which trusts the pid file blindly.

    Never raises for an expected refusal: a kill that did not happen returns
    False with a log line, and still starts the cooldown so it is not retried
    every tick.
    """
    from magent.hotkey import forget_listener  # ImportError off-Windows; stays lazy

    log = get_logger("hotkey")
    last_pulse = heartbeat_mtime("hotkey")
    if last_pulse is None or now - last_pulse <= WEDGED_LISTENER_GRACE_S:
        watch.suspect = None  # healthy, or nothing to judge by
        return False
    identity = process_identity(pid)
    reason = wedged_listener_reason(last_pulse, now, identity)
    if reason is None or identity is None:
        watch.suspect = None
        if identity is None and watch.unverifiable_pid != pid:
            watch.unverifiable_pid = pid
            log.warning(
                "listener pid=%d heartbeat is silent for %.0fs but its identity "
                "cannot be read; leaving it alone",
                pid,
                now - last_pulse,
            )
        return False
    if not watch.confirm(pid, last_pulse):
        return False
    if watch.in_cooldown(mono):
        if not watch.cooldown_logged:
            watch.cooldown_logged = True
            log.warning(
                "wedged listener pid=%d not replaced: cooldown (%.0fs) after the "
                "last replacement",
                pid,
                WEDGED_REPLACE_COOLDOWN_S,
            )
        return False
    watch.last_replaced = mono
    watch.cooldown_logged = False
    log.warning("replacing wedged listener pid=%d: %s", pid, reason)
    if terminate_verified(pid, identity) is None:
        log.warning("could not end wedged listener pid=%d; leaving it in place", pid)
        return False
    watch.suspect = None
    forget_listener()
    return True


def ensure_hotkey_listener(
    default_url: str, watch: ListenerWatch | None = None
) -> int | None:
    """Make sure SOME Alt+V listener is running; never re-aim a healthy one.

    The supervision entry point (``upload_server``'s serve loop calls this on an
    interval), as opposed to ``start_hotkey_listener``, which is the *wiring*
    entry point the launch and attach paths use. See
    ``supervised_hotkey_target`` for why the two must differ.

    Idempotent by construction -- it delegates to ``start_hotkey_listener``, so
    a healthy current listener is a pid-file read plus a manifest read and no
    spawn, and the "never two listeners" property is exactly the one that
    function already had.

    With a ``watch`` it also replaces a WEDGED listener (see
    ``retire_wedged_listener``). Replacement is not re-aiming: the target is
    read from the manifest BEFORE the old listener is ended -- ending it forgets
    the manifest -- and the new one is started at exactly that target, so
    `magent attach`'s remote wiring survives it. Without a ``watch`` (the
    one-shot callers) nothing is ever ended.

    Refused in a non-interactive logon session (Session 0) before the pid is
    even read: ``start_hotkey_listener`` would refuse the replacement there, so
    retiring a wedged listener first would end one and put nothing back.

    Windows-only, like everything hotkey: the caller owns the
    ``supports_hotkey()`` gate that keeps the import below reachable.
    """
    refusal = session0_block(SESSION0_HOTKEY_REFUSAL)
    if refusal:
        get_logger("hotkey").warning("%s", refusal)
        return None
    from magent.hotkey import (  # ImportError off-Windows (hotkey.py guards); must stay lazy
        listener_manifest,
        listener_pid,
    )

    pid = listener_pid()
    if pid is None:
        return start_hotkey_listener(default_url, None)
    url, ssh_host = supervised_hotkey_target(listener_manifest(), default_url)
    if watch is not None:
        retire_wedged_listener(pid, watch, now=time.time(), mono=time.monotonic())
    return start_hotkey_listener(url, ssh_host)


# --- Upload-server supervision ------------------------------------------------
# The same doctrine as the Alt+V listener above, one process up. `magent serve`
# is what every mobile upload and every Alt+V press goes through, and nothing in
# the product ever re-checked that it was still there: attach panes redial,
# sessions get revived, the listener is supervised -- serve alone had no
# supervisor and left no trace when it died. It died silently twice in one day
# (a machine-wide ConPTY wedge, then an unexplained disappearance over three
# hours), and both times the first symptom was an Alt+V press doing nothing.
#
# serve cannot supervise itself: a supervisor that only ran while serve ran
# would supervise nothing the moment serve died. The attention daemon is the
# other long-lived process, it already polls on an interval, and it is the one
# users leave running -- so it is the owner.
#
# All of this lives here, next to spawn_detached and ensure_hotkey_listener,
# rather than in cli/background.py where the spawn recipe started: launch.py
# must not import the cli package (cli/__init__ imports every command module, so
# a reverse import cycles -- LS-A-001), and a supervisor in a src module cannot
# reach a recipe that lives in one. cli/background._maybe_start_upload_server is
# now a thin delegation to ensure_upload_server for exactly that reason.

UPLOAD_RESPAWN_COOLDOWN_S = 60.0


def _probe_upload_port(port: int) -> bool:
    """True when something accepts a TCP connection on loopback ``port``.

    The same question ``cli/background._probe_port`` and ``status`` ask, with
    the same 0.3s budget: a refused loopback connect answers instantly, and a
    probe that could block would freeze the loop it rides on.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.3)
    try:
        probe.connect(("127.0.0.1", port))
    except OSError:
        return False
    else:
        return True
    finally:
        probe.close()


def upload_server_argv(port: int, config_path: str | None) -> list[str]:
    """The argv of a detached ``magent serve`` on ``port``.

    One builder for both spawn sites (the bring-up ensure and the supervisor),
    so the revived server can never drift from the one the launch path starts --
    same interpreter, same ``--config``, same port.
    """
    args = [sys.executable, "-m", "magent"]
    if config_path:
        args += ["--config", config_path]
    return [*args, "serve", "-p", str(port)]


def ensure_upload_server(port: int, config_path: str | None = None) -> bool:
    """Start the upload server detached unless something already answers on
    ``port``. Returns True when a spawn was actually issued.

    Detached because it must outlive the SSH bring-up command that spawns it --
    see ``spawn_detached``.
    """
    if _probe_upload_port(port):
        return False
    # The choke point for every serve spawner that does not hand off (--go,
    # menu "u", and anything run over ssh): a Session-0 serve takes the port
    # this host's desktop needs. Asked only once a spawn is due, so a live
    # server costs no probe of the logon session.
    refusal = session0_block(SESSION0_SERVE_REFUSAL)
    if refusal:
        get_logger("upload").warning("%s", refusal)
        return False
    spawn_detached(upload_server_argv(port, config_path))
    return True


def _validated_env(
    label: str = "upload supervisor", log_name: str = "attention"
) -> MagentEnv | None:
    """The env singleton, or None if it no longer validates.

    A daemon must never die of an environment variable it does not use, and by
    the time the attention loop is running every other MAGENT_* consumer has
    already failed loudly at CLI entry -- so an env that goes bad underneath a
    detached process degrades to the defaults with a log line, exactly as
    ``upload_server.supervision_enabled`` and ``log._configured_level`` do.
    ``label`` names the asking supervisor and ``log_name`` is its log, so the
    line lands where that supervisor's reader looks.
    """
    from pydantic import ValidationError

    from magent.env import get_env

    try:
        return get_env()
    except ValidationError:
        get_logger(log_name).warning(
            "%s: environment did not validate; using defaults", label
        )
        return None


def upload_supervision_enabled() -> bool:
    """Whether MAGENT_UPLOAD_SUPERVISOR permits the attention daemon to keep
    ``magent serve`` alive. Public because ``status`` must ask the same question
    the supervisor answers before it offers the daemon as a repair."""
    env = _validated_env()
    return True if env is None else env.upload_supervisor


# --- Node sync supervision ---------------------------------------------------
# The same doctrine one more time: `magent serve` is the process that is always
# there, so it keeps `magent node sync` alive (upload_server._supervise_node_sync
# -> ensure_node_sync every NODE_SYNC_SUPERVISE_INTERVAL_S). Two gates, like the
# upload watchdog's: the config must have a node project to sync, and
# MAGENT_NODE_SYNC must not say 0 -- the opt-out for a user who runs the daemon
# themselves, and the test-isolation law (a real daemon dials real machines).
# The env gates ONLY this supervised spawn: `magent node sync --once/-d` typed by
# a person (or an e2e test) never reads it.


def node_sync_env_enabled() -> bool:
    """Whether MAGENT_NODE_SYNC permits serve to keep the node sync daemon alive.
    Fail-open on an env that no longer validates, like every supervisor: the
    config gate (``node_sync.expected``) still has to pass."""
    # in-body: keeps launch's import list the launch path's
    from magent.node_sync import LOG_NAME

    env = _validated_env("node sync supervisor", LOG_NAME)
    return True if env is None else env.node_sync


def node_sync_enabled(config: MagentConfig) -> bool:
    """Both gates: the env allows it, and ``node_sync.expected`` -- a project
    runs on a node AND a session is placed on one. Without the second, serve
    would respawn the daemon that just wound down for want of work."""
    # in-body: keeps launch's import list the launch path's
    from magent.node_sync import expected

    return node_sync_env_enabled() and expected(config)


def node_sync_argv(config_path: str | None) -> list[str]:
    """The argv of a detached ``magent node sync`` (the foreground loop)."""
    args = [sys.executable, "-m", "magent"]
    if config_path:
        args += ["--config", config_path]
    return [*args, "node", "sync"]


@dataclass
class _NodeSyncReport:
    """What ensure_node_sync last said about a wedged daemon: the warning
    fires on the transition into a wedge and the recovery on the way out,
    never once per supervisor interval."""

    wedged: bool = False


_node_sync_report = _NodeSyncReport()


def ensure_node_sync(config: MagentConfig, config_path: str | None = None) -> bool:
    """Start the node sync daemon detached unless it is gated off or already
    running. True ONLY when a spawn was actually issued (the
    ``ensure_upload_server`` contract); False when a gate is off, when a
    healthy daemon is already running, and when the running one is wedged.

    "Running" is the daemon's LOCK (``node_sync.daemon_running``), never its pid
    file: after a crash or a reboot the pid file survives, the number is
    recycled onto an unrelated process, and a pid check would read "alive"
    forever -- never respawning, and pointing the user's `--stop` at a
    stranger. The pid is read for the log line only.

    A live daemon is NEVER re-aimed or replaced: it re-reads its own config file
    when that changes, and a second one would only lose the lock. A held lock
    with a stale heartbeat is a wedged daemon -- reported once, left for the
    user (`magent node sync --stop`), never killed from here. The respawn rate
    of a daemon that keeps dying is the caller's interval.

    A lock file that would not open (Windows answers EACCES while one is
    pending delete) is ``node_sync.DaemonLockUnknown``: whether a daemon runs is
    unknown, so nothing is spawned, and the caller can tell it from a refused
    spawn.
    """
    if not node_sync_enabled(config):
        return False
    from magent import node_sync  # in-body: same reason as node_sync_enabled

    log = get_logger(node_sync.LOG_NAME)
    try:
        running = node_sync.daemon_running()
    except OSError as e:
        raise node_sync.DaemonLockUnknown(e) from e
    if not running:
        _node_sync_report.wedged = False
        spawn_detached(node_sync_argv(config_path))
        return True
    if heartbeat_fresh(node_sync.HEARTBEAT_NAME):
        if _node_sync_report.wedged:
            _node_sync_report.wedged = False
            log.info("node sync: the daemon's heartbeat is fresh again")
        return False
    if not _node_sync_report.wedged:
        _node_sync_report.wedged = True
        log.warning(
            (
                "node sync: the daemon (pid %s) holds its lock but its heartbeat "
                "is stale; leaving it (`magent node sync --stop` to restart it)"
            ),
            node_sync.daemon_pid(),
        )
    return False


def upload_respawn_cooldown_s() -> float:
    """The configured minimum seconds between two respawn attempts."""
    env = _validated_env()
    configured = None if env is None else env.upload_respawn_cooldown_s
    if configured is None:
        return UPLOAD_RESPAWN_COOLDOWN_S
    return max(0.0, configured)


class UploadServerSupervisor:
    """Revives a dead ``magent serve``, at most once per cooldown.

    ``tick`` is called once per attention poll, so DETECTION latency is the poll
    interval while the RESPAWN RATE is bounded by ``cooldown_s``. The split is
    the whole design: a serve that dies at 03:00 must not wait out a long timer
    before anyone notices, and a serve that crashes on startup must not be
    respawned in a tight loop. Looking is free; spawning is not.

    Liveness is the loopback TCP probe. The recorded pid is read too, but only
    for the log line, and deliberately so: ``run_server`` writes its pid file
    AFTER the bind, so the pid can never be the earlier signal, and a pid number
    the OS later recycles onto an unrelated process would blind the watchdog
    permanently. What the pid does buy is a truthful diagnosis in the log --
    "recorded pid 8123 is gone" (the observed failure) reads very differently
    from "pid 8123 is alive but not answering", which is a wedge, not a death.

    A spawn that fails outright (``spawn_detached`` raising) propagates to the
    caller, which logs it and ticks again next poll -- see
    ``cli/attention_cmd._upload_watchdog``. The handling lives there, at the
    boundary with the loop that must survive, rather than here: a supervisor
    that could take down the daemon it rides on would be trading one silent
    death for another.
    """

    def __init__(
        self,
        port: int,
        config_path: str | None = None,
        *,
        cooldown_s: float | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._port = port
        self._config_path = config_path
        self._cooldown = (
            upload_respawn_cooldown_s() if cooldown_s is None else cooldown_s
        )
        self._now = now
        self._last_spawn: float | None = None
        self._child: subprocess.Popen[bytes] | None = None

    @property
    def cooldown_s(self) -> float:
        """The resolved cooldown — read by the daemon's startup log line so the
        value in force is visible without re-deriving it from the env."""
        return self._cooldown

    def _pid_note(self) -> str:
        """How the recorded pid contradicts (or corroborates) the dead port."""
        from magent.upload_server import (  # in-body: upload_server imports launch back, and only one direction may be a module-level import
            server_pid,
        )

        pid = server_pid(self._port)
        if pid is None:
            return "no pid file"
        return f"recorded pid {pid} is {'alive' if pid_alive(pid) else 'gone'}"

    def _still_starting(self, now: float) -> bool:
        """The serve this supervisor last spawned is alive and still inside the
        shared registration window: slow, not failed (DESIGN.md section 2, "A
        slow child is not a failed child"). Measured on a loaded desktop, a serve
        took ~4.7s to bind, so a short cooldown respawned beside it. Before the
        exclusive bind, that second bind SUCCEEDED on Windows (``SO_REUSEADDR``):
        two live servers on one port. Now it exits with ``PortInUse``, but a
        spawn that can only fail is still a wasted one. Past the
        window, or once the child has exited, the cooldown alone decides, as it
        always did. The child is never ended here."""
        if self._child is None or self._last_spawn is None:
            return False
        if now - self._last_spawn >= REGISTRATION_TIMEOUT_S:
            return False
        return self._child.poll() is None

    def tick(self) -> bool:
        """One liveness check. True when a respawn was issued."""
        if _probe_upload_port(self._port):
            return False
        log = get_logger("attention")
        now = self._now()
        if self._last_spawn is not None and (now - self._last_spawn) < self._cooldown:
            log.debug(
                "upload supervisor: port %d still dead, within the %.0fs cooldown",
                self._port,
                self._cooldown,
            )
            return False
        if self._still_starting(now):
            log.debug(
                "upload supervisor: port %d not answering yet; the serve spawned "
                "%.1fs ago is still starting",
                self._port,
                now - (self._last_spawn or now),
            )
            return False
        self._last_spawn = now
        refusal = session0_block(SESSION0_SERVE_REFUSAL)
        if refusal:
            # Stamped like a spawn so the refusal is logged once per cooldown,
            # not once per poll; there is no child for _still_starting to time.
            log.warning("upload supervisor: %s", refusal)
            return False
        # ASCII only: this line goes to a rotating logfile that gets read back
        # through whatever the host console's code page happens to be.
        log.warning(
            "upload supervisor: nothing answering on port %d (%s); starting a new "
            "magent serve",
            self._port,
            self._pid_note(),
        )
        # Forget the previous child first: if this spawn raises, that child must
        # not be timed against this attempt's stamp as though it were the new one.
        self._child = None
        self._child = spawn_detached(upload_server_argv(self._port, self._config_path))
        return True


@dataclass
class RunOpts:
    retile_all: bool = False
    dry_run: bool = False
    group: str | None = None
    config_path: str = ""
    # Tile what is already open and launch nothing: the dispatchers still build
    # the full target list but skip every spawn -- no IDE, no terminal, no
    # psmux collection. A window the user closed must stay closed, and under
    # `retile_all` it is dropped from the tiling set entirely (see
    # `_retile_targets`) rather than waited on and reported "not found".
    tile_only: bool = False
    # The project names the user checked in `cli/checklist.py`, or None for
    # "every enabled project" -- which is what a skipped checklist (no terminal,
    # or `--all`) means, and what this phase has always done.
    only: frozenset[str] | None = None
    # Node projects (PR-D): start one whose local tree is dirty or has
    # unpushed commits anyway -- the node gets origin's copy (D7).
    allow_dirty: bool = False


@dataclass
class _Target:
    name: str
    key: str
    mode: str
    is_new: bool


def _candidate_path(raw: str, base_dir: str | None) -> str | None:
    """Where ``raw`` would be, expanded and joined to ``base_dir`` when it
    is relative; None when relative with no base. Touches no disk."""
    expanded = os.path.expandvars(os.path.expanduser(raw))
    if Path(expanded).is_absolute():
        return expanded
    if base_dir:
        return os.path.join(base_dir, expanded)
    return None


def _resolve_path(raw: str, base_dir: str | None) -> str | None:
    candidate = _candidate_path(raw, base_dir)
    return candidate if candidate and Path(candidate).is_dir() else None


def _expand_base_dir(base_dir: str) -> str:
    """Normalize a configured base dir: expand env vars and ~, then unify
    forward slashes to the OS separator."""
    return os.path.expandvars(os.path.expanduser(base_dir)).replace("/", os.sep)


def _get_session_ids(
    tool: str, project_dir: str, count: int, config_dir: Path | None = None
) -> list[str | None]:
    """``project_dir``'s resumable session ids for ``tool``, newest first.

    ``config_dir`` names which of that tool's stores answers for the project --
    None is its default store, i.e. today's answer for every project no account
    was chosen for. See ``sessions.build_start_command``.
    """
    caps = AGENT_TOOLS.get(tool)
    if caps and caps.session_ids:
        return caps.session_ids(project_dir, count, config_dir)
    return [None] * count


HAPPY_AGENTS = {
    t for t, c in AGENT_TOOLS.items() if c.happy
}  # derived; name kept for tests


def _psmux_session_name(title: str) -> str:
    """Sanitize a window title into a valid psmux/tmux session name.

    Thin wrapper kept for backward compatibility with upload_server's import.
    Delegates to ``psmux.session_name()``.
    """
    from magent.psmux import session_name

    return session_name(title)


def _wrap_happy(tool: str, cmd: str) -> str:
    """Wrap a CLI agent command with Happy for mobile/web access."""
    if tool in HAPPY_AGENTS:
        return f"happy {cmd}"
    return cmd


def run_magent(config: MagentConfig, opts: RunOpts) -> int:
    log = get_logger("launch")
    plat = get_platform()

    slots = _prepare_grid(plat, config, opts)
    if slots is None:
        log.error("no monitors detected; aborting")
        click.echo(f"  {style('✗', fg='red')} No monitors detected.", err=True)
        return 2

    projects = _select_projects(config, opts)
    if projects is None:
        return 0

    # "node": "auto" becomes a nick here, before any dispatcher runs -- the
    # same slot account routing takes on its branch. A dry run or a tile-only
    # pass never opens a connection to sample a node.
    placements = place_node_projects(
        config, projects, live=not (opts.dry_run or opts.tile_only)
    )
    for note in placements.notes:
        click.echo(f"  {style('!', fg='yellow')} {style(note, dim=True)}")
    for line in placements.refused:
        click.echo(f"  {style('x', fg='red')} {line}")
    projects = placements.projects

    base_dir = config.base_dir
    if base_dir:
        base_dir = _expand_base_dir(base_dir)

    try:
        result = _launch_projects(plat, config, opts, projects, base_dir)
    except TerminalNotFoundError as exc:
        # The OS terminal emulator is missing (e.g. Windows Terminal not
        # installed). Surface the actionable install hint as one clean line --
        # no traceback -- and abort, mirroring the no-monitors failure shape.
        log.exception("terminal launcher unavailable; aborting")
        click.echo(f"  {style('✗', fg='red')} {exc}", err=True)
        return 2

    _start_psmux_and_upload(plat, config, opts, result)
    if result.node_projects:
        result = _bring_up_node_windows(plat, config, opts, result)

    targets = (
        _retile_targets(config, opts, result) if opts.retile_all else result.targets
    )
    _tile_targets(plat, opts, slots, targets)

    return 0


def _prepare_grid(
    plat: Platform, config: MagentConfig, opts: RunOpts
) -> list[TileSlot] | None:
    """DPI-init, enumerate monitors, compute the tile grid, print the grid/
    dry-run banner. Returns the tile slots, or None when no monitors are
    detected -- the caller owns the no-monitors echo/log/exit code."""
    plat.set_dpi_aware()

    monitors = plat.list_monitors()
    if not monitors:
        return None

    slots = compute_grid(monitors, config.layout.columns, config.layout.rows)

    grid_label = f"{config.layout.columns}x{config.layout.rows}"
    click.echo(
        f"\n  {style('#', fg='cyan')} {style(str(len(monitors)), fg='cyan', bold=True)} screen(s)  "
        f"{style('->', dim=True)}  {style(str(len(slots)), fg='green', bold=True)} tile slots  "
        f"{style(f'({grid_label} per screen)', dim=True)}"
    )
    if opts.dry_run:
        click.echo(
            f"  {style('! DRY RUN', fg='yellow', bold=True)} {style('-- nothing will be launched or moved.', dim=True)}\n"
        )

    return slots


def _select_projects(config: MagentConfig, opts: RunOpts) -> list[ProjectConfig] | None:
    """Enabled projects, optionally narrowed to opts.group and then to the
    names in opts.only. Returns None (caller exits 0) when a named group matches
    nothing (after printing the same 'No projects in group' message it does
    today), or when the checked set matches no project at all.

    Group first, then the checklist: the checklist was shown over the group's
    projects, so narrowing the other way round could only ever widen it back.
    """
    projects = [p for p in config.projects if p.enabled]
    if opts.group:
        projects = [
            p for p in projects if p.group and p.group.lower() == opts.group.lower()
        ]
        if not projects:
            groups = sorted({p.group for p in config.projects if p.group})
            click.echo(
                f"No projects in group '{opts.group}'. Available: {', '.join(groups)}",
                err=True,
            )
            return None
        click.echo(f"Group '{opts.group}': {len(projects)} project(s)")
    if opts.only is not None:
        projects = [
            p for p in projects if (p.title or get_leaf_name(p.path)) in opts.only
        ]
        if not projects:
            return None
    return projects


@dataclass(frozen=True)
class NodePlacements:
    """What the placement phase hands the launch phase (spec §11): the
    projects with every ``"auto"`` replaced by a concrete nick (an unplaceable
    one is dropped), the lines to print, and each auto project's Placement --
    `magent node plan` renders these same objects. ``notes`` are advisories;
    ``refused`` are failures -- an auto project not brought up because where
    it runs is unknown -- printed as a red ``x`` like ``up``'s.

    The same facts per project, for a caller that reports each project
    on its own row (``bring_up_node_projects``' outcomes): ``unplaced``
    maps each dropped auto project's name to why it was not launched --
    the words ``refused``/``notes`` print after the name -- and
    ``history_notes`` are the notes about a node's unreadable load
    history, which every project scored in this pass rests on."""

    projects: list[ProjectConfig]
    notes: list[str]
    placements: dict[str, NodePlacement]
    refused: list[str] = dataclasses.field(default_factory=list)
    unplaced: dict[str, str] = dataclasses.field(default_factory=dict)
    history_notes: tuple[str, ...] = ()


def _kept(
    config: MagentConfig, entries: dict[str, NodeMapEntry], proj: ProjectConfig
) -> bool:
    from magent import nodes

    held = entries.get(nodes.project_name(proj))
    return held is not None and held.nick in config.settings.nodes


def live_sampler(config: MagentConfig) -> Callable[[str], LoadSample | None]:
    """The sparse rule's one live reading per node, through remote_mux. A node
    that cannot be resolved or does not answer is left unscored, never fatal."""
    from magent import env, nodes, remote_mux

    user = env.local_username()
    log = get_logger("launch")
    # Warm remote_mux's own logger here, on the calling thread: get_logger is
    # check-then-set, so two first calls racing on sampler worker threads
    # could each attach a handler and double every "nodes" line.
    get_logger("nodes")

    def sample(nick: str) -> LoadSample | None:
        try:
            node = nodes.node_for_nick(config, nick, local_user=user)
            reading = remote_mux.sample(node)
        except (nodes.NodeConfigError, remote_mux.RemoteError) as exc:
            log.warning("live load sample for node %s failed: %s", nick, exc)
            return None
        return reading

    return sample


def _unplaced_reason(samples: dict[str, list[LoadSample]], *, live: bool) -> str:
    """Why no node could be scored, named per cause. ``samples`` is
    ``placement_samples``' output, where a node ends up with no sample only
    when its window was empty and either no live reading was allowed (a dry
    run or a tile-only pass -- the wording fits both) or the live reading
    failed -- a thin node always gets one."""
    from magent import nodes

    blank = ", ".join(nick for nick, window in samples.items() if not window)
    if not blank:
        return nodes.PLACE_REASONS["no-data"]
    if not live:
        return f"no live reading taken: {blank} would take a live reading at launch"
    return f"live reading failed for {blank} (see ~/.magent/logs/launch.log)"


def place_node_projects(
    config: MagentConfig,
    projects: list[ProjectConfig],
    *,
    live: bool = True,
    now: float | None = None,
) -> NodePlacements:
    """Resolve every ``"node": "auto"`` project to a nick (spec §11).

    Its own phase between selection and launch, so a dispatcher only ever sees
    a concrete nick. It NEVER writes node-map.json: the bring-up records a
    placement once it has actually happened, so a failed launch leaves nothing
    sticky behind. Nothing is sampled when every auto project is already
    placed on a configured node. ``live=False`` (``--dry-run``, tile-only)
    never opens a connection: a thin node is then scored on what it has.
    Only ``auto`` is ever placed: local, pinned and ``cloud`` projects
    (DECISION-15; ``cloud`` is pin-only) pass through untouched.

    The map is read strictly (``_node_map_for_placement``). Unreadable, NO
    ``auto`` project is placed: each is dropped with the map's refusal (in
    ``refused``, a failure) and an ``"unknown"`` Placement (D17: its node is
    None), nothing is sampled, and the rest of the fleet goes on. A node
    whose load history cannot be read is placed on as if it had none (one
    live reading, or unscored in a dry run) and named in ``notes``.
    """
    from magent import nodes
    from magent.config import NODE_AUTO

    auto = [p for p in projects if p.node == NODE_AUTO]
    if not auto:
        return NodePlacements(list(projects), [], {})
    entries, unreadable = _node_map_for_placement()
    if unreadable is not None:
        # Read as {}, an auto project already running on a node would be
        # scored onto a fresh one: a second session while the first runs.
        get_logger("nodes").warning(
            "auto placement skipped, node map unreadable: %s", unreadable
        )
        unknown = {
            nodes.project_name(p): _map_unreadable_text(unreadable) for p in auto
        }
        return NodePlacements(
            [p for p in projects if p.node != NODE_AUTO],
            [],
            {name: nodes.Placement(None, "unknown") for name in unknown},
            refused=[f"{name}: {text}" for name, text in unknown.items()],
            unplaced=unknown,
        )
    when = time.time() if now is None else now
    samples: dict[str, list[LoadSample]] = {}
    sampled: frozenset[str] = frozenset()
    unreadable_history: dict[str, OSError | ValueError] = {}
    if not all(_kept(config, entries, p) for p in auto):
        samples, sampled = nodes.placement_samples(
            config,
            now=when,
            live_sample=live_sampler(config) if live else None,
            on_unreadable=unreadable_history.__setitem__,
        )
    spread: dict[str, int] = {}
    out: list[ProjectConfig] = []
    # An unreadable history is unknown, not "never sampled": said, class only.
    history_notes = tuple(
        f"@{nick}: its load history is unreadable ({type(exc).__name__});"
        + (" scored on one live reading" if nick in sampled else " not scored")
        for nick, exc in unreadable_history.items()
    )
    notes: list[str] = list(history_notes)
    unplaced: dict[str, str] = {}
    chosen: dict[str, NodePlacement] = {}
    for proj in projects:
        if proj.node != NODE_AUTO:
            out.append(proj)
            continue
        name = nodes.project_name(proj)
        held = entries.get(name)
        placement = nodes.place(
            config,
            samples,
            now=when,
            map_entry=held.nick if held else None,
            placed=spread,
            live=sampled,
        )
        chosen[name] = placement
        if placement.note:
            notes.append(f"{name}: {placement.note}")
        if placement.nick is None:
            unplaced[name] = (
                f"not launched -- {_unplaced_reason(samples, live=live)};"
                ' pin a node with "node": "<nick>"'
            )
            notes.append(f"{name}: {unplaced[name]}")
            continue
        # A kept project's session is already running there and already counts
        # in that node's my_sessions; adding it to the spread would count it twice.
        if placement.reason != "kept":
            spread[placement.nick] = spread.get(placement.nick, 0) + 1
        out.append(dataclasses.replace(proj, node=placement.nick))
    return NodePlacements(
        out, notes, chosen, unplaced=unplaced, history_notes=history_notes
    )


@dataclass(frozen=True)
class _LaunchResult:
    """Everything the launch phase produces for the downstream phases."""

    targets: list[_Target]
    psmux_windows: list[PsmuxWindowOpts]
    psmux_colors: dict[str, str | None]
    # Window titles as the launch phase saw them -- the same snapshot its
    # already-running probe used. `_retile_targets` reads it to find
    # magent-owned windows that no configured project accounts for.
    open_titles: tuple[str, ...] = ()
    # Node projects this launch owes a bring-up (PR-D): collected by the loop,
    # brought up after the local phases, before tiling.
    node_projects: tuple[ProjectConfig, ...] = ()


def _discovered_targets(
    open_titles: tuple[str, ...], targets: list[_Target], prefix: bool
) -> list[_Target]:
    """magent-owned windows on screen that no configured project accounts for.

    These are `magent attach` panes: real magent windows whose names are the
    REMOTE host's session names, so they never appear in this machine's config
    and were invisible to `--retile-all` until now. They are never `is_new`
    (they are open by definition and nothing here launches them), so a plain
    `--go` still ignores them -- only a retile picks them up.

    Discovery is only possible with ``settings.windowTitlePrefix`` ON. With it
    off, magent's own titles are bare project names (``titles.make_title``
    with ``prefix=False``), indistinguishable from any other application's
    window, so there is nothing to key on and this returns nothing rather than
    guess.
    """
    if not prefix:
        return []
    known = {t.key for t in targets if t.mode == "magent-name"}
    return [
        _Target(name=name, key=name, mode="magent-name", is_new=False)
        for name in magent_window_names(open_titles)
        if name not in known
    ]


def _retile_targets(
    config: MagentConfig, opts: RunOpts, result: _LaunchResult
) -> list[_Target]:
    """The window set a `--retile-all` places: only what is on screen.

    Configured targets come first (config order), then the discovered extras
    in snapshot order, so slot assignment is deterministic. Under
    ``tile_only`` -- a retile that launches nothing -- a configured window
    that is not open right now is dropped: it can never appear, so enqueueing
    it would only buy `place_windows` a poll deadline and the user a red "not
    found" line. ``--go --retile-all`` keeps every configured target (the
    launch phase is bringing the missing ones up) and still gains the extras,
    which is the "then tile everything" half of its documented meaning.
    """
    base = (
        [t for t in result.targets if not t.is_new]
        if opts.tile_only
        else result.targets
    )
    extras = _discovered_targets(
        result.open_titles, result.targets, config.settings.window_title_prefix
    )
    return [*base, *extras]


# The one wording for a cloud project that loads (config accepts it) but has
# nothing to start a session on. J12's doctor row and J8's bring-up read it too.
NO_CLOUD_TASK = 'no "cloudTask" set: name the task the cloud session starts on'

# Why ``--go`` and the menu launch skip every cloud project when they have no
# psmux pane to put it in. `doctor` words the same condition with this constant;
# ``up`` does not read ``settings.psmux`` and is not subject to it.
CLOUD_NEEDS_PSMUX = (
    "cloud projects run in a psmux pane (settings.psmux, Windows); a plain"
    " terminal would create a new cloud session on every launch"
)


def launch_uses_psmux(config: MagentConfig, plat: Platform) -> bool:
    """Whether ``--go`` and the menu launch put agents in psmux panes: the
    setting is on AND this platform has psmux. The one spelling of the
    condition ``CLOUD_NEEDS_PSMUX`` is about, for the launch loop and `doctor`."""
    return config.settings.psmux and plat.supports_psmux()


# The one tool a cloud session runs: ``claude --cloud``.
CLOUD_TOOL = "claude"


def _exe_stem(exe: str) -> str:
    """``exe``'s file name without its directory, lower-cased, minus ONE
    ``.exe``/``.cmd`` (the two ways Windows spells a claude launcher). Either
    separator splits, whatever the OS: a configured path is typed into a pane,
    not resolved here."""
    name = exe.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def cloud_tool_refusal(tool: str, base_cmd: str | None) -> str | None:
    """Why this tool and command cannot start a cloud session, or None.

    ``claude --cloud`` is the only cloud session there is, and
    ``cloud_pane_command`` keeps just the first token of ``base_cmd``: a
    wrapper such as ``bash -c "claude ..."`` would be typed as ``bash --cloud``.
    So the tool must be ``claude`` and its command's executable must be a
    claude launcher. Read from config alone -- no git, no ssh -- so it can run
    first."""
    if tool != CLOUD_TOOL:
        return (
            f"a cloud session runs claude --cloud, but this project's tool is {tool!r}"
        )
    parts = (base_cmd or "").split()
    if not parts:
        return f"unknown tool {tool!r} (add under settings.tools)"
    if _exe_stem(parts[0]) != CLOUD_TOOL:
        return (
            f"a cloud session runs claude --cloud, but the {tool!r} command"
            f" starts with {parts[0]!r}, which is not claude"
        )
    return None


def cloud_command(tool: str, base_cmd: str | None, task: str | None) -> tuple[str, str]:
    """``(command, "")`` for a cloud pane, or ``("", why)`` when it has none.

    THE ladder every surface reads -- ``--go``, ``up``'s bring-up rows, the
    create gate and `doctor` -- so none words a refusal or orders two of them
    differently: the tool (``cloud_tool_refusal``), then the task
    (``NO_CLOUD_TASK``), then the command a pane can safely be typed
    (``cloud_pane_command``'s own ``ValueError`` text). An empty command is
    therefore never a bare ``bash --cloud "t"``. From config alone: no git, no
    ssh, no records."""
    # heavy subsystem: in-body per policy
    from magent.sessions.claude import cloud_pane_command

    refusal = cloud_tool_refusal(tool, base_cmd)
    if refusal:
        return "", refusal
    if not task:
        return "", NO_CLOUD_TASK
    try:
        return cloud_pane_command(base_cmd or "", task), ""
    except ValueError as exc:
        return "", str(exc)


def project_for_session(config: MagentConfig, sid: str) -> ProjectConfig | None:
    """The project the psmux session ``sid`` belongs to: the FIRST ENABLED one
    in config order, which is the one ``psmux.eligible_projects`` names the
    session after (it skips a disabled entry and keeps the first of a duplicate
    id). The id is ``nodes.node_sid``'s, the one spelling of the title-or-leaf
    rule."""
    # heavy subsystem: in-body per policy
    from magent import nodes

    for proj in config.projects:
        if proj.enabled and nodes.node_sid(proj) == sid:
            return proj
    return None


def twin_session_refusal(sid: str) -> str:
    """Why a cloud pane cannot be created under session name ``sid`` when the
    gate would read ANOTHER project: the gate answers by name for the first
    enabled project that owns it, and creating on that answer would skip the
    cloud project's own git and ``.env`` checks. The one wording both create
    paths (``--go`` and ``up``) refuse with."""
    return (
        f"another enabled project uses the session name {sid}; rename one (set a title)"
    )


def shadowed_cloud_projects(config: MagentConfig) -> list[tuple[ProjectConfig, str]]:
    """Every enabled cloud project whose session name ANOTHER enabled project
    owns, each with that session id: ``(project, sid)``, in config order.

    ``psmux.eligible_projects`` keeps the first of a duplicate id and the create
    gate reads the first enabled project by name, so a ``[local, cloud]`` pair
    for one folder silently never starts the cloud entry -- ``up`` says nothing.
    This is the one place that names it, for ``status`` (and anything else that
    can say so out loud) to word with ``twin_session_refusal``'s fix."""
    # heavy subsystem: in-body per policy
    from magent import nodes

    out: list[tuple[ProjectConfig, str]] = []
    for proj in config.projects:
        if not proj.enabled or not is_cloud(proj):
            continue
        sid = nodes.node_sid(proj)
        owner = project_for_session(config, sid)
        if owner is not None and owner is not proj:
            out.append((proj, sid))
    return out


def shadowed_local_projects(config: MagentConfig) -> list[tuple[ProjectConfig, str]]:
    """The mirror of ``shadowed_cloud_projects``: every enabled LOCAL project
    (one ``eligible_projects`` would list -- no IDE tool, host or node) whose
    session name an enabled CLOUD project owns, each with that session id.

    In a ``[cloud, local]`` pair for one folder the first-wins dedupe keeps the
    cloud row and drops the local one without a word, so ``up`` never starts the
    local agent and nothing says why. Named here for ``status``, with
    ``twin_session_refusal``'s fix."""
    # heavy subsystem: in-body per policy
    from magent import nodes

    out: list[tuple[ProjectConfig, str]] = []
    for proj in config.projects:
        if (
            not proj.enabled
            or is_cloud(proj)
            or proj.host
            or runs_on_node(proj)
            or is_ide_tool(proj.tool or config.settings.default_tool)
        ):
            continue
        sid = nodes.node_sid(proj)
        owner = project_for_session(config, sid)
        if owner is not None and owner is not proj and is_cloud(owner):
            out.append((proj, sid))
    return out


def cloud_refusal(config: MagentConfig, sid: str) -> str | None:
    """Why the cloud session for ``sid`` must NOT be created now, or None.

    Asked only right before a create -- never for a live session: every
    ``claude --cloud`` is a NEW cloud session (spec §18.5), so a refused create
    costs nothing and a wrong one costs a duplicate the CLI cannot list or stop.
    Gate, not advisory (plan J, "User decision"). Never raises for a reason it
    can name: a lock that stays taken, an unreadable file or a git that fails
    each come back as the refusal that says so."""
    # heavy subsystem: in-body per policy (nodes + the ssh/git layer)
    from magent import nodes, remote_mux
    from magent.node_sync import printable

    proj = project_for_session(config, sid)
    if proj is None or not is_cloud(proj):
        return None
    # tool -> task -> typing -> git -> push set, the order every surface
    # refuses in; the first three are the one ``cloud_command`` ladder.
    tool = proj.tool or config.settings.default_tool
    _cmd, why = cloud_command(tool, config.settings.tools.get(tool), proj.cloud_task)
    if why:
        return why
    try:
        return _cloud_checkout_refusal(config, proj, sid)
    except remote_mux.RemoteError as exc:
        lines = exc.row_text.strip().splitlines()
        if lines:
            said = lines[-1]
        elif exc.rc is None:
            # git never answered: it timed out, or could not be started.
            said = "no answer"
        else:
            said = f"exit {exc.rc}"
        return f"git could not read {proj.path}: {printable(said)}"
    # LockHeld and PushSetUnreadable are OSErrors: they come before it.
    except LockHeld:
        return "another magent is updating cloud hand-off state; try again"
    except nodes.PushSetUnreadable as exc:
        return (
            f"the push set cannot be checked: {printable(exc.label)} cannot be read"
            f" ({exc.reason}); fix or remove it, then run: magent node push"
            f" {nodes.project_name(proj)}"
        )
    except nodes.NodeConfigError as exc:
        return printable(str(exc))
    except OSError as exc:
        # The class only: the OS's own words carry an absolute path.
        get_logger("nodes").warning("cloud gate: %s: %s", sid, exc)
        return f"{proj.path}: {_local_error_text(exc)}"


def _cloud_checkout_refusal(
    config: MagentConfig, proj: ProjectConfig, sid: str
) -> str | None:
    """The git and ``.env`` half of ``cloud_refusal``. It RAISES whatever
    reading them raises; ``cloud_refusal`` is the one place that becomes words."""
    # heavy subsystem: in-body per policy
    from magent import nodes

    project_dir = _node_project_dir(config, proj)
    if project_dir is None:
        return f"{proj.path} was not found on this PC"
    states = node_git_states(config, proj)
    if not states:
        return (
            f"{proj.path} is not a git repository: a cloud session clones one"
            " from GitHub"
        )
    if len(states) > 1:
        return f"a cloud session runs ONE repository; {proj.path} holds {len(states)}"
    refusal = nodes.cloud_git_refusal(states[0])
    if refusal:
        return refusal
    ps = nodes.cloud_push_set(
        project_dir, states, home=Path.home(), extras=proj.push or ()
    )
    return nodes.cloud_env_refusal(
        sid, nodes.project_name(proj), ps, nodes.read_recipient()
    )


def _launch_projects(
    plat: Platform,
    config: MagentConfig,
    opts: RunOpts,
    projects: list[ProjectConfig],
    base_dir: str | None,
) -> _LaunchResult:
    """The per-project dispatch loop: launch IDEs/terminals (or collect psmux
    windows), build the tiling target list. Pure w.r.t. tiling -- it never
    moves a window."""
    has_remote = any(p.host for p in projects)
    # attach_client's rule, the client the remote panes dial: Windows' own
    # OpenSSH with nothing on PATH is not a missing client.
    if has_remote and attach_client.find_ssh() is None:
        click.echo(
            style(
                "  ! Remote projects configured but no ssh client found.", fg="yellow"
            )
        )

    targets: list[_Target] = []
    new_count = 0
    tools = config.settings.tools
    use_psmux = launch_uses_psmux(config, plat)
    psmux_windows: list[PsmuxWindowOpts] = []
    node_projects: list[ProjectConfig] = []
    _psmux_colors: dict[str, str | None] = {}

    win_snapshot = plat.snapshot_windows()

    def _is_running(key: str, mode: str) -> bool:
        if mode == "magent-name":
            return any(
                (parsed := parse_title(t)) is not None and parsed[0] == key
                for t in win_snapshot
            )
        if mode == "exact":
            return key in win_snapshot
        return any(key.lower() in t.lower() for t in win_snapshot)

    node_map = _node_map_snapshot(config, projects)

    for proj in projects:
        tool = proj.tool or config.settings.default_tool
        is_remote = bool(proj.host)

        if runs_on_node(proj) and not is_ide_tool(tool):
            new_count += _dispatch_node_project(
                config, opts, proj, tool, _is_running, targets, node_projects, node_map
            )
            continue

        if is_cloud(proj) and is_ide_tool(tool):
            # The user asked for a cloud session, and an IDE hosts none: say so
            # rather than open a local window that is not what they configured.
            title = proj.title or get_leaf_name(proj.path)
            click.echo(f"SKIP: {title} — {cloud_tool_refusal(tool, tools.get(tool))}")
            continue

        if is_ide_tool(tool):
            new_count += _dispatch_ide_project(
                plat,
                config,
                opts,
                proj,
                tool,
                is_remote,
                base_dir,
                _is_running,
                targets,
            )
            continue

        new_count += _dispatch_cli_agent_project(
            plat,
            config,
            opts,
            proj,
            tool,
            is_remote,
            base_dir,
            tools,
            use_psmux,
            _is_running,
            targets,
            psmux_windows,
            _psmux_colors,
        )

    return _LaunchResult(
        targets=targets,
        psmux_windows=psmux_windows,
        psmux_colors=_psmux_colors,
        open_titles=tuple(win_snapshot),
        node_projects=tuple(node_projects),
    )


def _dispatch_ide_project(
    plat: Platform,
    config: MagentConfig,
    opts: RunOpts,
    proj: ProjectConfig,
    tool: str,
    is_remote: bool,
    base_dir: str | None,
    is_running: Callable[[str, str], bool],
    targets: list[_Target],
) -> int:
    """Launch (or skip, if already running) a code/vscode/cursor project's
    IDE window; append its tiling target to the caller-owned `targets` list.
    Returns the new_count delta (1 if newly launched, 0 if already running)."""
    key = (
        get_leaf_name(proj.remote_path or proj.path)
        if is_remote
        else get_leaf_name(proj.path)
    )
    name = proj.title or key
    running = is_running(key, "contains")
    if not running and not opts.dry_run and not opts.tile_only:
        vsc_dir = (
            proj.remote_path or proj.path
            if is_remote
            else (_resolve_path(proj.path, base_dir) or proj.path)
        )
        ide_cmd = ide_command(tool)
        plat.launch_vscode(
            VSCodeLaunchOpts(
                dir=vsc_dir,
                ssh_host=proj.host if is_remote else None,
                command=ide_cmd,
            )
        )
        time.sleep(config.settings.launch_delay_ms / 1000)
    new_count_delta = 0 if running else 1
    targets.append(_Target(name=name, key=key, mode="contains", is_new=not running))
    _log_project(name, tool, running, proj.host, happy=False)
    return new_count_delta


def _dispatch_cli_agent_project(
    plat: Platform,
    config: MagentConfig,
    opts: RunOpts,
    proj: ProjectConfig,
    tool: str,
    is_remote: bool,
    base_dir: str | None,
    tools: dict[str, str],
    use_psmux: bool,
    is_running: Callable[[str, str], bool],
    targets: list[_Target],
    psmux_windows: list[PsmuxWindowOpts],
    psmux_colors: dict[str, str | None],
) -> int:
    """Generate this project's window titles, resolve resumable sessions, and
    launch (or collect into the caller-owned `psmux_windows`) each window;
    append its tiling target(s) to the caller-owned `targets` list. Returns
    the new_count delta (windows newly launched or newly collected, summed
    across every window this project owns)."""
    new_count = 0

    # windowTitlePrefix off: titles are bare project names, so the magent:
    # grammar can't resolve them. The launcher set the title itself, so it
    # tiles (and probes "already running") by exact-title match instead.
    prefix = config.settings.window_title_prefix
    match_mode = "magent-name" if prefix else "exact"

    windows_cfg = proj.windows
    if is_remote or is_ide_tool(tool) or is_cloud(proj):
        windows_cfg = None
    titles = generate_titles(proj.title, proj.path, windows_cfg)
    window_count = len(titles)

    # The directory the agent command will actually run in, or None when this
    # machine cannot honestly answer for it. A remote project's command runs on
    # the far host, so neither the resume scan below nor the fresh-start probe
    # may consult THIS machine's session store.
    agent_dir = None if is_remote else _resolve_path(proj.path, base_dir)

    session_ids: list[str | None] = [None] * window_count
    caps = AGENT_TOOLS.get(tool)
    if window_count > 1 and caps and caps.multi_window and agent_dir:
        session_ids = _get_session_ids(tool, agent_dir, window_count)

    base_cmd = tools.get(tool)
    if not base_cmd:
        click.echo(
            f"SKIP: {titles[0]} — unknown tool '{tool}' (add under settings.tools)"
        )
        return new_count

    if is_cloud(proj):
        return _dispatch_cloud_project(
            config,
            opts,
            proj,
            tool,
            titles[0],
            base_cmd,
            base_dir,
            use_psmux,
            is_running,
            match_mode,
            targets,
            psmux_windows,
            psmux_colors,
        )

    use_happy = proj.happy if proj.happy is not None else config.settings.happy
    # Only a config with a cloud project pays for the owner lookup below.
    has_cloud = any(is_cloud(p) for p in config.projects)

    for i, win_title in enumerate(titles):
        win_cfg = windows_cfg[i] if windows_cfg and i < len(windows_cfg) else None
        override = win_cfg.tool if win_cfg and win_cfg.tool else None
        if override and override != tool:
            override_cmd = tools.get(override)
            if override_cmd is None:
                # An override naming a tool absent from settings.tools can't be
                # honored -- warn and fall back to the base tool ENTIRELY, so
                # resume/happy/log all reflect what actually runs.
                click.echo(
                    f"WARN: {win_title} — unknown tool '{override}' in windows[{i}]"
                    f" (add under settings.tools); using '{tool}'"
                )
                win_tool, win_base = tool, base_cmd
            else:
                win_tool, win_base = override, override_cmd
        else:
            win_tool, win_base = tool, base_cmd

        if win_cfg and win_cfg.command:
            # A per-window `command` is the user's literal command line. It is
            # never rewritten -- not even to drop a resume flag.
            cmd = win_cfg.command
        elif win_tool != tool:
            # Per-window override: the discovered session ids belong to the
            # base `tool`, not `win_tool` -- never reuse them for the override.
            cmd = (
                build_resume_command(win_tool, win_base, None)
                if window_count > 1
                else build_start_command(win_tool, win_base, agent_dir)
            )
        elif window_count > 1 and session_ids[i] is not None:
            cmd = build_resume_command(win_tool, win_base, session_ids[i])
        elif window_count > 1:
            cmd = build_resume_command(win_tool, win_base, None)
        else:
            # Single window: the configured command runs verbatim, so this is
            # the one place a bare `claude --continue` reaches a project
            # directory that may have no conversation to continue.
            cmd = build_start_command(win_tool, win_base, agent_dir)

        if use_happy:
            cmd = _wrap_happy(win_tool, cmd)

        proj_psmux = use_psmux and not is_remote
        # A psmux window's REAL title carries the sanitized session name --
        # that is what `attach_psmux` titles it with (spaces/dots/colons
        # become "-", see `psmux.session_name`). The already-running probe and
        # the tiling target must key on that same string: probing with the raw
        # title meant a project named "GitHub Advertisment" respawned a fresh
        # window on every --go and its tile pass hunted a title that never
        # exists ("x ... not found"). Non-psmux windows are titled with the
        # raw title, so they keep keying on it.
        tile_key = _psmux_session_name(win_title) if proj_psmux else win_title
        # A cloud project that owns this session name owns the pane: a local
        # window queued under it would be verified, re-sent and revived by
        # typing `claude --continue` into that `claude --cloud` session. The
        # cloud entry's own dispatch tiles and re-attaches the pane. (The cloud
        # twin of an earlier LOCAL project is the other half: it is skipped in
        # `_dispatch_cloud_project`.) Nothing is created or typed before this.
        if proj_psmux and has_cloud:
            owner = project_for_session(config, tile_key)
            if owner is not None and is_cloud(owner):
                click.echo(f"SKIP: {win_title} — {twin_session_refusal(tile_key)}")
                continue
        running = is_running(tile_key, match_mode)
        # Window-level dedupe, the same three-way rule the attach path uses:
        # an already-OPEN window is never collected, because every collected
        # window gets an `attach_psmux` -- which spawns a BRAND-NEW terminal
        # (`wt -w new ... psmux attach`) with no dedupe of its own. Only
        # `launch_psmux_session`'s `has-session` probe dedupes, and that
        # dedupes sessions, not windows. Closed window + live session =>
        # collected, create is skipped, attach reopens onto the live session;
        # dead session => collected, created, attached.
        if proj_psmux and not running and not opts.dry_run and not opts.tile_only:
            resolved_dir = _resolve_path(proj.path, base_dir)
            if resolved_dir:
                psmux_windows.append(
                    PsmuxWindowOpts(
                        window_name=tile_key,
                        cwd=resolved_dir,
                        command=cmd,
                    )
                )
                psmux_colors[tile_key] = proj.color
        if not running and not opts.dry_run and not opts.tile_only and not proj_psmux:
            if is_remote:
                resolved_dir = proj.remote_path or proj.path
                plat.launch_terminal(
                    TerminalLaunchOpts(
                        title=make_title(win_title, prefix=prefix),
                        cwd=os.getcwd(),
                        command=cmd,
                        color=proj.color,
                        ssh_host=proj.host,
                        ssh_remote_dir=resolved_dir,
                        ssh_shell=config.settings.ssh.shell,
                    )
                )
            else:
                resolved_dir = _resolve_path(proj.path, base_dir)
                if not resolved_dir:
                    click.echo(f"SKIP: {proj.path} not found")
                    continue
                plat.launch_terminal(
                    TerminalLaunchOpts(
                        title=make_title(win_title, prefix=prefix),
                        cwd=resolved_dir,
                        command=cmd,
                        color=proj.color,
                    )
                )
            if not proj_psmux:
                time.sleep(config.settings.launch_delay_ms / 1000)
        if not running:
            new_count += 1
        targets.append(
            _Target(name=tile_key, key=tile_key, mode=match_mode, is_new=not running)
        )
        _log_project(
            win_title, win_tool, running, proj.host, happy=use_happy, psmux=proj_psmux
        )

    return new_count


def _dispatch_cloud_project(
    config: MagentConfig,
    opts: RunOpts,
    proj: ProjectConfig,
    tool: str,
    title: str,
    base_cmd: str,
    base_dir: str | None,
    use_psmux: bool,
    is_running: Callable[[str, str], bool],
    match_mode: str,
    targets: list[_Target],
    psmux_windows: list[PsmuxWindowOpts],
    psmux_colors: dict[str, str | None],
) -> int:
    """One LOCAL psmux pane running ``claude --cloud "<task>"`` (spec §18.5):
    typed once, and created only past ``cloud_refusal``. A project that cannot
    be created is skipped by name, one ``SKIP:`` line; an already-open window or
    a live session is never gated, only re-tiled or re-attached."""
    # heavy subsystem: in-body per policy
    from magent import psmux as psmux_mod

    if not use_psmux:
        click.echo(f"SKIP: {title} — {CLOUD_NEEDS_PSMUX}")
        return 0
    # tool -> task -> typing -> git -> push set, the order every surface refuses
    # in. The first three are the one ``cloud_command`` ladder, asked before any
    # git is read: the tool decides what the project is.
    cmd, why = cloud_command(tool, base_cmd, proj.cloud_task)
    if why:
        click.echo(f"SKIP: {title} — {why}")
        return 0
    tile_key = _psmux_session_name(title)
    # The gate answers by session name, for the FIRST enabled project that owns
    # it. If that is another project (a local one listed ahead of this one),
    # the gate would be reading a project that is not being created: refuse
    # rather than create ungated. The other project's own dispatch already
    # tiles and re-attaches that session.
    if project_for_session(config, tile_key) != proj:
        click.echo(f"SKIP: {title} — {twin_session_refusal(tile_key)}")
        return 0
    # An entry identical to one already handled (the twin check compares by
    # value, so it passes both) must not queue a second `claude --cloud` under
    # the same name: a duplicate is a second billed cloud session, and relying
    # on the bring-up's has-session timing within one batch is not a guard.
    # Every path that queues a window or tiles a pane appends a target.
    if any(t.key == tile_key for t in targets):
        click.echo(
            f"SKIP: {title} — already queued under session {tile_key}"
            " (duplicate project entry)"
        )
        return 0
    running = is_running(tile_key, match_mode)
    if not running and not opts.dry_run and not opts.tile_only:
        resolved_dir = _resolve_path(proj.path, base_dir)
        if not resolved_dir:
            click.echo(f"SKIP: {proj.path} not found")
            return 0
        # THE liveness answer: a live session is re-attached, never gated and
        # never re-created. A probe that drops answers "not live", which asks
        # the gate: the safe direction, because a refusal only skips the
        # window and the bring-up's own has-session probe still dedupes.
        if not psmux_mod.live_sessions([tile_key]):
            refusal = cloud_refusal(config, tile_key)
            if refusal:
                click.echo(f"SKIP: {title} — {refusal}")
                return 0
        psmux_windows.append(
            PsmuxWindowOpts(
                window_name=tile_key,
                cwd=resolved_dir,
                command=cmd,
                resend=False,
                nick="cloud",
            )
        )
        psmux_colors[tile_key] = proj.color
    targets.append(
        _Target(name=tile_key, key=tile_key, mode=match_mode, is_new=not running)
    )
    _log_project(title, tool, running, None, psmux=True, node="cloud")
    # A dry run says what the real run would do: for an open window, nothing.
    if opts.dry_run and not running:
        # The gate reads git and the push set, which a preview must not: say so,
        # so the line cannot read as an approval.
        click.echo(
            style(f"      would run: {cmd} (create gate not consulted)", dim=True)
        )
    return 0 if running else 1


def _node_map_snapshot(
    config: MagentConfig, projects: list[ProjectConfig]
) -> dict[str, NodeMapEntry]:
    """ONE read of the node map for the whole launch loop: every node
    project's badge and dry-run preview answer from it, so none can disagree
    with another. A launch with no node project in it never reads the map."""
    if not any(
        runs_on_node(proj)
        and not is_ide_tool(proj.tool or config.settings.default_tool)
        for proj in projects
    ):
        return {}
    # heavy subsystem: in-body per policy
    from magent import nodes

    # Best effort on purpose: a badge and a dry-run line only DISPLAY; every
    # placement decision reads strictly (``_node_map_for_placement``).
    return nodes.read_node_map()


def _node_map_for_placement() -> tuple[
    dict[str, NodeMapEntry], OSError | ValueError | None
]:
    """The node map for a decision that places or stops something: ``(entries,
    None)``, or ``({}, the error)`` when it cannot be read -- torn, or still
    busy after its retries. Never ``read_node_map``'s ``{}``: an unreadable
    map is UNKNOWN, not "nothing is placed", and read as empty an ``auto``
    project running on a node would look free to place again. A pinned
    project needs no map to resolve, so only ``auto`` ones are refused
    (``_map_unreadable_text``)."""
    # heavy subsystem: in-body per policy
    from magent import nodes

    try:
        return nodes.load_node_map_strict(), None
    except (OSError, ValueError) as exc:
        return {}, exc


def _map_unreadable_text(exc: OSError | ValueError) -> str:
    """An ``auto`` project's one-line refusal under an unreadable map, in the
    sentence every surface shares (``nodes.map_unread_text``). The error CLASS
    only: the full error goes to nodes.log."""
    # heavy subsystem: in-body per policy
    from magent import nodes

    return (
        f"{nodes.map_unread_text(exc)}, so where this auto project runs is"
        " unknown; not brought up"
    )


def _folder_unknown_text(exc: OSError | ValueError, leaf: str, nick: str) -> str:
    """A pinned project's one-line refusal when its only folder rivals are
    ``auto`` projects an unreadable map hides. The error CLASS only."""
    # heavy subsystem: in-body per policy
    from magent import nodes

    return (
        f"{nodes.map_unread_text(exc)}, so whether '{leaf}' on {nick} is already"
        " in use is unknown; not brought up"
    )


def _dispatch_node_project(
    config: MagentConfig,
    opts: RunOpts,
    proj: ProjectConfig,
    tool: str,
    is_running: Callable[[str, str], bool],
    targets: list[_Target],
    node_projects: list[ProjectConfig],
    node_map: dict[str, NodeMapEntry],
) -> int:
    """A pool-node project: one window, titled by its session id. The
    bring-up itself runs after the local phases (``_bring_up_node_windows``);
    here it is listed, targeted for tiling and queued. The target is
    provisional until then: the bring-up re-keys it on the title the window
    actually opened under. The window is always C's ``magent:<sid>``, so the
    already-open probe matches in ``magent-name`` mode. ``node_map`` is the
    loop's one snapshot (``_node_map_snapshot``)."""
    # heavy subsystem: in-body per policy
    from magent import nodes

    sid = nodes.node_sid(proj)
    running = is_running(sid, "magent-name")
    held = node_map.get(nodes.project_name(proj))
    label = held.nick if proj.node == NODE_AUTO and held else str(proj.node)
    _log_project(nodes.project_name(proj), tool, running, None, node=label)
    targets.append(_Target(name=sid, key=sid, mode="magent-name", is_new=not running))
    # The skip comes BEFORE the preview: a dry run says what the real run
    # would do, and for an open window or a re-tile that is nothing.
    if running or opts.tile_only:
        return 0
    if opts.dry_run:
        _echo_node_dry_run(config, proj, held)
        return 0
    node_projects.append(proj)
    return 1


def _echo_node_dry_run(
    config: MagentConfig, proj: ProjectConfig, held: NodeMapEntry | None
) -> None:
    """Where ``proj`` would land, from config and the map alone: no ssh, no
    git, no write. ``held`` is the map entry the caller already read, so the
    badge and this preview answer from one snapshot."""
    # heavy subsystem: in-body per policy
    from magent import nodes
    from magent.env import local_username
    from magent.node_sync import printable

    try:
        # A folder this user may not read is named, like a bad pin.
        project_dir = _node_project_dir(config, proj) or Path(proj.path)
        node = nodes.resolve(
            config,
            proj,
            local_user=local_username(),
            placed=held.nick if held else None,
        )
        # Inside the try: a folder with no usable name is refused here too,
        # and a dry run names that reason rather than raising it.
        folder = nodes.remote_root_for(node, project_dir)
    except (ValueError, OSError) as exc:
        # The row the real run's outcome would show (``_node_error_text``).
        if isinstance(exc, OSError):
            get_logger("nodes").warning("dry run: %s: %s", nodes.node_sid(proj), exc)
            text = _local_error_text(exc)
        else:
            text = printable(str(exc))
        click.echo(f"      {style('x', fg='red')} {text}")
        return
    click.echo(style(f"      -> {node.target}:{folder}", dim=True))
    # DECISION-24: the real run provisions first (``_provision_once``).
    click.echo(style(f"      would provision {node.nick}", dim=True))


def _warn_node_windows_will_not_reconnect(plat: Platform) -> None:
    """``attach_client.spawn_attach_window`` degrades a supervisor missing
    from PATH to a bare-ssh pane silently; the batch caller says so once.
    Mirrors ``magent attach``'s ``_spawn_windows`` warning. Nothing to say
    where no window can open at all."""
    if not plat.supports_attach_windows() or attach_client.client_exe() is not None:
        return
    click.echo(
        f"  {style('!', fg='yellow')} {style(attach_client.CLIENT_EXE_NAME, bold=True)}"
        f" {style('is not on PATH -- node windows will not auto-reconnect.', fg='yellow')}"
    )
    click.echo(
        f"  {style('Reinstall with', dim=True)}"
        f" {style('pip install -U magent-multi-ai-agents-manager', bold=True)}"
        f"{style('.', dim=True)}"
    )


def _node_window_target(target: _Target, title: str) -> _Target:
    """``target`` re-keyed on the title its window really opened under. A
    ``magent:`` title matches by parsed name, so a state badge the attention
    daemon adds before the tile pass cannot hide it; anything else (never
    produced today) matches exactly."""
    parsed = parse_title(title)
    if parsed is None:
        return replace(target, key=title, mode="exact")
    return replace(target, key=parsed[0], mode="magent-name")


def _bring_up_node_windows(
    plat: Platform, config: MagentConfig, opts: RunOpts, result: _LaunchResult
) -> _LaunchResult:
    """Bring the queued node projects up with their windows, then point each
    one's tiling target at the title ``NodeBringUpOutcome.title`` carries back
    from the spawn -- never one rebuilt from the sid, so tiling cannot drift
    from the window. A project with no window coming (a failed bring-up, a
    platform without attach windows, a spawn that raised) has its target
    dropped rather than polled for and reported "not found" -- and where a
    window WAS meant to open, that is said here instead, since tiling no
    longer will."""
    # A clone is minutes of network: say what is running before it runs. The
    # caller only gets here with a non-empty queue, so N is never 0.
    count = len(result.node_projects)
    click.echo(
        f"\n  {style('#', fg='blue')} Bringing up "
        f"{style(str(count), fg='blue', bold=True)} node project(s)..."
    )
    _warn_node_windows_will_not_reconnect(plat)
    outcomes = _run_node_bring_ups(
        config, list(result.node_projects), allow_dirty=opts.allow_dirty, window=True
    )
    _echo_node_outcomes(outcomes)
    if any(o.ok for o in outcomes):
        # RunOpts spells "no config file" as "".
        _keep_node_sync(config, opts.config_path or None)
    windows_expected = plat.supports_attach_windows()
    by_sid = {o.sid: o for o in outcomes}
    targets: list[_Target] = []
    for target in result.targets:
        outcome = by_sid.get(target.key)
        if outcome is None:
            targets.append(target)
        elif outcome.ok and outcome.title is not None:
            targets.append(_node_window_target(target, outcome.title))
        elif outcome.ok and windows_expected:
            # The session is up; only the window failed (the spawn's reason is
            # in nodes.log). A re-run re-queues it and attaches to the session.
            click.echo(
                f"  {style('!', fg='yellow')} {outcome.sid}"
                f" {style('@' + outcome.node, fg='blue')}: window did not open"
                f" {style('(see ~/.magent/logs/nodes.log) -- re-run magent --go to open it', dim=True)}"
            )
    return replace(result, targets=targets)


def _start_psmux_and_upload(
    plat: Platform, config: MagentConfig, opts: RunOpts, result: _LaunchResult
) -> None:
    """Create + attach the collected psmux sessions and, when configured,
    spawn the upload server. No-op when result.psmux_windows is empty or dry_run."""
    psmux_windows = result.psmux_windows
    psmux_colors = result.psmux_colors
    if psmux_windows and not opts.dry_run:
        # In-body like every other psmux call here: psmux.eligible_projects
        # imports back into this module, so neither side may import the other
        # at top level. `launch_verified` is `launch_psmux_session` plus the
        # creation verify the attach path's `bring_up` gets -- the --go path
        # reaches sessions through the same platform call, so it would
        # otherwise be the one bring-up left with no proof a session came up.
        from magent import psmux

        failed = psmux.launch_verified(plat, psmux_windows)
        # Same honesty the attach/menu bring-up paths now have: a session the
        # verify proved never came up must not be counted among the ones this
        # path reports below. `--go` never hands off (it is a local,
        # interactive command by definition), so a Session-0 casualty here
        # means the choke point refused, and the note names it.
        if failed:
            click.echo()
        report_bring_up_casualties(failed)
        for pw in psmux_windows:
            plat.attach_psmux(
                pw.window_name,
                make_title(pw.window_name, prefix=config.settings.window_title_prefix),
                psmux_colors.get(pw.window_name),
            )
        # The fleet that was just created IS the interactive path -- every
        # keystroke in every pane crosses one of these processes. Sweeping here
        # (rather than flagging the spawn) is the only thing that can work: the
        # psmux SERVER is a grandchild forked by the one-shot client, and a
        # Windows priority class is not inherited across that. Failure is never
        # this path's problem -- the sessions are up either way.
        try:
            psmux.boost_priority()
        except OSError as exc:
            get_logger("launch").warning("psmux boost: priority sweep failed (%s)", exc)
        click.echo(
            f"\n  {style('#', fg='yellow')} psmux: {style(str(len(psmux_windows)), fg='yellow', bold=True)} sessions"
            f" {style('(synced with mobile)', dim=True)}"
        )
        click.echo(
            f"  {style('From SSH:', dim=True)} {style('psmux -L <name> attach', fg='cyan')}"
            f" {style('or', dim=True)} {style('magent sessions', fg='cyan')}"
        )

        # The sessions above were refused at the psmux choke point in Session
        # 0; the server and listener below would be refused at theirs, so say
        # it once here instead of advertising a URL nothing will answer.
        serve_refusal = config.settings.upload_server and session0_block(
            SESSION0_SERVE_REFUSAL, plat
        )
        if serve_refusal:
            click.echo(f"\n  {style(serve_refusal, dim=True)}")
        elif config.settings.upload_server:
            port = config.settings.upload_port
            # Probe first, like every other spawner: this used to spawn a
            # serve on EVERY bring-up, and on a machine whose server was
            # already up that second one could only die of "port in use".
            ensure_upload_server(port, opts.config_path)
            ip = tailnet.ip4()
            url = f"http://{ip}:{port}" if ip else f"http://localhost:{port}"
            click.echo(
                f"\n  {style('#', fg='magenta')} upload server: {style(url, fg='cyan', bold=True)}"
                f" {style('(open on phone)', dim=True)}"
            )

            # Only `magent attach` used to start the listener, so on a local
            # launch the psmux status bar advertised "F2 code" with nothing
            # listening. Point it at loopback, not the tailnet IP: a local
            # listener must not depend on Tailscale being up. Nested under the
            # upload_server gate because F2 resolves its folder via that
            # server's /api/sessions -- no server, nothing for F2 to do.
            if plat.supports_hotkey():
                pid = start_hotkey_listener(f"http://127.0.0.1:{port}")
                if pid:
                    click.echo(
                        f"  {style('#', fg='magenta')} hotkey listener: "
                        f"{style('Alt+V', fg='cyan', bold=True)}"
                        f" {style('pastes an image,', dim=True)} "
                        f"{style('F2', fg='cyan', bold=True)}"
                        f" {style('opens the project in VS Code', dim=True)}"
                    )


def _tile_targets(
    plat: Platform, opts: RunOpts, slots: list[TileSlot], targets: list[_Target]
) -> None:
    """Place (or, under dry_run, preview) each target into a slot. Delegates
    the resolve-and-move-with-retry logic to magent.tiling.place_windows
    (R13/E9's shared helper) -- no lookup/retry loop is re-implemented here.

    Under ``retile_all`` the caller has already narrowed `targets` to the
    windows that are actually on screen (`_retile_targets`), so everything
    here gets placed."""
    to_place = targets if opts.retile_all else [t for t in targets if t.is_new]

    if not to_place:
        # A retile with an empty set means nothing is open -- saying "already
        # positioned" would claim windows exist that don't.
        note = (
            "No open magent windows to tile."
            if opts.retile_all
            else "All windows already positioned."
        )
        click.echo(f"\n  {style('+', fg='green')} {note}")
        return

    mode_label = (
        style(" retile all", fg="yellow")
        if opts.retile_all
        else (style(" dry run", fg="yellow") if opts.dry_run else "")
    )
    click.echo(
        f"\n  {style('#', fg='cyan')} Tiling {style(str(len(to_place)), fg='cyan', bold=True)} window(s)...{mode_label}"
    )

    if opts.dry_run:
        for slot_idx, target in enumerate(to_place):
            pos = slots[slot_idx % len(slots)]
            screen_num = pos.monitor_index + 1
            dims = style(f"{pos.w}x{pos.h}", dim=True)
            at = style(f"({pos.x},{pos.y})", dim=True)
            click.echo(
                f"    {style('>', fg='cyan')} {target.name:<28} {style('->', dim=True)} screen {screen_num}  {dims} {at}"
            )
        click.echo(f"\n  {style('Done!', fg='green', bold=True)}")
        return

    placements = [
        Placement(
            name=target.name,
            key=target.key,
            mode=target.mode,
            slot=slots[i % len(slots)],
        )
        for i, target in enumerate(to_place)
    ]

    def _placed(p: Placement) -> None:
        click.echo(
            f"    {style('+', fg='green')} {p.name} {style('->', dim=True)} screen {p.slot.monitor_index + 1}"
        )

    def _missing(p: Placement) -> None:
        click.echo(
            f"    {style('x', fg='red')} {p.name} {style('not found', dim=True)}"
        )

    place_windows(plat, placements, on_placed=_placed, on_missing=_missing)

    click.echo(f"\n  {style('Done!', fg='green', bold=True)}")


def _log_project(
    name: str,
    tool: str,
    running: bool,
    host: str | None,
    happy: bool = False,
    psmux: bool = False,
    node: str | None = None,
) -> None:
    if running:
        icon = style("*", fg="green")
        label = style("open", dim=True)
    else:
        icon = style("o", fg="cyan")
        label = style("new", fg="cyan")
    loc = style(f" @ {host}", dim=True) if host else ""
    tool_badge = style(f"[{tool}]", dim=True)
    extras = ""
    if happy:
        extras += style(" [happy]", fg="magenta")
    if psmux:
        extras += style(" [psmux]", fg="yellow")
    if node:
        extras += style(f" [@{node}]", fg="blue")
    click.echo(f"  {icon} {name:<30} {label}  {tool_badge}{extras}{loc}")


# ---------------------------------------------------------------------------
# Headless psmux session management -- the host side of `magent attach`.
# These never open GUI windows, so they work over a plain SSH command.
# ---------------------------------------------------------------------------


def eligible_psmux_projects(
    config: MagentConfig, group: str | None = None
) -> list[dict[str, object]]:
    """Delegate to ``psmux.eligible_projects``."""
    from magent.psmux import eligible_projects

    return eligible_projects(config, group)


def psmux_status(
    config: MagentConfig, group: str | None = None
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    """Delegate to ``psmux.psmux_status``."""
    from magent import psmux

    return psmux.psmux_status(config, group)


def bring_up_psmux(
    config: MagentConfig,
    only: list[str] | None = None,
    group: str | None = None,
    *,
    allow_dirty: bool = False,
    config_path: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Delegate to ``psmux.bring_up``, then bring the pool-node projects up
    too (PR-D; no windows -- this is the host side of attach). Returns
    ``(created, failed)`` over both, where ``failed`` maps each session that
    stayed down to why ("" = see the log). A node casualty maps to "": its
    reason is printed with the node outcomes, the only place it appears, and
    ``report_bring_up_casualties`` must not print it twice. A node session
    that came up gets the node sync daemon started on ``config_path``, the
    file this bring-up read."""
    from magent import psmux

    created, failed = psmux.bring_up(config, only, group)
    outcomes = bring_up_node_projects(
        config, only=only, group=group, allow_dirty=allow_dirty
    )
    _echo_node_outcomes(outcomes)
    if any(o.ok for o in outcomes):
        # The sync daemon reads the same file this bring-up did (E).
        _keep_node_sync(config, config_path)
    return (
        [*created, *(o.sid for o in outcomes if o.ok)],
        {**failed, **{o.sid: "" for o in outcomes if not o.ok}},
    )


def revive_psmux(
    config: MagentConfig,
    only: list[str] | None = None,
    group: str | None = None,
    *,
    resume_parked: bool = False,
) -> list[str]:
    """Delegate to ``psmux.revive_sessions``. Every bulk caller (``up``,
    attach's ``up --json --revive``) takes the default, so a parked session
    stays parked."""
    from magent import psmux

    return psmux.revive_sessions(config, only, group, resume_parked=resume_parked)


def decorate_psmux_sessions(
    names: list[str],
    code_hint: bool | None = None,
    *,
    nicks: Mapping[str, str] | None = None,
) -> list[str]:
    """Delegate to ``psmux.decorate_sessions``.

    ``code_hint`` stays optional here (unlike ``decoration_argv``'s required
    one) so existing callers keep working and get the default "probe on this
    machine" behaviour, which is what every one of them wants.

    ``nicks`` (session name -> brand nick) is keyword-only, and a caller with
    no nick must not pass it at all: other code fakes this wrapper with a
    one-argument callable.
    """
    from magent import psmux

    return psmux.decorate_sessions(names, code_hint=code_hint, nicks=nicks)


def decorate_psmux_sessions_async(
    names: list[str],
    code_hint: bool | None = None,
    *,
    nicks: Mapping[str, str] | None = None,
) -> list[str]:
    """Delegate to ``psmux.decorate_sessions_async``.

    The status-path variant: fires the same commands without waiting, and is
    throttled by a stamp file. `up --json` uses this one so a slow psmux can
    never delay (or fail) a status query -- see the psmux docstring.

    ``nicks`` is keyword-only and passed only by a caller that has one, as in
    ``decorate_psmux_sessions``.
    """
    from magent import psmux

    return psmux.decorate_sessions_async(names, code_hint=code_hint, nicks=nicks)


def stop_psmux(names: list[str]) -> tuple[list[str], list[str]]:
    """Delegate to ``psmux.stop_sessions``. Returns ``(stopped, still_running)``.

    Replaces the old ``kill_psmux`` (a pass-through to the attempt-only
    ``kill_servers``): a shutdown command has to be able to tell the user what
    it PROVED it stopped, and what it could not.
    """
    from magent.psmux import stop_sessions

    return stop_sessions(names)


# ---------------------------------------------------------------------------
# Pool nodes (PR-D): a project with "node" set runs in tmux on that machine.
# Everything that dials a node is remote_mux; this block is the policy -- the
# D7 refusals, one bring-up per node at a time, the map write, the window.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeBringUpOutcome:
    """One node project's bring-up, for the shells to print. ``error`` is the
    whole user-facing reason when ``ok`` is False; ``warnings`` are true but
    non-fatal (an unshippable ``push`` entry, a refusal that was moot because
    the session was already running). ``title`` is the one the attach window
    opened under -- what tiling places it by -- or None when none opened."""

    ok: bool
    sid: str
    node: str = ""
    error: str | None = None
    attached_existing: bool = False
    warnings: tuple[str, ...] = ()
    title: str | None = None


# One bring-up per node at a time, within this process: two sessions' first
# `new-session` racing to start the node's one tmux server, or two clones into
# the same root, is not a failure anyone should have to diagnose. Different
# nodes run in parallel. A threading.Lock does not reach a second magent
# process; two `magent up`s at once can still race on one node.
_BRING_UP_LOCKS: dict[str, threading.Lock] = {}
_BRING_UP_LOCKS_GUARD = threading.Lock()


def _bring_up_lock(nick: str) -> threading.Lock:
    with _BRING_UP_LOCKS_GUARD:
        return _BRING_UP_LOCKS.setdefault(nick, threading.Lock())


_PROVISIONED: set[str] = set()


def _provision_once(node: Node, config: MagentConfig) -> None:
    """Make ``node`` able to run a project, at most once per process. Runs
    ``remote_mux.provision_node`` (DECISION-24) and records the node once that
    call returns. A ``RemoteError`` is logged and re-raised, failing this
    project: an unreachable node is left unrecorded so the next project on it
    retries, but one whose outcome is unknown (a timeout, an over-cap reply)
    is recorded, because a retry would start a second applier beside the
    first.
    ``config`` is here from day one so K needs no signature change.
    Called under the node's lock, after every refusal -- a refused project
    never provisions anything, and ``--dry-run`` never calls it."""
    if node.nick in _PROVISIONED:
        return
    # heavy subsystem: in-body per policy (remote_mux: ssh + tar)
    from magent import remote_mux

    log = get_logger("nodes")
    try:
        report = remote_mux.provision_node(
            node, config, home=Path.home(), timeout_s=remote_mux.PROVISION_TIMEOUT_S
        )
    except remote_mux.RemoteError as exc:
        if exc.outcome_unknown:
            # A killed ssh -- at the timeout or past the reply cap -- does not
            # stop the apply on the node (RemoteError): this run never
            # provisions the node again.
            _PROVISIONED.add(node.nick)
            log.warning(
                "provision @%s: outcome unknown (%s); not retrying this run",
                node.nick,
                exc,
            )
        else:
            log.warning(
                "provision @%s: failed (%s); the next project retries", node.nick, exc
            )
        raise
    _PROVISIONED.add(node.nick)
    for line in report.lines:
        if line.status in ("warn", "fail"):
            # A fail row never blocks the session (F3), and neither row
            # reaches the screen -- node doctor shows the node's; nodes.log
            # keeps both, this PC's gh row included.
            log.warning(
                "provision %s: %s %s: %s",
                node.nick,
                line.status,
                line.item,
                line.detail,
            )


def _node_project_dir(config: MagentConfig, proj: ProjectConfig) -> Path | None:
    """``proj``'s folder on this PC; None when it is not there. A folder this
    user may not read RAISES OSError instead (``nodes.path_is_dir``): from
    Python 3.14 ``Path.is_dir`` answers False for every OSError, which would
    call an unreadable folder "not found on this PC" there and raise on
    3.10-3.13. Unknown is never absent, on any version."""
    # heavy subsystem: in-body per policy
    from magent import nodes

    base_dir = _expand_base_dir(config.base_dir) if config.base_dir else None
    candidate = _candidate_path(proj.path, base_dir)
    if candidate is None:
        return None
    path = Path(candidate)
    return path if nodes.path_is_dir(path) else None


def node_git_states(config: MagentConfig, proj: ProjectConfig) -> list[LocalGitState]:
    """The LOCAL git state of every repo ``proj`` is made of; ``[]`` when its
    folder is missing or holds no repo. Raises RemoteError when git fails."""
    # heavy subsystem: in-body per policy (remote_mux is the ssh + git layer)
    from magent import remote_mux

    project_dir = _node_project_dir(config, proj)
    if project_dir is None:
        return []
    return [remote_mux.git_state(path) for path in remote_mux.repo_paths(project_dir)]


def node_recipe(
    config: MagentConfig, proj: ProjectConfig, node: Node, states: list[LocalGitState]
) -> Recipe:
    """``nodes.recipe_for`` plus what only the config knows: the tool, its
    command, and the command's fresh form (C1). Raises NodeConfigError."""
    # heavy subsystem: in-body per policy
    from magent import nodes

    project_dir = _node_project_dir(config, proj)
    if project_dir is None:
        raise nodes.NodeConfigError(f"{proj.path}: not found on this PC")
    tool = proj.tool or config.settings.default_tool
    command = config.settings.tools.get(tool)
    if not command:
        raise nodes.NodeConfigError(
            f"{proj.path}: unknown tool '{tool}' (add under settings.tools)"
        )
    recipe = nodes.recipe_for(
        proj, node, states, home=Path.home(), project_dir=project_dir
    )
    return replace(
        recipe,
        tool=tool,
        command=command,
        fresh_command=fresh_start_command(tool, command),
    )


def _node_error_text(exc: Exception) -> str:
    """The one line a user sees for a failed node bring-up: a RemoteError's
    last line of ``row_text`` (bring_up.sh writes its reason last, prefixed
    ``magent:``; over the cap it is magent's own ``reply exceeded N bytes``,
    and the child's words are nodes.log's), an OSError's class (its message
    names a path on this PC; ``nodes.log`` has it), anything else's message
    -- nodes' own words."""
    # heavy subsystem: in-body per policy
    from magent.remote_mux import RemoteError

    if isinstance(exc, RemoteError):
        lines = exc.row_text.strip().splitlines()
        return lines[-1].removeprefix("magent: ") if lines else f"exit {exc.rc}"
    if isinstance(exc, OSError):
        return _local_error_text(exc)
    return str(exc)


def _local_error_text(exc: OSError) -> str:
    """A node row's words for an OSError raised on this PC: its class only."""
    return f"local error: {type(exc).__name__}; see nodes.log"


def _node_busy_text(nick: str, waited_s: float) -> str:
    """A node whose ``node-pull-<nick>`` another magent process (a sync tick,
    a bring-up, a ``down``) held past ``waited_s`` -- pass the constant the
    wait itself used, so the figure is the wait that ran out. The bring-up's
    outcome and ``down``'s not-pulled line both say it this way."""
    return (
        f"node {nick} is busy: another magent process held its pull lock"
        f" past {waited_s:.0f}s"
    )


def _pull_error_text(exc: Exception) -> str:
    """``_node_error_text`` for ``down``'s not-pulled line, but an OSError is
    its class only: its message can name a local path, and its ``strerror`` is
    the OS's words (localized, per platform), not ours. ``nodes.log`` has the
    whole error."""
    if isinstance(exc, OSError):
        return type(exc).__name__
    return _node_error_text(exc)


def _open_node_window(node: Node, sid: str) -> str | None:
    """The attach window for ``sid`` on ``node`` -- C's one wt spawn, running
    the reconnecting supervisor -- and the title it opened under (tiling
    matches on that, never a rebuilt one). None when this platform has no
    attach windows, or the spawn failed: a window that cannot open is logged,
    never a failed bring-up, because the session is up either way."""
    if not get_platform().supports_attach_windows():
        return None
    try:
        # remote_mux.MUX, spelled here so this stays import-free; the pane's
        # remote command is derived from it inside spawn_attach_window.
        return attach_client.spawn_attach_window(node.target, sid, mux="tmux")
    except OSError as exc:
        get_logger("nodes").warning(
            "attach window for %s on %s: %s", sid, node.nick, exc
        )
        return None


def bring_up_node_project(
    config: MagentConfig,
    proj: ProjectConfig,
    *,
    allow_dirty: bool = False,
    window: bool = False,
    resume_id: str | None = None,
) -> NodeBringUpOutcome:
    """Bring ``proj`` up on its node. ``resume_id`` names the conversation to
    resume (only G's ``recall --to`` passes one, after shipping that
    conversation to the node -- DECISION-23); None lets the NODE pick --
    ``--continue`` over its own transcripts for this folder, else the tool's
    fresh form, so a fresh clone never starts a dead ``claude --continue``
    (DECISION-11c). Then: resolve the node (the map's placement for
    ``"auto"``), refuse a tree the node could not reproduce (D7) -- unless its
    session is already running there, which is attached instead -- then,
    under that node's lock, provision once, build the recipe, run
    ``remote_mux.bring_up`` -- inside the node sync daemon's lock for that
    node too, so no pull reads a half-made session -- record the placement and
    open the window. Never raises for a node, git or config failure: every one
    is an outcome, a node the sync daemon kept busy past one pull included."""
    # heavy subsystem: in-body per policy (nodes + remote_mux: ssh/git/tar)
    from magent import node_sync, nodes, remote_mux
    from magent.env import local_username

    log = get_logger("nodes")
    name = nodes.project_name(proj)
    sid = nodes.node_sid(proj)
    nick = ""
    entries, unreadable = _node_map_for_placement()
    if unreadable is not None and proj.node == NODE_AUTO:
        log.warning("node ?: %s refused, node map unreadable: %s", sid, unreadable)
        return NodeBringUpOutcome(
            ok=False, sid=sid, node="", error=_map_unreadable_text(unreadable)
        )
    try:
        # Unreadable, a pinned project goes to its pin: resolve ignores the
        # map for it, and the node attaches to a session already running.
        held = entries.get(name)
        node = nodes.resolve(
            config,
            proj,
            local_user=local_username(),
            placed=held.nick if held else None,
        )
        nick = node.nick
        if _node_project_dir(config, proj) is None:
            raise nodes.NodeConfigError(f"{proj.path}: not found on this PC")
        states = node_git_states(config, proj)
        if not states:
            raise nodes.NodeConfigError(
                f"{proj.path}: no git repository; a node clones the project "
                "from its origin"
            )
        refusals = [
            text
            for state in states
            if (text := nodes.refusal_for(state, allow_dirty=allow_dirty))
        ]
        if refusals:
            if unreadable is not None:
                # Nothing records where it runs, which is not "nowhere": ask
                # its pin, under its own session id. The whole error goes to
                # nodes.log once here; either outcome names only its class.
                log.warning(
                    "node %s: %s looked up on its pin, node map unreadable: %s",
                    nick,
                    sid,
                    unreadable,
                )
                running = sid if remote_mux.has_session(node, sid) else None
            elif (
                held is not None
                and held.nick == nick
                and remote_mux.has_session(node, held.sid)
            ):
                running = held.sid
            else:
                running = None
            if running is not None:
                # What is uncommitted HERE does not touch a session already
                # running there: attach to it, and keep the refusal as a warning.
                remote_mux.decorate(node, running, nick)
                title = _open_node_window(node, running) if window else None
                log.info(
                    "node %s: %s already running; attached despite: %s",
                    nick,
                    running,
                    refusals,
                )
                return NodeBringUpOutcome(
                    ok=True,
                    sid=sid,
                    node=nick,
                    attached_existing=True,
                    warnings=(
                        *refusals,
                        *(
                            (
                                (
                                    f"{nodes.map_unread_text(unreadable)};"
                                    f" attached to {sid} on its pin @{nick}"
                                ),
                            )
                            if unreadable is not None
                            else ()
                        ),
                    ),
                    title=title,
                )
            log.info("node %s: refused %s: %s", nick, sid, refusals)
            error = "; ".join(refusals)
            if unreadable is not None:
                error += (
                    f"; {nodes.map_unread_text(unreadable)},"
                    f" and {sid} was not found running on @{nick}"
                )
            return NodeBringUpOutcome(ok=False, sid=sid, node=nick, error=error)
        with _bring_up_lock(nick):
            try:
                _provision_once(node, config)
            except remote_mux.RemoteError as exc:
                # _provision_once logged it; the row names the step that failed.
                return NodeBringUpOutcome(
                    ok=False,
                    sid=sid,
                    node=nick,
                    error=f"provisioning: {_node_error_text(exc)}",
                )
            recipe = node_recipe(config, proj, node, states)
            # Spec section 13: a sync tick holds this same per-node lock for
            # its pull, across PROCESSES, which _bring_up_lock cannot reach.
            # DECISION-19: wait out one pull and no longer -- a node still
            # held past that is hung, and saying so beats stalling the `up`.
            with node_sync.node_lock(nick, wait_s=remote_mux.PULL_TIMEOUT_S):
                result = remote_mux.bring_up(
                    node, recipe, allow_dirty=allow_dirty, resume_id=resume_id
                )
                # What the node's repos were at this bring-up, for a later
                # recall from a node that no longer answers. Clean or dirty
                # only where bring_up.sh read the tree (repo_status.sh's rule,
                # untracked files included); an attach and --allow-dirty read
                # nothing, and a tree it could not read is missing from
                # result.dirty -- all unknown, never clean. A record that
                # cannot be written is logged and keeps the old one
                # (write_repo_record).
                nodes.write_repo_record(
                    nick,
                    result.sid,
                    nodes.RepoRecord(
                        ts=time.time(),
                        source="bring-up",
                        repos=tuple(
                            nodes.RepoStatus(
                                remote_dir=repo,
                                head=sha,
                                branch="",
                                dirty=(
                                    None
                                    if result.attached_existing or allow_dirty
                                    else result.dirty.get(repo)
                                ),
                                unpushed=None,
                            )
                            for repo, sha in sorted(result.commits.items())
                        ),
                    ),
                )
            warnings = recipe.warnings
            try:
                nodes.update_node_map(
                    name,
                    nodes.NodeMapEntry(
                        nick=nick,
                        sid=result.sid,
                        placed_ts=time.time(),
                        attached_existing=result.attached_existing,
                        remote_root=recipe.remote_root,
                        target=node.target,
                        cwd=result.cwd,
                    ),
                )
            except (ValueError, OSError) as exc:
                # The node said yes: the session IS running there. A failed
                # record (LockHeld past its wait, an unreadable map) is not a
                # failed bring-up -- reporting one would invite a second. A
                # re-run attaches to the live session and records it then.
                log.warning(
                    "node %s: %s up but not recorded in the node map: %s",
                    nick,
                    result.sid,
                    exc,
                )
                # The class only: the error itself (the map's path, the
                # parser's words) is in nodes.log, logged just above.
                warnings = (
                    *warnings,
                    (
                        f"up on @{nick} but not recorded ({type(exc).__name__});"
                        " re-run magent"
                        " up from a clean tree or --allow-dirty"
                    ),
                )
        title = _open_node_window(node, result.sid) if window else None
        log.info(
            "node %s: %s up (attached_existing=%s)",
            nick,
            result.sid,
            result.attached_existing,
        )
        return NodeBringUpOutcome(
            ok=True,
            sid=sid,
            node=nick,
            attached_existing=result.attached_existing,
            warnings=warnings,
            title=title,
        )
    except node_sync.NodeLockHeld as exc:
        log.warning("node %s: bring-up of %s: node busy: %s", nick, sid, exc)
        return NodeBringUpOutcome(
            ok=False,
            sid=sid,
            node=nick,
            error=(
                f"{_node_busy_text(nick, remote_mux.PULL_TIMEOUT_S)};"
                " re-run to try again"
            ),
        )
    except (ValueError, remote_mux.RemoteError, OSError) as exc:
        # ValueError covers NodeConfigError (its subclass) and a recipe that
        # cannot be framed; OSError a local file that vanished mid-read, and
        # LockHeld (the map held past its wait). A plain ValueError may also be
        # a bug wearing an outcome, so it keeps its traceback in the log.
        # A NodeConfigError names the OS error under it by class alone.
        said = str(exc) if exc.__cause__ is None else f"{exc}: {exc.__cause__}"
        log.warning(
            "node %s: bring-up of %s failed: %s",
            nick or "?",
            sid,
            # A RemoteError carries the node's own words, whatever their bytes.
            node_sync.escaped(said)
            if isinstance(exc, remote_mux.RemoteError)
            else said,
            exc_info=isinstance(exc, ValueError)
            and not isinstance(exc, nodes.NodeConfigError),
        )
        return NodeBringUpOutcome(
            ok=False, sid=sid, node=nick, error=_node_error_text(exc)
        )


def _placement_recipes(
    config: MagentConfig,
    projects: list[ProjectConfig],
    held: dict[str, NodeMapEntry],
    *,
    map_known: bool,
) -> dict[str, tuple[str, Recipe, bool]]:
    """``{sid: (nick, recipe, holder)}`` for every project in ``projects``
    whose node folder is already known, from config and the map alone -- no
    ssh, no git. The recipe carries only what ``nodes.remote_root_collisions``
    reads (its project, sid and remote_root); ``holder`` is whether the map
    records the project in that very folder on that very node -- a project
    re-pinned elsewhere is a newcomer there. A project that cannot be placed
    yet (no folder here or none this user may read, an unplaced ``auto``, a
    folder with no usable name) is left out: its own bring-up names that
    reason.

    ``held`` is the map read strictly; ``map_known`` False means it could
    not be read. Then an ``auto`` project is not "unplaced" but UNKNOWN: it
    may hold its folder on any node, so it stays in, under
    ``nodes.UNKNOWN_NODE_ROOT`` and with no nick, and nobody is its folder's
    holder."""
    # heavy subsystem: in-body per policy
    from magent import nodes
    from magent.env import local_username

    out: dict[str, tuple[str, Recipe, bool]] = {}
    for proj in projects:
        name = nodes.project_name(proj)
        entry = held.get(name)
        try:
            project_dir = _node_project_dir(config, proj)
            if project_dir is None:
                continue
            if not map_known and proj.node == NODE_AUTO:
                nick, remote_root = "", nodes.unknown_node_remote_root(project_dir)
            else:
                node = nodes.resolve(
                    config,
                    proj,
                    local_user=local_username(),
                    placed=entry.nick if entry else None,
                )
                nick, remote_root = node.nick, nodes.remote_root_for(node, project_dir)
        except (nodes.NodeConfigError, OSError):
            continue
        out[nodes.node_sid(proj)] = (
            nick,
            nodes.Recipe(
                project=name,
                sid=nodes.node_sid(proj),
                repos=(),
                push_files=(),
                memory_dir=None,
                remote_root=remote_root,
            ),
            entry is not None
            and entry.nick == nick
            and entry.remote_root == remote_root,
        )
    return out


def _folder_clashes(
    placed: dict[str, tuple[str, Recipe, bool]],
    unreadable: OSError | ValueError | None,
) -> dict[str, str]:
    """``{sid: why}`` for every member of a group ``placed`` (from
    ``_placement_recipes``) would share a node folder name in: the X3 text,
    or -- the map ``unreadable`` -- the map's own reason for a member whose
    only rivals are auto projects of unknown node. Pure."""
    # heavy subsystem: in-body per policy
    from magent import nodes

    clash: dict[str, str] = {}
    for group in nodes.remote_root_collisions([r for _, r, _ in placed.values()]):
        text = nodes.remote_root_collision_text(group)
        for recipe in group:
            clash[recipe.sid] = text
            if (
                unreadable is not None
                and not nodes.on_unknown_node(recipe)
                and all(
                    nodes.on_unknown_node(other)
                    for other in group
                    if other is not recipe
                )
            ):
                # Its only rivals are auto projects whose node the map would
                # name: no rename is owed, the map is -- a holder is refused
                # only because it cannot prove it holds the folder.
                clash[recipe.sid] = _folder_unknown_text(
                    unreadable, nodes.folder_leaf(recipe), placed[recipe.sid][0]
                )
    return clash


def _fleet_folder_clashes(
    config: MagentConfig, projects: list[ProjectConfig]
) -> tuple[dict[str, tuple[str, Recipe, bool]], dict[str, str]]:
    """X3 over the WHOLE fleet with ``projects`` in it: ``(placed, clash)``,
    ``_placement_recipes``' ``{sid: (nick, recipe, holder)}`` and
    ``_folder_clashes``' ``{sid: why}``, read from config and the map alone."""
    # heavy subsystem: in-body per policy
    from magent import nodes

    # A copy in ``projects`` wins over config's: the placement phase hands
    # over an auto project already turned into a nick, and writes nothing to
    # the map until it is up -- config's "auto" copy would place nowhere, and
    # the folder it is about to be cloned into would go unchecked.
    batch = {nodes.node_sid(proj): proj for proj in projects}
    listed = nodes.node_projects(config)
    known = {nodes.node_sid(proj) for proj in listed}
    fleet = [batch.get(nodes.node_sid(proj), proj) for proj in listed]
    fleet += [proj for proj in projects if nodes.node_sid(proj) not in known]
    held, unreadable = _node_map_for_placement()
    placed = _placement_recipes(config, fleet, held, map_known=unreadable is None)
    return placed, _folder_clashes(placed, unreadable)


def node_folder_refusal(config: MagentConfig, proj: ProjectConfig) -> str | None:
    """X3 for ONE project about to be placed (``recall --to``'s moved copy):
    the refusal ``up`` would give it -- a newcomer to a node folder name
    another project would share -- or None. Config and the map only: no ssh,
    no git, so a caller can ask before it touches anything."""
    # heavy subsystem: in-body per policy
    from magent import nodes

    placed, clash = _fleet_folder_clashes(config, [proj])
    sid = nodes.node_sid(proj)
    if sid in clash and not placed[sid][2]:
        return clash[sid]
    return None


def _run_node_bring_ups(
    config: MagentConfig,
    projects: list[ProjectConfig],
    *,
    allow_dirty: bool,
    window: bool,
) -> list[NodeBringUpOutcome]:
    """Each project's bring-up on a pool thread (a clone is minutes of
    network, not CPU), at most eight at once. The per-node lock that keeps one
    node serial is taken INSIDE a pool slot, so a bring-up waiting on its
    node's lock still holds its slot: with more than eight queued for one node
    ahead of another node's projects, that other node waits for a slot however
    idle it is (head-of-line blocking). Outcomes in the order given.

    First, ONCE and before anything is dialed, the WHOLE fleet's node folders
    are checked (X3, ``nodes.remote_root_collisions``): a batch project whose
    folder name another project -- in this batch or not -- would share is
    refused, naming the other. Fanned out, two clones would race for one
    folder; a batch of one today and another tomorrow would overwrite it.
    A collision among projects outside the batch refuses nothing here. Only
    newcomers are refused: the member the map already records in that folder
    is brought up as usual -- attached if its session runs, restarted in its
    own folder if not, neither overwriting anyone -- with the collision as a
    warning, so a healthy session is never reported failed because a newcomer
    arrived.

    Every project arrives placed: ``place_node_projects`` turns each ``auto``
    one into a nick or leaves it out (an unreadable map included), so none
    reaches here still ``auto``."""
    # heavy subsystem: in-body per policy
    from magent import nodes

    if not projects:
        return []
    placed, clash = _fleet_folder_clashes(config, projects)
    outcomes: dict[str, NodeBringUpOutcome] = {}
    for proj in projects:
        sid = nodes.node_sid(proj)
        if sid in clash and not placed[sid][2]:
            get_logger("nodes").warning("node project %s refused: %s", sid, clash[sid])
            outcomes[sid] = NodeBringUpOutcome(
                ok=False, sid=sid, node=placed[sid][0], error=clash[sid]
            )
    go = [proj for proj in projects if nodes.node_sid(proj) not in outcomes]
    if go:
        with ThreadPoolExecutor(max_workers=min(8, len(go))) as pool:
            futures = [
                pool.submit(
                    bring_up_node_project,
                    config,
                    proj,
                    allow_dirty=allow_dirty,
                    window=window,
                )
                for proj in go
            ]
            for proj, future in zip(go, futures, strict=True):
                sid = nodes.node_sid(proj)
                outcome = future.result()
                if sid in clash:  # the folder's recorded holder
                    outcome = replace(outcome, warnings=(*outcome.warnings, clash[sid]))
                outcomes[sid] = outcome
    return [outcomes[nodes.node_sid(proj)] for proj in projects]


def bring_up_node_projects(
    config: MagentConfig,
    *,
    only: list[str] | None = None,
    group: str | None = None,
    allow_dirty: bool = False,
    window: bool = False,
) -> list[NodeBringUpOutcome]:
    """Bring up every node project in scope. ``only`` holds session ids, the
    same currency as ``psmux.bring_up``'s -- a local id in it is ignored.

    Every ``"auto"`` project becomes a nick first, as ONE batch, so a
    single ``up`` spreads across equal nodes exactly as ``--go`` does
    (G-C12). An auto project that cannot be placed is a failed outcome
    saying why (``place_node_projects``' own words), never a silent
    drop; what the placement said about a project rides on its outcome
    as warnings, which the callers print."""
    # heavy subsystem: in-body per policy
    from magent import nodes

    projects = [
        proj
        for proj in nodes.node_projects(config, group)
        if only is None or nodes.node_sid(proj) in only
    ]
    placed = place_node_projects(config, projects)
    # node_projects keeps one project per session id, and the placer keeps
    # their order: the fan-out's outcomes line up with `chosen`'s.
    chosen = {nodes.node_sid(proj): proj for proj in placed.projects}
    ready = iter(
        _run_node_bring_ups(
            config, list(chosen.values()), allow_dirty=allow_dirty, window=window
        )
    )
    outcomes: list[NodeBringUpOutcome] = []
    for proj in projects:
        name, sid = nodes.project_name(proj), nodes.node_sid(proj)
        placement = placed.placements.get(name)
        said: tuple[str, ...] = ()
        if placement is not None:
            # A kept project was not scored: the load history is not why
            # it runs where it does.
            if placement.reason != "kept":
                said = placed.history_notes
            if placement.note:
                said = (*said, placement.note)
        if sid not in chosen:
            outcomes.append(
                NodeBringUpOutcome(
                    ok=False, sid=sid, error=placed.unplaced[name], warnings=said
                )
            )
            continue
        outcome = next(ready)
        if said:
            outcome = replace(outcome, warnings=(*said, *outcome.warnings))
        outcomes.append(outcome)
    return outcomes


def node_session_ids(config: MagentConfig, group: str | None = None) -> list[str]:
    """The session ids of the node projects in scope, config order."""
    # heavy subsystem: in-body per policy
    from magent import nodes

    return [nodes.node_sid(proj) for proj in nodes.node_projects(config, group)]


_KEPT = "kept in the node map for `magent node sync --once`"


def _say_not_pulled(sid: str, reason: str, then: str = _KEPT) -> None:
    """The one line a session whose last turn did not come home gets:
    ``reason`` is why, ``then`` what is left to fetch it with."""
    click.echo(
        f"  {style('!', fg='yellow')} {sid}: last turn not pulled ({reason}); {then}"
    )


def _final_pull(
    config: MagentConfig, key: str, sid: str, nick: str, no_pull: dict[str, str]
) -> bool:
    """Bring map entry ``key``'s last turn home before session ``sid`` is
    killed (R-D6). False when it could not be pulled -- said on one line; the
    caller still kills, but keeps the map entry, so ``magent node sync
    --once`` can fetch the transcript, which outlives the tmux session on the
    node's disk.

    ``key`` is the map key ``placement_of`` found, never the project's current
    name: ``final_pull`` looks the entry up by it, and a retitled project's
    name would find nothing and read as "never placed" -- pulled.

    A node that did not answer a pull (``timed_out``, or ssh's own rc 255:
    node_sync's UNREACHABLE), and one whose pull lock another magent process
    held past ``FINAL_PULL_WAIT_S`` (``NodeLockHeld``), joins ``no_pull`` --
    nick to the reason its later sessions print -- and is not pulled again in
    this call, so ``down --all`` against a hung or held node costs one wait,
    not one per project on it. Its sessions are still killed. Every other
    failure -- a reply over the cap is a node that answered -- is this
    session's.

    ``final_pull`` re-reads the map strictly. Busy or torn there, it raises
    NodeMapUnreadable, said with the shared sentence and the map error's class
    (the whole error goes to nodes.log). Its None ("never placed") after the
    caller read ``key`` out of the map strictly is the entry vanishing between
    the two reads -- a concurrent ``down``/``up`` that moved it -- said as "its
    node map entry was not found again": a pull that did not happen, never
    one that did."""
    # heavy subsystem: in-body per policy (node_sync dials the node)
    from magent import node_sync, remote_mux

    if nick in no_pull:
        reason = no_pull[nick]
    else:
        try:
            result = node_sync.final_pull(
                config, key, wait_s=node_sync.FINAL_PULL_WAIT_S
            )
        except (OSError, ValueError, remote_mux.RemoteError) as exc:
            # OSError covers NodeLockHeld (another magent process -- a sync
            # tick, a bring-up, another down -- held the node past
            # FINAL_PULL_WAIT_S), NodeMapUnreadable, and a pulled file this PC
            # could not write; ValueError covers NodeConfigError. None of them
            # may abort the down.
            detail, reason = str(exc), _pull_error_text(exc)
            if isinstance(exc, remote_mux.RemoteError):
                # The node's own words, whatever their bytes -- over the cap,
                # this line is the only place they go.
                detail = node_sync.escaped(detail)
                # The pull's ssh call is quiet: a local ssh that would not
                # start is its class alone, so this line adds the OS's words.
                words = remote_mux.os_detail(exc)
                if words:
                    detail = f"{detail}: {words}"
            if isinstance(exc, node_sync.NodeMapUnreadable):
                # Its own text is the shared sentence plus the MAP error's
                # class (final_pull built it with nodes.map_unread_text): the
                # screen's. The error it chains names the path and the
                # parser's words: the log's. Never _pull_error_text's words
                # (the wrapper's class, not the map error's), and never the
                # None branch's: the map, not the entry, went unread.
                detail, reason = f"{exc}: {exc.__cause__}", str(exc)
            get_logger("nodes").warning(
                "down: final pull of %s failed: %s", sid, detail
            )
            if isinstance(exc, remote_mux.RemoteError) and (
                exc.timed_out or exc.rc == attach_client.SSH_TRANSPORT_RC
            ):
                # The first line keeps ssh's own reason (rc 255 is also a
                # refused key); the node's later sessions just skip.
                no_pull[nick] = f"node {nick} did not answer the pull"
            elif isinstance(exc, node_sync.NodeLockHeld):
                # Our words, not the lock's name, on every session of the node.
                reason = no_pull[nick] = _node_busy_text(
                    nick, node_sync.FINAL_PULL_WAIT_S
                )
            # Printable ASCII: the reason may be the node's words or a path.
            reason = node_sync.printable(reason)
        else:
            if result is not None:
                return True
            get_logger("nodes").warning(
                "down: final pull of %s found no map entry for %r", sid, key
            )
            # The entry the strict read found, missing from final_pull's own
            # strict re-read: no exception, so no class.
            reason = "its node map entry was not found again"
    _say_not_pulled(sid, reason)
    return False


def stop_node_sessions(
    config: MagentConfig, sids: list[str]
) -> tuple[list[str], list[str]]:
    """Kill each node session in ``sids`` ON ITS NODE: where the node map
    placed it, else where its project is pinned. Returns ``(stopped,
    still_running)`` like ``stop_psmux``, in config order.

    A session the node confirmed killed is stopped; one that was not there is
    neither. Either way its map entry is cleared. One whose node could not be
    asked (unreachable, or a placement the config no longer names) is a
    survivor, and its entry stays for the next ``down`` to find. A node that
    failed once is not dialed again in this call: its other sessions are
    survivors too, so ``down --all`` against a powered-off node costs one
    probe timeout, not one per project. An ``auto`` project the map never
    placed runs nowhere this PC knows of and is skipped.

    The map is read STRICTLY: this answer becomes a report, and a torn or
    busy map read as ``{}`` would hide every placed session behind "No
    running sessions to stop.". Unreadable, nothing it might hold is claimed:
    an ``auto`` project is a survivor, and a pinned one that is "not there"
    on its pin is a survivor too -- it may run where the lost map said.
    Likewise one unmap that fails stops the rest from queueing on the same
    map lock; their entries stay, which the next bring-up records over.

    Each placed session's last turn is pulled home first (``_final_pull``);
    one that could not be pulled is still killed, but keeps its map entry. A
    pinned session behind an unreadable map is killed on its pin with no pull
    -- there is no entry to pull from -- and says so, since the lost map may
    have placed it.

    The node half only. The LOCAL session a node project may have left here
    (D9) is ``stop_psmux``'s, and the ``down`` shell folds the two halves
    into one report."""
    # heavy subsystem: in-body per policy (nodes + remote_mux: ssh)
    from magent import nodes, remote_mux
    from magent.config import NODE_AUTO
    from magent.env import local_username

    log = get_logger("nodes")
    entries, unreadable = _node_map_for_placement()
    map_known = unreadable is None
    if unreadable is not None:
        log.warning(
            "down: node map unreadable, no placement is trusted: %s", unreadable
        )
    # A pull can wait out a held lock and then a slow node, one session after
    # another: minutes. Say so before the first (the fan-out rule).
    due = sum(
        1
        for proj in nodes.node_projects(config)
        if nodes.node_sid(proj) in sids and nodes.placement_of(proj, entries)
    )
    if due:
        click.echo(
            f"  {style('-', dim=True)} Pulling the last turn of {due} node"
            " session(s) home..."
        )
    map_writable = True
    unreachable: set[str] = set()
    no_pull: dict[str, str] = {}
    stopped: list[str] = []
    still: list[str] = []
    for proj in nodes.node_projects(config):
        sid = nodes.node_sid(proj)
        if sid not in sids:
            continue
        key, entry = nodes.placement_of(proj, entries) or (None, None)
        if entry is None and proj.node == NODE_AUTO:
            if not map_known:
                still.append(sid)
            continue
        try:
            # The map wins over the pin: a project re-pinned since its
            # bring-up still runs where it was started.
            node = nodes.resolve(
                config,
                replace(proj, node=entry.nick) if entry else proj,
                local_user=local_username(),
            )
        except nodes.NodeConfigError as exc:
            log.warning("down: %s not stopped: %s", sid, exc)
            still.append(sid)
            continue
        if node.nick in unreachable:
            still.append(sid)
            continue
        # While the entry still names it. No entry in a map that was read is
        # nothing to pull; no entry in a map that was not is a pull not made.
        pulled = key is not None and _final_pull(config, key, sid, node.nick, no_pull)
        if key is None and not map_known:
            _say_not_pulled(
                sid,
                nodes.MAP_UNREAD,
                then=(
                    "if the map placed it, `magent node sync --once` fetches it"
                    " once the map reads again"
                ),
            )
        killed = remote_mux.kill_session(node, entry.sid if entry else sid)
        if killed is None:
            log.warning("down: %s not stopped: node %s did not answer", sid, node.nick)
            unreachable.add(node.nick)
            still.append(sid)
            continue
        if killed:
            stopped.append(sid)
        elif not map_known:
            log.warning(
                "down: %s not stopped: not on %s, map unreadable", sid, node.nick
            )
            still.append(sid)
        if not pulled:
            continue
        if not map_writable:
            log.warning("down: %s map entry stays: the map could not be written", sid)
            continue
        try:
            # Compare-and-delete: an `up` that re-placed this project while
            # the kill was in flight keeps its fresh entry.
            nodes.update_node_map(key, None, expect=entry)
        except (ValueError, OSError) as exc:
            # The kill is proved. A stale entry is harmless: the next bring-up
            # of this project records its placement over it.
            log.warning("down: %s stopped, but its map entry stays: %s", sid, exc)
            map_writable = False
    return stopped, still


def _keep_node_sync(config: MagentConfig, config_path: str | None) -> None:
    """``ensure_node_sync`` after a bring-up put a node session up. Best
    effort: the sessions are up either way, and a daemon that cannot start
    (its lock dir, the spawn) must not cost the bring-up its report or its exit
    code -- ``status`` shows the daemon off, and serve's supervisor retries."""
    try:
        ensure_node_sync(config, config_path=config_path)
    except OSError as exc:
        get_logger("nodes").warning(
            "node sync daemon not started after the bring-up: %s", exc
        )


def _echo_node_outcomes(outcomes: list[NodeBringUpOutcome]) -> None:
    # heavy subsystem: in-body per policy
    from magent.node_sync import printable

    for o in outcomes:
        if o.ok:
            verb = "attached" if o.attached_existing else "started"
            click.echo(
                f"  {style('+', fg='green')} {o.sid} "
                f"{style('@' + o.node, fg='blue')} {verb}"
            )
        else:
            # The node's last stderr line and names read off disk: one row.
            click.echo(f"  {style('x', fg='red')} {o.sid}: {printable(o.error or '')}")
        for warning in o.warnings:
            click.echo(
                f"    {style('!', fg='yellow')} {style(printable(warning), dim=True)}"
            )
