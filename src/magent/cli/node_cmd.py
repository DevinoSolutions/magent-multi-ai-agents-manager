"""`magent node`: run projects on a pool of Linux machines over ssh.

This module holds `node sync`, the daemon that mirrors the pool onto this PC,
and `node doctor`; the other subcommands arrive with their own sub-plans. Exit
codes and lines live here, the work in magent.node_sync, magent.nodes and
magent.remote_mux (imported in-body: the registration hub imports every
command module, and `magent --help` must not pay for ssh and tar).
"""

from __future__ import annotations

import dataclasses
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Literal, NoReturn

import click

from magent import env, log
from magent.cli.app import main
from magent.cli.config_io import _load_config_or_exit
from magent.cli.fleet_cmd import _stdout_safe
from magent.lockfile import LockHeld
from magent.paths import find_config
from magent.style import style

if TYPE_CHECKING:
    from collections.abc import Sequence

    from magent.config import MagentConfig
    from magent.nodes import Node
    from magent.remote_mux import RemoteError, ScriptLine

# How long `node sync -d` waits for the detached child to record its pid.
_START_POLLS = 20
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


# One mark per row status, shared by node setup and node doctor.
_ROW_MARKS: dict[str, tuple[str, str]] = {
    "ok": ("+", "green"),
    "did": ("+", "cyan"),
    "skip": ("-", "white"),
    "drop": ("-", "yellow"),
    "warn": ("!", "yellow"),
    "fail": ("x", "red"),
    "key": ("*", "cyan"),
}


def _refuse(message: str, *, as_json: bool = False) -> NoReturn:
    """A request magent cannot act on (unknown nick, bad argument): exit 2,
    before anything reaches a node."""
    if as_json:
        click.echo(json.dumps({"ok": False, "error": message}))
    else:
        click.echo(f"Error: {message}", err=True)
    sys.exit(2)


def _unreachable(node: Node, exc: RemoteError) -> ScriptLine:
    """ssh's own failure as a row: the target and ssh's last stderr line."""
    # heavy subsystem: in-body per policy (remote_mux: ssh/tar; --help never pays)
    from magent.remote_mux import ScriptLine

    tail = exc.stderr_tail.strip().splitlines()
    why = tail[-1] if tail else f"rc={exc.rc}"
    # D-MERGE: once D10 merges, word a timeout off RemoteError.timed_out
    # ("no answer from", not "cannot reach"); rc None alone also means an
    # over-cap reply, so it is not the signal.
    return ScriptLine("fail", "reach", f"cannot reach {node.target}: {why}")


def _print_rows(lines: Sequence[ScriptLine]) -> None:
    width = max((len(line.item) for line in lines), default=0)
    for line in lines:
        mark, color = _ROW_MARKS.get(line.status, ("?", "white"))
        quiet = line.status in ("ok", "skip")
        # A row's item and detail are the NODE's words (_stdout_safe's reason).
        click.echo(
            f"    {style(mark, fg=color, bold=True)} "
            f"{_stdout_safe(line.item):<{width}}  "
            f"{style(_stdout_safe(line.detail), dim=quiet)}"
        )


def sync_lines(cfg: MagentConfig, nick: str, *, now: float) -> list[ScriptLine]:
    """This PC's half of a node's health: is the sync daemon alive, and how old
    is the sessions snapshot it last pulled from ``nick``. Reads only."""
    # heavy subsystem: in-body per policy (ssh/tar; --help never pays)
    from magent import node_sync, nodes
    from magent.remote_mux import ScriptLine

    lines: list[ScriptLine] = []
    state = _daemon_state()
    if state == "ok":
        lines.append(ScriptLine("ok", "sync-daemon", "running"))
    elif state == "stale":
        lines.append(
            ScriptLine(
                "warn", "sync-daemon", "its heartbeat is stale -- see: magent status"
            )
        )
    elif node_sync.wanted(cfg):
        # The daemon's own "anything to sync?" -- serve's config gate. (Serve
        # also obeys MAGENT_NODE_SYNC=0, which this row does not read.)
        lines.append(
            ScriptLine(
                "warn",
                "sync-daemon",
                "not running -- magent serve starts it while a project runs on a node",
            )
        )
    else:
        lines.append(ScriptLine("skip", "sync-daemon", "no project runs on a node"))
    # E's reader and E's staleness rule; F does not parse sessions.json itself.
    snap = nodes.read_sessions(nick)
    interval = cfg.settings.node_sync.pull_interval_s
    limit = 2 * interval
    age = 0.0 if snap is None else max(0.0, now - snap.ts)
    if snap is None:
        lines.append(
            ScriptLine("skip", "snapshot", "no sessions snapshot from this node yet")
        )
    elif not nodes.sessions_stale(snap, pull_interval_s=interval, now=now):
        lines.append(ScriptLine("ok", "snapshot", f"pulled {age:.0f}s ago"))
    elif snap.ts > now:
        # sessions_stale reads a ts too far AHEAD as stale as well: this PC's
        # clock went backwards since the pull, it is not an old snapshot.
        lines.append(
            ScriptLine(
                "warn",
                "snapshot",
                (
                    f"stamped {snap.ts - now:.0f}s in the future, more than "
                    f"2 x pullIntervalS ({limit}s) -- this PC's clock moved "
                    "back: its sessions read stale"
                ),
            )
        )
    else:
        lines.append(
            ScriptLine(
                "warn",
                "snapshot",
                (
                    f"pulled {age:.0f}s ago, older than 2 x pullIntervalS ({limit}s): "
                    "its sessions read stale"
                ),
            )
        )
    return lines


