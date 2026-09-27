"""`magent node`: run projects on a pool of Linux machines over ssh.

This module starts with `node sync`, the daemon that mirrors the pool onto
this PC; the other subcommands arrive with their own sub-plans. Exit codes and
lines live here, the work in magent.node_sync (imported in-body: the
registration hub imports every command module, and `magent --help` must not
pay for ssh and tar).
"""

from __future__ import annotations

import dataclasses
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NoReturn

import click

from magent import env, log
from magent.cli.app import main
from magent.cli.config_io import _load_config_or_exit
from magent.config import NODE_AUTO, is_cloud, runs_on_node
from magent.fleet import resolve_session
from magent.lockfile import LockHeld
from magent.paths import find_config
from magent.style import style

if TYPE_CHECKING:
    import contextlib
    from pathlib import PurePath

    from magent.config import MagentConfig, ProjectConfig
    from magent.nodes import Node, NodeMapEntry, Placement, RepoStatus
    from magent.remote_mux import RemoteError

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
        _print_node_table(find_config(ctx.obj.get("config_path")))


def _table_row(cells: list[str], widths: list[int]) -> str:
    """Left-aligned cells; a width of 0 leaves the last column unpadded."""
    return "  " + "  ".join(
        cell.ljust(width) if width else cell
        for cell, width in zip(cells, widths, strict=True)
    )


# What the user column shows when the D4 rule refuses the login a node's
# sessions would run as: a node with no "user" whose local login is not a
# usable node login (e.g. "Amin Dhouib") or is "root". An explicit empty
# "user" never gets here -- config load refuses it.
_NO_LOGIN = "? (set user)"


def _node_login(cfg: MagentConfig, nick: str, local_user: str) -> str:
    """The login sessions on ``nick`` run as -- read from the D4 rule's one
    home, never re-derived here."""
    from magent import nodes  # heavy subsystem: in-body per policy

    try:
        return nodes.node_for_nick(cfg, nick, local_user=local_user).user
    except nodes.NodeConfigError:
        return _NO_LOGIN


def _node_rows(cfg: MagentConfig, *, now: float) -> list[list[str]]:
    from magent import nodes  # heavy subsystem: in-body per policy

    daemon = _daemon_state()
    local_user = env.local_username()
    # The same windows placement reads; None: a table never samples live.
    unreadable: dict[str, OSError | ValueError] = {}
    windows, _ = nodes.placement_samples(
        cfg, now=now, live_sample=None, on_unreadable=unreadable.__setitem__
    )
    rows: list[list[str]] = []
    for nick, conf in cfg.settings.nodes.items():
        window = windows.get(nick, [])
        score = nodes.score_node(nick, window)
        load, mem, mine = "no data", "-", "-"
        if nick in unreadable:
            # Unknown, never "no data": the class only, the rest is in nodes.log.
            load = f"unreadable ({type(unreadable[nick]).__name__})"
        if score is not None:
            load = f"{score.p75:.2f} ({score.samples})"
            mine = str(score.my_sessions)
            latest = max(window, key=lambda s: s.ts)
            if latest.mem_total_mb > 0:
                mem = f"{latest.mem_avail_mb / latest.mem_total_mb:.0%} free"
        login = _node_login(cfg, nick, local_user)
        rows.append([nick, conf.host, login, load, mem, mine, daemon])
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


# Exit codes:
# 1 = a step failed: `node sync --once` with a node that did not sync, `node
#     sync -d` whose daemon did not start, push with the node map unreadable
#     or a file refused on this PC, and recall's failures -- the node map
#     unreadable, the last pull failing on this PC or left unfinished (a node
#     that ANSWERED with a nonzero rc is this too, cq-G14 I1, not 3), the
#     placement unreadable for it, a linked mirror, the conversation not
#     installed, the placement not cleared;
# 2 = nothing to act on (unknown project, not a node project, a push of a
#     project not placed yet, a recipe that cannot be built, a recall of a
#     project the node-map does not place, bad destination -- an unknown
#     node, the one it is on, a pinned project, a root the install refuses);
# 3 = the node could not be acted on: recall's last pull finding the
#     node-sync daemon still holding that node's lock, a node that did not
#     take push's files, and recall --to's new node refusing the install or
#     failing the bring-up.
# A plan that places a project nowhere is an answer, not a failure: it exits
# 0. A node that does not answer during recall --local is a note, never an
# exit: recall goes on with what was already pulled. So is a last pull that
# cannot be made at all (an answer that was not a pull, a refusal on this PC).
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
    from magent import nodes  # heavy subsystem: in-body per policy

    names = [nodes.project_name(p) for p in cfg.projects]
    hit = resolve_session(query, names)
    if hit is None:
        _fail(f"no configured project matches {query!r}", _EXIT_USAGE)
    proj = cfg.projects[names.index(hit)]
    if not (runs_on_node(proj) or is_cloud(proj)):
        _fail(f'{hit} has no "node" set -- it runs on this machine', _EXIT_USAGE)
    return proj


