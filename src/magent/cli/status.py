"""Status / down: `_render_status` (shared by `status` and the menu's
`_menu_status`) and the shutdown commands. Carries E3's `_gather_status`/
`_upload_state`/`_listener_state`/`_health_check` observability additions and
the `status --json`/exit-3 contract, plus E4's remaining 2 supports_hotkey()
gates (_listener_state, down_cmd). NF-S3-001 resolved (pass-2): _menu_down
now branches on stop_server()'s return value like down_cmd; and status --json
routes an invalid-config load through _load_config_or_exit's as_json mode, so
it emits a JSON error envelope instead of a stderr line (NF-S3-005).

`_psmux_sessions` adds the missing half of "what's running": the psmux
sessions the agents actually live in (and that an SSH client attaches to),
with each one's foreground app and agent state. It is pure data -- rendered
by `_render_status`, published additively under `status --json`'s
`psmux_sessions` key, and made actionable (attach / revive) by
`_session_actions` off the interactive status menu.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, NamedTuple
from urllib.error import URLError
from urllib.request import urlopen

import click

from magent.cli.app import main
from magent.cli.background import _maybe_start_upload_server, _probe_port
from magent.cli.config_io import _as_str, _load_config_or_exit
from magent.cli.ui import (
    _banner,
    _divider,
    _grouped,
    _menu_item,
    _print_names,
    _print_session_overview,
)
from magent.log import heartbeat_age, heartbeat_fresh
from magent.paths import find_config
from magent.procs import pid_alive
from magent.style import style

if TYPE_CHECKING:
    from pathlib import Path

    from magent.config import MagentConfig


def _health_check(port: int) -> bool:
    """HTTP GET /health -- proves the upload server is actually SERVING, not
    just that something is bound to the port or that a pid is alive."""
    try:
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5) as resp:
            data = json.loads(resp.read())
            return bool(data.get("ok"))
    except (URLError, OSError, json.JSONDecodeError):
        return False


def _upload_state(port: int) -> str:
    """ "on" (serving) / "dead" (port open or pid alive but not serving --
    the "reports ON while dead" bug, now surfaced) / "off"."""
    if _health_check(port):
        return "on"
    from magent.upload_server import (
        server_pid,  # heavy subsystem: in-body per policy
    )

    if _probe_port(port) or pid_alive(server_pid(port)):
        return "dead"
    return "off"


def _listener_state() -> str:
    """ "on" (heartbeat fresh) / "stale" (pid alive, heartbeat expired) / "off"."""
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    if not get_platform().supports_hotkey():
        return "off"
    from magent.hotkey import (
        listener_pid,  # ImportError off-Windows (hotkey.py guards); must stay lazy
    )

    pid = listener_pid()
    if not pid:
        return "off"
    return "on" if heartbeat_fresh("hotkey") else "stale"


def _attention_state() -> str:
    """ "on" (pid + fresh heartbeat) / "stale" (pid alive, heartbeat expired) /
    "crashed" (no pid but a heartbeat file lingers -- the daemon died without a
    clean stop) / "off" (neither). A clean stop removes the heartbeat file
    (attention_cmd), so a leftover heartbeat with no live pid can only mean an
    uncaught crash -- surfaced loudly rather than swept under "off" (P6-01)."""
    from magent.cli.attention_cmd import daemon_pid

    if daemon_pid():
        return "on" if heartbeat_fresh("attention") else "stale"
    return "crashed" if heartbeat_age("attention") is not None else "off"


def _agents_snapshot(cfg: MagentConfig) -> list[dict[str, object]]:
    """One-shot engine poll — the scripting face of `magent watch`. Builds
    the engine via the shared config-driven builder so `status` ages states
    with settings.attention staleness/debounce, not the module defaults."""
    from magent.cli.attention_cmd import engine_from_config

    engine = engine_from_config(cfg)
    return [
        {"name": v.name, "state": v.state, "age_s": round(v.age_s, 1)}
        for v in engine.poll()
    ]


def _agents_attention_rollup(cfg: MagentConfig) -> None:
    """Print a one-line 'N agents need you' summary when any session is blocked
    on the user (needs-input) or errored — the human counterpart to `status
    --json`'s agents array. Silent when nothing needs attention (WIN, P6)."""
    from magent import agent_state  # heavy subsystem: in-body per policy

    waiting = [
        a
        for a in _agents_snapshot(cfg)
        if a.get("state") in (agent_state.NEEDS_INPUT, agent_state.ERROR)
    ]
    if not waiting:
        return
    names = ", ".join(str(a.get("name")) for a in waiting)
    click.echo(
        f"  {style(f'{len(waiting)} agent(s) need you', fg='yellow', bold=True)}"
        f"  {style('(' + names + ')', dim=True)}"
    )


