from __future__ import annotations

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
    fresh_start_command,
    ide_command,
    is_ide_tool,
)
from magent.style import style
from magent.tiling import Placement, magent_window_names, place_windows
from magent.titles import generate_titles, get_leaf_name, make_title, parse_title

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable

    from magent.config import MagentConfig, ProjectConfig
    from magent.env import MagentEnv
    from magent.nodes import LocalGitState, Node, Recipe


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
) -> _LaunchResult:
    """The per-project dispatch loop: launch IDEs/terminals (or collect psmux
    windows), build the tiling target list. Pure w.r.t. tiling -- it never
    moves a window."""
    has_remote = any(p.host for p in projects)
    # attach_client's rule, the client the remote panes dial: Windows' own
    # OpenSSH with nothing on PATH is not a missing client.
    if has_remote and attach_client.find_ssh() is None:
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
        session_ids = _get_session_ids(tool, agent_dir, window_count)

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
) -> tuple[list[str], list[str]]:
    """Delegate to ``psmux.bring_up``, then bring the pool-node projects up
    too (PR-D; no windows -- this is the host side of attach). Returns
    ``(created, failed)`` over both; node outcomes are printed as they are
    the only place their reasons appear."""
    from magent import psmux

    created, failed = psmux.bring_up(config, only, group)
    outcomes = bring_up_node_projects(
        config, only=only, group=group, allow_dirty=allow_dirty
    )
    _echo_node_outcomes(outcomes)
    return (
        [*created, *(o.sid for o in outcomes if o.ok)],
        [*failed, *(o.sid for o in outcomes if not o.ok)],
    )


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
    """Make ``node`` able to run a project, at most once per process. Body
    landed by PR-F (DECISION-24): ``remote_mux.provision_node(node, config,
    home=Path.home(), timeout_s=remote_mux.PROVISION_TIMEOUT_S)``. Until then a
    pool machine is provisioned by hand and this only records that it was
    asked; ``config`` is here from day one so K needs no signature change.
    Called under the node's lock, after every refusal -- a refused project
    never provisions anything, and ``--dry-run`` never calls it."""
    del config  # PR-F's body reads it
    _PROVISIONED.add(node.nick)


