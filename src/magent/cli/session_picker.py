"""The psmux session picker: live-session listing (`sessions_cmd`) and the
looping attach-and-return picker (`_run_sessions_picker`). Named
session_picker (not "sessions") to avoid confusion with magent.sessions.

Liveness is NOT decided here: the sweep is `psmux.live_sessions`, the one
enumeration `status`/`down`/the upload server also use. This module used to
carry the product's only retrying probe, which made the picker the one surface
that could see a flapping session -- and `magent down` the one that skipped it.
Per-session cwds still come from config rather than a psmux probe per paint,
direct-name attach resolves from config (no sweep dependency), and a failed
attach is surfaced + retried instead of being wiped by the redraw.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from collections.abc import Callable

    from magent.nodes import Node

from magent.cli import picker
from magent.cli.app import main
from magent.cli.background import _running_upload_port, _tailnet_host
from magent.cli.config_io import _load_config_or_exit
from magent.cli.ui import _banner, _divider
from magent.paths import find_config
from magent.psmux import attach_argv
from magent.style import style

# The one answer that is a command rather than a search here, so a session
# literally named `queue` can never shadow "go back".
_PICKER_COMMANDS = frozenset({"q"})


def _session_cwds(
    psmux: str, names: list[str], resolved: dict[str, str]
) -> dict[str, str]:
    """Delegate to ``fleetview.session_cwds`` (config's resolved folder, a
    ``pane_cwd`` probe only for a folder that did not resolve)."""
    from magent import fleetview  # heavy subsystem: in-body per policy

    return fleetview.session_cwds(psmux, names, resolved)


def _status_label(state: str | None, age_s: float | None = None) -> str:
    from magent import agent_state  # heavy subsystem: in-body per policy

    if state == agent_state.WORKING:
        # The hook refreshes ts on every tool call, so the age here is time
        # since the agent last did something -- a live "still going" signal.
        mins = int(age_s // 60) if age_s is not None and age_s >= 60 else 0
        text = f"still going... {mins}m" if mins else "still going..."
        return style(text, fg="yellow", bold=True)
    return {
        agent_state.DONE: style("done", fg="green", bold=True),
        agent_state.NEEDS_INPUT: style("needs input", fg="red", bold=True),
        agent_state.ERROR: style("error", fg="red", bold=True),
        agent_state.PARKED: style("parked", dim=True),
    }.get(state, "")


def _session_states(
    cwds: dict[str, str], staleness: dict[str, float] | None = None
) -> dict[str, tuple[str | None, float | None]]:
    """Delegate to ``fleetview.session_states``: each session's ``(state,
    age_s)`` from the agent-state store, a stale record reported as None."""
    from magent import fleetview  # heavy subsystem: in-body per policy

    return fleetview.session_states(cwds, staleness)


def _session_statuses(
    cwds: dict[str, str], staleness: dict[str, float] | None = None
) -> dict[str, str]:
    """The picker's display face of ``_session_states``: one styled label each."""
    return {
        sock: _status_label(state, age_s)
        for sock, (state, age_s) in _session_states(cwds, staleness).items()
    }


_FOCUS_TARGET_FILE = Path.home() / ".magent" / "focus-target"


_PICKER_ATTACHED_FILE = Path.home() / ".magent" / "picker-attached"


def _consume_focus_target() -> str | None:
    """Read and clear the session a notification/web tap asked us to jump to."""
    try:
        t = _FOCUS_TARGET_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    with contextlib.suppress(OSError):
        _FOCUS_TARGET_FILE.unlink()
    return t or None


def _set_picker_attached(name: str | None) -> None:
    """Record which session this picker is attached to, so the /focus endpoint
    knows whose client to detach to trigger a switch (None = at the menu)."""
    try:
        if name:
            _PICKER_ATTACHED_FILE.parent.mkdir(parents=True, exist_ok=True)
            _PICKER_ATTACHED_FILE.write_text(name, encoding="utf-8")
        else:
            _PICKER_ATTACHED_FILE.unlink()
    except OSError:
        pass


