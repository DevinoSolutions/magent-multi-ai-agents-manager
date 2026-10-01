"""`magent node add` / `magent node remove`: a machine joins the pool with one
command and leaves it with one.

`add` resolves the host through this PC's ssh config (``ssh -G``, never a
dial), derives a nick when none is given, writes ``settings.nodes`` through
the raw-dict round-trip (creating the config when there is none), then runs
``node setup``'s whole flow inline -- the root hop, GitHub, the Claude token,
the user scope and the health check -- and ends on one verdict. `remove`
edits the config only: the machine, its users and its files are left as
they are. `ready_gate` is the same setup offered from inside a bring-up, for
a node a project needs that cannot run it yet.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import click

from magent import env, node_auth
from magent.cli import node_cmd
from magent.cli.config_io import (
    _as_dict,
    _load_config_or_exit,
    _load_raw_config,
    _project_dicts,
    _save_raw_config_atomic,
    _sub,
    _validate_config_text,
)
from magent.cli.node_cmd import (
    _refuse,
    _setup_plan,
    _setup_verdict,
    node_group,
    run_setup,
)
from magent.config import NODE_AUTO, NODE_PLACEMENTS, default_config
from magent.paths import find_config
from magent.style import style
from magent.titles import get_leaf_name

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from magent.config import MagentConfig, ProjectConfig

# config.py's nick rule (``_NODE_NICK_RE``), mirrored so a derived nick is
# checked before anything is written; pinned equal by test.
NICK_RE = re.compile(r"[a-z0-9-]{1,6}")
_IPV4 = re.compile(r"\d{1,3}(\.\d{1,3}){3}")


def derive_nick(host: str, *, taken: Collection[str]) -> str:
    """A nick for ``host``: its most telling part, fitted to the status bar's
    1-6 characters of ``[a-z0-9-]`` -- ``gpu-server`` -> ``server``,
    ``build.example.com`` -> ``build``, ``10.0.0.7`` -> ``ip7`` -- then made
    unique against ``taken`` and the placement words with a trailing digit."""
    low = host.lower()
    if _IPV4.fullmatch(low):
        base = "ip" + low.rsplit(".", 1)[1]
    else:
        label = re.sub(r"[^a-z0-9-]+", "-", low.split(".", 1)[0]).strip("-")
        parts = [p for p in label.split("-") if p]
        base = label if len(label) <= 6 else (parts[-1] if parts else "")
    base = base[:6].strip("-") or "node"
    blocked = {*taken, *NODE_PLACEMENTS}
    if base not in blocked:
        return base
    for n in range(2, 100):
        suffix = str(n)
        candidate = base[: 6 - len(suffix)] + suffix
        if candidate not in blocked:
            return candidate
    raise ValueError("no free nick -- pass one with --nick")


def _usage(message: str) -> NoReturn:
    _refuse(message)


def _check_host(host: str) -> None:
    """config load's host rule, checked before anything runs: no user part
    (the node user is --user; setup's root login is its own), no whitespace,
    no leading '-' (ssh would read an option)."""
    if "@" in host:
        _usage(
            "give the host alone: the node user is --user, and setup logs in as "
            "root on its own"
        )
    if not host or host.startswith("-") or any(c.isspace() for c in host):
        _usage(f"not a host name: {host!r}")


def _existing_nick(pool: dict[str, object], host: str) -> str | None:
    for nick, conf in pool.items():
        if isinstance(conf, dict) and conf.get("host") == host:
            return nick
    return None


def _check_login(nick: str, host: str, user: str | None) -> MagentConfig:
    """The D4 user rule for the new node, before it is written: without
    --user, sessions run as this PC's login, which must be a node login.
    Returns a one-node config for setup's own pre-checks."""
    from magent import nodes  # heavy subsystem: in-body per policy
    from magent.config import MagentConfig, NodeConfig, Settings

    probe = MagentConfig(
        projects=[],
        settings=Settings(nodes={nick: NodeConfig(nick=nick, host=host, user=user)}),
    )
    try:
        nodes.node_for_nick(probe, nick, local_user=env.local_username())
    except nodes.NodeConfigError as exc:
        _usage(f"{exc} -- pass --user <name>")
    return probe


@node_group.command("add")
@click.argument("host")
@click.option(
    "--nick", help="The node's short name (1-6 of a-z 0-9 -; default: from HOST)."
)
@click.option(
    "--user", help="The Unix user sessions run as there (default: your login)."
)
@click.option(
    "--key",
    "key_file",
    type=click.Path(dir_okay=False, path_type=Path),
    help="This PC's ssh PUBLIC key to authorize (default: ~/.ssh/id_ed25519.pub, ...).",
)
@click.pass_context
def node_add_cmd(
    ctx: click.Context,
    host: str,
    nick: str | None,
    user: str | None,
    key_file: Path | None,
) -> None:
    """Add a machine to the node pool and set it up, in one go.

    HOST is a name or an alias from your ssh config; setup logs in as
    root@HOST once. Writes settings.nodes (creating the config if there is
    none), then runs `magent node setup` inline and checks the node. The first
    node asks for one browser approval (the Claude subscription token). Adding
    a host already in the pool sets it up again. Exit 0 when the node is
    ready, 1 when a step failed (the node stays in the pool: rerun `magent node
    setup <nick>`), 2 when nothing was written.
    """
    # heavy subsystem: in-body per policy (remote_mux: ssh/tar; --help never pays)
    from magent import remote_mux

    _check_host(host)
    path = find_config(ctx.obj.get("config_path"))
    data = _load_raw_config(path) if path.exists() else default_config([])
    pool = _sub(_sub(data, "settings"), "nodes")
    same = _existing_nick(pool, host)
    if nick is None:
        nick = same or derive_nick(host, taken=set(pool))
    elif nick in pool and nick != same:
        _usage(f"node {nick!r} is already {pool[nick]!r}: pick another --nick")
    if not NICK_RE.fullmatch(nick) or nick in NODE_PLACEMENTS:
        _usage(
            f"not a node nick: {nick!r} -- 1-6 characters of a-z, 0-9 and '-', "
            f"and not {' or '.join(NODE_PLACEMENTS)}"
        )
    entry: dict[str, object] = dict(_as_dict(pool.get(nick)))
    entry["host"] = host
    if user is not None:
        entry["user"] = user
    stored_user = entry.get("user")
    probe = _check_login(
        nick, host, stored_user if isinstance(stored_user, str) else None
    )
    pool[nick] = entry
    why = _validate_config_text(json.dumps(data))
    if why is not None:
        _usage(f"not saved: {why}")
    real = remote_mux.ssh_config_hostname(host)
    shown = f"{host} ({real})" if real and real != host else host
    try:
        # Everything setup refuses (no public key, a bad user name) is refused
        # BEFORE the config is written: a failed add leaves nothing behind.
        _setup_plan(probe, nick, (), key_file)
    except ValueError as exc:
        _usage(str(exc))
    _save_raw_config_atomic(path, data)
    click.echo(
        f"  {style('magent node add', bold=True)} {shown}  "
        f"{style(f'as @{nick} in {path}', dim=True)}"
    )
    click.echo()
    cfg = _load_config_or_exit(path)
    failed = run_setup(cfg, nick, key_file=key_file)
    if failed:
        click.echo()
        click.echo(
            f"  @{nick} stays in the pool -- fix the step above, then rerun: "
            f"magent node setup {nick}"
        )
        _setup_verdict(failed)
    _setup_verdict(0)
    click.echo(
        f"  Next: magent config add <project> --node {nick}  "
        f"{style('(or --node auto)', dim=True)}, then magent up"
    )


def _stranded(
    projects: Sequence[object], nick: str, pool: Collection[str]
) -> list[str]:
    """Why each project would have no node once ``nick`` leaves ``pool``,
    as ``"<name> (<reason>)"``, in config order: pinned to ``nick``, or
    ``auto`` with no node left. Read off typed projects and raw dicts alike
    (``node`` and ``title``/``path``), so the list shown and the entries
    switched are one rule."""
    last = set(pool) <= {nick}
    out: list[str] = []
    for proj in projects:
        node = _field(proj, "node")
        if node == nick:
            why = f"pinned to @{nick}"
        elif node == NODE_AUTO and last:
            why = "node: auto, no node left"
        else:
            continue
        path = _field(proj, "path") or ""
        name = _field(proj, "title") or get_leaf_name(path)
        out.append(f"{name} ({why})")
    return out


def _bring_home(cfg: MagentConfig, name: str) -> str:
    """The command that ends ``name``'s node session without stranding it.

    recall, not down: down stops the pane but leaves the conversation on the
    node, where the removal would strand it; recall pulls it home, then
    stops the session. recall moves Claude Code conversations only, so any
    other tool -- or a name no project carries any more -- is just stopped.
    """
    from magent import nodes  # heavy subsystem: in-body per policy

    for proj in cfg.projects:
        if nodes.project_name(proj) == name:
            if (proj.tool or cfg.settings.default_tool) == "claude":
                return f"magent node recall {name} --local"
            break
    return f"magent down {name}"


def _field(proj: object, key: str) -> str | None:
    value = proj.get(key) if isinstance(proj, dict) else getattr(proj, key, None)
    return value if isinstance(value, str) and value else None


@node_group.command("remove")
@click.argument("nick")
@click.option(
    "--local",
    is_flag=True,
    help="Run the projects that would be left without a node (pinned to "
    "NICK, or auto with no node left) on this PC, in the same save.",
)
@click.pass_context
def node_remove_cmd(ctx: click.Context, nick: str, *, local: bool) -> None:
    """Take a node out of the pool. Edits the config only: the machine, its
    users and its files are left as they are.

    A project that would be left with no node -- pinned to NICK, or auto
    with NICK the last node -- runs on this PC instead: at a terminal you are
    asked (default yes), elsewhere pass --local. Refused while a session is
    placed there (bring it home first: magent node recall <name> --local).
    Exit 0 when removed, 1 when refused, 2 when NICK is not a node.
    """
    from magent import nodes  # heavy subsystem: in-body per policy

    path = find_config(ctx.obj.get("config_path"))
    cfg = _load_config_or_exit(path)
    if nick not in cfg.settings.nodes:
        known = ", ".join(sorted(cfg.settings.nodes)) or "none"
        _usage(f"{nick!r} is not a node; known nodes: {known}")
    try:
        placed = sorted(
            name for name, e in nodes.load_node_map_strict().items() if e.nick == nick
        )
    except (OSError, ValueError) as exc:
        _refused(f"{nodes.map_unread_text(exc)}, so what runs on @{nick} is unknown")
    if placed:
        # Recall first: once the node is out of the pool, magent could no
        # longer reach -- or stop -- a session still running there.
        them = "it" if len(placed) == 1 else "them"
        _refused(
            f"@{nick} still runs {', '.join(placed)} -- bring {them} home first: "
            f"{'; '.join(_bring_home(cfg, name) for name in placed)} "
            f"(once @{nick} is removed, magent could no longer reach it)"
        )
    stranded = _stranded(cfg.projects, nick, cfg.settings.nodes)
    if stranded and not local:
        one_step = f"magent node remove {nick} --local"
        if not node_cmd._can_approve():
            _refused(
                f"{', '.join(stranded)} would have no node to run on.\n"
                f"  Run them on this PC and remove @{nick} in one step: {one_step}"
            )
        click.echo(f"  Without @{nick} these projects would have no node to run on:")
        for row in stranded:
            click.echo(f"    {row}")
        if not click.confirm("  Run them on this PC instead?", default=True):
            _refused(f"nothing changed. To do it later in one step: {one_step}")
    data = _load_raw_config(path)
    pool = _sub(_sub(data, "settings"), "nodes")
    moved = [
        proj
        for proj in _project_dicts(data)
        if _stranded([proj], nick, cfg.settings.nodes)
    ]
    names = [row.split(" (", 1)[0] for row in _stranded(moved, nick, pool)]
    for proj in moved:
        proj.pop("node", None)
    pool.pop(nick, None)
    why = _validate_config_text(json.dumps(data))
    if why is not None:
        _refused(f"not saved: {why}")
    _save_raw_config_atomic(path, data)
    runs = "runs" if len(names) == 1 else "run"
    here = f"; {', '.join(names)} now {runs} on this PC" if names else ""
    click.echo(
        f"  {style('Removed', fg='green', bold=True)} @{nick} from the pool{here}. "
        f"{style('Nothing on the machine was touched.', dim=True)}"
    )


def _refused(message: str) -> NoReturn:
    click.echo(f"  Not removed: {message}", err=True)
    sys.exit(1)


# Why a node a bring-up needs cannot run a project yet: the words after
# "@<nick> ". ``_fix`` is the command that makes it ready.
UNANSWERED = "unanswered"
NO_TOKEN = "no-token"
_NOT_READY = {
    UNANSWERED: "is not set up, or did not answer",
    NO_TOKEN: "has no Claude token, and this PC has none to give it",
}


def _fix(nick: str, state: str) -> str:
    if state == NO_TOKEN:
        return node_auth.REFRESH_COMMAND
    return f"magent node setup {nick}"


def node_readiness(
    cfg: MagentConfig, nicks: Sequence[str], *, now: float | None = None
) -> dict[str, str | None]:
    """Each of ``nicks``: None when it can run a project, else ``UNANSWERED``
    or ``NO_TOKEN``. Read the way placement reads a node -- its load window,
    one live reading when that is thin (``nodes.placement_samples``) -- so a
    bring-up keeps one notion of a node's health. A node that yields no
    sample did not answer; one whose newest sample says it has no Claude
    token is ready only while this PC holds a token its provision can ship."""
    # heavy subsystem: in-body per policy (launch/nodes: sampling over ssh)
    from magent import launch, nodes

    if not nicks:
        return {}
    view = dataclasses.replace(
        cfg,
        settings=dataclasses.replace(
            cfg.settings, nodes={n: cfg.settings.nodes[n] for n in nicks}
        ),
    )
    samples, _ = nodes.placement_samples(
        view,
        now=time.time() if now is None else now,
        live_sample=launch.live_sampler(view),
    )
    can_ship = node_auth.token_health().state in ("ok", "soon")
    states: dict[str, str | None] = {}
    for nick in nicks:
        window = samples.get(nick, [])
        if not window:
            states[nick] = UNANSWERED
            continue
        latest = max(window, key=lambda s: s.ts)
        tokenless = latest.claude_auth is False and not can_ship
        states[nick] = NO_TOKEN if tokenless else None
    return states


def _offer(cfg: MagentConfig, nick: str, state: str) -> bool:
    """One question for an unready node, at a terminal; True once it is
    ready. Not set up: ``node setup``'s whole flow, inline. No token: one
    mint (a browser Approve) -- the bring-up's own provision ships it."""
    click.echo(f"  {style('!', fg='yellow', bold=True)} @{nick} {_NOT_READY[state]}.")
    if state == NO_TOKEN:
        if not click.confirm(
            "  Create the Claude token now? (one browser Approve)", default=True
        ):
            return False
        row = node_cmd._claude_token_row()
        node_cmd._print_rows([row])
        return row.status in ("did", "ok")
    if not click.confirm(f"  Set up @{nick} now?", default=True):
        return False
    try:
        failed = run_setup(cfg, nick)
    except ValueError as exc:
        click.echo(f"  {style('x', fg='red')} {exc}")
        return False
    click.echo()
    return failed == 0


def ready_gate(
    cfg: MagentConfig,
    scope: Sequence[ProjectConfig],
    *,
    now: float | None = None,
) -> MagentConfig:
    """``cfg`` with every project in ``scope`` that cannot run on its node
    turned off, after offering -- at a terminal only -- to make that node
    ready. A pinned project needs its node; an ``auto`` project needs any
    one node of the pool, so setup is offered for it only while none is
    ready, one node at a time. Without a terminal nothing is asked, minted
    or sent: one line per skipped project names its node and the command
    that fixes it. Returns ``cfg`` itself when nothing was skipped."""
    # heavy subsystem: in-body per policy
    from magent import nodes

    pool = cfg.settings.nodes
    pinned = list(dict.fromkeys(p.node for p in scope if p.node in pool))
    auto = any(p.node == NODE_AUTO for p in scope)
    if not pinned and not auto:
        return cfg
    states = node_readiness(cfg, list(pool) if auto else pinned, now=now)
    if node_cmd._can_approve():
        for nick in pinned:
            state = states[nick]
            if state is not None and _offer(cfg, nick, state):
                states[nick] = None
        if auto and all(states.values()):
            for nick, state in list(states.items()):
                if state is not None and _offer(cfg, nick, state):
                    states[nick] = None
                    break
    unready = {n: s for n, s in states.items() if s is not None}
    # An auto project is blocked only while no node is ready; it names the
    # first node that is not.
    first = next(iter(unready.items()), None) if len(unready) == len(states) else None
    skipped: set[int] = set()
    for proj in scope:
        if proj.node in unready:
            nick, state = proj.node, unready[proj.node]
        elif proj.node == NODE_AUTO and first is not None:
            nick, state = first
        else:
            continue
        skipped.add(id(proj))
        click.echo(
            f"  {style('x', fg='red')} {nodes.project_name(proj)}: skipped -- "
            f"@{nick} {_NOT_READY[state]}; run: {_fix(nick, state)}"
        )
    if not skipped:
        return cfg
    return dataclasses.replace(
        cfg,
        projects=[
            dataclasses.replace(p, enabled=False) if id(p) in skipped else p
            for p in cfg.projects
        ],
    )
