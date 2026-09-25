"""`magent node`: run projects on a pool of Linux machines over ssh.

This module starts with `node sync`, the daemon that mirrors the pool onto
this PC; the other subcommands arrive with their own sub-plans. Exit codes and
lines live here, the work in magent.node_sync (imported in-body: the
registration hub imports every command module, and `magent --help` must not
pay for ssh and tar).
"""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING, Literal, NoReturn

import click

from magent import env, log, nodes
from magent.cli.app import main
from magent.cli.config_io import _load_config_or_exit
from magent.config import NODE_AUTO, is_cloud, runs_on_node
from magent.fleet import resolve_session
from magent.lockfile import LockHeld
from magent.paths import find_config
from magent.style import style

if TYPE_CHECKING:
    from pathlib import Path

    from magent.config import MagentConfig, ProjectConfig
    from magent.nodes import Placement

# How long `node sync -d` waits for the detached child to record its pid.
_START_POLLS = 20
_START_POLL_S = 0.1


@main.group("node", invoke_without_command=True)
@click.pass_context
def node_group(ctx: click.Context) -> None:
    """Run projects on a pool of Linux machines over ssh."""
    if ctx.invoked_subcommand is None:
        _print_node_table(find_config(ctx.obj.get("config_path")))


def _table_row(cells: list[str], widths: list[int]) -> str:
    """Left-aligned cells; a width of 0 leaves the last column unpadded."""
    return "  " + "  ".join(
        cell.ljust(width) if width else cell
        for cell, width in zip(cells, widths, strict=True)
    )


def _node_rows(cfg: MagentConfig, *, now: float) -> list[list[str]]:
    daemon = _daemon_state()
    local_user = env.local_username()
    rows: list[list[str]] = []
    for nick, conf in cfg.settings.nodes.items():
        window = nodes.in_window(nodes.read_load_history(nick), now=now)
        score = nodes.score_node(nick, window)
        load, mem, mine = "no data", "-", "-"
        if score is not None:
            load = f"{score.p75:.2f} ({score.samples})"
            latest = max(window, key=lambda s: s.ts)
            mine = str(latest.my_sessions)
            if latest.mem_total_mb > 0:
                mem = f"{latest.mem_avail_mb / latest.mem_total_mb:.0%} free"
        rows.append([nick, conf.host, conf.user or local_user, load, mem, mine, daemon])
    return rows


def _print_node_table(config_file: Path) -> None:
    """``magent node``: the pool at a glance -- the same history placement
    reads (load p75 over 30 minutes), newest memory and session count, and
    whether the sync daemon that feeds it is alive. Reads only."""
    cfg = _load_config_or_exit(config_file)
    if not cfg.settings.nodes:
        click.echo(
            f"  {style('-', dim=True)} no nodes configured -- add one under"
            " settings.nodes, then run: magent node setup <nick>"
        )
        return
    headers = ["nick", "host", "user", "load p75 30m", "mem", "my sessions", "daemon"]
    rows = _node_rows(cfg, now=time.time())
    widths = [max(len(r[i]) for r in [headers, *rows]) for i in range(len(headers))]
    widths[-1] = 0
    click.echo()
    click.echo(style(_table_row(headers, widths), bold=True))
    for row in rows:
        click.echo(_table_row(row, widths))


def _daemon_state() -> Literal["ok", "stale", "stopped"]:
    """The sync daemon's liveness as its heartbeat tells it -- the ONE reader
    (DECISION-17; F's `node doctor` and G's table call it):

    - ``"ok"``: a beat within ``log.HEARTBEAT_MAX_AGE`` (the daemon beats every
      ``log.HEARTBEAT_INTERVAL``, 10 s, on its own thread);
    - ``"stale"``: an older beat -- wedged, or crashed and left its marker;
    - ``"stopped"``: no heartbeat -- never started, or stopped cleanly.
    """
    from magent import node_sync  # heavy subsystem: in-body per policy

    age = log.heartbeat_age(node_sync.HEARTBEAT_NAME)
    if age is None:
        return "stopped"
    return "ok" if age <= log.HEARTBEAT_MAX_AGE else "stale"


