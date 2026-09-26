"""`magent node`: run projects on a pool of Linux machines over ssh.

This module starts with `node sync`, the daemon that mirrors the pool onto
this PC; the other subcommands arrive with their own sub-plans. Exit codes and
lines live here, the work in magent.node_sync (imported in-body: the
registration hub imports every command module, and `magent --help` must not
pay for ssh and tar).
"""

from __future__ import annotations

import re
import shutil
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
    from magent.config import MagentConfig, ProjectConfig
    from magent.nodes import Node, NodeMapEntry, Placement, RepoStatus
    from magent.remote_mux import RemoteError

# D-MERGE: index -- everything plan G defers to sub-plan D's merge, in one
# place (the local D-MERGE notes below mark each insertion point). The tests
# that wait on D are gated through ONE list, tests/unit/_node_fixtures.py's
# D_ATTRS: launch.node_recipe, launch.node_git_states,
# launch.bring_up_node_project, launch.NodeBringUpOutcome,
# remote_mux.push_files, remote_mux.kill_session.
# - plan (Task 12): `_print_push_set` (plan G :3192-3210) and its call at the
#   end of plan_cmd's loop (:3257). Needs node_git_states.
# - push (Task 13): `_current_nick` and `push_cmd` (:3390-3440) and the docs
#   row "magent node push <project>" after plan's row in cli/docs.py
#   (:3444-3449). Needs node_recipe, node_git_states, push_files. When it
#   lands:
#   - `_tail` (:3398-3399) already landed with recall (T14); do not re-add it;
#   - add `import dataclasses` (push_cmd, and recall --to's `_destination`
#     and `_recall_to`, call `dataclasses.replace`);
#   - `RemoteError` stays under TYPE_CHECKING (already there, for `_tail`);
#   - `_current_nick` and `push_cmd` import `nodes` in-body like every other
#     function here -- the plan's code reads a module-level `nodes`.
# - recall's kill (Task 14): `_stop_session`'s reachable branch becomes
#   :3853-3874 (remote_mux.kill_session and its three outcomes); on None it
#   prints `_kill_hint(source.target, held.sid)`, never the plan's raw
#   f-string (spec-G14 P1). Needs kill_session.
# - recall --to (Task 15): the option (:3934) and its usage rule; the two
#   `to_local` guards -- `_local_dir` only for --local (:3960-3962), and
#   `_recall_local` back under `if local_dir is not None:` with `_recall_to`
#   as its `elif` (:3969-3970, :4252-4266); `_destination` and `_recall_to`
#   (:4173-4249, which need `import dataclasses`, above); the recall docs
#   row's "(--to <nick> | --local)" wording in cli/docs.py; and regenerate the
#   help snapshots in tests/unit/test_cli_structure.py -- the ("node",
#   "recall") entry and, if the docstring changes, the ("node",) group's
#   short help. Needs node_recipe, node_git_states, bring_up_node_project,
#   NodeBringUpOutcome.
# - `Path` is a RUNTIME import (recall builds paths at run time); keep it out
#   of TYPE_CHECKING when D's imports are merged.
# Exit criterion: after D merges,
#   uv run pytest tests/unit/test_node_cmd.py tests/unit/test_node_recall.py -rs
# shows no skip reason containing "D-MERGE", and
#   git grep -n D-MERGE -- src tests
# comes back empty.

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


# What the user column shows for a node whose login cannot be resolved (an
# explicit empty "user", or a local name that is not a node login).
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
    windows, _ = nodes.placement_samples(cfg, now=now, live_sample=None)
    rows: list[list[str]] = []
    for nick, conf in cfg.settings.nodes.items():
        window = windows.get(nick, [])
        score = nodes.score_node(nick, window)
        load, mem, mine = "no data", "-", "-"
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


# Exit codes: 2 = nothing to act on (unknown project, not a node project, a
# recall of a project the node-map does not place, bad destination), 3 = a node
# did not answer or refused. A plan that places a project nowhere is an
# answer, not a failure: it exits 0.
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
    """The project's folder on THIS machine, fully resolved (the exact string
    Claude will see as its cwd after a ``cd`` to it), or None when missing."""
    from magent.launch import (  # heavy subsystem: in-body per policy
        _expand_base_dir,
        _resolve_path,
    )

    base_dir = _expand_base_dir(cfg.base_dir) if cfg.base_dir else None
    resolved = _resolve_path(proj.path, base_dir)
    return Path(resolved).resolve() if resolved else None