def _psmux_sessions(
    up: list[dict[str, object]], projects: list[dict[str, object]]
) -> list[dict[str, object]]:
    """One row per LIVE psmux session: ``{name, app, idle, state}``.

    The agents themselves live inside these sessions -- they are what an SSH
    client attaches to -- so a report that stops at the daemons says nothing
    about the actual work. Liveness is already settled by ``psmux_status``'s
    fan-out; the only added cost is one ``#{pane_current_command}`` probe per
    live session, and those go out as a single unbounded fan-out
    (``psmux.pane_current_commands``), so 40 sessions stay ~one psmux
    round-trip. Agent states come from the same store the picker reads, so the
    two surfaces can never disagree. Pure data: the shell decides how to print
    it and what to exit with.
    """
    from magent import psmux as psmux_mod  # heavy subsystem: in-body per policy
    from magent.cli.session_picker import _session_cwds, _session_states

    sids = [psmux_mod.socket_id(u) for u in up]
    if not sids:
        return []
    binary = psmux_mod.find_psmux() or ""
    apps = psmux_mod.pane_current_commands(sids, psmux=binary or None)
    resolved = {psmux_mod.socket_id(p): _as_str(p.get("resolved")) for p in projects}
    states = _session_states(_session_cwds(binary, sids, resolved))
    rows: list[dict[str, object]] = []
    for sid in sids:
        app = apps.get(sid, "")
        state, _age = states.get(sid, (None, None))
        rows.append(
            {
                "name": sid,
                "app": app,
                "idle": psmux_mod.is_idle_command(app),
                # "" (never None) when the store has no record, so a JSON
                # consumer treats it as a plain string field -- the same
                # convention psmux.config_sessions uses for "resolved".
                "state": state or "",
            }
        )
    return rows


def _print_session_row(idx: int, row: dict[str, object]) -> None:
    """One live-session line: pick index, socket id, foreground app, agent state."""
    from magent.cli.session_picker import (
        _status_label,  # sibling module: one label vocabulary for both surfaces
    )

    sid = _as_str(row.get("name"))
    idle = bool(row.get("idle"))
    app = "idle" if idle else (_as_str(row.get("app")) or "?")
    app_txt = (
        style(app, fg="yellow", bold=True) if idle else style(app, fg="cyan", dim=True)
    )
    extra = f"{' ' * max(2, 26 - len(sid))}{app_txt}"
    label = _status_label(_as_str(row.get("state")) or None)
    if label:
        extra += f"{' ' * max(2, 14 - len(app))}{label}"
    _menu_item(str(idx), sid, extra=extra)


def _gather_status(cfg: MagentConfig) -> dict[str, str]:
    return {
        "upload_server": _upload_state(cfg.settings.upload_port),
        "listener": _listener_state(),
        "attention": _attention_state(),
    }


def _is_degraded(status: dict[str, str]) -> bool:
    return (
        status["upload_server"] == "dead"
        or status["listener"] == "stale"
        or status["attention"] in ("stale", "crashed")
    )


