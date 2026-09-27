"""`magent node`: run projects on a pool of Linux machines over ssh.

This module holds `node sync`, the daemon that mirrors the pool onto this PC,
`node doctor` and `node setup`; the other subcommands arrive with their own
sub-plans. Exit codes and lines live here, the work in magent.node_sync,
magent.nodes and magent.remote_mux (imported in-body: the registration hub
imports every command module, and `magent --help` must not pay for ssh and
tar).
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
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
    import contextlib
    from collections.abc import Sequence

    from magent.config import MagentConfig
    from magent.nodes import Node
    from magent.remote_mux import ProvisionReport, RemoteError, ScriptLine

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
            tail = f"  {detail}" if detail else ""
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
    """ssh's own failure as a row: the target and ssh's last stderr line. A
    node that went silent is "no answer from", not "cannot reach"."""
    # heavy subsystem: in-body per policy (remote_mux: ssh/tar; --help never pays)
    from magent.remote_mux import ScriptLine

    tail = exc.stderr_tail.strip().splitlines()
    why = tail[-1] if tail else f"rc={exc.rc}"
    # timed_out, not rc None: rc None is also a spawn failure or an over-cap
    # reply, and neither is a node that went silent.
    verb = "no answer from" if exc.timed_out else "cannot reach"
    return ScriptLine("fail", "reach", f"{verb} {node.target}: {why}")


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
    from magent import nodes
    from magent.remote_mux import ScriptLine

    try:
        node = nodes.node_for_nick(cfg, nick, local_user=env.local_username())
    except nodes.NodeConfigError as exc:
        return [ScriptLine("fail", "config", str(exc))]
    # This PC's rows first, at `now`: doctor.sh can take a minute, and a
    # snapshot the daemon pulls meanwhile is stamped after `now` -- read later,
    # a healthy node would read as a clock that moved back.
    local = sync_lines(cfg, nick, now=now)
    return [*_doctor_rows(node), *local]


def _doctor_rows(node: Node) -> list[ScriptLine]:
    """doctor.sh's rows for ``node``, one ssh call -- ``node doctor``'s and
    ``node setup``'s alike. An unreachable node is one ``fail reach`` row, and
    a login that printed no row at all is one ``fail doctor`` row."""
    # heavy subsystem: in-body per policy (remote_mux: ssh/tar; --help never pays)
    from magent import remote_mux
    from magent.remote_mux import ScriptLine

    try:
        remote = list(
            remote_mux.doctor(node, timeout_s=remote_mux.DOCTOR_TIMEOUT_S).lines
        )
    except remote_mux.RemoteError as exc:
        return [_unreachable(node, exc)]
    if not remote:
        # Exit 0 and not one row: a ForceCommand, a MOTD-only login -- doctor.sh
        # never ran, and silence is not health.
        return [
            ScriptLine(
                "fail",
                "doctor",
                "the node printed no doctor rows -- a restricted login (ForceCommand)?",
            )
        ]
    return remote


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


# The Unix user rule setup.sh enforces (its USER_RE), checked here too so a
# typo costs no ssh. fullmatch: `$` would let a trailing newline through.
_USER_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
_DEFAULT_PUBKEYS = ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub")
# setup.sh's KEY_RE, checked here too so a line it would refuse after the root
# hop is refused before it (pinned equal by test). Its [[:cntrl:]] is spelled
# out; fullmatch stands in for its ^...$.
_KEY_RE = re.compile(
    r"(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)"
    r"|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)"
    r" [A-Za-z0-9+/]+={0,3}( [^\x00-\x1f\x7f]*)?"
)


def _pubkey(key_file: Path | None) -> str:
    """This PC's public key line: ``key_file``, or the first default under
    ~/.ssh. Raises ValueError with the message to print; the file's content
    is never part of it -- a mistaken file may hold a secret."""
    if key_file is None:
        ssh_dir = Path.home() / ".ssh"
        key_file = next(
            (ssh_dir / name for name in _DEFAULT_PUBKEYS if (ssh_dir / name).is_file()),
            None,
        )
        if key_file is None:
            raise ValueError(
                f"no public key in {ssh_dir} -- pass one: --key <file.pub>"
            )
    try:
        # -sig: a BOM (Windows PowerShell 5.1's UTF8) is not part of the key.
        text = key_file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read {key_file}: {type(exc).__name__}") from exc
    if "PRIVATE KEY" in text:
        raise ValueError(
            f"{key_file} is a PRIVATE key -- pass its .pub file with --key"
        )
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1 or not _KEY_RE.fullmatch(lines[0]):
        raise ValueError(f"{key_file} is not one ssh public key line")
    return lines[0]


def _outcome_unknown(exc: RemoteError) -> bool:
    """Did ``exc`` kill a call that may have run to the end? A timeout or an
    over-cap reply: killing the local ssh does not stop the remote command.
    setup's row wording and its root-login hint both read this ONE answer, so
    they can never contradict each other. The flags, never ``stderr_tail``:
    that is the node's own words."""
    return exc.outcome_unknown


