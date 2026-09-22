from __future__ import annotations

import os
import shutil
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import click

from magent import tailnet
from magent.grid import TileSlot, compute_grid
from magent.log import get_logger
from magent.platform import (
    Platform,
    PsmuxWindowOpts,
    TerminalLaunchOpts,
    TerminalNotFoundError,
    VSCodeLaunchOpts,
    get_platform,
)
from magent.procs import pid_alive, spawn_unjobbed
from magent.sessions import (
    AGENT_TOOLS,
    build_resume_command,
    build_start_command,
    ide_command,
    is_ide_tool,
)
from magent.style import style
from magent.tiling import Placement, magent_window_names, place_windows
from magent.titles import generate_titles, get_leaf_name, make_title, parse_title

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable, Sequence

    from magent.accounts import AccountsSnapshot, SettingsReport
    from magent.config import MagentConfig, ProjectConfig
    from magent.env import MagentEnv


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


def session0_note() -> str | None:
    """The one-line reason session creation is blocked here, or None.

    What the "N session(s) failed to come up" printers add so a casualty list
    carries its cause. A user staring at 40 failed names must not have to find
    launch.log to learn that nothing was even attempted.
    """
    plat = get_platform()
    if session0_disposition(plat) == "run":
        return None
    return session0_refusal(plat)


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
        click.echo(
            f"  {style('x', fg='red')} hand-off could not run on the desktop: "
            f"{result.detail} "
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
    """
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
    spawn_detached(args)
    # The child writes its pid only after the keyboard hook installs; give it a
    # short window to come up so we can report (and so a hook failure surfaces).
    # `pid != existing` guards the restart path: a kill that didn't take must
    # not read back as "the new listener came up".
    for _ in range(20):
        time.sleep(0.1)
        pid = listener_pid()
        if pid and pid != existing:
            return pid
    return None


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


def ensure_hotkey_listener(default_url: str) -> int | None:
    """Make sure SOME Alt+V listener is running; never re-aim a healthy one.

    The supervision entry point (``upload_server``'s serve loop calls this on an
    interval), as opposed to ``start_hotkey_listener``, which is the *wiring*
    entry point the launch and attach paths use. See
    ``supervised_hotkey_target`` for why the two must differ.

    Idempotent by construction -- it delegates to ``start_hotkey_listener``, so
    a healthy current listener is a pid-file read plus a manifest read and no
    spawn, and the "never two listeners" property is exactly the one that
    function already had.

    Windows-only, like everything hotkey: the caller owns the
    ``supports_hotkey()`` gate that keeps the import below reachable.
    """
    from magent.hotkey import (  # ImportError off-Windows (hotkey.py guards); must stay lazy
        listener_manifest,
        listener_pid,
    )

    if listener_pid() is None:
        return start_hotkey_listener(default_url, None)
    url, ssh_host = supervised_hotkey_target(listener_manifest(), default_url)
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
    spawn_detached(upload_server_argv(port, config_path))
    return True


def _validated_env() -> MagentEnv | None:
    """The env singleton, or None if it no longer validates.

    A daemon must never die of an environment variable it does not use, and by
    the time the attention loop is running every other MAGENT_* consumer has
    already failed loudly at CLI entry -- so an env that goes bad underneath a
    detached process degrades to the defaults with a log line, exactly as
    ``upload_server.supervision_enabled`` and ``log._configured_level`` do.
    """
    from pydantic import ValidationError

    from magent.env import get_env

    try:
        return get_env()
    except ValidationError:
        get_logger("attention").warning(
            "upload supervisor: environment did not validate; using defaults"
        )
        return None


def upload_supervision_enabled() -> bool:
    """Whether MAGENT_UPLOAD_SUPERVISOR permits the attention daemon to keep
    ``magent serve`` alive. Public because ``status`` must ask the same question
    the supervisor answers before it offers the daemon as a repair."""
    env = _validated_env()
    return True if env is None else env.upload_supervisor


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
        self._last_spawn = now
        # ASCII only: this line goes to a rotating logfile that gets read back
        # through whatever the host console's code page happens to be.
        log.warning(
            "upload supervisor: nothing answering on port %d (%s); starting a new "
            "magent serve",
            self._port,
            self._pid_note(),
        )
        spawn_detached(upload_server_argv(self._port, self._config_path))
        return True


# --- Account routing ----------------------------------------------------------
# One phase, between selecting the projects and launching them, because both
# things a routed window needs -- the environment overlay and the config dir its
# session probe must answer from -- have to exist BEFORE any window's command is
# built. Like every other phase here it returns data and decides no exit code.
#
# Three properties are load-bearing, and all three point the same way: routing
# can never be the reason a bring-up fails, is slow, or surprises anyone.
#
# * It is OFF unless three independent gates all say yes -- the config asked for
#   it, ccswap is new enough (accounts.MIN_CCSWAP_VERSION), and ccswap's own
#   required settings are in effect. With routing off not one byte of this
#   module's behaviour changes: no ccswap process is spawned, no window carries
#   an overlay, and every command is built exactly as it is today.
# * Every refusal is NAMED and falls through to an unrouted launch. Five of
#   them: ccswap too old, a required ccswap setting not in effect, a non-empty
#   `duplicateAccountWarnings` (which means utilization readings may be
#   attributed to the wrong account -- distrusting the SNAPSHOT, not an
#   account), a snapshot error, and no eligible account at all.
# * The whole phase is bounded by ONE budget. Expiring it launches the fleet
#   unrouted rather than late: a bring-up must not wait on somebody else's
#   credential tool.
#
# ...and one property that is about the OTHER tool rather than about magent:
# this phase is READ-ONLY toward ccswap. It runs the four reads `accounts.py`
# owns and no mutation -- not even `profile hydrate`, which exists and would
# work. A bring-up is the worst possible moment to write into a store holding
# the user's live credentials, and the alternative costs nothing that matters:
# with `profiles.persistent` on, a one-time `ccswap profile hydrate --all`
# keeps profiles hydrated, so an un-hydrated profile is a setup step the user
# takes once. magent makes that account INELIGIBLE (ccswap's own
# `profileHydrated` is the signal, `routing._blocker` the refusal), re-plans its
# projects onto other eligible accounts, and prints the one command that fixes
# it (`_hydration_hints`).
#
# Rationale for `CLAUDE_CONFIG_DIR` rather than a command prefix, and for the
# pin living in the config while the ASSIGNMENT does not: DESIGN.md §2.

# How long the whole phase gets -- the version probe, the settings reads and the
# snapshot together. Generous for a handful of local CLI calls, finite because
# the alternative is a fleet that does not come up while ccswap thinks.
ROUTE_BUDGET_S = 20.0


@dataclass(frozen=True)
class RoutedProject:
    """One project's account, and what that costs its window's environment."""

    account: str
    config_dir: Path
    env: dict[str, str]
    drop_env: frozenset[str]


@dataclass(frozen=True)
class RoutePlan:
    """What the routing phase decided, keyed by psmux session id.

    Empty is the ordinary answer: routing off, ccswap absent, a refusal, or a
    budget that ran out. ``notes`` are the human lines the caller prints -- a
    refusal that nobody can read is the silent failure this feature must not
    have.
    """

    routes: dict[str, RoutedProject] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def route(self, session: str) -> RoutedProject | None:
        return self.routes.get(session)

    def config_dirs(self) -> dict[str, Path]:
        """``{session id: config dir}`` -- what a session probe must read.

        The shape ``psmux.eligible_projects`` takes, so the ``up`` path asks
        the same question the ``--go`` path does.
        """
        return {sid: r.config_dir for sid, r in self.routes.items()}


def routing_session_id(proj: ProjectConfig) -> str:
    """The psmux session id routing keys a project by.

    The same string ``psmux.eligible_projects`` derives, so the map, the
    overlay and every status surface agree on what a project is called without
    a translation step anywhere.
    """
    return _psmux_session_name(proj.title or get_leaf_name(proj.path))


def _remaining(deadline: float) -> float:
    return deadline - time.monotonic()


def _routing_refusals(
    snapshot: AccountsSnapshot, settings: SettingsReport
) -> list[str]:
    """Every reason this snapshot must not be routed on, named.

    ``duplicate_warnings`` is the one refusal that fires while ccswap reports
    every account eligible, and it is the sharpest: a known ccswap bug can flip
    which slot is reported active, so each utilization reading may belong to a
    different account than the one it is printed against. Placing work by
    numbers that belong to somebody else is worse than not placing it at all.

    A setting that could not be READ is a refusal too, not a pass: "could not
    ask" must never be treated as "the answer was yes" for a switch that would
    otherwise fight every placement magent makes.
    """
    problems: list[str] = []
    if settings.error:
        problems.append(settings.error)
    problems.extend(settings.problems)
    if snapshot.duplicate_warnings:
        problems.append(
            "ccswap reports duplicate accounts, so a usage reading may belong "
            "to a different account than it is shown against: "
            + "; ".join(snapshot.duplicate_warnings)
        )
    return problems


def _hydration_hints(snapshot: AccountsSnapshot) -> tuple[str, ...]:
    """One line per account that only an un-hydrated profile keeps out of play.

    magent is READ-ONLY toward ccswap -- it runs four reads and no mutation, so
    it does not hydrate a profile itself. `profileHydrated: false` therefore
    makes an account ineligible (``routing._blocker`` refuses it), its projects
    are re-planned onto other eligible accounts, and the way back is a command
    the USER runs. Which is why the hint exists at all: an account silently
    sitting out is the same symptom as an account that does not exist, and the
    repair is one line of ccswap the user has to be told.

    Scoped to accounts ccswap otherwise calls usable, so a disabled or
    api-key slot never grows a hint suggesting hydration would help it.
    """
    # heavy subsystem: in-body per policy (accounts spawns ccswap).
    from magent.accounts import SUBSCRIPTION_KIND

    return tuple(
        f"account {acct.id} is not hydrated, so nothing was placed on it; "
        f"run: ccswap profile hydrate {acct.id}"
        for acct in snapshot.accounts
        if acct.eligible and not acct.hydrated and acct.kind in ("", SUBSCRIPTION_KIND)
    )


def _unrouted(reason: str) -> RoutePlan:
    """No routing, and the reason said out loud."""
    get_logger("launch").info("account routing off: %s", reason)
    return RoutePlan(notes=(f"account routing is off: {reason}",))


def _route_projects(
    config: MagentConfig,
    projects: Sequence[ProjectConfig],
    *,
    now: float | None = None,
) -> RoutePlan:
    """Decide which account each project's pane starts on. Never raises.

    The phase between ``_select_projects`` and ``_launch_projects``. Returns an
    empty plan for every "no" -- routing disabled, ccswap missing or too old, a
    refusal, a budget that expired, no account able to take the work -- and the
    caller launches exactly as it does today.

    Read-only toward ccswap: four reads, no mutation. An account whose profile
    is not hydrated is one ccswap reports as unusable, so it is simply not
    placed on, and the hint that fixes it is printed (``_hydration_hints``).
    """
    # heavy subsystem: in-body per policy (accounts spawns ccswap; env pulls
    # pydantic in, and neither is paid for by an unrouted bring-up).
    from magent import accounts, routing
    from magent.env import ACCOUNT_OVERRIDE_VARS

    policy = routing.policy_for(config.settings.accounts)
    if not policy.enabled:
        if not config.settings.accounts.enabled:
            # Silent: off is the default, and a line saying so on every launch
            # would be the loudest thing in the output for the least reason.
            return RoutePlan()
        # The config DID ask for routing, so the kill switch is holding it off
        # and the user is about to get an unrouted fleet they did not choose.
        # One shared sentence with `magent account` and `doctor`, because it
        # names WHICH of the two gates is the cause.
        reason = routing.routing_off_reason()
        get_logger("launch").info("%s", reason)
        return RoutePlan(notes=(reason,))

    deadline = time.monotonic() + ROUTE_BUDGET_S
    log = get_logger("launch")

    version = accounts.read_version(timeout=_remaining(deadline))
    if not accounts.version_at_least(version):
        return _unrouted(
            f"ccswap {version or 'is not installed'} is below the "
            f"{accounts.MIN_CCSWAP_VERSION} magent needs to route safely"
        )

    snapshot = accounts.read_accounts(timeout=_remaining(deadline))
    if snapshot.error:
        return _unrouted(snapshot.error)
    refusals = _routing_refusals(
        snapshot, accounts.read_settings(timeout=_remaining(deadline))
    )
    if refusals:
        return RoutePlan(notes=tuple(f"account routing is off: {r}" for r in refusals))
    if not snapshot.accounts:
        return _unrouted("ccswap reports no claude accounts")
    if _remaining(deadline) <= 0:
        return _unrouted(f"reading ccswap took longer than {ROUTE_BUDGET_S:.0f}s")

    # Local CLI-agent projects only. A remote project's command runs on the far
    # machine, where magent sets no environment at all, and an IDE window is not
    # an agent pane -- neither can be routed, and pretending otherwise would put
    # an account in a table for a window that never reads it.
    # Same rules as `psmux.eligible_projects`, INCLUDING its first-occurrence-
    # wins de-duplication by session id: two config entries that resolve to one
    # session are one pane, and planning it twice would place one account in
    # the map and start the pane on the other. Not delegated to that function
    # because it probes each project's stored sessions to build a command, and
    # WHICH store to probe is the answer this phase has not computed yet.
    candidates: dict[str, ProjectConfig] = {}
    for p in projects:
        if (
            not p.enabled
            or p.host
            or is_ide_tool(p.tool or config.settings.default_tool)
        ):
            continue
        candidates.setdefault(routing_session_id(p), p)
    if not candidates:
        return RoutePlan()

    prior_map = accounts.read_map()
    planned = routing.plan(
        [
            routing.project_from_config(p, session=session)
            for session, p in candidates.items()
        ],
        snapshot,
        policy,
        prior_map,
        now=time.time() if now is None else now,
    )
    rows = planned.rows

    by_id = snapshot.by_id()
    if not any(r.account for r in rows):
        return RoutePlan(
            notes=(
                *_hydration_hints(snapshot),
                "account routing is off: no account can take these projects right now",
            )
        )

    routes: dict[str, RoutedProject] = {}
    notes: list[str] = list(_hydration_hints(snapshot))
    new_map = dict(prior_map)
    stamp = accounts.now_stamp()
    for row in rows:
        acct = by_id.get(row.account) if row.account else None
        overlay = accounts.profile_env(acct) if acct else {}
        if acct is None or not overlay:
            new_map.pop(row.session, None)
            continue
        routes[row.session] = RoutedProject(
            account=acct.id,
            config_dir=Path(accounts.config_dir(acct)),
            env=overlay,
            drop_env=ACCOUNT_OVERRIDE_VARS,
        )
        new_map[row.session] = accounts.MapEntry(
            account=acct.id,
            klass=row.klass,
            class_source=row.class_source,
            assigned_at=stamp,
            reason=row.reason,
        )
        if row.warning:
            notes.append(f"{row.project}: {row.warning}")

    # `planned.stale` rather than a second comparison of the same two numbers:
    # "is this data too old" is the planner's decision, and two copies of it can
    # disagree -- which would mean the table and the warning telling different
    # stories about the same snapshot. Stale is a CAVEAT, never a refusal: a
    # launch-time placement on ten-minute-old readings is exactly the use
    # ccswap's cache is adequate for, and the thresholds carry the margin.
    if planned.stale and planned.usage_age_s is not None:
        notes.append(
            f"ccswap usage data is {planned.usage_age_s / 60:.0f}m old; "
            "routing on it anyway"
        )
    if not routes:
        # Nothing placed. Whatever was already collected SAYS WHY (a hydrate
        # that failed, a pin that named nothing), so the notes are kept rather
        # than replaced by a generic line -- and the map is left exactly as it
        # was, because a pass that placed nothing has decided nothing.
        reason = "no account could take these projects"
        log.info("account routing off: %s", reason)
        return RoutePlan(notes=(*notes, f"account routing is off: {reason}"))

    accounts.write_map(new_map)
    log.info(
        "account routing: %d project(s) placed across %d account(s)",
        len(routes),
        len({r.account for r in routes.values()}),
    )
    return RoutePlan(routes=routes, notes=tuple(notes))


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


@dataclass
class _Target:
    name: str
    key: str
    mode: str
    is_new: bool


def _resolve_path(raw: str, base_dir: str | None) -> str | None:
    expanded = os.path.expandvars(os.path.expanduser(raw))
    if Path(expanded).is_absolute():
        return expanded if Path(expanded).is_dir() else None
    if base_dir:
        joined = os.path.join(base_dir, expanded)
        return joined if Path(joined).is_dir() else None
    return None


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

    # Between selection and launch: both things a routed window needs -- its
    # environment overlay and the config dir its session probe answers from --
    # have to exist before any window's command is built. Empty (and silent)
    # whenever routing is off, which is the default.
    routes = _route_projects(config, projects)
    for note in routes.notes:
        click.echo(f"  {style('!', fg='yellow')} {style(note, dim=True)}")

    base_dir = config.base_dir
    if base_dir:
        base_dir = _expand_base_dir(base_dir)

    try:
        result = _launch_projects(plat, config, opts, projects, base_dir, routes)
    except TerminalNotFoundError as exc:
        # The OS terminal emulator is missing (e.g. Windows Terminal not
        # installed). Surface the actionable install hint as one clean line --
        # no traceback -- and abort, mirroring the no-monitors failure shape.
        log.exception("terminal launcher unavailable; aborting")
        click.echo(f"  {style('✗', fg='red')} {exc}", err=True)
        return 2

    _start_psmux_and_upload(plat, config, opts, result)

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
    """Enabled projects, optionally narrowed to opts.group. Returns None
    (caller exits 0) when a named group matches nothing (after printing the
    same 'No projects in group' message it does today)."""
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
    return projects


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


def _launch_projects(
    plat: Platform,
    config: MagentConfig,
    opts: RunOpts,
    projects: list[ProjectConfig],
    base_dir: str | None,
    routes: RoutePlan | None = None,
) -> _LaunchResult:
    """The per-project dispatch loop: launch IDEs/terminals (or collect psmux
    windows), build the tiling target list. Pure w.r.t. tiling -- it never
    moves a window.

    ``routes`` is the routing phase's answer. None (and an empty plan) mean
    every window is unrouted, i.e. today's behaviour exactly."""
    has_remote = any(p.host for p in projects)
    if has_remote and not shutil.which("ssh"):
        click.echo(
            style("  ! Remote projects configured but 'ssh' not on PATH.", fg="yellow")
        )

    targets: list[_Target] = []
    new_count = 0
    tools = config.settings.tools
    use_psmux = config.settings.psmux and plat.supports_psmux()
    psmux_windows: list[PsmuxWindowOpts] = []
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

    for proj in projects:
        tool = proj.tool or config.settings.default_tool
        is_remote = bool(proj.host)

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
            None if routes is None else routes.route(routing_session_id(proj)),
        )

    return _LaunchResult(
        targets=targets,
        psmux_windows=psmux_windows,
        psmux_colors=_psmux_colors,
        open_titles=tuple(win_snapshot),
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
    route: RoutedProject | None = None,
) -> int:
    """Generate this project's window titles, resolve resumable sessions, and
    launch (or collect into the caller-owned `psmux_windows`) each window;
    append its tiling target(s) to the caller-owned `targets` list. Returns
    the new_count delta (windows newly launched or newly collected, summed
    across every window this project owns).

    ``route`` is this project's account, if the routing phase chose one. It
    does two things and only two: it names the store the session probe reads
    (``config_dir``), and it rides along as the window's environment overlay.
    The COMMAND is never rewritten for it -- the account is environment, which
    is exactly the property that makes it verifiable."""
    new_count = 0

    # The store that answers for this project: its account's profile when it is
    # routed, the tool's own default otherwise. None here is today's probe for
    # every project, byte for byte.
    config_dir = route.config_dir if route else None

    # windowTitlePrefix off: titles are bare project names, so the magent:
    # grammar can't resolve them. The launcher set the title itself, so it
    # tiles (and probes "already running") by exact-title match instead.
    prefix = config.settings.window_title_prefix
    match_mode = "magent-name" if prefix else "exact"

    windows_cfg = proj.windows
    if is_remote or is_ide_tool(tool):
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
        session_ids = _get_session_ids(tool, agent_dir, window_count, config_dir)

    base_cmd = tools.get(tool)
    if not base_cmd:
        click.echo(
            f"SKIP: {titles[0]} — unknown tool '{tool}' (add under settings.tools)"
        )
        return new_count

    use_happy = proj.happy if proj.happy is not None else config.settings.happy

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
                else build_start_command(
                    win_tool, win_base, agent_dir, config_dir=config_dir
                )
            )
        elif window_count > 1 and session_ids[i] is not None:
            cmd = build_resume_command(win_tool, win_base, session_ids[i])
        elif window_count > 1:
            cmd = build_resume_command(win_tool, win_base, None)
        else:
            # Single window: the configured command runs verbatim, so this is
            # the one place a bare `claude --continue` reaches a project
            # directory that may have no conversation to continue -- or, for a
            # routed project, one whose ACCOUNT has no conversation for it.
            cmd = build_start_command(
                win_tool, win_base, agent_dir, config_dir=config_dir
            )

        # A routed project whose command lost its implicit resume flag is one
        # whose account holds no transcript for this directory: the agent will
        # start FRESH. Visible rather than silent -- a user who wanted
        # continuity can pin the project back to its old account. Unrouted rows
        # are unchanged, because for them "no conversation here" is just a new
        # project directory and has always been unremarkable.
        fresh_start = route is not None and cmd != win_base

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
                        env=route.env if route else None,
                        drop_env=route.drop_env if route else frozenset(),
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
                        env=route.env if route else None,
                        drop_env=route.drop_env if route else frozenset(),
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
            win_title,
            win_tool,
            running,
            proj.host,
            happy=use_happy,
            psmux=proj_psmux,
            account=route.account if route else None,
            fresh=fresh_start,
        )

    return new_count


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
        if failed:
            # Same honesty the attach/menu bring-up paths now have: a session
            # the verify proved never came up must not be counted among the
            # ones this path reports below.
            click.echo(
                f"\n  {style('x', fg='red')} {style(str(len(failed)), fg='red', bold=True)}"
                f" session(s) failed to come up: {style(', '.join(failed), fg='red')}"
                f" {style('(see ~/.magent/logs/launch.log)', dim=True)}"
            )
            # `--go` never hands off (it is a local, interactive command by
            # definition), so reaching here in Session 0 means the choke point
            # refused -- and a casualty list with no cause is what sent a user
            # hunting through launch.log last time.
            note = session0_note()
            if note:
                click.echo(f"  {style(note, dim=True)}")
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

        if config.settings.upload_server:
            port = config.settings.upload_port
            python = sys.executable
            serve_args = [python, "-m", "magent"]
            if opts.config_path:
                serve_args.extend(["--config", opts.config_path])
            serve_args.extend(["serve", "-p", str(port)])
            spawn_detached(serve_args)
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
    account: str | None = None,
    fresh: bool = False,
) -> None:
    """One launch-table row.

    ``account``/``fresh`` are the routing annotations, and both default to the
    unrouted answer so an unrouted row is byte-for-byte the row it has always
    been. ``fresh`` is deliberately only ever set for a ROUTED window: it means
    "this account holds no transcript for this project, so the agent starts a
    new conversation", which is a thing the user may want to undo before it
    happens. ASCII only, like every other badge here."""
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
    if account:
        extras += style(f" [a{account}]", fg="cyan")
    if fresh:
        extras += style(" [fresh]", fg="yellow")
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
    config: MagentConfig, only: list[str] | None = None, group: str | None = None
) -> tuple[list[str], list[str]]:
    """Delegate to ``psmux.bring_up``. Returns ``(created, failed)``."""
    from magent import psmux

    return psmux.bring_up(config, only, group)