class StatusReport(NamedTuple):
    """What ``_render_status`` just printed, for a caller that acts on it.

    ``sessions`` are the live psmux rows in the exact on-screen order, so the
    interactive menu's pick numbers line up with the printed ones without
    re-probing psmux (and without the list drifting between paint and pick).
    """

    degraded: bool
    sessions: list[dict[str, object]]


def _render_status(config_file: Path) -> StatusReport:
    """Prints the status report; reports whether any daemon is degraded
    (dead/stale) plus the live psmux sessions it listed, in display order.
    Never exits -- shared with the menu's _menu_status."""
    from magent.launch import psmux_status  # heavy subsystem: in-body per policy

    cfg = _load_config_or_exit(config_file)
    up, down, projects = psmux_status(cfg)
    rows = {_as_str(r.get("name")): r for r in _psmux_sessions(up, projects)}
    listed: list[dict[str, object]] = []

    _banner()
    click.echo(
        f"  {style('Status', bold=True)}   "
        f"{style(str(len(up)), fg='green', bold=True)} running  {style('/', dim=True)}  "
        f"{style(str(len(down)), fg='yellow', bold=True)} stopped"
    )
    _divider()
    if up:
        order, buckets = _grouped(up)
        for g in order:
            click.echo(
                f"  {style(g, fg='green', bold=True)}  {style(f'({len(buckets[g])})', dim=True)}"
            )
            for sid in buckets[g]:
                row = rows.get(
                    sid, {"name": sid, "app": "", "idle": False, "state": ""}
                )
                listed.append(row)
                _print_session_row(len(listed), row)
    else:
        click.echo(
            f"  {style('No sessions running.', dim=True)}  "
            f"{style('Bring some up from the menu or `magent up`.', dim=True)}"
        )
    if down:
        preview = ", ".join(
            _as_str(d.get("session")) or _as_str(d.get("name")) for d in down[:6]
        ) + ("..." if len(down) > 6 else "")
        click.echo(
            f"\n  {style(str(len(down)), fg='yellow', bold=True)} not running  {style('(' + preview + ')', dim=True)}"
        )
    _divider()

    status = _gather_status(cfg)
    upload_labels = {
        "on": style(f"ON  port {cfg.settings.upload_port}", fg="green", bold=True),
        "dead": style(
            f"DEAD  port {cfg.settings.upload_port} (not responding)",
            fg="red",
            bold=True,
        ),
        "off": style("off", dim=True),
    }
    click.echo(
        f"  {style('Upload server', bold=True)}   {upload_labels[status['upload_server']]}"
    )

    listener_labels = {
        "on": style("ON", fg="green", bold=True),
        "stale": style("STALE  (heartbeat expired)", fg="red", bold=True),
        "off": style("off  (starts with `magent attach`)", dim=True),
    }
    click.echo(
        f"  {style('Alt+V listener', bold=True)}   {listener_labels[status['listener']]}"
    )

    attention_labels = {
        "on": style("ON", fg="green", bold=True),
        "stale": style("STALE  (heartbeat expired)", fg="red", bold=True),
        "crashed": style("CRASHED  (daemon died — see logs)", fg="red", bold=True),
        "off": style("off  (start with `magent attention -d`)", dim=True),
    }
    click.echo(
        f"  {style('Attention', bold=True)}        {attention_labels[status['attention']]}"
    )

    _agents_attention_rollup(cfg)

    return StatusReport(_is_degraded(status), listed)