def _plan_heading(name: str, proj: ProjectConfig, placement: Placement | None) -> str:
    from magent import nodes  # heavy subsystem: in-body per policy

    if proj.node != NODE_AUTO:
        return f"  {style(name, bold=True)}  pinned -> @{proj.node}"
    if placement is not None and placement.reason == "unknown":
        # D17: the map could not be read, so the node is unknown -- never a
        # guess, and never "nowhere" (it may be running somewhere).
        return (
            f"  {style(name, bold=True)}  auto -> (node unknown)"
            f"  {style('(' + nodes.PLACE_REASONS['unknown'] + ')', dim=True)}"
        )
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
    from magent import nodes  # heavy subsystem: in-body per policy

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
    # The floor only skips a node while another is above it; when every node
    # is under it, place() falls back to all of them and nothing was skipped.
    floor_applied = any(not s.below_floor for s in placement.scores)
    rows = [
        [
            s.nick,
            f"{s.samples} live" if s.live else str(s.samples),
            f"{s.p75:.2f}",
            f"{s.spike:.2f}",
            f"{s.mem:.2f}",
            str(s.my_sessions),
            f"{s.score:.2f}",
            "*"
            if s.nick == placement.nick
            else ("floor" if floor_applied and s.below_floor else ""),
        ]
        for s in placement.scores
    ]
    widths = [max(len(r[i]) for r in [headers, *rows]) for i in range(len(headers))]
    widths[-1] = 0
    click.echo("  " + style(_table_row(headers, widths), dim=True))
    for row in rows:
        click.echo("  " + _table_row(row, widths))
    if floor_applied and any(s.below_floor for s in placement.scores):
        floor = f"{nodes.MEM_HARD_FLOOR:.0%}"
        click.echo(
            "  "
            + style(
                f"floor = under {floor} free memory; skipped while another node"
                " is above it",
                dim=True,
            )
        )


def _local_dir(cfg: MagentConfig, proj: ProjectConfig) -> Path | None:
    """The project's folder on THIS machine exactly as a launch resolves it
    (``launch._resolve_path``: expanded, joined to the base dir, links NOT
    followed), or None when missing. Never ``Path.resolve()``d (cq-G14 M4): a
    launch cds to this string, Claude files the conversation under it, and a
    project reached through a link must be recalled under the link's name."""
    from magent.launch import (  # heavy subsystem: in-body per policy
        _expand_base_dir,
        _resolve_path,
    )

    base_dir = _expand_base_dir(cfg.base_dir) if cfg.base_dir else None
    resolved = _resolve_path(proj.path, base_dir)
    return Path(resolved) if resolved else None


def _print_push_set(cfg: MagentConfig, proj: ProjectConfig) -> None:
    """The non-git files a bring-up of ``proj`` would ship beside the clone,
    relative to the project: ``nodes.push_set`` over D's local git read, the
    project's ``push`` entries included -- what the recipe builder ships. A
    tree that cannot be read is unknown, never "nothing beyond git": its
    class on screen, the whole error in nodes.log."""
    # heavy subsystem: in-body per policy
    from magent import launch, nodes, remote_mux

    project_dir = _local_dir(cfg, proj)
    if project_dir is None:
        click.echo(
            f"    ships  {style('(the project folder is missing here)', dim=True)}"
        )
        return
    try:
        states = launch.node_git_states(cfg, proj)
        files = nodes.push_set(
            project_dir, states, home=Path.home(), extras=tuple(proj.push or ())
        )
    except (OSError, ValueError, remote_mux.RemoteError) as exc:
        log.get_logger("nodes").warning(
            "plan could not list %s's push set: %s", nodes.project_name(proj), exc
        )
        unknown = f"(unknown: {type(exc).__name__}; see nodes.log)"
        click.echo(f"    ships  {style(unknown, dim=True)}")
        return
    if not files:
        click.echo(f"    ships  {style('nothing beyond git', dim=True)}")
        return
    for index, path in enumerate(files):
        shown = (
            path.relative_to(project_dir).as_posix()
            if path.is_relative_to(project_dir)
            else str(path)
        )
        click.echo(f"    {'ships' if index == 0 else '     '}  {shown}")


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
    from magent import launch, nodes  # heavy subsystem: in-body per policy

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
    for line in placed.refused:
        # A launch would fail these (red x), so plan says so the same way.
        click.echo(f"  {style('x', fg='red')} {line}")
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
        _print_push_set(cfg, proj)


# repo_status on a node that answers: a quick read, never a fetch.
RECALL_TIMEOUT_S = 60.0


def _tail(exc: RemoteError) -> str:
    """A node call's stderr tail as one screen row: each non-blank line
    as printable ASCII (a node's words must not write to this terminal),
    joined with "; "; its rc when it said nothing."""
    from magent import node_sync  # heavy subsystem: in-body per policy

    lines = [line.strip() for line in exc.stderr_tail.splitlines() if line.strip()]
    if lines:
        return "; ".join(node_sync.printable(line) for line in lines)
    return "timed out" if exc.rc is None else f"exit {exc.rc}"


def _local_failure(exc: Exception, doing: str) -> str:
    """The words for a failure on THIS PC (the git read, a file, the config)
    while ``doing``: a bring-up's one line for it (``launch._node_error_text``
    -- ours for a config error, the class only for an OSError) as printable
    ASCII, and the whole error in nodes.log."""
    # heavy subsystem: in-body per policy
    from magent import launch, node_sync

    log.get_logger("nodes").warning("%s: %s", doing, exc)
    return node_sync.printable(launch._node_error_text(exc))


