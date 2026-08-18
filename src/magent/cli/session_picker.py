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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from collections.abc import Callable

from magent.cli.app import main
from magent.cli.background import _running_upload_port, _tailnet_host
from magent.cli.config_io import _load_config_or_exit
from magent.cli.ui import _banner, _divider, _menu_item
from magent.paths import find_config
from magent.style import style


def _session_cwds(
    psmux: str, names: list[str], resolved: dict[str, str]
) -> dict[str, str]:
    """Each session's working directory -- the key we match against the
    agent-state store.

    Config is the source of truth here: magent creates every session with
    ``-c <resolved>``, so the pane cwd is already known without asking psmux.
    Probing ``pane_cwd`` per session per paint cost ~3.4s of a ~4.8s first
    paint at 40 live sessions, and matched what config already said. Only a
    session whose configured path failed to resolve falls back to a live
    probe -- normally an empty set, so the concurrency here rarely runs."""
    from magent import psmux as psmux_mod  # heavy subsystem: in-body per policy

    cwds = {n: resolved.get(n, "") for n in names}
    missing = [n for n in names if not cwds[n]]
    if missing:
        with ThreadPoolExecutor(max_workers=16) as pool:
            probed = list(
                pool.map(lambda n: psmux_mod.pane_cwd(n, psmux=psmux), missing)
            )
        cwds.update(zip(missing, probed, strict=True))
    return cwds


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
    }.get(state, "")


def _session_states(cwds: dict[str, str]) -> dict[str, tuple[str | None, float | None]]:
    """Map each session to its ``(state, age_s)`` from the agent-state store,
    which agents populate via their own lifecycle events (Claude Code hooks,
    Codex notify, ...) -- ground truth, not terminal scraping. A staleness guard
    keeps a session killed mid-turn from showing 'working...' forever.

    Split out of ``_session_statuses`` so ``magent status`` can report the same
    ground truth as *data* (its ``--json`` session rows) instead of re-deriving
    it from a styled label."""
    from magent import agent_state  # heavy subsystem: in-body per policy
    from magent.attention import (
        STALENESS_S as stale,  # heavy subsystem: in-body per policy
    )

    out: dict[str, tuple[str | None, float | None]] = {}
    for sock, cwd in cwds.items():
        rec = agent_state.state_for(cwd) if cwd else None
        raw_state = rec.get("state") if rec else None
        state = raw_state if isinstance(raw_state, str) else None
        age_s: float | None = None
        if rec is not None and state is not None:
            ts = rec.get("ts", 0)
            ts_num = (
                ts if isinstance(ts, (int, float)) and not isinstance(ts, bool) else 0
            )
            age_s = time.time() - ts_num
            if state in stale and age_s > stale[state]:
                state = None
        out[sock] = (state, age_s)
    return out


def _session_statuses(cwds: dict[str, str]) -> dict[str, str]:
    """The picker's display face of ``_session_states``: one styled label each."""
    return {
        sock: _status_label(state, age_s)
        for sock, (state, age_s) in _session_states(cwds).items()
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
            rc = subprocess.call([psmux_bin, "-L", target, "attach"])
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


def _run_sessions_picker(config_file: Path, name: str | None = None) -> None:
    """Looping psmux session picker: list live sessions, attach to a choice, repeat.

    A focus-target file (set by the upload server's /focus endpoint, e.g. from a
    notification tap) lets the currently-attached session be switched remotely:
    /focus detaches this picker's client, the attach returns, and the loop jumps
    straight to the requested project."""

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

        click.clear()
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
        statuses = _session_statuses(_session_cwds(psmux_bin, sessions, resolved))
        for i, sess in enumerate(sessions, 1):
            status = statuses.get(sess, "")
            extra = (" " * max(2, 26 - len(sess)) + status) if status else ""
            _menu_item(str(i), sess, extra=extra)
        click.echo()
        _menu_item("q", "Back", key_fg="yellow")
        click.echo()

        choice = (
            click.prompt(
                f"  {style('attach to', fg='cyan')}",
                default="1",
                show_default=False,
                prompt_suffix=" ",
            )
            .strip()
            .lower()
        )

        if choice == "q":
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
@click.pass_context
def sessions_cmd(ctx: click.Context, name: str | None) -> None:
    """List psmux sessions or attach to one. Usage: magent sessions [name]"""
    config_file = find_config(ctx.obj.get("config_path"))
    _run_sessions_picker(config_file, name)