def _reset_terminal() -> None:
    """Put the terminal back in a sane state after a psmux client detaches.

    Module level (not a closure inside the picker loop) so every attach site --
    the picker and the status menu's session actions -- shares one definition.
    """
    if sys.platform == "win32":
        subprocess.run(["cmd", "/c", "cls"], shell=False, check=False)
    else:
        subprocess.run(["stty", "sane"], capture_output=True, check=False)
        subprocess.run(["tput", "reset"], capture_output=True, check=False)


def _attach_session(psmux_bin: str, target: str, reset: Callable[[], None]) -> None:
    """Attach to a session; surface and retry a failed attach.

    The old flow cleared the screen the moment the attach client returned, so
    a failure (e.g. the client losing a resource race on an overloaded host)
    was invisible -- the picker appeared to 'process' the choice and silently
    bounce back to the menu."""
    rc = 0
    for attempt in (1, 2):
        _set_picker_attached(target)
        try:
            rc = subprocess.call(attach_argv(psmux_bin, target))
        finally:
            _set_picker_attached(None)
            reset()
        if rc == 0:
            return
        if attempt == 1:
            click.echo(
                f"  {style('!', fg='yellow')} attach to {target} exited {rc} -- retrying..."
            )
            time.sleep(1)
    click.echo(
        f"  {style('x', fg='red')} attach to {target} failed twice (exit {rc})."
        f" {style('The host may be overloaded -- try again in a moment.', dim=True)}"
    )
    time.sleep(2)


def _session_rows(
    sessions: list[str], statuses: dict[str, str]
) -> list[picker.PickerItem]:
    """The picker's rows: one per live session, then Back.

    A row's key is its PRINTED number, so filtering the list never renumbers
    what you can see -- and the caller's index math is the same one it has
    always run on a typed digit.
    """
    rows = []
    for i, sess in enumerate(sessions, 1):
        status = statuses.get(sess, "")
        extra = (" " * max(2, 26 - len(sess)) + status) if status else ""
        rows.append(picker.PickerItem(str(i), sess, extra=extra))
    rows.append(picker.PickerItem("q", "Back", key_fg="yellow", gap_before=True))
    return rows


def _read_choice(rows: list[picker.PickerItem], upload_url: str | None) -> str | None:
    """Paint the session list and return the answer, lowercased. None means the
    user escaped out, which is the same as choosing Back."""

    def _header() -> None:
        _banner()
        click.echo(
            f"  {style('psmux sessions', bold=True)}  {style('(synced with desktop)', dim=True)}"
        )
        _divider()
        click.echo()
        if upload_url:
            click.echo(
                f"  {style('WebApp To Upload Images', bold=True)}  {style(upload_url, fg='cyan', bold=True)}"
            )
            click.echo()

    if picker.raw_mode_available():
        result = picker.pick(
            rows,
            _header,
            commands=_PICKER_COMMANDS,
            prompt=f"  {style('attach to', fg='cyan')} ",
        )
        if result.kind == picker.CANCEL:
            return None
        return result.value.strip().lower()
    picker.show(rows, _header)
    return (
        click.prompt(
            f"  {style('attach to', fg='cyan')}",
            default="1",
            show_default=False,
            prompt_suffix=" ",
        )
        .strip()
        .lower()
    )


