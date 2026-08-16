from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

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
    from collections.abc import Callable

    from magent.config import MagentConfig, ProjectConfig


def spawn_detached(args: list[str], extra_flags: int = 0) -> subprocess.Popen[bytes]:
    """Popen a process that outlives both this process and a launching SSH session.

    On Windows, OpenSSH puts the command's children in a job object marked
    kill-on-close, so when the SSH session ends the children are terminated.
    ``DETACHED_PROCESS`` only detaches the console -- it does not escape the job.
    ``CREATE_BREAKAWAY_FROM_JOB`` does, but CreateProcess fails outright if the
    parent job forbids breakaway, so fall back to a plain detached spawn (the
    normal case when launched from an interactive console, not under a job).
    """
    if sys.platform != "win32":
        return subprocess.Popen(args)
    CREATE_NO_WINDOW = 0x08000000
    DETACHED_PROCESS = 0x00000008
    CREATE_BREAKAWAY_FROM_JOB = 0x01000000
    base = CREATE_NO_WINDOW | DETACHED_PROCESS | extra_flags
    try:
        return subprocess.Popen(args, creationflags=base | CREATE_BREAKAWAY_FROM_JOB)
    except OSError:
        return subprocess.Popen(args, creationflags=base)


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


def _get_session_ids(tool: str, project_dir: str, count: int) -> list[str | None]:
    caps = AGENT_TOOLS.get(tool)
    if caps and caps.session_ids:
        return caps.session_ids(project_dir, count)
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
) -> _LaunchResult:
    """The per-project dispatch loop: launch IDEs/terminals (or collect psmux
    windows), build the tiling target list. Pure w.r.t. tiling -- it never
    moves a window."""
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
        running = is_running(win_title, match_mode)
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
                wname = _psmux_session_name(win_title)
                psmux_windows.append(
                    PsmuxWindowOpts(
                        window_name=wname,
                        cwd=resolved_dir,
                        command=cmd,
                    )
                )
                psmux_colors[wname] = proj.color
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
            _Target(name=win_title, key=win_title, mode=match_mode, is_new=not running)
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
        for pw in psmux_windows:
            plat.attach_psmux(
                pw.window_name,
                make_title(pw.window_name, prefix=config.settings.window_title_prefix),
                psmux_colors.get(pw.window_name),
            )
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


def kill_psmux(names: list[str]) -> list[str]:
    """Delegate to ``psmux.kill_servers``."""
    from magent.psmux import kill_servers

    return kill_servers(names)