# D-MERGE: `_print_push_set` (plan G :3192-3210) and its call at the end of
# plan_cmd's loop (:3257) need D's launch.node_git_states; they land with D's
# merge, and test_plan_lists_the_push_set_relative_to_the_project switches on
# with it.


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


# D-MERGE: `magent node push` (plan G Task 13: push_cmd and _current_nick at
# :3390-3440, its docs row at :3444-3449) re-ships through D's recipe builder
# and delivery -- launch.node_recipe, launch.node_git_states and
# remote_mux.push_files -- so the whole command lands with D's merge. `_tail`
# (:3398-3399) already landed with recall (T14); do not re-add it.
# tests/unit/test_node_cmd.py::TestNodePush is written and switches on then.


# repo_status on a node that answers: a quick read, never a fetch.
RECALL_TIMEOUT_S = 60.0


def _tail(exc: RemoteError) -> str:
    return exc.stderr_tail.strip() or (
        "timed out" if exc.rc is None else f"exit {exc.rc}"
    )


def _source_node(cfg: MagentConfig, held: NodeMapEntry) -> Node | None:
    from magent import nodes  # heavy subsystem: in-body per policy

    try:
        return nodes.node_for_nick(cfg, held.nick, local_user=env.local_username())
    except nodes.NodeConfigError as exc:
        _note(
            f"@{held.nick} cannot be reached from this config ({exc});"
            " using what was already pulled"
        )
        return None


_RERUN = "nothing was stopped or cleared -- run the recall again"