def _run_sessions_picker(config_file: Path, name: str | None = None) -> None:
    """Looping psmux session picker: list live sessions, attach to a choice, repeat.

    A focus-target file (set by the upload server's /focus endpoint, e.g. from a
    notification tap) lets the currently-attached session be switched remotely:
    /focus detaches this picker's client, the attach returns, and the loop jumps
    straight to the requested project."""

    from magent import fleetview  # heavy subsystem: in-body per policy
    from magent import psmux as psmux_mod  # heavy subsystem: in-body per policy

    psmux_bin = psmux_mod.find_psmux()
    if not psmux_bin:
        click.echo(
            f"  {style('x', fg='red')} psmux not found on PATH. Install: choco install psmux"
        )
        return

    # Candidates come from config, in config order -- liveness is NOT checked
    # here, so a direct-name attach never depends on a sweep. Each project's
    # resolved path rides along: it is the cwd magent created the session with,
    # which is what the agent-state lookup keys on.
    cfg = _load_config_or_exit(config_file)
    # Read once here rather than per paint: the windows cannot change under a
    # running picker, and every redraw must age states the same way. The one
    # config -> staleness-window translation, shared with the attention
    # daemon / watch / status so no surface ages states differently.
    staleness = fleetview.staleness_from_config(cfg)
    candidates: list[str] = []
    resolved: dict[str, str] = {}
    for proj in psmux_mod.eligible_projects(cfg):
        sid = psmux_mod.socket_id(proj)
        candidates.append(sid)
        path = proj.get("resolved")
        resolved[sid] = path if isinstance(path, str) else ""

    def _attach(target: str) -> None:
        _attach_session(psmux_bin, target, _reset_terminal)

    if name:
        matches = [s for s in candidates if name.lower() in s.lower()]
        if matches:
            _attach(matches[0])

    # Tappable from a phone SSH client: one tap opens the uploader, then Add to
    # Home Screen (iOS: tap to install the Web Clip profile). Shown only when a
    # live upload server is detected, so the link always works.
    port = _running_upload_port()
    upload_url = f"http://{_tailnet_host()}:{port}/" if port else None

    while True:
        # Fresh sweep every redraw: sessions created or killed while the
        # picker was attached elsewhere show up without restarting it. The
        # sweep is `psmux.live_sessions` -- the SAME call `status` and `down`
        # make, so the picker can no longer be the only surface that sees a
        # session (this module used to own the only retrying probe in the
        # product, which is why `down` skipped what the picker was showing).
        sessions = psmux_mod.live_sessions(candidates, psmux=psmux_bin)
        if not sessions:
            click.echo(f"  {style('x', fg='red')} No active psmux sessions.")
            click.echo(
                f"  {style('Run', dim=True)} {style('magent up', bold=True)} {style('or', dim=True)} "
                f"{style('magent --go', bold=True)} {style('first.', dim=True)}"
            )
            return

        # Remote switch: a notification/web tap dropped a target here -> jump to it.
        focus = _consume_focus_target()
        if focus and focus in sessions:
            _attach(focus)
            continue

        statuses = _session_statuses(
            _session_cwds(psmux_bin, sessions, resolved), staleness
        )
        choice = _read_choice(_session_rows(sessions, statuses), upload_url)
        if choice is None or choice == "q":
            return

        target = None
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(sessions):
                target = sessions[idx]
        except ValueError:
            matches = [s for s in sessions if choice in s.lower()]
            if matches:
                target = matches[0]

        if target:
            _attach(target)
        else:
            click.echo(f"  {style('x', fg='red')} Invalid choice.")


@main.command("sessions")
@click.argument("name", required=False)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print live sessions as JSON (name, cwd, model, effort, state) and exit.",
)
@click.option(
    "--v1",
    "v1",
    is_flag=True,
    help=(
        "Print the /api/v1/sessions?fresh=1 envelope (every SessionRow field) and "
        "exit. A missing config is an empty list; an unreadable (broken) one is "
        "an unavailable envelope, exit 1."
    ),
)
@click.pass_context
def sessions_cmd(ctx: click.Context, name: str | None, as_json: bool, v1: bool) -> None:
    """List psmux sessions or attach to one. Usage: magent sessions [name]"""
    config_file = find_config(ctx.obj.get("config_path"))
    if v1:
        _emit_sessions_v1(ctx.obj.get("config_path"))
        return
    if as_json:
        _emit_sessions_json(ctx.obj.get("config_path"))
        return
    _run_sessions_picker(config_file, name)


