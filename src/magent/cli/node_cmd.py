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

from magent import log
from magent.cli.app import main
from magent.cli.config_io import _load_config_or_exit
from magent.lockfile import LockHeld
from magent.paths import find_config
from magent.style import style

if TYPE_CHECKING:
    import contextlib

# How long `node sync -d` waits for the detached child to record its pid:
# ~10 s nominal, returning as soon as it appears or the child exits. A cold
# child spends seconds importing before it takes the lock (measured 2.5-13 s
# on a loaded desktop); one still alive at the deadline is "still starting".
_START_POLLS = 100
_START_POLL_S = 0.1


NodeSyncStop = Literal["stopped", "stuck", "absent", "unknown"]


def _stop_node_sync() -> tuple[NodeSyncStop, str]:
    """Stop the node sync daemon and name what happened -- with, for
    "unknown", the error's class. ``stop_daemon``'s False is two answers --
    nothing to stop, or a daemon that outlived the kill -- and only the
    daemon's lock tells them apart. A lock file that would not open (Windows
    answers EACCES while one is pending delete) is "unknown", never "stopped"
    or "absent": the class goes on screen, the whole error to nodes.log."""
    from magent import node_sync  # heavy subsystem: in-body per policy

    try:
        if node_sync.stop_daemon():
            return "stopped", ""
        if node_sync.daemon_running():
            # False is also "a daemon holds the lock and outlived the stop"
            # (pid unknown, kill refused, or not dead within the settle).
            return "stuck", ""
    except OSError as exc:
        log.get_logger(node_sync.LOG_NAME).warning(
            "node sync: could not stop or check the daemon: %s", exc
        )
        return "unknown", type(exc).__name__
    return "absent", ""


def _say_stuck() -> None:
    from magent import node_sync  # heavy subsystem: in-body per policy

    pid = node_sync.daemon_pid()
    click.echo(
        f"  {style('x', fg='red')} Could not stop the node sync daemon "
        f"(pid {pid or 'unknown'})."
    )


def _say_unknown(cause: str) -> None:
    click.echo(
        f"  {style('!', fg='yellow')} Could not tell whether the node sync daemon"
        f" stopped ({cause}); see nodes.log"
    )


def _exit_running_unknown(exc: OSError) -> NoReturn:
    """The daemon's lock file would not open (Windows answers EACCES while
    one is pending delete), so whether a daemon runs is unknown: never "not
    running", never a traceback. The class goes on screen, the whole error to
    nodes.log, and it is not a success."""
    from magent import node_sync  # heavy subsystem: in-body per policy

    log.get_logger(node_sync.LOG_NAME).warning(
        "node sync: could not tell whether the daemon is running: %s", exc
    )
    click.echo(
        f"  {style('!', fg='yellow')} Could not tell whether the node sync daemon"
        f" is running ({type(exc).__name__}); see nodes.log"
    )
    sys.exit(1)


def stop_node_sync_and_say(*, say_absent: bool = True) -> NodeSyncStop:
    """Stop the node sync daemon and say what happened, in the words
    `node sync --stop` and `down --all` share. ``say_absent=False`` keeps
    "was not running" to itself; a daemon that is running is always said.
    Exit codes stay the caller's."""
    outcome, cause = _stop_node_sync()
    if outcome == "stopped":
        click.echo(f"  {style('+', fg='green')} Stopped the node sync daemon.")
    elif outcome == "stuck":
        _say_stuck()
    elif outcome == "unknown":
        _say_unknown(cause)
    elif say_absent:
        click.echo(f"  {style('-', dim=True)} Node sync daemon was not running.")
    return outcome


def restop_node_sync_and_say(first: NodeSyncStop) -> NodeSyncStop:
    """``down --all``'s second stop, once serve and ``attention -d`` are down:
    a daemon that started late -- already on its way when the ``first`` stop
    looked, and locked only after -- is stopped here. Says only what is news
    -- a daemon it stopped, or a running one the first stop did not already
    name, or a stop it could not check. Silent otherwise."""
    outcome, cause = _stop_node_sync()
    if outcome == "stopped" and first == "stopped":
        click.echo(
            f"  {style('+', fg='green')} Stopped the node sync daemon again"
            " (a daemon that started late)."
        )
    elif outcome == "stopped":
        # No "again": the first stop's survivor died after all, or the first
        # found none -- a daemon a supervisor tick spawned just before this
        # down took its lock, which locked only after the first stop looked.
        click.echo(f"  {style('+', fg='green')} Stopped the node sync daemon.")
    elif outcome == "stuck" and first != "stuck":
        _say_stuck()
    elif outcome == "unknown" and first != "unknown":
        _say_unknown(cause)
    return outcome