def _step_failed(node: Node, exc: RemoteError, step: str) -> ScriptLine:
    """A setup step's ssh failure as a row. A step whose outcome is unknown is
    not "unreachable": it may still be running there. Nothing is ever sent
    again on its own -- the user reruns setup, which is idempotent."""
    # heavy subsystem: in-body per policy (remote_mux: ssh/tar; --help never pays)
    from magent.remote_mux import ScriptLine

    if not _outcome_unknown(exc):
        return _unreachable(node, exc)
    return ScriptLine(
        "fail",
        "reach",
        (
            f"no answer from {node.target} ({exc.stderr_tail.strip()}) -- {step} "
            "may still be running there: rerun magent node setup once it has "
            "finished (every step is idempotent)"
        ),
    )


def _provision_and_check(node: Node, cfg: MagentConfig) -> list[ScriptLine]:
    """A freshly set-up user: the whole user scope (forced), then the node's
    health rows. The scope is built by ``remote_mux.provision_node``, THE
    provisioning body (DECISION-24) -- the same call the bring-up makes, so
    setup and ``--go`` can never ship different scopes. A provision that did
    not finish gets no doctor: its row already says why."""
    # heavy subsystem: in-body per policy (remote_mux: ssh/tar; --help never pays)
    from magent import remote_mux

    try:
        rows = list(
            remote_mux.provision_node(
                node,
                cfg,
                home=Path.home(),
                timeout_s=remote_mux.PROVISION_TIMEOUT_S,
                force=True,
            ).lines
        )
    except remote_mux.RemoteError as exc:
        return [_step_failed(node, exc, "the provision")]
    return rows + _doctor_rows(node)


def _missing_keys(report: ProvisionReport, names: Sequence[str]) -> list[ScriptLine]:
    """A ``fail`` row for each user setup.sh sent no key for, when nothing
    else failed: exit 0 with no row for them (a ForceCommand, a MOTD-only
    root login) means setup.sh never ran, and silence is not success."""
    # heavy subsystem: in-body per policy (remote_mux: ssh/tar; --help never pays)
    from magent.remote_mux import ScriptLine

    if report.failed:
        return []
    keys = report.keys()
    return [
        ScriptLine(
            "fail",
            f"node-key:{name}",
            (
                f"no node key came back for {name} -- setup.sh did not run to "
                "the end (a restricted root login, ForceCommand?)"
            ),
        )
        for name in names
        if name not in keys
    ]


def _root_hop(node: Node, names: list[str], pubkey: str) -> ProvisionReport:
    """setup.sh as ``root@<host>``, this once. An ssh failure prints its row
    and exits 1: nothing after it can run without the users it creates."""
    # heavy subsystem: in-body per policy (remote_mux: ssh/tar; --help never pays)
    from magent import remote_mux

    root = dataclasses.replace(node, user="root")
    try:
        # setup_node's own budget grows with the users; never pass one in.
        return remote_mux.setup_node(node, names, pubkey)
    except remote_mux.RemoteError as exc:
        _print_rows([_step_failed(root, exc, "setup")])
        if not _outcome_unknown(exc):
            click.echo(
                "    setup logs in as root once, with this PC's ssh key: check "
                f"that `ssh {root.target} true` works without a password prompt"
            )
        sys.exit(1)