def _current_nick(proj: ProjectConfig) -> str | None:
    """Where a node project runs now: its pin, or its node-map placement.
    The map is read strictly, so one that cannot be read raises (OSError /
    ValueError) -- unknown, never "not placed"."""
    from magent import nodes  # heavy subsystem: in-body per policy

    if proj.node != NODE_AUTO:
        return proj.node
    held = nodes.load_node_map_strict().get(nodes.project_name(proj))
    return held.nick if held else None


@node_group.command("push")
@click.argument("project")
@click.pass_context
def push_cmd(ctx: click.Context, project: str) -> None:
    """Re-ship a project's non-git files (.env* etc.) to its node."""
    # heavy subsystem: in-body per policy
    from magent import launch, node_sync, nodes, remote_mux

    cfg = _load_config_or_exit(find_config(ctx.obj.get("config_path")))
    proj = _node_project_or_exit(cfg, project)
    name = nodes.project_name(proj)
    if is_cloud(proj):
        # DECISION-15: no node to push to. J11m replaces this refusal with its
        # by-hand hand-off (_push_cloud).
        _fail(
            f"{name} runs in the cloud; there is no node to push its files to",
            _EXIT_USAGE,
        )
    try:
        nick = _current_nick(proj)
    except (OSError, ValueError) as exc:
        log.get_logger("nodes").warning("push could not read the node map: %s", exc)
        _fail(
            f"{nodes.map_unread_text(exc)}, so where {name} runs is unknown;"
            " nothing was shipped",
            1,
        )
    if nick is None:
        _fail(
            f"{name} is not placed yet -- `magent up {name}` places and starts it",
            _EXIT_USAGE,
        )
    try:
        node = nodes.node_for_nick(cfg, nick, local_user=env.local_username())
    except nodes.NodeConfigError as exc:
        # nodes' own words, but they can quote the map's nick.
        _fail(node_sync.printable(str(exc)), _EXIT_USAGE)
    placed = dataclasses.replace(proj, node=nick)
    try:
        # D's one recipe builder (DECISION-22): the push set a bring-up ships.
        recipe = launch.node_recipe(
            cfg, placed, node, launch.node_git_states(cfg, placed)
        )
    except (OSError, ValueError, remote_mux.RemoteError) as exc:
        text = _local_failure(exc, f"push could not build {name}'s recipe")
        _fail(f"cannot build {name}'s recipe ({text})", _EXIT_USAGE)
    try:
        shipped = remote_mux.push_files(node, recipe)
    except remote_mux.RemoteError as exc:
        _fail(f"@{nick} did not take the files ({_tail(exc)})", _EXIT_UNREACHABLE)
    except (OSError, ValueError) as exc:
        # Refused on this PC before the files left (a file that will not
        # read, a root the node script would not be sent).
        text = _local_failure(exc, f"push could not ship {name}'s files")
        _fail(f"could not ship {name}'s files ({text})", 1)
    if not shipped:
        click.echo(
            f"  {style('-', dim=True)} nothing to ship for {name}:"
            " no ignored .env*, local settings or push entries"
        )
        return
    # The names are the node's reply: printable ASCII only on this screen.
    listed = node_sync.printable(", ".join(shipped))
    _ok(f"shipped {len(shipped)} file(s) to @{nick}: {listed}")


def _source_node(cfg: MagentConfig, held: NodeMapEntry) -> Node | None:
    from magent import node_sync, nodes  # heavy subsystem: in-body per policy

    try:
        return nodes.node_for_nick(cfg, held.nick, local_user=env.local_username())
    except nodes.NodeConfigError as exc:
        # nodes' own words, but they can quote the map's nick.
        _note(
            f"@{held.nick} cannot be reached from this config"
            f" ({node_sync.printable(str(exc))});"
            " using what was already pulled"
        )
        return None


_RERUN = "nothing was stopped or cleared -- run the recall again"


def _resume_id(held: NodeMapEntry) -> str | None:
    """The newest pulled conversation's id, read BEFORE the session is
    stopped: a pulled folder that cannot be listed is unknown, never "nothing
    was ever pulled" (inv-unknown) -- ``--to`` would start the session fresh
    and ``--local`` print a bare ``claude``, leaving the conversation behind.
    So the recall stops here with nothing stopped or cleared. The error CLASS
    only on screen; the full error goes to nodes.log. The folder is magent's
    own path, built from a checked nick and sid."""
    from magent import nodes  # heavy subsystem: in-body per policy

    try:
        return nodes.latest_transcript_id(held.nick, held.sid)
    except OSError as exc:
        folder = nodes.transcripts_dir(held.nick, held.sid)
        log.get_logger("nodes").warning(
            "recall could not list the conversations in %s: %s", folder, exc
        )
        _fail(
            f"could not list the conversations pulled from @{held.nick} in"
            f" {folder} ({type(exc).__name__}); {_RERUN}",
            1,
        )


