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
from typing import Literal

import click

from magent import log
from magent.cli.app import main
from magent.cli.config_io import _load_config_or_exit
from magent.lockfile import LockHeld
from magent.paths import find_config
from magent.style import style

# How long `node sync -d` waits for the detached child to record its pid:
# ~10 s nominal, returning as soon as it appears or the child exits. A cold
# child spends seconds importing before it takes the lock (measured 2.5-13 s
# on a loaded desktop); one still alive at the deadline is "still starting".
_START_POLLS = 100
_START_POLL_S = 0.1


@main.group("node", invoke_without_command=True)
@click.pass_context
def node_group(ctx: click.Context) -> None:
    """Run projects on a pool of Linux machines over ssh."""
    if ctx.invoked_subcommand is None:
        # A bare `magent node` has nothing to show yet: the node table (load,
        # sessions, sync state) arrives with sub-plan G and fills this branch.
        return


def _daemon_state() -> Literal["ok", "stale", "stopped"]:
    """The sync daemon's liveness as its heartbeat tells it -- the ONE reader
    (DECISION-17; F's `node doctor` and G's table call it):

    - ``"ok"``: a beat within ``log.HEARTBEAT_MAX_AGE`` (the daemon beats every
      ``log.HEARTBEAT_INTERVAL``, 10 s, on its own thread);
    - ``"stale"``: an older beat -- wedged, or crashed and left its marker;
    - ``"stopped"``: no heartbeat -- never started, or stopped cleanly.
    """
    from magent import node_sync  # heavy subsystem: in-body per policy

    # One read: a clean stop removing the file between two reads would turn
    # "stopped" into "stale".
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
    if as_daemon:
        # Each asks for a different run; doing only one would be a silent guess.
        for flag, given in (
            ("--stop", do_stop),
            ("--once", once),
            ("--ticks", ticks is not None),
        ):
            if given:
                raise click.UsageError(f"{flag} cannot be combined with -d.", ctx=ctx)

    from magent import node_sync  # heavy subsystem: in-body per policy

    if do_stop:
        if node_sync.stop_daemon():
            click.echo(f"  {style('+', fg='green')} Stopped the node sync daemon.")
        elif node_sync.daemon_running():
            # False is also "a daemon holds the lock and outlived the stop"
            # (pid unknown, kill refused, or not dead within the settle).
            pid = node_sync.daemon_pid()
            click.echo(
                f"  {style('x', fg='red')} Could not stop the node sync daemon "
                f"(pid {pid or 'unknown'})."
            )
            sys.exit(1)
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
        child = spawn_detached(
            node_sync_argv(str(config_path) if config_path else None)
        )
        for _ in range(_START_POLLS):
            time.sleep(_START_POLL_S)
            pid = node_sync.daemon_pid()
            if pid and pid != leftover:
                click.echo(
                    f"  {style('+', fg='green')} Node sync daemon running "
                    f"{style(f'(pid {pid})', dim=True)}"
                )
                return
            if child.poll() is not None:
                break
        # No budget outlasts every cold start (measured 13 s once), so a child
        # still alive is starting, not failed -- and is never killed for being
        # slow. On Windows child.pid is the venv launcher, which lives exactly
        # as long as the interpreter it ran -- hence "launcher": it is the only
        # pid there is yet, and `--stop` will later name the daemon's own.
        # Future: share procs.await_registration (fix/serve-watchdog-e2e).
        if child.poll() is None:
            click.echo(
                f"  {style('-', dim=True)} Node sync daemon still starting "
                + style(
                    f"(launcher pid {child.pid}) -- see ~/.magent/logs/nodes.log",
                    dim=True,
                )
            )
            return
        click.echo(
            f"  {style('x', fg='red')} node sync daemon failed to start"
            f" {style('(see ~/.magent/logs/nodes.log)', dim=True)}"
        )
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