@node_group.command("sync")
@click.option("--daemon", "-d", "as_daemon", is_flag=True, help="Run detached")
@click.option("--once", is_flag=True, help="Run one tick in the foreground, then exit")
@click.option("--stop", "do_stop", is_flag=True, help="Stop the running daemon")
@click.option("--ticks", default=None, type=int, hidden=True)  # test seam
@click.pass_context
def sync_cmd(
    ctx: click.Context, as_daemon: bool, once: bool, do_stop: bool, ticks: int | None
) -> None:
    """Mirror every node's sessions, load and agent state onto this PC.

    Each tick makes one ssh per node, running pull.sh: the node's tmux session
    list, a load sample, and the transcript and state files that changed since
    the last pull for the sessions this PC placed there. `magent serve` keeps it
    running whenever a project has a node; run it by hand to debug.
    """
    from magent import node_sync  # heavy subsystem: in-body per policy

    if do_stop:
        if node_sync.stop_daemon():
            click.echo(f"  {style('+', fg='green')} Stopped the node sync daemon.")
        else:
            click.echo(f"  {style('-', dim=True)} Node sync daemon was not running.")
        return

    config_path = ctx.obj.get("config_path")
    config_file = find_config(config_path)
    # Stamped BEFORE the load, so an edit landing in between is still reloaded.
    stamp = node_sync.config_stamp(config_file)
    cfg = _load_config_or_exit(config_file)
    if not node_sync.wanted(cfg):
        click.echo(
            f"  {style('-', dim=True)} Nothing to sync: no project runs on a node."
        )
        return

    if once:
        try:
            results = node_sync.run_once(cfg)
        except LockHeld:
            click.echo(
                f"  {style('-', dim=True)} The node sync daemon is running; "
                "its own next tick is this one."
            )
            return
        failed = False
        for nick, (outcome, detail) in sorted(results.items()):
            ok = outcome == node_sync.OK
            failed = failed or not ok
            mark = style("+", fg="green") if ok else style("x", fg="red")
            tail = f"  {detail}" if detail else ""
            click.echo(f"  {mark} @{nick}  {outcome}{tail}")
        if failed:
            sys.exit(1)
        return

    # "Running" is the daemon's lock, never its pid file: a pid file outlives
    # a crash and its number is recycled onto strangers (daemon_running).
    if node_sync.daemon_running():
        existing = node_sync.daemon_pid()
        shown = f"(pid {existing})" if existing else "(pid unknown)"
        click.echo(
            f"  {style('+', fg='green')} Node sync daemon already running "
            f"{style(shown, dim=True)}"
        )
        return

    if as_daemon:
        from magent.launch import (  # heavy subsystem: in-body per policy
            node_sync_argv,
            spawn_detached,
        )

        # The lock is free, so a pid the file names now is a leftover -- maybe
        # a stranger's by now -- and never the child about to be spawned.
        leftover = node_sync.daemon_pid()
        spawn_detached(node_sync_argv(str(config_path) if config_path else None))
        for _ in range(_START_POLLS):
            time.sleep(_START_POLL_S)
            pid = node_sync.daemon_pid()
            if pid and pid != leftover:
                click.echo(
                    f"  {style('+', fg='green')} Node sync daemon running "
                    f"{style(f'(pid {pid})', dim=True)}"
                )
                return
        click.echo(f"  {style('x', fg='red')} node sync daemon failed to start")
        sys.exit(1)

    # Foreground loop (also the body of the detached child).
    click.echo(
        f"  {style('#', fg='cyan')} Syncing {len(cfg.settings.nodes)} node(s)"
        " -- Ctrl+C to stop."
    )
    node_sync.run_sync_loop(
        cfg,
        max_ticks=ticks,
        reload=node_sync.ConfigWatch(config_file, cfg, stamp=stamp).current,
    )


# Exit codes: 2 = nothing to act on (unknown project, not a node project, not
# placed, bad destination), 3 = a node did not answer or refused.
_EXIT_USAGE = 2
_EXIT_UNREACHABLE = 3


def _fail(text: str, code: int) -> NoReturn:
    click.echo(f"  {style('x', fg='red')} {text}", err=True)
    sys.exit(code)


def _note(text: str) -> None:
    click.echo(f"  {style('!', fg='yellow')} {text}")


def _ok(text: str) -> None:
    click.echo(f"  {style('+', fg='green')} {text}")


