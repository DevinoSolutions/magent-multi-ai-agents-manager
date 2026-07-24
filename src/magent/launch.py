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
    ide_command,
    is_ide_tool,
)
from magent.style import style
from magent.tiling import Placement, place_windows
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


@dataclass
class RunOpts:
    retile_all: bool = False
    dry_run: bool = False
    group: str | None = None
    config_path: str = ""


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

    _tile_targets(plat, opts, slots, result.targets)

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
        # "md-name" is the pre-rename spelling, still accepted as a deprecated alias.
        if mode in ("magent-name", "md-name"):
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
        targets=targets, psmux_windows=psmux_windows, psmux_colors=_psmux_colors
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
    if not running and not opts.dry_run:
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

    session_ids: list[str | None] = [None] * window_count
    caps = AGENT_TOOLS.get(tool)
    if window_count > 1 and caps and caps.multi_window and not is_remote:
        resolved_dir = _resolve_path(proj.path, base_dir)
        if resolved_dir:
            session_ids = _get_session_ids(tool, resolved_dir, window_count)

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
            cmd = win_cfg.command
        elif win_tool != tool:
            # Per-window override: the discovered session ids belong to the
            # base `tool`, not `win_tool` -- never reuse them for the override.
            cmd = (
                build_resume_command(win_tool, win_base, None)
                if window_count > 1
                else win_base
            )
        elif window_count > 1 and session_ids[i] is not None:
            cmd = build_resume_command(win_tool, win_base, session_ids[i])
        elif window_count > 1:
            cmd = build_resume_command(win_tool, win_base, None)
        else:
            cmd = win_base

        if use_happy:
            cmd = _wrap_happy(win_tool, cmd)

        proj_psmux = use_psmux and not is_remote
        if proj_psmux and not opts.dry_run:
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
        running = is_running(win_title, match_mode)
        if not running and not opts.dry_run and not proj_psmux:
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
        plat.launch_psmux_session(psmux_windows)
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


def _tile_targets(
    plat: Platform, opts: RunOpts, slots: list[TileSlot], targets: list[_Target]
) -> None:
    """Place (or, under dry_run, preview) each target into a slot. Delegates
    the resolve-and-move-with-retry logic to magent.tiling.place_windows
    (R13/E9's shared helper) -- no lookup/retry loop is re-implemented here."""
    to_place = targets if opts.retile_all else [t for t in targets if t.is_new]

    if not to_place:
        click.echo(f"\n  {style('+', fg='green')} All windows already positioned.")
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
) -> list[str]:
    """Delegate to ``psmux.bring_up``."""
    from magent import psmux

    return psmux.bring_up(config, only, group)


def kill_psmux(names: list[str]) -> list[str]:
    """Delegate to ``psmux.kill_servers``."""
    from magent.psmux import kill_servers

    return kill_servers(names)