def revive_psmux(
    config: MagentConfig, only: list[str] | None = None, group: str | None = None
) -> list[str]:
    """Delegate to ``psmux.revive_sessions``."""
    from magent import psmux

    return psmux.revive_sessions(config, only, group)


def decorate_psmux_sessions(
    names: list[str], code_hint: bool | None = None
) -> list[str]:
    """Delegate to ``psmux.decorate_sessions``.

    ``code_hint`` stays optional here (unlike ``decoration_argv``'s required
    one) so existing callers keep working and get the default "probe on this
    machine" behaviour, which is what every one of them wants.
    """
    from magent import psmux

    return psmux.decorate_sessions(names, code_hint=code_hint)


def decorate_psmux_sessions_async(
    names: list[str], code_hint: bool | None = None
) -> list[str]:
    """Delegate to ``psmux.decorate_sessions_async``.

    The status-path variant: fires the same commands without waiting, and is
    throttled by a stamp file. `up --json` uses this one so a slow psmux can
    never delay (or fail) a status query -- see the psmux docstring.
    """
    from magent import psmux

    return psmux.decorate_sessions_async(names, code_hint=code_hint)


def stop_psmux(names: list[str]) -> tuple[list[str], list[str]]:
    """Delegate to ``psmux.stop_sessions``. Returns ``(stopped, still_running)``.

    Replaces the old ``kill_psmux`` (a pass-through to the attempt-only
    ``kill_servers``): a shutdown command has to be able to tell the user what
    it PROVED it stopped, and what it could not.
    """
    from magent.psmux import stop_sessions

    return stop_sessions(names)