def _node_project_or_exit(cfg: MagentConfig, query: str) -> ProjectConfig:
    """The configured node or cloud project ``query`` names (exact, then a
    unique substring, then a unique prefix -- the fleet commands' rule), or
    exit 2. A cloud project is returned too: plan, push and recall each route
    it explicitly, right after the name (DECISION-15; J11m's branches sit
    there)."""
    names = [nodes.project_name(p) for p in cfg.projects]
    hit = resolve_session(query, names)
    if hit is None:
        _fail(f"no configured project matches {query!r}", _EXIT_USAGE)
    proj = cfg.projects[names.index(hit)]
    if not (runs_on_node(proj) or is_cloud(proj)):
        _fail(f'{hit} has no "node" set -- it runs on this machine', _EXIT_USAGE)
    return proj


def _plan_heading(name: str, proj: ProjectConfig, placement: Placement | None) -> str:
    if proj.node != NODE_AUTO:
        return f"  {style(name, bold=True)}  pinned -> @{proj.node}"
    if placement is None or placement.nick is None:
        return (
            f"  {style(name, bold=True)}  auto -> nowhere"
            f" ({nodes.PLACE_REASONS['no-data']})"
        )
    return (
        f"  {style(name, bold=True)}  auto -> @{placement.nick}"
        f"  {style('(' + nodes.PLACE_REASONS[placement.reason] + ')', dim=True)}"
    )


def _print_scores(placement: Placement) -> None:
    headers = [
        "nick",
        "samples",
        "p75",
        "spike",
        "mem",
        "my sessions",
        "score",
        "chosen",
    ]
    rows = [
        [
            s.nick,
            f"{s.samples} live" if s.live else str(s.samples),
            f"{s.p75:.2f}",
            f"{s.spike:.2f}",
            f"{s.mem:.2f}",
            str(s.my_sessions),
            f"{s.score:.2f}",
            "*" if s.nick == placement.nick else ("floor" if s.below_floor else ""),
        ]
        for s in placement.scores
    ]
    widths = [max(len(r[i]) for r in [headers, *rows]) for i in range(len(headers))]
    widths[-1] = 0
    click.echo("  " + style(_table_row(headers, widths), dim=True))
    for row in rows:
        click.echo("  " + _table_row(row, widths))
    if any(s.below_floor for s in placement.scores):
        floor = f"{nodes.MEM_HARD_FLOOR:.0%}"
        click.echo(
            "  "
            + style(
                f"floor = under {floor} free memory; skipped while another node"
                " is above it",
                dim=True,
            )
        )


# D-MERGE: `_local_dir` and `_print_push_set` (plan G :3137-3147, :3192-3210)
# and their call at the end of plan_cmd's loop (:3257) need D's
# launch.node_git_states; they land with D's merge, and
# test_plan_lists_the_push_set_relative_to_the_project switches on with it.


@node_group.command("plan")
@click.argument("project", required=False)
@click.option("--all", "all_projects", is_flag=True, help="Every enabled node project.")
@click.pass_context
def plan_cmd(ctx: click.Context, project: str | None, all_projects: bool) -> None:
    """Show where a node project would run and what it would ship. Writes nothing.

    The same placement a launch makes -- the node-map, the load history and,
    for a node with too few recent samples, one live reading -- but nothing
    is recorded and nothing is started.
    """
    from magent import launch  # heavy subsystem: in-body per policy

    if (project is None) == (not all_projects):
        raise click.UsageError("name one project, or pass --all")
    cfg = _load_config_or_exit(find_config(ctx.obj.get("config_path")))
    if project is not None:
        chosen = [_node_project_or_exit(cfg, project)]
    else:
        # DECISION-15/26 ix: a cloud project is pin-only and has no node to
        # plan, so --all takes node projects only.
        chosen = [p for p in cfg.projects if p.enabled and runs_on_node(p)]
        if not chosen:
            click.echo(f'  {style("-", dim=True)} no enabled project has "node" set')
            return
    placed = launch.place_node_projects(cfg, chosen)
    click.echo(
        f"\n  {style('magent node plan', bold=True)}"
        f" {style('(a dry run -- nothing is changed)', dim=True)}"
    )
    for note in placed.notes:
        _note(note)
    for proj in chosen:
        name = nodes.project_name(proj)
        click.echo()
        if is_cloud(proj):
            # Named on request, never placed: what a cloud session ships is
            # plan J's `node push`.
            click.echo(
                f"  {style(name, bold=True)}  cloud -- pinned;"
                " there is no node to place it on"
            )
            continue
        placement = placed.placements.get(name)
        click.echo(_plan_heading(name, proj, placement))
        if placement is not None and placement.scores:
            _print_scores(placement)
        # D-MERGE: _print_push_set(cfg, proj) goes here (plan G :3257).