def _map_unreadable_fix(exc: OSError | ValueError) -> str:
    """The line for a node map that is there and cannot be read: a re-run
    alone would only read it again. No magent command rewrites or rebuilds
    node-map.json, so the repair is the user's -- fix it or move it aside,
    never delete it: it is the only record of where each project runs. The
    error CLASS only: the full error goes to nodes.log."""
    from magent import nodes  # heavy subsystem: in-body per policy

    return (
        f"the node map at {nodes.NODE_MAP_PATH} is unreadable"
        f" ({type(exc).__name__}); fix or move it aside, then run the recall again"
    )


def _final_pull(cfg: MagentConfig, name: str, held: NodeMapEntry) -> bool:
    """Step 1: one last pull, through node_sync's per-node lock -- the lock the
    daemon's tick holds -- so it never races a running daemon (DECISION-26
    xi). True when the node may still be read and named with its ssh stop
    command; False when it did not answer (ssh's 255, a timeout) or the node
    map or config cannot address it -- a refusal made on this PC proves
    neither (cq-G14 m-R3-1).

    The placement is cleared after this, and a cleared placement is never
    pulled again, so a pull that can be retried stops the recall here, before
    anything is stopped or cleared (cq-G14 I1): a daemon that keeps the node
    past the wait, a node that answered with an error (a nonzero rc), left
    files behind (PullUnfinished, matched by type -- m1) or answered with
    something that is not a pull (NotAPull -- C-R3-1), a node map the pull
    could not read again (NodeMapUnreadable) and an entry it did not find
    again. A node that did not answer at all, one the
    config cannot pull from, and a pull refused on this PC before any ssh
    (PullRefused) go on with what was already pulled -- the plan's "never
    fatal" rule, which no re-run helps."""
    from magent import (  # heavy subsystem: in-body per policy
        node_sync,
        nodes,
        remote_mux,
    )

    if not held.remote_root:
        # final_pull refuses this entry before any ssh (PullRefused), and no
        # re-run can fix it: it is a note.
        _note(
            f"@{held.nick} cannot be pulled from (the node map has no remote"
            f" root for {held.sid}); going on with what was already pulled"
        )
        return False
    try:
        pulled = node_sync.final_pull(cfg, name, local_user=env.local_username())
    except LockHeld:
        _fail(
            f"the node-sync daemon is still pulling from @{held.nick}; {_RERUN}",
            _EXIT_UNREACHABLE,
        )
    except node_sync.NodeMapUnreadable as exc:
        # Before OSError, its base: D's final_pull (Dsync 08cfa62) re-reads
        # the map strictly and raises this when it cannot. recall_cmd's rule
        # for its own read: the MAP error's class only on screen, busy past
        # the reader's retries is a re-run, torn or any other error names the
        # repair. The chained error (the path, the parser's words) goes to
        # nodes.log.
        cause = exc.__cause__
        log.get_logger("nodes").warning(
            "recall's last pull could not read the node map: %s: %s", exc, cause
        )
        if isinstance(cause, ValueError) or (
            isinstance(cause, OSError) and not isinstance(cause, PermissionError)
        ):
            _fail(_map_unreadable_fix(cause), 1)
        _fail(f"{exc}; {_RERUN}", 1)
    except OSError as exc:
        # After LockHeld (an OSError itself): this PC's side of the pull -- the
        # watermark file, the per-node lock file -- failed (cq-G14 M1). The
        # error CLASS only on screen (str(exc) carries a path); the full error
        # goes to nodes.log.
        log.get_logger("nodes").warning(
            "recall's last pull from %s failed on this PC: %s", held.nick, exc
        )
        _fail(f"could not pull from @{held.nick} ({type(exc).__name__}); {_RERUN}", 1)
    except nodes.NodeConfigError as exc:
        _note(
            f"@{held.nick} cannot be pulled from ({node_sync.printable(str(exc))});"
            " going on with what was already pulled"
        )
        return False
    except node_sync.PullUnfinished as exc:
        # Before RemoteError, its base: it answered, and left files behind.
        # cq-G14 m2: a stop that can recur names its way out -- m-R3-2: the
        # reason once, and only the way out that applies. remote_mux logs the
        # file it could not store (to node_sync's log); the message can't.
        if exc.not_stored:
            stored_log = log.LOG_DIR / f"{node_sync.LOG_NAME}.log"
            remedy = (
                f"A file this PC could not store is named in {stored_log}:"
                " close what holds it open, or free disk space, first."
            )
        else:
            remedy = "A reply that ran out of room needs only another run."
        _fail(
            f"the last pull from @{held.nick} did not finish: {exc.why}; {_RERUN}"
            f"\n    {remedy}",
            1,
        )
    except remote_mux.NotAPull as exc:
        # Before RemoteError, its base (cq-G14 C-R3-1): the node answered, but
        # with nothing this PC can read as a pull -- another magent's framing,
        # a damaged or over-cap reply -- so nothing of it was stored and the
        # node still holds its newest turns. Clearing the placement now would
        # never pull them.
        _fail(
            f"@{held.nick} answered, but not with a pull this PC can read"
            f" ({_tail(exc)}); {_RERUN}\n    Bring magent on @{held.nick} to"
            " this PC's version, then run the recall again.",
            1,
        )
    except remote_mux.PullRefused as exc:
        # Refused on this PC before any ssh (cq-G14 m1), matched by TYPE: no
        # re-run clears it, so it must never block the recall. Nothing was
        # dialed, so nothing proves the node unreachable either -- only 255
        # and a timeout do -- so the live repo read and the ssh stop command
        # still follow (m-R3-1, kept by team-lead's ruling).
        _note(
            f"@{held.nick} cannot be pulled from ({_tail(exc)});"
            " going on with what was already pulled"
        )
        return True
    except remote_mux.RemoteError as exc:
        if exc.rc not in (255, None):
            # Any rc 0 of no known kind lands here too: the node's answer until
            # proven otherwise, so it keeps the placement (C-R3-1).
            # It answered with an error of its own -- one that may come back on
            # every run (no python3 is rc 3), so the stop names the fix (m2).
            _fail(
                f"the last pull from @{held.nick} did not finish ({_tail(exc)});"
                f" {_RERUN}\n    If it stops here again, fix what it names on"
                f" @{held.nick} first (python3, free disk space), then run the"
                " recall again.",
                1,
            )
        # ssh's own failure (255) or a timeout (rc None, DECISION-19) means the
        # node is unreachable: no more live reads that would only wait again.
        _note(
            f"@{held.nick} did not answer ({_tail(exc)});"
            " going on with what was already pulled"
        )
        return False
    if pulled is None:
        # D's final_pull reads the map strictly, so None is the entry gone
        # between recall's read and its own -- another magent moved it
        # (launch._final_pull's "not found again"): a pull that did not happen.
        _fail(
            f"{name}'s node map entry was not found again for the last pull; {_RERUN}",
            1,
        )
    _ok(f"pulled {held.sid} from @{held.nick} one last time")
    return True