@node_group.command("setup")
@click.argument("nick")
@click.option(
    "--user",
    "users",
    multiple=True,
    help="A Unix user to create on the node (repeatable; default: the node's user).",
)
@click.option(
    "--key",
    "key_file",
    type=click.Path(dir_okay=False, path_type=Path),
    help=(
        "This PC's ssh PUBLIC key to authorize (default: ~/.ssh/id_ed25519.pub, "
        "then id_ecdsa, id_rsa)."
    ),
)
@click.pass_context
def node_setup_cmd(
    ctx: click.Context, nick: str, users: tuple[str, ...], key_file: Path | None
) -> None:
    """Prepare a machine once: packages, a per-person user, your key, Claude
    Code and the node's own GitHub key, then the user scope and a check.

    Logs in as root@<host> for this one hop. Idempotent: every step prints
    ok/did/skip. The Claude login is NOT copied: run `ssh <user>@<host> claude`
    once. Exit 0 when nothing failed but that login, 1 when a step failed, 2
    when nothing was sent (unknown nick, bad user name, no public key).
    """
    # heavy subsystem: in-body per policy (remote_mux: ssh/tar; --help never pays)
    from magent import nodes, remote_mux

    cfg = _load_config_or_exit(find_config(ctx.obj.get("config_path")))
    try:
        node = nodes.node_for_nick(cfg, nick, local_user=env.local_username())
        pubkey = _pubkey(key_file)
    except ValueError as exc:
        _refuse(str(exc))
    # A repeated --user is one user: set up, keyed and provisioned once.
    names = list(dict.fromkeys(users)) or [node.user]
    for name in names:
        if not _USER_RE.fullmatch(name):
            _refuse(f"not a valid Unix user name: {name}")
        if name == "root":
            _refuse("root is not a node user: name a person's own account")

    click.echo(
        f"  {style('magent node setup', bold=True)} {nick}  "
        f"{style(f'as root@{node.host}, this once', dim=True)}"
    )
    report = _root_hop(node, names, pubkey)
    # A key row is the node's public key: it goes to GitHub, not the screen.
    rows = [line for line in report.lines if line.status != "key"]
    rows += _missing_keys(report, names)
    _print_rows(rows)

    keys = report.keys()
    ready = [name for name in names if name in keys]
    github = [
        remote_mux.register_ssh_key(keys[name], title=f"magent {name}@{node.host}")
        for name in ready
    ]
    if github:
        click.echo()
        click.echo(f"  {style('GitHub', bold=True)}")
        _print_rows(github)
    rows += github

    no_login: list[str] = []
    for name in ready:
        user_node = dataclasses.replace(node, user=name)
        click.echo()
        click.echo(f"  {style(user_node.target, bold=True)}")
        checked = _provision_and_check(user_node, cfg)
        _print_rows(checked)
        rows += checked
        if any(r.item == "claude-login" and r.status != "ok" for r in checked):
            no_login.append(user_node.target)

    # The Claude login is manual by design (D5): its row is the one fail that
    # is a step still to take, not a step that broke.
    failed = [r for r in rows if r.status == "fail" and r.item != "claude-login"]
    click.echo()
    for target in no_login:
        click.echo(
            f"  {style('!', fg='yellow', bold=True)} last step, by hand, once: "
            f"ssh {target} claude  {style('(log in; magent never copies it)', dim=True)}"
        )
    if failed:
        click.echo(f"  {style(f'{len(failed)} step(s) failed.', fg='red', bold=True)}")
        sys.exit(1)
    click.echo(f"  {style('Ready.', fg='green', bold=True)}")