@main.command("status")
@click.option("--json", "as_json", is_flag=True, help="Print daemon status as JSON")
@click.pass_context
def status_cmd(ctx: click.Context, as_json: bool) -> None:
    """Show which psmux sessions and services are currently running."""
    config_file = find_config(ctx.obj.get("config_path"))
    if not config_file.exists():
        if as_json:
            # P3-04: one error envelope shape across every CLI JSON surface.
            click.echo(json.dumps({"ok": False, "error": "No config found."}))
        else:
            click.echo("No config found. Run magent first.", err=True)
        sys.exit(1)

    if as_json:
        from magent.launch import (
            psmux_status,  # heavy subsystem: in-body per policy
        )

        cfg = _load_config_or_exit(config_file, as_json=True)
        status = _gather_status(cfg)
        # P3-04: `ok: true` is the success discriminator (only errors carry
        # ok: false); degraded is still signalled by the state fields + exit 3.
        payload: dict[str, object] = {"ok": True, **status}
        payload["agents"] = _agents_snapshot(cfg)
        # Additive (P3-04): a new key alongside the existing envelope, never a
        # change to its shape or to the 0/1/3 exit contract -- a dead psmux
        # session is a "not running" row, not a degraded daemon.
        up, _down, projects = psmux_status(cfg)
        payload["psmux_sessions"] = _psmux_sessions(up, projects)
        click.echo(json.dumps(payload))
        sys.exit(3 if _is_degraded(status) else 0)

    if _render_status(config_file).degraded:
        sys.exit(3)


@main.command("down")
@click.argument("names", nargs=-1)
@click.option("-g", "--group", default=None, help="Only sessions in this group")
@click.option(
    "--all",
    "do_all",
    is_flag=True,
    help="Stop every session, the upload server, and the Alt+V listener",
)
@click.option("--server", "stop_srv", is_flag=True, help="Also stop the upload server")
@click.pass_context
def down_cmd(
    ctx: click.Context,
    names: tuple[str, ...],
    group: str | None,
    do_all: bool,
    stop_srv: bool,
) -> None:
    """Shut down running psmux sessions (and optionally the upload server)."""
    config_file = find_config(ctx.obj.get("config_path"))
    cfg = _load_config_or_exit(config_file)

    from magent.launch import (  # heavy subsystem: in-body per policy
        kill_psmux,
        psmux_status,
    )

    up, _, _ = psmux_status(cfg, group=group)
    # Kill keys off the psmux socket id (P3-01); `status` shows the same ids.
    up_names = [_as_str(u.get("session")) or _as_str(u.get("name")) for u in up]
    if names:
        wanted = {n.lower() for n in names}
        targets = [n for n in up_names if n.lower() in wanted]
    else:
        targets = up_names

    if targets:
        kill_psmux(targets)
        click.echo(
            f"  {style('+', fg='green')} Stopped {style(str(len(targets)), fg='green', bold=True)}"
            f" session(s): {style(', '.join(targets), dim=True)}"
        )
    else:
        click.echo(f"  {style('-', dim=True)} No matching running sessions.")

    if do_all or stop_srv:
        from magent.upload_server import (
            stop_server,  # heavy subsystem: in-body per policy
        )

        if stop_server(cfg.settings.upload_port):
            click.echo(
                f"  {style('+', fg='green')} Stopped upload server on port {cfg.settings.upload_port}."
            )
        else:
            click.echo(
                f"  {style('-', dim=True)} Upload server not running, or could not be stopped (see logs)."
            )

    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    if do_all and get_platform().supports_hotkey():
        from magent.hotkey import (
            stop_listener,  # ImportError off-Windows (hotkey.py guards); must stay lazy
        )

        if stop_listener():
            click.echo(f"  {style('+', fg='green')} Stopped the Alt+V listener.")
        else:
            click.echo(f"  {style('-', dim=True)} Alt+V listener was not running.")

    if do_all:
        from magent.cli.attention_cmd import stop_daemon

        if stop_daemon():
            click.echo(f"  {style('+', fg='green')} Stopped the attention daemon.")
        else:
            click.echo(f"  {style('-', dim=True)} Attention daemon was not running.")


def _open_session(sid: str) -> None:
    """Attach this terminal to a live session -- the picker's own attach path
    (retry + surfaced failure + terminal reset), not a second copy of it."""
    from magent import psmux as psmux_mod  # heavy subsystem: in-body per policy
    from magent.cli.session_picker import _attach_session, _reset_terminal

    binary = psmux_mod.find_psmux()
    if not binary:
        click.echo(
            f"  {style('x', fg='red')} psmux not found on PATH. Install: choco install psmux"
        )
        return
    _attach_session(binary, sid, _reset_terminal)