def _when(ts: float) -> str:
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M")
    )


def _repo_line(status: RepoStatus) -> str:
    if not status.head:
        return f"{status.remote_dir}  no repo found"
    state = {True: "UNCOMMITTED CHANGES", False: "clean", None: "dirty unknown"}[
        status.dirty
    ]
    ahead = f", {status.unpushed} unpushed commit(s)" if status.unpushed else ""
    branch = f" on {status.branch}" if status.branch else ""
    return f"{status.remote_dir}  {status.head[:12]}{branch}  {state}{ahead}"


def _report_repos(source: Node | None, held: NodeMapEntry) -> None:
    """Step 2: the node's commit per repo and whether its tree was dirty --
    live when the node answers (and recorded), else the last record."""
    # heavy subsystem: in-body per policy
    from magent import node_sync, nodes, remote_mux

    record = None
    if source is not None:
        try:
            repos = remote_mux.repo_status(
                source, held.remote_root, timeout_s=RECALL_TIMEOUT_S
            )
        except remote_mux.RemoteError as exc:
            _note(f"could not read the repos on @{held.nick} ({_tail(exc)})")
        except nodes.NodeConfigError as exc:
            # The map's remote_root is untrusted too: repo_status refuses a
            # root it will not send, before any dial (plan G Task 9).
            _note(
                f"could not read the repos on @{held.nick}"
                f" ({node_sync.printable(str(exc))})"
            )
        else:
            record = nodes.RepoRecord(
                ts=time.time(), source="recall", repos=tuple(repos)
            )
            nodes.write_repo_record(held.nick, held.sid, record)
            click.echo(f"  repos on @{held.nick}, now:")
    if record is None:
        try:
            record = nodes.read_repo_record(held.nick, held.sid)
        except (OSError, ValueError) as exc:
            # There, but unreadable: unknown, never "never recorded". The
            # error CLASS only on screen; the full error goes to nodes.log.
            log.get_logger("nodes").warning(
                "recall could not read the repo record for %s/%s: %s",
                held.nick,
                held.sid,
                exc,
            )
            _note(
                f"the commit record for {held.sid} on @{held.nick} is unreadable"
                f" ({type(exc).__name__}) -- check the node before relying on"
                " `git pull`"
            )
            return
        if record is None:
            _note(
                f"no commit was ever recorded for {held.sid} on @{held.nick} --"
                " check the node before relying on `git pull`"
            )
            return
        click.echo(
            f"  repos on @{held.nick}, last known"
            f" ({record.source}, {_when(record.ts)}):"
        )
    for status in record.repos:
        # The dir, head and branch are the node's reply (or its record).
        click.echo(f"    {node_sync.printable(_repo_line(status))}")
    if any(s.dirty or s.unpushed for s in record.repos):
        _note(
            f"@{held.nick} holds work that is not pushed; it stays on the node"
            " until it is committed and pushed there"
        )


# A sid the ssh one-liner can carry inside its double quotes untouched.
_PLAIN_SID = re.compile(r"[A-Za-z0-9._-]+")


def _kill_hint(target: str | None, sid: str) -> str:
    """How to stop ``sid`` by hand: the tmux command to run on the node, or,
    given the node's ssh ``target``, how to run it from here.

    DECISION-26 iii: the target is single-quoted -- zsh reads a bare =sid as
    a command lookup. ``pullable_sid`` lets ``'``, ``$``, a backtick and ``!``
    through (a title like "Amin's site"), so the quoting is real rather than
    pasted: a ``'`` is closed, escaped and reopened. Inside the ssh line's
    double quotes the LOCAL shell would still expand ``$(...)``, a backtick or
    ``!``, so only a plain sid gets the plan's one-liner; any other gets two
    steps, with nothing double-quoted."""
    from magent import remote_mux  # heavy subsystem: in-body per policy

    quoted = "'" + ("=" + sid).replace("'", "'\\''") + "'"
    kill = f"tmux -L {remote_mux.SOCKET} kill-session -t {quoted}"
    if target is None:
        return f"stop it there with: {kill}"
    if _PLAIN_SID.fullmatch(sid):
        return f'stop it with: ssh {target} "{kill}"'
    return f"stop it there -- ssh {target}, then run on the node: {kill}"