class DownSyncStop:
    """``down --all``'s stops of the node sync daemon: the first one just
    before the node pulls (``before_pulls``, when there are any) and the one
    after serve and ``attention -d`` are down (``at_end``, always).

    From the first stop until the last, it holds serve's supervisor lock
    (``node_sync.supervisor_held``, entered on ``hold``), so no serve can
    restart the daemon in between. Only a hint that a daemon may still be on
    its way makes ``at_end`` look for a late one
    (``node_sync.await_late_daemon``) before its stop -- otherwise it stops at
    once -- and each hint sets how long:

    - a supervisor tick held the lock when down asked for it: the daemon that
      tick may have spawned locks only once its interpreter is up, so it is
      looked for until a cold start (``_START_POLLS`` polls) past the hold,
      however soon the end comes;
    - the first stop found a daemon: whatever spawned it may spawn another,
      looked for ``STOP_SETTLE_S`` from the end.

    With both, the later deadline stands."""

    def __init__(self, hold: contextlib.ExitStack, *, say_absent: bool) -> None:
        self._hold = hold
        self._say_absent = say_absent
        self._held = False
        self._seen = False
        # A monotonic reading: when a contended hold's look ends.
        self._held_tick_until: float | None = None
        self._first: NodeSyncStop | None = None

    def _take_hold(self) -> None:
        from magent import node_sync  # heavy subsystem: in-body per policy

        if not self._held:
            self._held = True
            if self._hold.enter_context(node_sync.supervisor_held()):
                self._held_tick_until = time.monotonic() + _START_POLLS * _START_POLL_S

    def before_pulls(self) -> None:
        from magent import node_sync  # heavy subsystem: in-body per policy

        self._take_hold()
        try:
            self._seen = node_sync.daemon_running()
        except OSError:
            self._seen = True  # unknown is never "no daemon": the end stop waits
        self._first = stop_node_sync_and_say(say_absent=self._say_absent)

    def at_end(self) -> None:
        from magent import node_sync  # heavy subsystem: in-body per policy

        self._take_hold()
        until = self._held_tick_until
        if self._seen:
            settle = time.monotonic() + node_sync.STOP_SETTLE_S
            until = settle if until is None else max(until, settle)
        if until is not None:
            node_sync.await_late_daemon(until=until)
        if self._first is None:
            stop_node_sync_and_say(say_absent=self._say_absent)
        else:
            restop_node_sync_and_say(self._first)


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
        if stop_node_sync_and_say() in ("stuck", "unknown"):
            sys.exit(1)
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
        except node_sync.DaemonLockUnknown as exc:
            _exit_running_unknown(exc.error)
        failed = False
        for nick, (outcome, detail) in sorted(results.items()):
            ok = outcome == node_sync.OK
            failed = failed or not ok
            mark = style("+", fg="green") if ok else style("x", fg="red")
            # Printable ASCII: a detail can be a node's words, and a cp1252
            # console cannot encode every character.
            tail = f"  {node_sync.printable(detail)}" if detail else ""
            click.echo(f"  {mark} @{nick}  {outcome}{tail}")
        if failed:
            sys.exit(1)
        return

    # "Running" is the daemon's lock, never its pid file: a pid file outlives
    # a crash and its number is recycled onto strangers (daemon_running).
    try:
        running = node_sync.daemon_running()
    except OSError as exc:
        _exit_running_unknown(exc)
    if running:
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
    try:
        node_sync.run_sync_loop(
            cfg,
            max_ticks=ticks,
            reload=node_sync.ConfigWatch(config_file, cfg, stamp=stamp).current,
        )
    except node_sync.DaemonLockUnknown as exc:
        # The probe above opened the lock file and this take, right after,
        # did not: on Windows the probe's own delete can leave it pending.
        _exit_running_unknown(exc.error)