def _emit_sessions_json(config_path: str | None) -> None:
    """Print each configured session with its live state, one JSON array:
    ``fleetview.rows`` in their legacy form (``SessionRow.to_legacy``).

    Only stdout carries the JSON. ``hooks=False`` keeps the local rows on the
    raw ``config_sessions`` loader (no `load_config` version warning); a
    config that names a pool node also gets a typed load for the node rows,
    and one that fails validation answers the ``{"ok": false, "error": ...}``
    envelope with exit 1 instead of an array.
    """
    import json

    from magent import fleetview  # heavy subsystem: in-body per policy

    try:
        rows = fleetview.rows(config_path, hooks=False, dial_nodes=True)
    except (ValueError, FileNotFoundError) as e:
        click.echo(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)
    click.echo(json.dumps([r.to_legacy() for r in rows], indent=2))


def _emit_sessions_v1(config_path: str | None) -> None:
    """``sessions --v1``: exactly ``GET /api/v1/sessions?fresh=1``, both
    halves of every row (hook state and pane state), one envelope."""
    import json

    from magent import api, wire  # heavy subsystem: in-body per policy

    try:
        listing = api.session_list(config_path, fresh=True)
    except wire.WireError as e:
        click.echo(json.dumps(wire.from_exc(e)))
        sys.exit(1)
    click.echo(json.dumps(wire.ok(listing)))


def _node_session_targets(
    config_path: str | None,
) -> list[tuple[dict[str, object], Node | None]]:
    """Each node project's ``sessions --json`` row (``model``/``effort`` None)
    with the Node its session runs on -- None when it is placed nowhere or
    its node has left ``settings.nodes``. Reads files only: the state is the
    sync daemon's last pull, never a dial. The fleet commands (``peek``, and
    the refusals of ``send``/``model``) share it.

    The typed config is loaded only when the raw file names a pool node, so
    every config without one keeps the raw loader's no-version-warning path;
    a failed validation exits through ``_load_config_or_exit``."""
    import json

    from magent import env, nodes  # heavy subsystem: in-body per policy

    config_file = find_config(config_path)
    if not config_file.exists():
        return []
    raw = json.loads(config_file.read_text(encoding="utf-8"))
    # Raw dict: same rule as config_sessions' node skip (DECISION-15). A
    # deliberate SUPERSET of nodes.node_projects (it ignores enabled, IDE tools
    # and duplicate sids): it may trigger a typed load that lists nothing, but
    # it can never drop a node row. Mirroring those skips here would be a third
    # spelling of node_projects' predicate to keep in step.
    if not any(
        isinstance(p, dict) and p.get("node") not in (None, "cloud")
        for p in raw.get("projects", [])
    ):
        return []
    cfg = _load_config_or_exit(config_file)
    # The state is nodes.session_rows' (the one answer `status` gives too);
    # the map is read again only for the folder, which status does not show.
    entries = nodes.read_node_map()
    out: list[tuple[dict[str, object], Node | None]] = []
    for row in nodes.session_rows(cfg, now=time.time()):
        entry = entries.get(str(row["name"]))
        state = str(row["state"])
        nick = row["node"]
        node = None
        if isinstance(nick, str):
            # A nick the map kept after settings.nodes dropped it: no node to
            # read, and the row still lists.
            with contextlib.suppress(nodes.NodeConfigError):
                node = nodes.node_for_nick(cfg, nick, local_user=env.local_username())
        out.append(
            (
                {
                    "name": row["session"],
                    "cwd": (entry.cwd or entry.remote_root) if entry else "",
                    "live": None if state == "stale" else state == "live",
                    "state": state,
                    "model": None,
                    "effort": None,
                    "node": nick,
                },
                node,
            )
        )
    return out