def _stop_session(source: Node | None, held: NodeMapEntry) -> None:
    """Step 3, best effort: a session that cannot be stopped is named, with
    the exact command that stops it. "stopped" is said only when D's
    ``kill_session`` returned True (DECISION-26 x); its None -- the call
    failed -- is unknown, never "stopped" or "gone"."""
    from magent import remote_mux  # heavy subsystem: in-body per policy

    if source is None:
        _note(
            f"{held.sid} may still be running on @{held.nick};"
            f" {_kill_hint(None, held.sid)}"
        )
        return
    killed = remote_mux.kill_session(source, held.sid)
    if killed is None:
        _note(
            f"could not stop {held.sid} on @{held.nick} (unreachable, or the"
            f" kill failed); it may still be running --"
            f" {_kill_hint(source.target, held.sid)}"
        )
    elif killed:
        _ok(f"stopped {held.sid} on @{held.nick}")
    else:
        _note(f"no such session {held.sid} on @{held.nick}; nothing to stop")


def _clear_placement(name: str, held: NodeMapEntry) -> None:
    """Drop ``name`` from the node map through D's one writer. A lock another
    process keeps past its wait (``LockHeld``, an OSError -- DECISION-13), a
    map that is busy or torn by now (the strict read's OSError / ValueError)
    or a failed write is a printed failure, never a traceback. By then the
    session is already stopped, so a re-run only redoes the install and the
    clear -- once the map can be read: a torn one (ValueError) is named with
    its repair (``_map_unreadable_fix``). The error CLASS only on screen: the
    full error goes to nodes.log."""
    from magent import nodes  # heavy subsystem: in-body per policy

    try:
        nodes.update_node_map(name, None)
    except (OSError, ValueError) as exc:
        log.get_logger("nodes").warning(
            "recall could not clear %s's placement: %s", name, exc
        )
        if isinstance(exc, ValueError):
            _fail(
                f"could not clear {name}'s placement on @{held.nick}:"
                f" {_map_unreadable_fix(exc)}",
                1,
            )
        # A lock or a busy map (OSError) passes: a re-run is the whole remedy.
        _fail(
            f"could not clear {name}'s placement on @{held.nick}"
            f" ({type(exc).__name__}); run the recall again",
            1,
        )


def _cmd_needs_cd_d(target: PurePath, here: PurePath | None) -> bool:
    """Whether cmd.exe's plain ``cd`` would leave the user outside ``target``:
    it names a drive letter other than ``here``'s (cmd's ``cd`` changes that
    drive's folder without switching drives), or ``here`` is unknown. A
    drive-less path (POSIX) never does, and neither does a UNC path -- cmd
    cannot sit in one at all, so ``cd /d`` would be no truer."""
    drive = target.drive
    if len(drive) != 2 or drive[1] != ":":
        return False
    return here is None or drive.lower() != here.drive.lower()


def _shell_folder() -> Path | None:
    """The folder the user's shell is in, or None when it cannot be read --
    the folder was deleted under the shell. None means "drive unknown", which
    ``_cmd_needs_cd_d`` answers with the hint rather than a guess."""
    try:
        return Path.cwd()
    except OSError:
        return None