def node_checks(cfg: MagentConfig, nick: str, *, now: float) -> list[ScriptLine]:
    """Every health row for ``nick``: the node's own (doctor.sh, one ssh call),
    then this PC's sync rows. An unreachable node is one ``fail reach`` row;
    every expected failure is a row, and ``_checks_or_crash_row`` turns a bug
    into one too. Read-only: it never provisions (DECISION-24 wires
    provisioning into ``node setup`` and the bring-up, not the doctor)."""
    # heavy subsystem: in-body per policy (remote_mux: ssh/tar; --help never pays)
    from magent import nodes, remote_mux
    from magent.remote_mux import ScriptLine

    try:
        node = nodes.node_for_nick(cfg, nick, local_user=env.local_username())
    except nodes.NodeConfigError as exc:
        return [ScriptLine("fail", "config", str(exc))]
    # This PC's rows first, at `now`: doctor.sh can take a minute, and a
    # snapshot the daemon pulls meanwhile is stamped after `now` -- read later,
    # a healthy node would read as a clock that moved back.
    local = sync_lines(cfg, nick, now=now)
    try:
        remote = list(
            remote_mux.doctor(node, timeout_s=remote_mux.DOCTOR_TIMEOUT_S).lines
        )
    except remote_mux.RemoteError as exc:
        remote = [_unreachable(node, exc)]
    if not remote:
        # Exit 0 and not one row: a ForceCommand, a MOTD-only login -- doctor.sh
        # never ran, and silence is not health.
        remote = [
            ScriptLine(
                "fail",
                "doctor",
                "the node printed no doctor rows -- a restricted login (ForceCommand)?",
            )
        ]
    return [*remote, *local]


def _checks_or_crash_row(
    cfg: MagentConfig, nick: str, *, now: float
) -> list[ScriptLine]:
    """``node_checks``, with a bug of any type turned into that node's one
    ``fail doctor`` row: one node's crash must not take every other node's rows
    down with a traceback."""
    # heavy subsystem: in-body per policy (remote_mux: ssh/tar; --help never pays)
    from magent.remote_mux import ScriptLine

    try:
        return node_checks(cfg, nick, now=now)
    except Exception as exc:  # noqa: BLE001  # reason: one node's bug, of any type, must fail only that node's rows -- the traceback goes to the log
        log.get_logger("nodes").exception("node doctor: checking %s crashed", nick)
        return [
            ScriptLine(
                "fail",
                "doctor",
                (
                    f"the check itself crashed ({type(exc).__name__}) -- "
                    "see ~/.magent/logs/nodes.log"
                ),
            )
        ]


def doctor_report(cfg: MagentConfig, nicks: list[str]) -> dict[str, list[ScriptLine]]:
    """``node_checks`` for each nick, concurrently -- one ssh each, so N nodes
    cost one DOCTOR_TIMEOUT_S -- keyed in the order given."""
    if not nicks:
        return {}
    now = time.time()
    with ThreadPoolExecutor(max_workers=len(nicks)) as pool:
        results = list(pool.map(lambda n: _checks_or_crash_row(cfg, n, now=now), nicks))
    return dict(zip(nicks, results, strict=True))


@node_group.command("doctor")
@click.argument("nick", required=False)
@click.option("--json", "as_json", is_flag=True, help="Print the rows as JSON")
@click.pass_context
def node_doctor_cmd(ctx: click.Context, nick: str | None, as_json: bool) -> None:
    """Check a node, or every node: tools, the Claude login, the node's GitHub
    key, locale, disk, and this PC's sync daemon and snapshot.

    Exit 0 when nothing failed (warnings allowed), 1 when a check failed,
    2 when NICK is not in settings.nodes.
    """
    from magent import nodes  # heavy subsystem: in-body per policy

    cfg = _load_config_or_exit(find_config(ctx.obj.get("config_path")), as_json=as_json)
    if nick is not None and nick not in cfg.settings.nodes:
        # Only a nick outside the pool is a bad REQUEST (exit 2); a pool node
        # whose user cannot resolve is a broken node, its `fail config` row
        # below. node_for_nick owns the wording, and for this nick it raises.
        try:
            nodes.node_for_nick(cfg, nick, local_user=env.local_username())
        except nodes.NodeConfigError as exc:
            _refuse(str(exc), as_json=as_json)
    nicks = [nick] if nick is not None else list(cfg.settings.nodes)
    report = doctor_report(cfg, nicks)
    failures = sum(
        1 for lines in report.values() for line in lines if line.status == "fail"
    )
    if as_json:
        body = {
            n: [dataclasses.asdict(line) for line in lines]
            for n, lines in report.items()
        }
        click.echo(json.dumps({"ok": True, "failures": failures, "nodes": body}))
        sys.exit(1 if failures else 0)
    if not nicks:
        click.echo(
            f"  {style('-', dim=True)} no nodes configured -- add one under"
            " settings.nodes, then run: magent node setup <nick>"
        )
        return
    click.echo(f"  {style('magent node doctor', bold=True)}")
    for n, lines in report.items():
        click.echo()
        click.echo(
            f"  {style(n, bold=True)}  {style(cfg.settings.nodes[n].host, dim=True)}"
        )
        _print_rows(lines)
    click.echo()
    if failures:
        click.echo(f"  {style(f'{failures} check(s) failed.', fg='red', bold=True)}")
        sys.exit(1)
    click.echo(f"  {style('No failures.', fg='green', bold=True)}")