def _node_project_dir(config: MagentConfig, proj: ProjectConfig) -> Path | None:
    base_dir = _expand_base_dir(config.base_dir) if config.base_dir else None
    resolved = _resolve_path(proj.path, base_dir)
    return Path(resolved) if resolved else None


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
    last stderr line (bring_up.sh writes its reason there, prefixed
    ``magent:``), anything else's message."""
    # heavy subsystem: in-body per policy
    from magent.remote_mux import RemoteError

    if isinstance(exc, RemoteError):
        lines = exc.stderr_tail.strip().splitlines()
        return lines[-1].removeprefix("magent: ") if lines else f"exit {exc.rc}"
    return str(exc)


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
    collision: str | None = None,
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
    ``remote_mux.bring_up``, record the placement and open the window. Never
    raises for a node, git or config failure: every one is an outcome.

    ``collision`` is the fleet check's refusal for a project that is the
    RECORDED holder of a folder name another project shares (X3): refused like
    an unreproducible tree, so a session still running there is attached and
    keeps it as a warning, and one that is gone is not restarted."""
    # heavy subsystem: in-body per policy (nodes + remote_mux: ssh/git/tar)
    from magent import nodes, remote_mux
    from magent.env import local_username

    log = get_logger("nodes")
    name = nodes.project_name(proj)
    sid = nodes.node_sid(proj)
    nick = ""
    try:
        held = nodes.read_node_map().get(name)
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
        if collision is not None:
            refusals.append(collision)
        if refusals:
            if (
                held is not None
                and held.nick == nick
                and remote_mux.has_session(node, held.sid)
            ):
                # What is uncommitted HERE does not touch a session already
                # running there: attach to it, and keep the refusal as a warning.
                remote_mux.decorate(node, held.sid, nick)
                title = _open_node_window(node, held.sid) if window else None
                log.info(
                    "node %s: %s already running; attached despite: %s",
                    nick,
                    held.sid,
                    refusals,
                )
                return NodeBringUpOutcome(
                    ok=True,
                    sid=sid,
                    node=nick,
                    attached_existing=True,
                    warnings=tuple(refusals),
                    title=title,
                )
            log.info("node %s: refused %s: %s", nick, sid, refusals)
            return NodeBringUpOutcome(
                ok=False, sid=sid, node=nick, error="; ".join(refusals)
            )
        with _bring_up_lock(nick):
            _provision_once(node, config)
            recipe = node_recipe(config, proj, node, states)
            result = remote_mux.bring_up(
                node, recipe, allow_dirty=allow_dirty, resume_id=resume_id
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
                # ASCII end to end: the cause is the OS's or the map's words.
                cause = str(exc).encode("ascii", "replace").decode("ascii")
                warnings = (
                    *warnings,
                    (
                        f"up on @{nick} but not recorded ({cause}); re-run magent"
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
    except (ValueError, remote_mux.RemoteError, OSError) as exc:
        # ValueError covers NodeConfigError (its subclass) and a recipe that
        # cannot be framed; OSError a local file that vanished mid-read, and
        # LockHeld (the map held past its wait). A plain ValueError may also be
        # a bug wearing an outcome, so it keeps its traceback in the log.
        log.warning(
            "node %s: bring-up of %s failed: %s",
            nick or "?",
            sid,
            exc,
            exc_info=isinstance(exc, ValueError)
            and not isinstance(exc, nodes.NodeConfigError),
        )
        return NodeBringUpOutcome(
            ok=False, sid=sid, node=nick, error=_node_error_text(exc)
        )


def _placement_recipes(
    config: MagentConfig, projects: list[ProjectConfig]
) -> dict[str, tuple[str, Recipe, bool]]:
    """``{sid: (nick, recipe, holder)}`` for every project in ``projects``
    whose node folder is already known, from config and the map alone -- no
    ssh, no git. The recipe carries only what ``nodes.remote_root_collisions``
    reads (its project, sid and remote_root); ``holder`` is whether the map
    records the project in that very folder (its bring-up checks the node and
    the session). A project that cannot be placed yet (no
    folder here or none this user may read, an unplaced ``auto``, a folder
    with no usable name) is left out: its own bring-up names that reason."""
    # heavy subsystem: in-body per policy
    from magent import nodes
    from magent.env import local_username

    held = nodes.read_node_map()
    out: dict[str, tuple[str, Recipe, bool]] = {}
    for proj in projects:
        name = nodes.project_name(proj)
        entry = held.get(name)
        try:
            project_dir = _node_project_dir(config, proj)
            if project_dir is None:
                continue
            node = nodes.resolve(
                config,
                proj,
                local_user=local_username(),
                placed=entry.nick if entry else None,
            )
            remote_root = nodes.remote_root_for(node, project_dir)
        except (nodes.NodeConfigError, OSError):
            continue
        out[nodes.node_sid(proj)] = (
            node.nick,
            nodes.Recipe(
                project=name,
                sid=nodes.node_sid(proj),
                repos=(),
                push_files=(),
                memory_dir=None,
                remote_root=remote_root,
            ),
            entry is not None and entry.remote_root == remote_root,
        )
    return out


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
    A collision among projects outside the batch refuses nothing here. The
    one member the map already records in that folder is not refused here:
    its bring-up gets the refusal as ``collision``, so a session of it still
    running is attached, not reported failed because a newcomer arrived."""
    # heavy subsystem: in-body per policy
    from magent import nodes

    if not projects:
        return []
    fleet = nodes.node_projects(config)
    known = {nodes.node_sid(proj) for proj in fleet}
    fleet += [proj for proj in projects if nodes.node_sid(proj) not in known]
    placed = _placement_recipes(config, fleet)
    clash: dict[str, str] = {}
    for group in nodes.remote_root_collisions([r for _, r, _ in placed.values()]):
        text = nodes.remote_root_collision_text(group)
        for recipe in group:
            clash[recipe.sid] = text
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
                    collision=clash.get(nodes.node_sid(proj)),
                )
                for proj in go
            ]
            for proj, future in zip(go, futures, strict=True):
                outcomes[nodes.node_sid(proj)] = future.result()
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
    same currency as ``psmux.bring_up``'s -- a local id in it is ignored."""
    # heavy subsystem: in-body per policy
    from magent import nodes

    projects = [
        proj
        for proj in nodes.node_projects(config, group)
        if only is None or nodes.node_sid(proj) in only
    ]
    return _run_node_bring_ups(config, projects, allow_dirty=allow_dirty, window=window)


def node_session_ids(config: MagentConfig, group: str | None = None) -> list[str]:
    """The session ids of the node projects in scope, config order."""
    # heavy subsystem: in-body per policy
    from magent import nodes

    return [nodes.node_sid(proj) for proj in nodes.node_projects(config, group)]


def _echo_node_outcomes(outcomes: list[NodeBringUpOutcome]) -> None:
    for o in outcomes:
        if o.ok:
            verb = "attached" if o.attached_existing else "started"
            click.echo(
                f"  {style('+', fg='green')} {o.sid} "
                f"{style('@' + o.node, fg='blue')} {verb}"
            )
        else:
            click.echo(f"  {style('x', fg='red')} {o.sid}: {o.error}")
        for warning in o.warnings:
            click.echo(f"    {style('!', fg='yellow')} {style(warning, dim=True)}")