def _recall_local(
    held: NodeMapEntry,
    name: str,
    local_dir: Path,
    resume_id: str | None,
) -> None:
    """Steps 4-5 for ``--local``: install into THIS machine's Claude dir for
    the local folder, clear the placement, print the resume -- never launch
    it, because the user picks the terminal."""
    # heavy subsystem: in-body per policy
    from magent import node_sync, nodes, remote_mux
    from magent.sessions import claude  # beside its siblings: one import site

    # The store a launch reads for this folder, from the one seam that names it
    # (cq-G14 M5) -- never a second hand-built ~/.claude path.
    # ROUTING-MERGE: a routed project's conversations live under its account's
    # CLAUDE_CONFIG_DIR; after the routing merge pass that project's routed
    # config dir here instead of None, or recall installs into the wrong store.
    dest = claude._projects_dir(None, str(local_dir))
    pulled = nodes.transcripts_dir(held.nick, held.sid)
    if pulled.is_dir():
        try:
            # --to's rules (cq-G14 M3): no link followed, no pull temp copied.
            replaced = remote_mux.copy_mirror(pulled, dest)
        except remote_mux.MirrorIsALink as exc:
            # No re-run fixes a linked mirror: the user must look at it.
            _fail(
                f"{exc}; nothing was installed and {name} stays placed on @{held.nick}",
                1,
            )
        except OSError as exc:
            # The error CLASS only on screen (str(exc) carries a path); the
            # full error goes to nodes.log.
            log.get_logger("nodes").warning(
                "recall could not install the conversation into %s: %s", dest, exc
            )
            _fail(
                f"could not install the conversation into {dest}"
                f" ({type(exc).__name__}); {name} stays placed on @{held.nick}"
                " -- run the recall again",
                1,
            )
        if replaced:
            # Said aloud, like --to's KEPT lines (cq-G14 M2): a local file the
            # node's copy overwrote is work this PC may have had.
            # The names come from the node's mirror: printable ASCII only.
            _note(
                f"replaced {len(replaced)} file(s) already in {dest} with the"
                f" node's copy: {node_sync.printable(', '.join(replaced))}"
            )
        _ok(f"installed the conversation into {dest}")
    else:
        _note(
            f"nothing was ever pulled from @{held.nick} for {held.sid};"
            " there is no conversation to install"
        )
    _clear_placement(name, held)
    click.echo(
        f"\n  {style(name, bold=True)} is home."
        " Once the node's commits are pushed, run:"
    )
    click.echo(f'    git -C "{local_dir}" pull')
    # Forward correction (plan G :3919-3921, M6 ruling): the plan prints
    # `cd "<dir>" && claude ...` on one line, which Windows PowerShell 5.1
    # cannot parse and cmd.exe runs in the wrong folder when <dir> is on
    # another drive. Two lines work in every shell; the hint covers cmd.
    click.echo(f'    cd "{local_dir}"')
    if _cmd_needs_cd_d(local_dir, _shell_folder()):
        click.echo(style("    (cmd.exe: use cd /d)", dim=True))
    if resume_id is None:
        click.echo("    claude")
        return
    click.echo(f"    claude --resume {resume_id}")
    click.echo(
        style(
            f"  If claude says it cannot find that conversation, it is on disk at"
            f" {dest / (resume_id + '.jsonl')}; resume by hand: run `claude --resume`"
            f" in {local_dir} and pick it from the list.",
            dim=True,
        )
    )


def _destination(
    cfg: MagentConfig, proj: ProjectConfig, held: NodeMapEntry, to_nick: str
) -> tuple[Node, str]:
    """Everything ``--to`` can refuse, checked BEFORE the source session is
    touched: the node and the session root the conversation goes to."""
    # heavy subsystem: in-body per policy
    from magent import launch, node_sync, nodes, remote_mux

    name = nodes.project_name(proj)
    if to_nick not in cfg.settings.nodes:
        _fail(f"no node named {to_nick!r} in settings.nodes", _EXIT_USAGE)
    if to_nick == held.nick:
        _fail(f"{name} is already on @{to_nick}", _EXIT_USAGE)
    if proj.node != NODE_AUTO:
        _fail(
            f'{name} is pinned to @{proj.node} in config; change its "node" to'
            f' "{to_nick}" (or "auto") to move it',
            _EXIT_USAGE,
        )
    try:
        target = nodes.node_for_nick(cfg, to_nick, local_user=env.local_username())
    except nodes.NodeConfigError as exc:
        _fail(node_sync.printable(str(exc)), _EXIT_USAGE)
    moved = dataclasses.replace(proj, node=to_nick)
    try:
        # D's one recipe builder (DECISION-22), so the root is the one the
        # bring-up will record.
        recipe = launch.node_recipe(
            cfg, moved, target, launch.node_git_states(cfg, moved)
        )
    except (OSError, ValueError, remote_mux.RemoteError) as exc:
        text = _local_failure(exc, f"recall could not build {name}'s recipe")
        _fail(f"cannot build {name}'s recipe ({text})", _EXIT_USAGE)
    return target, recipe.remote_root


def _recall_to(
    cfg: MagentConfig,
    proj: ProjectConfig,
    held: NodeMapEntry,
    target: Node,
    remote_root: str,
    resume_id: str | None,
    config_path: str,
) -> None:
    """Steps 4-5 for ``--to``: install on the new node, clear the placement,
    then the normal bring-up resuming the newest conversation (G-C8: its own
    ssh call, so D's bring_up.sh is untouched). A refused install keeps the
    OLD placement -- `magent up` resumes it where it was. A session up on
    the new node gets the node sync every bring-up leaves running, started
    on ``config_path``, the file this recall read."""
    # heavy subsystem: in-body per policy
    from magent import launch, node_sync, nodes, remote_mux

    name = nodes.project_name(proj)
    if resume_id is not None:
        try:
            installed = remote_mux.install_transcripts(
                target,
                remote_root,
                nodes.transcripts_dir(held.nick, held.sid),
                timeout_s=remote_mux.INSTALL_TIMEOUT_S,
            )
        except remote_mux.RemoteError as exc:
            _fail(
                f"could not install the conversation on @{target.nick}"
                f" ({_tail(exc)}); {name} stays placed on @{held.nick} --"
                f" `magent up {name}` resumes it there",
                _EXIT_UNREACHABLE,
            )
        except nodes.NodeConfigError as exc:
            # A root the install will not send, refused before any dial.
            _fail(
                f"could not install the conversation on @{target.nick}"
                f" ({node_sync.printable(str(exc))});"
                f" {name} stays placed on @{held.nick}",
                _EXIT_USAGE,
            )
        # The node's reply (where it landed, what it kept): printable ASCII
        # only on this screen. Forward correction (plan G :4015): the line
        # names .landed and prints .note, never the object.
        _ok(
            f"installed the conversation on @{target.nick} in"
            f" {node_sync.printable(installed.landed)}"
        )
        if installed.note:
            _note(node_sync.printable(installed.note))
    else:
        _note(
            f"nothing was ever pulled from @{held.nick} for {held.sid};"
            f" {name} starts fresh on @{target.nick}"
        )
    # A held map lock stops the move here, before the bring-up.
    _clear_placement(name, held)
    outcome = launch.bring_up_node_project(
        cfg, dataclasses.replace(proj, node=target.nick), resume_id=resume_id
    )
    if not outcome.ok:
        # The error can be the node's last stderr line (D's _node_error_text).
        installed_there = (
            " The conversation is installed there;" if resume_id is not None else ""
        )
        _fail(
            f"bring-up on @{target.nick} failed:"
            f" {node_sync.printable(outcome.error or 'see nodes.log')}."
            f"{installed_there} `magent up {name}` tries again"
            " (auto placement chooses by load)",
            _EXIT_UNREACHABLE,
        )
    # What `up` and --go do once a node session is up: only a running daemon
    # pulls it from here on.
    launch._keep_node_sync(cfg, config_path)
    for warning in outcome.warnings:
        _note(node_sync.printable(warning))
    if outcome.attached_existing:
        # D attached to a session already running there: nothing was resumed.
        _ok(f"{name} was already running on @{target.nick}; attached to it")
        return
    _ok(
        f"{name} runs on @{target.nick}"
        + (f", resuming {resume_id}" if resume_id else "")
    )


