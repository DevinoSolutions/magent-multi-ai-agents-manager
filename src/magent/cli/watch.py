"""`magent watch` — the live fleet table. Poll the attention engine,
render most-urgent-first, and jump to a session's window on a digit press.

Complements the ambient signals (`magent attention`): watch is the
at-a-glance overview for when you're AT the desk; the daemon covers the
rest. Both read the same engine, so they can never disagree.
"""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING

import click

from magent.cli.app import main
from magent.cli.attention_cmd import engine_from_config
from magent.cli.config_io import _load_config_or_exit
from magent.paths import find_config
from magent.style import style

if TYPE_CHECKING:
    from magent.attention import SessionView
    from magent.platform import Platform


def _state_label(state: str) -> str:
    padded = f"{state:<11}"
    labels = {
        "needs-input": style(padded, fg="red", bold=True),
        "error": style(padded, fg="red", bold=True),
        "done": style(padded, fg="green", bold=True),
        "working": style(padded, fg="yellow"),
        "idle": style(padded, dim=True),
    }
    return labels.get(state, padded)


def _age_label(age_s: float) -> str:
    secs = int(age_s)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def _render_frame(views: list[SessionView]) -> None:
    click.echo(
        f"  {style('magent watch', bold=True)}  "
        f"{style(f'{len(views)} session(s)', dim=True)}"
    )
    click.echo()
    if not views:
        click.echo(f"  {style('No agent sessions in the state store yet.', dim=True)}")
        click.echo(
            f"  {style('States appear once agents run with their hooks installed.', dim=True)}"
        )
    for i, v in enumerate(views, start=1):
        num = style(str(i), fg="cyan", bold=True) if i <= 9 else " "
        click.echo(
            f"  {num}  {v.name:<28} {_state_label(v.state)} "
            f"{style(_age_label(v.age_s), dim=True)}"
        )
    click.echo()
    click.echo(f"  {style('1-9 focus window · q quit', dim=True)}")


def _focus_by_name(plat: Platform, name: str) -> bool:
    """Focus the window for ``name``: md-grammar match first, then a
    contains fallback (IDE windows carry their own titles). Flash as the
    consolation signal when focus itself fails."""
    from magent import attention  # heavy subsystem: in-body per policy

    handle = attention.md_windows_by_name(plat).get(name)
    if handle is None:
        handle = plat.find_window(name, "contains")
    if handle is None:
        return False
    if plat.focus_window(handle):
        return True
    return plat.flash_window(handle)


def _poll_key(timeout_s: float) -> str | None:
    """Wait up to ``timeout_s`` for one keypress; None on timeout.

    Windows polls the console directly (msvcrt); POSIX uses select on stdin,
    which is line-buffered outside a raw tty — there, digits need Enter.
    Windows is the fully-interactive target, matching hotkey/psmux."""
    if sys.platform == "win32":
        import msvcrt  # win-only stdlib module

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                return ch if isinstance(ch, str) else None
            time.sleep(0.05)
        return None
    import select

    ready, _, _ = select.select([sys.stdin], [], [], timeout_s)
    if ready:
        return sys.stdin.read(1) or None
    return None


@main.command("watch")
@click.option("--interval", default=2.0, show_default=True, help="Refresh seconds")
@click.option("--once", is_flag=True, hidden=True)  # test seam: one frame, no loop
@click.pass_context
def watch_cmd(ctx: click.Context, interval: float, once: bool) -> None:
    """Live view of every agent session — who needs you, sorted first.

    Rows come from the same state store the attention daemon reads
    (needs-input and errors on top, time-in-state alongside). Press a row
    number to focus that session's window; q quits.
    """
    from magent.platform import get_platform  # heavy subsystem: in-body per policy

    config_file = find_config(ctx.obj.get("config_path"))
    cfg = _load_config_or_exit(config_file)
    plat = get_platform()
    engine = engine_from_config(cfg)

    while True:
        views = engine.poll()
        if not once:
            click.clear()
        _render_frame(views)
        if once:
            return

        key = _poll_key(interval)
        if key is None:
            continue
        if key.lower() == "q":
            return
        if key.isdigit() and key != "0":
            idx = int(key) - 1
            if idx < len(views) and not _focus_by_name(plat, views[idx].name):
                click.echo(
                    f"  {style('!', fg='yellow')} no window found for {views[idx].name}"
                )
                time.sleep(0.8)