def _revive_session(config_file: Path, sid: str) -> None:
    """Re-launch the agent in a live session whose pane fell back to a shell."""
    from magent import psmux as psmux_mod  # heavy subsystem: in-body per policy

    cfg = _load_config_or_exit(config_file)
    if psmux_mod.revive_sessions(cfg, only=[sid]):
        click.echo(f"  {style('+', fg='green')} Revived {style(sid, bold=True)}.")
    else:
        click.echo(
            f"  {style('-', dim=True)} Nothing to revive in {sid}"
            f" (its agent is still running), or the relaunch failed -- see logs."
        )


def _session_actions(config_file: Path, sessions: list[dict[str, object]]) -> None:
    """Act on one of the psmux sessions `_render_status` just listed.

    Its own prompt rather than more branches inside `_menu_status`: the menu
    handlers in this module stay thin shells and the complexity ceilings in
    pyproject.toml ratchet DOWN only. Indices are the printed ones, so what
    you see is what you act on.
    """
    click.echo(
        f"  {style('Session', bold=True)}   "
        f"{style('1-' + str(len(sessions)), fg='cyan', bold=True)}=attach   "
        f"{style('r<n>', fg='cyan', bold=True)}=revive a stopped agent   "
        f"{style('q', fg='cyan', bold=True)}=back"
    )
    choice = (
        click.prompt(
            f"  {style('>', fg='cyan', bold=True)}",
            default="q",
            show_default=False,
            prompt_suffix=" ",
        )
        .strip()
        .lower()
    )
    if choice in ("", "q", "n", "no", "back"):
        return
    revive = choice.startswith("r")
    digits = choice[1:].strip() if revive else choice
    if not digits.isdigit() or not 1 <= int(digits) <= len(sessions):
        click.echo(f"  {style('x', fg='red')} Invalid choice.")
        return
    sid = _as_str(sessions[int(digits) - 1].get("name"))
    if not revive:
        _open_session(sid)
        return
    _revive_session(config_file, sid)
    click.pause(info=f"  {style('press any key to return', dim=True)}")


def _menu_status(config_file: Path) -> None:
    report = _render_status(config_file)
    click.echo()
    if report.sessions:
        _session_actions(config_file, report.sessions)
        return
    click.pause(info=f"  {style('press any key to return', dim=True)}")


def _menu_up(config_file: Path) -> None:
    from magent.launch import (  # heavy subsystem: in-body per policy
        bring_up_psmux,
        psmux_status,
    )

    cfg = _load_config_or_exit(config_file)
    up, down, projects = psmux_status(cfg)
    _banner()
    click.echo(
        f"  {style('Bring up sessions in background', bold=True)}  {style('(no windows)', dim=True)}"
    )
    _divider()
    if not projects:
        click.echo(f"  {style('!', fg='yellow')} No psmux-eligible projects in config.")
    elif not down:
        click.echo(
            f"  {style('+', fg='green')} All {len(up)} session(s) already running."
        )
    else:
        _dn_order, dn_buckets = _grouped(down)
        pickable = _print_session_overview("this machine", up, down)
        opts = [f"{style('a', fg='cyan', bold=True)}=all {len(down)}"]
        if pickable:
            opts.append(
                f"{style('1-' + str(len(pickable)), fg='cyan', bold=True)}=one group"
            )
        opts.append(f"{style('n', fg='cyan', bold=True)}=cancel")
        click.echo(f"  {style('Bring up', bold=True)}   " + "   ".join(opts))
        choice = (
            click.prompt(
                f"  {style('>', fg='cyan', bold=True)}",
                default="a",
                show_default=False,
                prompt_suffix=" ",
            )
            .strip()
            .lower()
        )
        if choice in ("n", "no", "cancel", "q"):
            return
        if choice in ("a", "all", "y", ""):
            only = [_as_str(d.get("session")) or _as_str(d.get("name")) for d in down]
        elif choice.isdigit() and 1 <= int(choice) <= len(pickable):
            only = dn_buckets[pickable[int(choice) - 1]]
        else:
            sel = next((g for g in pickable if g.lower() == choice), None)
            if not sel:
                click.echo(f"  {style('?', fg='yellow')} cancelled.")
                return
            only = dn_buckets[sel]
        created = bring_up_psmux(cfg, only=only)
        click.echo(
            f"  {style('+', fg='green')} Brought up {style(str(len(created)), fg='green', bold=True)} "
            f"session(s) headlessly {style('(switch with the session switcher)', dim=True)}."
        )
        if cfg.settings.upload_server:
            _maybe_start_upload_server(cfg.settings.upload_port, str(config_file))
    click.echo()
    click.pause(info=f"  {style('press any key to return', dim=True)}")