@node_group.command("recall")
@click.argument("project")
@click.option(
    "--to",
    "to_nick",
    default=None,
    metavar="NICK",
    help="Move the session to this node and resume it there.",
)
@click.option(
    "--local",
    "to_local",
    is_flag=True,
    help="Bring the session home and print the command that resumes it.",
)
@click.pass_context
def recall_cmd(
    ctx: click.Context, project: str, to_nick: str | None, to_local: bool
) -> None:
    """Bring a node session home, or move it to another node.

    Pulls once more, reports the node's last commit per repo, stops the
    session, installs its conversation and memory where the destination's
    Claude looks, and clears the placement. A node that does not answer is
    reported, never fatal: what was already pulled is used.
    """
    from magent import nodes  # heavy subsystem: in-body per policy

    if (to_nick is not None) == to_local:
        raise click.UsageError("pass exactly one of --to <nick> or --local")
    config_file = find_config(ctx.obj.get("config_path"))
    cfg = _load_config_or_exit(config_file)
    proj = _node_project_or_exit(cfg, project)
    name = nodes.project_name(proj)
    if is_cloud(proj):
        # DECISION-15: a cloud session has no node session to recall. J11m
        # replaces this refusal with its teleport branch (_recall_cloud).
        _fail(f"{name} runs in the cloud; recall moves node sessions", _EXIT_USAGE)
    tool = proj.tool or cfg.settings.default_tool
    if tool != "claude":
        _fail(
            f"recall moves Claude Code conversations; {name} runs {tool!r}",
            _EXIT_USAGE,
        )
    # Strict (cq-G14 M7): the tolerant reader's {} for a busy or torn map
    # would be the untrue "not placed". Only a missing file means that.
    try:
        held = nodes.load_node_map_strict().get(name)
    except (OSError, ValueError) as exc:
        # The error CLASS only on screen: str(exc) carries the parser's text.
        log.get_logger("nodes").warning("recall could not read the node map: %s", exc)
        if isinstance(exc, PermissionError):
            # What the strict reader re-raises once its busy retries run out:
            # another process holds the map, and a re-run is the remedy.
            _fail(
                f"could not read the node map ({type(exc).__name__});"
                " run the recall again",
                1,
            )
        _fail(_map_unreadable_fix(exc), 1)
    if held is None:
        _fail(
            f"{name} is not placed on a node -- there is nothing to recall",
            _EXIT_USAGE,
        )
    # Forward correction (plan G :3973-3976): the map is untrusted and nothing
    # between it and the disk checks a sid, so it is checked here, before any
    # path under ~/.magent/nodes or ~/.claude is built from it.
    if not nodes.pullable_sid(held.sid):
        _fail(
            f"the node map names {name}'s session {held.sid!r}, which this"
            " machine cannot store; nothing was touched",
            _EXIT_USAGE,
        )
    local_dir = _local_dir(cfg, proj) if to_local else None
    if to_local and local_dir is None:
        _fail(
            f"{proj.path} does not exist on this machine -- clone it first,"
            " then recall",
            _EXIT_USAGE,
        )
    destination = (
        _destination(cfg, proj, held, to_nick) if to_nick is not None else None
    )
    click.echo(
        f"\n  {style(f'magent node recall {name}', bold=True)}"
        f" {style(f'(from @{held.nick})', dim=True)}"
    )
    source = _source_node(cfg, held)
    reachable = source is not None and _final_pull(cfg, name, held)
    _report_repos(source if reachable else None, held)
    resume_id = _resume_id(held)
    _stop_session(source if reachable else None, held)
    if local_dir is not None:
        _recall_local(held, name, local_dir, resume_id)
    elif destination is not None:
        target, remote_root = destination
        _recall_to(cfg, proj, held, target, remote_root, resume_id, str(config_file))