def _final_pull(cfg: MagentConfig, name: str, held: NodeMapEntry) -> bool:
    """Step 1: one last pull, through node_sync's per-node lock -- the lock the
    daemon's tick holds -- so it never races a running daemon (DECISION-26
    xi). False when the node is gone or cannot be pulled from.

    The placement is cleared after this, and a cleared placement is never
    pulled again, so a pull that can be retried stops the recall here, before
    anything is stopped or cleared (cq-G14 I1): a daemon that keeps the node
    past the wait, a node that answered with an error or left files behind,
    and a placement the pull could not read again. Only a node that did not
    answer at all, or one the config cannot pull from, goes on with what was
    already pulled -- the plan's "never fatal" rule, which no re-run helps."""
    from magent import (  # heavy subsystem: in-body per policy
        node_sync,
        nodes,
        remote_mux,
    )

    if not held.remote_root:
        # final_pull refuses this entry before any ssh with rc 0 -- the rc of a
        # node's bad answer -- but no re-run can fix it: it is a note.
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
    except nodes.NodeConfigError as exc:
        _note(
            f"@{held.nick} cannot be pulled from ({exc});"
            " going on with what was already pulled"
        )
        return False
    except remote_mux.RemoteError as exc:
        if exc.rc not in (255, None):
            # It answered: an error it gave, or a pull it left unfinished.
            _fail(
                f"the last pull from @{held.nick} did not finish ({_tail(exc)});"
                f" {_RERUN}",
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
        # final_pull re-reads the map tolerantly: a map busy past its retries
        # (or torn, or cleared meanwhile) reads as "never placed".
        _fail(f"could not read {name}'s placement again for the last pull; {_RERUN}", 1)
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
    from magent import nodes, remote_mux  # heavy subsystem: in-body per policy

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
            _note(f"could not read the repos on @{held.nick} ({exc})")
        else:
            record = nodes.RepoRecord(
                ts=time.time(), source="recall", repos=tuple(repos)
            )
            nodes.write_repo_record(held.nick, held.sid, record)
            click.echo(f"  repos on @{held.nick}, now:")
    if record is None:
        record = nodes.read_repo_record(held.nick, held.sid)
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
        click.echo(f"    {_repo_line(status)}")
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
    the exact command that stops it."""
    if source is None:
        _note(
            f"{held.sid} may still be running on @{held.nick};"
            f" {_kill_hint(None, held.sid)}"
        )
        return
    # D-MERGE: plan G :3865-3874 replaces this line with D's
    # remote_mux.kill_session(source, held.sid) and its three outcomes --
    # "stopped" only on True, "no such session" on False, and on None the
    # command from _kill_hint(source.target, held.sid), never the plan's raw
    # f-string (spec-G14 P1). Until D lands nothing can stop the session from
    # here, so it is named with the command that does.
    _note(
        f"{held.sid} may still be running on @{held.nick};"
        f" {_kill_hint(source.target, held.sid)}"
    )


def _clear_placement(name: str, held: NodeMapEntry) -> None:
    """Drop ``name`` from the node map through D's one writer. A lock another
    process keeps past its wait (``LockHeld``, an OSError -- DECISION-13) or a
    failed write is a printed failure, never a traceback. By then the session
    is already stopped, so a re-run only redoes the install and the clear."""
    from magent import nodes  # heavy subsystem: in-body per policy

    try:
        nodes.update_node_map(name, None)
    except OSError as exc:
        _fail(
            f"could not clear {name}'s placement on @{held.nick} ({exc});"
            " run the recall again",
            1,
        )


def _recall_local(
    held: NodeMapEntry,
    name: str,
    local_dir: Path,
    resume_id: str | None,
) -> None:
    """Steps 4-5 for ``--local``: install into THIS machine's Claude dir for
    the local folder, clear the placement, print the resume -- never launch
    it, because the user picks the terminal."""
    from magent import nodes  # heavy subsystem: in-body per policy

    dest = (
        Path.home() / ".claude" / "projects" / nodes.encoded_project_dir(str(local_dir))
    )
    pulled = nodes.transcripts_dir(held.nick, held.sid)
    if pulled.is_dir():
        try:
            shutil.copytree(pulled, dest, dirs_exist_ok=True)
        except OSError as exc:
            _fail(
                f"could not install the conversation into {dest} ({exc});"
                f" {name} stays placed on @{held.nick} -- run the recall again",
                1,
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
    if resume_id is None:
        click.echo(f'    cd "{local_dir}" && claude')
        return
    click.echo(f'    cd "{local_dir}" && claude --resume {resume_id}')
    click.echo(
        style(
            f"  If claude says it cannot find that conversation, it is on disk at"
            f" {dest / (resume_id + '.jsonl')}; resume by hand: run `claude --resume`"
            f" in {local_dir} and pick it from the list.",
            dim=True,
        )
    )


# D-MERGE: `recall --to NICK` (plan G Task 15) moves the session through D's
# recipe builder and bring-up, so until D lands a recall can only go home.
# With D it lands as:
# - the option at :3934, and the usage rule "pass exactly one of --to <nick>
#   or --local";
# - `_destination` and `_recall_to` (:4173-4249), `_destination` called
#   before the heading and `_recall_to` as the `elif` after `_recall_local`
#   (:4252-4266);
# - the two `to_local` guards come back with it: `local_dir = _local_dir(cfg,
#   proj) if to_local else None` with its refusal (:3960-3962), and `if
#   local_dir is not None: _recall_local(...)` (:3969-3970). Today
#   `_local_dir` runs unconditionally, so without them a --to recall of a
#   project with no local clone would wrongly exit 2;
# - per the forward correction at :4015, `_recall_to` reads
#   `InstalledTranscripts.landed` for the directory it names and prints
#   `.note` (the kept-items line) when it is non-empty -- never the object;
# - the sid check below already covers --to (:3976).
# tests/unit/test_node_recall.py::TestRecallTo is written and switches on then.
@node_group.command("recall")
@click.argument("project")
@click.option(
    "--local",
    "to_local",
    is_flag=True,
    help="Bring the session home and print the command that resumes it.",
)
@click.pass_context
def recall_cmd(ctx: click.Context, project: str, to_local: bool) -> None:
    """Bring a node session home and print the command that resumes it.

    Pulls once more, reports the node's last commit per repo, stops the
    session, installs its conversation and memory where this machine's
    Claude looks, and clears the placement. A node that does not answer is
    reported, never fatal: what was already pulled is used.
    """
    from magent import nodes  # heavy subsystem: in-body per policy

    if not to_local:
        raise click.UsageError("pass --local")
    cfg = _load_config_or_exit(find_config(ctx.obj.get("config_path")))
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
    held = nodes.read_node_map().get(name)
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
    local_dir = _local_dir(cfg, proj)
    if local_dir is None:
        _fail(
            f"{proj.path} does not exist on this machine -- clone it first,"
            " then recall",
            _EXIT_USAGE,
        )
    click.echo(
        f"\n  {style(f'magent node recall {name}', bold=True)}"
        f" {style(f'(from @{held.nick})', dim=True)}"
    )
    source = _source_node(cfg, held)
    reachable = source is not None and _final_pull(cfg, name, held)
    _report_repos(source if reachable else None, held)
    _stop_session(source if reachable else None, held)
    resume_id = nodes.latest_transcript_id(held.nick, held.sid)
    _recall_local(held, name, local_dir, resume_id)