def _menu_down(config_file: Path) -> None:
    from magent.launch import (  # heavy subsystem: in-body per policy
        kill_psmux,
        psmux_status,
    )

    cfg = _load_config_or_exit(config_file)
    up, _, _ = psmux_status(cfg)
    _banner()
    click.echo(f"  {style('Shut down sessions', bold=True)}")
    _divider()
    if not up:
        click.echo(f"  {style('-', dim=True)} Nothing is running.")
    else:
        order, buckets = _grouped(up)
        for g in order:
            click.echo(
                f"  {style(g, fg='green', bold=True)}  {style(f'({len(buckets[g])})', dim=True)}"
            )
            _print_names(buckets[g])
        pickable = [g for g in order if g != "(no group)"]
        srv_on = _probe_port(cfg.settings.upload_port)
        click.echo()
        opts = [f"{style('a', fg='cyan', bold=True)}=all {len(up)}"]
        if pickable:
            opts.append(
                f"{style('1-' + str(len(pickable)), fg='cyan', bold=True)}=one group"
            )
        if srv_on:
            opts.append(f"{style('x', fg='cyan', bold=True)}=all + server")
        opts.append(f"{style('n', fg='cyan', bold=True)}=cancel")
        click.echo(f"  {style('Shut down', bold=True)}   " + "   ".join(opts))
        choice = (
            click.prompt(
                f"  {style('>', fg='cyan', bold=True)}",
                default="n",
                show_default=False,
                prompt_suffix=" ",
            )
            .strip()
            .lower()
        )
        also_server = False
        if choice in ("n", "no", "cancel", "q", ""):
            return
        if choice in ("a", "all", "y"):
            targets = [_as_str(u.get("session")) or _as_str(u.get("name")) for u in up]
        elif choice == "x":
            targets = [_as_str(u.get("session")) or _as_str(u.get("name")) for u in up]
            also_server = True
        elif choice.isdigit() and 1 <= int(choice) <= len(pickable):
            targets = buckets[pickable[int(choice) - 1]]
        else:
            sel = next((g for g in pickable if g.lower() == choice), None)
            if not sel:
                click.echo(f"  {style('?', fg='yellow')} cancelled.")
                return
            targets = buckets[sel]
        kill_psmux(targets)
        click.echo(
            f"  {style('+', fg='green')} Stopped {style(str(len(targets)), fg='green', bold=True)} session(s)."
        )
        if also_server:
            from magent.upload_server import (
                stop_server,  # heavy subsystem: in-body per policy
            )

            if stop_server(cfg.settings.upload_port):
                click.echo(
                    f"  {style('+', fg='green')} Stopped upload server on port {cfg.settings.upload_port}."
                )
            else:
                click.echo(
                    f"  {style('-', dim=True)} Upload server not running, or could not be stopped (see logs)."
                )
    click.echo()
    click.pause(info=f"  {style('press any key to return', dim=True)}")
